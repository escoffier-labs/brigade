"""Hardened git commit resolver for the memory verification pass (verify slice 9a).

`commit_resolver(workspace, run)` returns the `Resolver` that
`memory_verify_contract.verify_card(resolve_commit=...)` takes. It follows D3's
"Commit resolver contract" rule by rule. The numbers below are D3's.

1. Every git call goes through `_git`, which launches through `proc.run`: a
   fixed timeout, no stdin, `--no-pager`, no prompt, and the whole process
   tree killed on a timeout (a process group on POSIX, a kill-on-close job
   object on Windows). Supervised runs also reap helpers left after git exits.
2. The environment is built from scratch, never copied from the caller. git
   itself is a trusted absolute path, resolved once per run from
   `[verify] git_executable` or from fixed system directories, and refused
   when it is group- or world-writable or owned by another user. The
   environment then holds only `ENV_ALLOWLIST`: `PATH` (git's exec directory
   and the fixed system directories), `HOME` (an empty private directory),
   `LC_ALL=C`, the four `GIT_*` settings D3 names, and on Windows the system
   variables listed in `WINDOWS_KEPT`.
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
import shutil
import stat
import tempfile
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import memory_verify_config as config
from . import proc
from .memory_verify_contract import MIN_ABBREVIATION, Resolver

TIMEOUT_SECONDS = 60.0
# git 2.41 added `git fetch --porcelain`. Older git proves freshness through `ls-remote` instead.
PORCELAIN_VERSION = (2, 41)
FULL_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
ABBREVIATION = re.compile(r"[0-9a-f]+")
VERSION = re.compile(r"git version (\d+)\.(\d+)")
# A directory that cannot exist: no path can sit below the null device.
NO_HOOKS = os.path.join(os.devnull, "brigade-no-hooks")
# Where git is looked up when `[verify] git_executable` is unset. The caller's PATH is never consulted.
POSIX_SYSTEM_DIRS = ("/usr/bin", "/bin", "/usr/local/bin")
WINDOWS_GIT_DIRS = (r"C:\Program Files\Git\cmd", r"C:\Program Files\Git\bin")
GIT_SETTINGS = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_TERMINAL_PROMPT": "0",
}
# Windows variables copied from the caller, and why git for Windows needs each one:
# SYSTEMROOT: Winsock and the system crypto provider fail to initialize without it.
# WINDIR: system DLL lookups and the msys runtime read it.
# TEMP and TMP: git for Windows and its msys runtime put temporary files there.
WINDOWS_KEPT = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")
ENV_ALLOWLIST = frozenset({"PATH", "HOME", "LC_ALL", *GIT_SETTINGS}) | (
    frozenset(WINDOWS_KEPT) if os.name == "nt" else frozenset()
)
# `proc.run` reports a timeout as exit 124, which git itself never uses.
PROC_TIMEOUT_CODE = 124
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


@dataclass(frozen=True)
class GitTool:
    """A trusted git executable and the from-scratch environment every call gets."""

    executable: str
    env: Mapping[str, str]

    def argv(self, args: Sequence[str]) -> list[str]:
        """The full argv for one git call (D3 rules 1 and 3)."""
        argv = [self.executable, "--no-pager", "--no-replace-objects"]
        for override in OVERRIDES:
            argv += ["-c", override]
        return argv + list(args)


@dataclass
class Run:
    """One verification run: the trusted git, its private HOME, the git version and this run's fetch proofs.

    `git_executable` is the `[verify] git_executable` value, when one is configured.
    """

    git_executable: str | None = None
    proofs: dict[tuple[config.Identity, tuple[str, ...]], bool] = field(default_factory=dict)
    version: tuple[int, int] | None = None
    tool: GitTool | None = None
    tool_failed: bool = False


@dataclass(frozen=True)
class _Result:
    code: int
    out: str


def trusted_file(path: str, *, directory: bool = False) -> bool:
    """True for an existing file (or directory) that no other user can replace.

    On POSIX the target and its parent directory must be owned by root or the
    current user and be writable by neither group nor others. Windows has no
    portable owner check here, so it trusts the fixed install locations and
    the configured path as given.
    """
    if os.name != "posix":
        return os.path.isdir(path) if directory else os.path.isfile(path)
    real = os.path.realpath(path)
    owners = {0, os.getuid()}
    for candidate, want_dir in ((real, directory), (os.path.dirname(real), True)):
        try:
            metadata = os.stat(candidate)
        except (OSError, ValueError):
            return False
        kind = stat.S_ISDIR if want_dir else stat.S_ISREG
        if not kind(metadata.st_mode) or metadata.st_mode & 0o022 or metadata.st_uid not in owners:
            return False
    return True


def find_git(explicit: str | None = None) -> str | None:
    """An absolute, trusted git executable: the configured one, else the first in the fixed system directories."""
    if explicit is not None:
        candidates: Sequence[str] = (explicit,)
    elif os.name == "nt":
        candidates = tuple(os.path.join(directory, "git.exe") for directory in WINDOWS_GIT_DIRS)
    else:
        candidates = tuple(os.path.join(directory, "git") for directory in POSIX_SYSTEM_DIRS)
    for candidate in candidates:
        if os.path.isabs(candidate) and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate if trusted_file(candidate) else None
    return None


def _system_dirs() -> tuple[str, ...]:
    if os.name != "nt":
        return POSIX_SYSTEM_DIRS
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return (*WINDOWS_GIT_DIRS, os.path.join(root, "System32"), root)


def git_environment(
    home: str, exec_dir: str | None = None, inherited: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The from-scratch environment every git call runs with (D3 rule 2). Only `ENV_ALLOWLIST` keys appear."""
    source = os.environ if inherited is None else inherited
    env = {
        "PATH": os.pathsep.join(((exec_dir,) if exec_dir else ()) + _system_dirs()),
        "HOME": home,
        "LC_ALL": "C",
        **GIT_SETTINGS,
    }
    if os.name == "nt":
        upper = {name.upper(): value for name, value in source.items()}
        env.update({name: upper[name] for name in WINDOWS_KEPT if name in upper})
    return env


def _private_home(run: Run) -> str:
    """An empty directory only this user can read, removed when the run is collected or the process exits."""
    home = tempfile.mkdtemp(prefix="brigade-git-home-")
    weakref.finalize(run, shutil.rmtree, home, True)
    return home


def _tool(run: Run) -> GitTool | None:
    """Resolve the trusted git once per run, then its exec directory for the helpers it launches."""
    if run.tool is not None or run.tool_failed:
        return run.tool
    run.tool_failed = True
    executable = find_git(run.git_executable)
    if executable is None:
        return None
    home = _private_home(run)
    bootstrap = GitTool(executable, git_environment(home))
    exec_dir = _output(bootstrap, home, "--exec-path")
    if exec_dir is None or not os.path.isabs(exec_dir) or not trusted_file(exec_dir, directory=True):
        return None
    run.tool, run.tool_failed = GitTool(executable, git_environment(home, exec_dir)), False
    return run.tool


def _git(tool: GitTool, cwd: str, *args: str) -> _Result | None:
    """Run one hardened git call. None on a launch failure, a timeout, truncated or non-UTF-8 output."""
    result = proc.run(tool.argv(args), timeout=TIMEOUT_SECONDS, env=dict(tool.env), cwd=Path(cwd), supervise_group=True)
    if (
        result.code == PROC_TIMEOUT_CODE
        or result.decode_failed
        or result.output_limit_exceeded
        or result.stream_limit_exceeded
        or result.incomplete_process_group
    ):
        return None
    return _Result(result.code, result.stdout)


def _output(tool: GitTool, cwd: str, *args: str) -> str | None:
    """Stdout of a call that must exit 0, without its final newline."""
    result = _git(tool, cwd, *args)
    if result is None or result.code != 0:
        return None
    return result.out[:-1] if result.out.endswith("\n") else result.out


def _same_directory(path: str, identity: config.Identity) -> bool:
    try:
        metadata = os.stat(path)
    except (OSError, ValueError):
        return False
    return metadata.st_ino != 0 and (metadata.st_dev, metadata.st_ino) == identity


def _local_config(tool: GitTool, cwd: str) -> list[tuple[str, str]] | None:
    """Every local config entry as (key, value), never following includes."""
    result = _git(tool, cwd, "config", "--local", "--no-includes", "--null", "--list")
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


def _allowed_refs(tool: GitTool, cwd: str, workspace: config.Workspace) -> tuple[str, ...] | None:
    if workspace.commit_refs:
        return workspace.commit_refs
    target = _output(tool, cwd, "symbolic-ref", "--quiet", DEFAULT_HEAD)
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


def _ref_value(tool: GitTool, cwd: str, ref: str) -> str | None:
    """The object id a fully qualified ref names. `show-ref --verify` takes no abbreviated or symbolic name."""
    out = _output(tool, cwd, "show-ref", "--verify", ref)
    if out is None:
        return None
    value, _, name = out.partition(" ")
    return value if name == ref and FULL_ID.fullmatch(value) else None


def _ls_remote_value(tool: GitTool, cwd: str, remote: str, branch: str) -> str | None:
    out = _output(tool, cwd, "ls-remote", remote, f"refs/heads/{branch}")
    if out is None:
        return None
    found = [line.split("\t") for line in out.splitlines()]
    matches = [value for value, *name in found if name == [f"refs/heads/{branch}"]]
    return matches[0] if len(matches) == 1 and FULL_ID.fullmatch(matches[0]) else None


def _version(tool: GitTool, cwd: str, run: Run) -> tuple[int, int] | None:
    if run.version is None:
        out = _output(tool, cwd, "version")
        match = VERSION.match(out or "")
        if match is None:
            return None
        run.version = (int(match[1]), int(match[2]))
    return run.version


def _fetch_proves(tool: GitTool, cwd: str, ref: str, run: Run) -> bool:
    """D3 rule 11: fetch `ref` now with an explicit refspec and prove the destination is current."""
    version = _version(tool, cwd, run)
    if version is None:
        return False
    remote, branch = _remote(ref)
    refspec = f"+refs/heads/{branch}:{ref}"
    common = ("--no-tags", "--no-prune-tags", "--prune", "--no-recurse-submodules", "--no-write-fetch-head")
    if version >= PORCELAIN_VERSION:
        # Without --verbose, porcelain output leaves out refs that were already up to date.
        result = _git(tool, cwd, "fetch", "--porcelain", "--verbose", *common, remote, refspec)
        if result is None or result.code != 0:
            return False
        value = _ref_value(tool, cwd, ref)
        return value is not None and _porcelain_proves(result.out, ref, value)
    result = _git(tool, cwd, "fetch", *common, remote, refspec)
    if result is None or result.code != 0:
        return False
    remote_value = _ls_remote_value(tool, cwd, remote, branch)
    return remote_value is not None and _ref_value(tool, cwd, ref) == remote_value


def _repository_ready(tool: GitTool, cwd: str, workspace: config.Workspace) -> tuple[str, ...] | None:
    """Rules 4 to 7, 9 and 10: the allowed refs of a repository that may be trusted, or None."""
    if config.revalidate(workspace) is not None:
        return None
    # The config is checked first, so no later call ever follows an include the repository holds.
    entries = _local_config(tool, cwd)
    if entries is None or any(refused_key(key) for key, _ in entries):
        return None
    top = _output(tool, cwd, "rev-parse", "--show-toplevel")
    if top is None or not _same_directory(top, workspace.identity):
        return None
    grafts = _output(tool, cwd, "rev-parse", "--git-path", "info/grafts")
    if grafts is None or os.path.lexists(os.path.join(cwd, grafts)):
        return None
    if _output(tool, cwd, "rev-parse", "--is-shallow-repository") != "false":
        return None
    refs = _allowed_refs(tool, cwd, workspace)
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
    tool = _tool(run)
    if tool is None:
        return None
    cwd = workspace.path
    refs = _repository_ready(tool, cwd, workspace)
    if refs is None:
        return None
    key = (workspace.identity, refs)
    if key not in run.proofs:
        run.proofs[key] = all(_fetch_proves(tool, cwd, ref, run) for ref in refs)
    if not run.proofs[key]:
        return None
    listed = _output(tool, cwd, "rev-parse", f"--disambiguate={abbreviation}")
    if listed is None or len(listed.splitlines()) != 1:
        return None
    full = listed.strip("\n")
    if FULL_ID.fullmatch(full) is None or not full.startswith(abbreviation):
        return None
    if _output(tool, cwd, "cat-file", "-t", full) != "commit":
        return None
    for ref in refs:
        result = _git(tool, cwd, "merge-base", "--is-ancestor", full, ref)
        if result is None or result.code not in (0, 1):
            return None
        if result.code == 0:
            return full
    return None


def commit_resolver(workspace: config.Workspace, run: Run | None = None) -> Resolver:
    """The resolver for one `[[verify.workspace]]` entry. Pass the same `run` for every card of one run.

    A run made for a configuration is `Run(git_executable=verify_config.git_executable)`.
    """
    current = Run() if run is None else run

    def resolve(abbreviation: str) -> str | None:
        try:
            return _resolve(workspace, current, abbreviation)
        except Exception:  # noqa: BLE001 - rule 16: any failure returns None and never signals success
            return None

    return resolve


__all__ = ["GitTool", "Run", "commit_resolver", "find_git", "git_environment", "https_url", "refused_key"]
