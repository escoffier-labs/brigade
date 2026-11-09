"""Hardened git commit resolver for the memory verification pass (verify slice 9a).

`commit_resolver(workspace, run)` returns the `Resolver` that
`memory_verify_contract.verify_card(resolve_commit=...)` takes. It follows D3's
"Commit resolver contract" rule by rule. The numbers below are D3's.

1. Every git call goes through `_git`: a fixed timeout, no stdin, `--no-pager`,
   no prompt, and the whole process group killed on a timeout.
2. The environment is the inherited one with every `GIT_*` variable and the
   curl proxy and TLS variables removed. Exactly `GIT_CONFIG_NOSYSTEM`,
   `GIT_CONFIG_GLOBAL` (the null device), `GIT_NO_REPLACE_OBJECTS` and
   `GIT_TERMINAL_PROMPT` are then set.
3. `--no-replace-objects` and the `OVERRIDES` list ride on every call.
4. The repository is the workspace root itself: `rev-parse --show-toplevel`
   must name the same directory (device and inode) the config bound.
5. `git config --local --no-includes --list`, read before any other call
   in the repository, refuses includes, command and helper settings,
   aliases, URL rewrites, remote helpers and proxies, every `http.*` and
   `protocol.*` key, bundle URIs and per-worktree config.
6. The remote of every allowed ref has exactly one `https://` URL.
7. Replacement refs never apply, and a grafts file refuses the repository.
8. An abbreviation shorter than `MIN_ABBREVIATION` never reaches git.
9. A shallow repository or a partial clone is refused.
10. Allowed refs come from `commit_refs`, else from the single ref that
    `refs/remotes/origin/HEAD` names. Local branches and tags never count.
11. Each allowed ref is fetched in this run with an explicit refspec, and the
    porcelain output (or `ls-remote` before git 2.41) proves it current.
12. `rev-parse --disambiguate` must list exactly one object.
13. That object must be a commit.
14. Its id must be 40 or 64 lowercase hex characters.
15. It must be an ancestor of at least one allowed ref.
16. Anything else returns None.

Fetch proofs are cached in a `Run`, per repository and ref list, so the cards
of one run share one fetch. A proof never outlives its `Run`: the next run
fetches again.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import memory_verify_config as config
from .memory_verify_contract import MIN_ABBREVIATION, Resolver

TIMEOUT_SECONDS = 60.0
# git 2.41 added `git fetch --porcelain`. Older git proves freshness through `ls-remote` instead.
PORCELAIN_VERSION = (2, 41)
FULL_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
ABBREVIATION = re.compile(r"[0-9a-f]+")
VERSION = re.compile(r"git version (\d+)\.(\d+)")
# A directory that cannot exist: no path can sit below the null device.
NO_HOOKS = os.path.join(os.devnull, "brigade-no-hooks")
OVERRIDES = (
    "core.fsmonitor=false",
    f"core.hooksPath={NO_HOOKS}",
    "credential.helper=",
    "core.askPass=",
    "core.alternateRefsCommand=",
    "maintenance.auto=false",
    "gc.auto=0",
    "fetch.recurseSubmodules=false",
    "protocol.allow=never",
    "protocol.https.allow=always",
    "http.sslVerify=true",
    "http.proxy=",
    "http.followRedirects=false",
    "http.extraHeader=",
    "http.cookieFile=",
)
# Variables curl reads for a proxy or a trust store. Compared case-insensitively.
CURL_VARIABLES = frozenset(
    {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "curl_ca_bundle", "ssl_cert_file", "ssl_cert_dir"}
)
# D3 rule 5, plus `bundle.*` and `fetch.bundleURI`: a bundle URI sends a fetch to a second host.
REFUSED_SECTIONS = frozenset({"include", "includeif", "credential", "alias", "http", "protocol", "bundle"})
REFUSED_KEYS = frozenset(
    {
        "core.fsmonitor",
        "core.hookspath",
        "core.sshcommand",
        "core.gitproxy",
        "core.askpass",
        "core.alternaterefscommand",
        "fetch.bundleuri",
        # Rule 9: a partial clone may lack the object that would make an abbreviation ambiguous.
        "extensions.partialclone",
        # `git config --local` never lists config.worktree, so settings there would go unchecked.
        "extensions.worktreeconfig",
    }
)
REFUSED_URL_VARIABLES = frozenset({"insteadof", "pushinsteadof"})
REFUSED_REMOTE_VARIABLES = frozenset({"vcs", "uploadpack", "proxy", "proxyauthmethod", "promisor"})
# Porcelain flags that leave the destination ref at the remote's value: fast-forward, forced, new, up to date.
PROVEN_FLAGS = frozenset(" +*=")
DEFAULT_HEAD = "refs/remotes/origin/HEAD"

# Test hook only. A CA bundle for the scratch HTTPS server in tests/test_memory_verify_resolver.py. Production
# code never sets it, and the environment variables that could name one are removed before every call.
_TEST_CA_FILE: str | None = None


@dataclass
class Run:
    """One verification run. Holds the git version and the fetch proofs made in this run."""

    proofs: dict[tuple[config.Identity, tuple[str, ...]], bool] = field(default_factory=dict)
    version: tuple[int, int] | None = None


@dataclass(frozen=True)
class _Result:
    code: int
    out: str


def git_environment(inherited: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment every git call runs with (D3 rule 2)."""
    source = os.environ if inherited is None else inherited
    env = {
        name: value
        for name, value in source.items()
        if not name.upper().startswith("GIT_") and name.lower() not in CURL_VARIABLES
    }
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def git_argv(args: Sequence[str]) -> list[str]:
    """The full argv for one git call (D3 rules 1 and 3)."""
    argv = ["git", "--no-pager", "--no-replace-objects"]
    for override in OVERRIDES:
        argv += ["-c", override]
    if _TEST_CA_FILE is not None:
        argv += ["-c", f"http.sslCAInfo={_TEST_CA_FILE}"]
    return argv + list(args)


def _kill(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except OSError:
        pass


def _git(cwd: str, *args: str) -> _Result | None:
    """Run one hardened git call. None on a launch failure, a timeout or output that is not UTF-8."""
    try:
        process = subprocess.Popen(
            git_argv(args),
            cwd=cwd,
            env=git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
    except (OSError, ValueError):
        return None
    try:
        out, _ = process.communicate(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None
    finally:
        if process.poll() is None:
            _kill(process)
            try:
                # Bounded: on Windows a surviving transport helper can hold the pipe open after git exits.
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    try:
        return _Result(process.returncode, out.decode("utf-8"))
    except UnicodeDecodeError:
        return None


def _output(cwd: str, *args: str) -> str | None:
    """Stdout of a call that must exit 0, without its final newline."""
    result = _git(cwd, *args)
    if result is None or result.code != 0:
        return None
    return result.out[:-1] if result.out.endswith("\n") else result.out


def _same_directory(path: str, identity: config.Identity) -> bool:
    try:
        metadata = os.stat(path)
    except (OSError, ValueError):
        return False
    return metadata.st_ino != 0 and (metadata.st_dev, metadata.st_ino) == identity


def _local_config(cwd: str) -> list[tuple[str, str]] | None:
    """Every local config entry as (key, value), never following includes."""
    result = _git(cwd, "config", "--local", "--no-includes", "--null", "--list")
    if result is None or result.code != 0:
        return None
    if result.out and not result.out.endswith("\0"):
        return None
    entries: list[tuple[str, str]] = []
    for record in result.out.split("\0")[:-1]:
        key, _, value = record.partition("\n")
        entries.append((key, value))
    return entries


def refused_key(key: str) -> bool:
    """True for a local config key that D3 rule 5 refuses (or rule 9's `extensions.partialClone`)."""
    lower = key.lower()
    section, _, rest = lower.partition(".")
    variable = rest.rsplit(".", 1)[-1]
    if section in REFUSED_SECTIONS or lower in REFUSED_KEYS:
        return True
    if section == "url" and variable in REFUSED_URL_VARIABLES:
        return True
    return section == "remote" and "." in rest and variable in REFUSED_REMOTE_VARIABLES


def https_url(value: str) -> bool:
    """True only for a plain `https://host/...` URL. Helpers, `ext::`, files, paths and SSH are refused."""
    if not value.startswith("https://") or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.hostname)


def _remote(ref: str) -> tuple[str, str]:
    """(remote, branch) of a validated `refs/remotes/<remote>/<branch>`."""
    remote, _, branch = ref[len("refs/remotes/") :].partition("/")
    return remote, branch


def _allowed_refs(cwd: str, workspace: config.Workspace) -> tuple[str, ...] | None:
    if workspace.commit_refs:
        return workspace.commit_refs
    target = _output(cwd, "symbolic-ref", "--quiet", DEFAULT_HEAD)
    if target is None or not config.valid_commit_ref(target) or _remote(target)[0] != "origin":
        return None
    return (target,)


def _porcelain_proves(out: str, destination: str, value: str) -> bool:
    """True when `git fetch --porcelain` lists `destination` as current at `value`, and nothing else."""
    lines = out.splitlines()
    if len(lines) != 1:
        return False
    line = lines[0]
    if len(line) < 2 or line[0] not in PROVEN_FLAGS or line[1] != " ":
        return False
    fields = line[2:].split(" ")
    return len(fields) == 3 and FULL_ID.fullmatch(fields[1]) is not None and fields[1:] == [value, destination]


def _ref_value(cwd: str, ref: str) -> str | None:
    """The object id a fully qualified ref names. `show-ref --verify` takes no abbreviated or symbolic name."""
    out = _output(cwd, "show-ref", "--verify", ref)
    if out is None:
        return None
    value, _, name = out.partition(" ")
    return value if name == ref and FULL_ID.fullmatch(value) else None


def _ls_remote_value(cwd: str, remote: str, branch: str) -> str | None:
    out = _output(cwd, "ls-remote", remote, f"refs/heads/{branch}")
    if out is None:
        return None
    found = [line.split("\t") for line in out.splitlines()]
    matches = [value for value, *name in found if name == [f"refs/heads/{branch}"]]
    return matches[0] if len(matches) == 1 and FULL_ID.fullmatch(matches[0]) else None


def _version(cwd: str, run: Run) -> tuple[int, int] | None:
    if run.version is None:
        out = _output(cwd, "version")
        match = VERSION.match(out or "")
        if match is None:
            return None
        run.version = (int(match[1]), int(match[2]))
    return run.version


def _fetch_proves(cwd: str, ref: str, run: Run) -> bool:
    """D3 rule 11: fetch `ref` now with an explicit refspec and prove the destination is current."""
    version = _version(cwd, run)
    if version is None:
        return False
    remote, branch = _remote(ref)
    refspec = f"+refs/heads/{branch}:{ref}"
    common = ("--no-tags", "--no-prune-tags", "--prune", "--no-recurse-submodules", "--no-write-fetch-head")
    if version >= PORCELAIN_VERSION:
        # Without --verbose, porcelain output leaves out refs that were already up to date.
        result = _git(cwd, "fetch", "--porcelain", "--verbose", *common, remote, refspec)
        if result is None or result.code != 0:
            return False
        value = _ref_value(cwd, ref)
        return value is not None and _porcelain_proves(result.out, ref, value)
    result = _git(cwd, "fetch", *common, remote, refspec)
    if result is None or result.code != 0:
        return False
    remote_value = _ls_remote_value(cwd, remote, branch)
    return remote_value is not None and _ref_value(cwd, ref) == remote_value


def _repository_ready(cwd: str, workspace: config.Workspace) -> tuple[str, ...] | None:
    """Rules 4 to 7, 9 and 10: the allowed refs of a repository that may be trusted, or None."""
    if config.revalidate(workspace) is not None:
        return None
    # The config is checked first, so no later call ever follows an include the repository holds.
    entries = _local_config(cwd)
    if entries is None or any(refused_key(key) for key, _ in entries):
        return None
    top = _output(cwd, "rev-parse", "--show-toplevel")
    if top is None or not _same_directory(top, workspace.identity):
        return None
    grafts = _output(cwd, "rev-parse", "--git-path", "info/grafts")
    if grafts is None or os.path.lexists(os.path.join(cwd, grafts)):
        return None
    if _output(cwd, "rev-parse", "--is-shallow-repository") != "false":
        return None
    refs = _allowed_refs(cwd, workspace)
    if refs is None:
        return None
    for ref in refs:
        urls = [value for key, value in entries if key == f"remote.{_remote(ref)[0]}.url"]
        if len(urls) != 1 or not https_url(urls[0]):
            return None
    return refs


def _resolve(workspace: config.Workspace, run: Run, abbreviation: str) -> str | None:
    if (
        not isinstance(abbreviation, str)
        or not MIN_ABBREVIATION <= len(abbreviation) < 64
        or ABBREVIATION.fullmatch(abbreviation) is None
    ):
        return None
    cwd = workspace.path
    refs = _repository_ready(cwd, workspace)
    if refs is None:
        return None
    key = (workspace.identity, refs)
    if key not in run.proofs:
        run.proofs[key] = all(_fetch_proves(cwd, ref, run) for ref in refs)
    if not run.proofs[key]:
        return None
    listed = _output(cwd, "rev-parse", f"--disambiguate={abbreviation}")
    if listed is None or len(listed.splitlines()) != 1:
        return None
    full = listed.strip("\n")
    if FULL_ID.fullmatch(full) is None or not full.startswith(abbreviation):
        return None
    if _output(cwd, "cat-file", "-t", full) != "commit":
        return None
    for ref in refs:
        result = _git(cwd, "merge-base", "--is-ancestor", full, ref)
        if result is None or result.code not in (0, 1):
            return None
        if result.code == 0:
            return full
    return None


def commit_resolver(workspace: config.Workspace, run: Run | None = None) -> Resolver:
    """The resolver for one `[[verify.workspace]]` entry. Pass the same `run` for every card of one run."""
    current = Run() if run is None else run

    def resolve(abbreviation: str) -> str | None:
        try:
            return _resolve(workspace, current, abbreviation)
        except Exception:  # noqa: BLE001 - rule 16: any failure returns None and never signals success
            return None

    return resolve


__all__ = ["Run", "commit_resolver", "git_argv", "git_environment", "https_url", "refused_key"]
