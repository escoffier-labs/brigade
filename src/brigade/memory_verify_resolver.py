"""Hardened git commit resolver for the memory verification pass (verify slice 9a).

`commit_resolver(workspace, run)` returns the `Resolver` that
`memory_verify_contract.verify_card(resolve_commit=...)` takes. It follows D3's
"Commit resolver contract" rule by rule. The numbers below are D3's.

1. Every git call goes through `_git`, which launches through
   `proc.run_held`: a fixed timeout, no stdin, `--no-pager`, no prompt, and
   the whole process group killed on a timeout. Supervised runs also reap
   helpers left after git exits.
2. git is the first of `GIT_CANDIDATES` that exists, resolved with
   `realpath`. The real file and every directory from it up to `/` must be
   owned by root and writable by neither group nor others, and so must git's
   exec directory. The verified file is opened once per run and executed
   through that held descriptor (`fexecve`), so swapping the path after the
   check changes nothing. The environment is built from scratch and holds
   only `ENV_ALLOWLIST`: `PATH` (git's exec directory and the fixed system
   directories), `HOME` (an empty directory inside a resolver-owned 0700
   parent), `LC_ALL=C` and the four `GIT_*` settings D3 names. Platforms
   without held execution (Windows, and POSIX systems without `fexecve`)
   fail closed: the resolver returns None and never runs git.
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

import logging
import os
import re
import stat
import tempfile
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import dirfd, proc
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
# The only git executables the resolver runs, in order. The first one that exists decides.
GIT_CANDIDATES = ("/usr/bin/git", "/bin/git", "/usr/local/bin/git")
SYSTEM_DIRS = ("/usr/bin", "/bin", "/usr/local/bin")
# The fixed parent of the resolver's private directories. Never taken from config or the environment.
SYSTEM_TEMP = "/tmp"
GIT_SETTINGS = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_TERMINAL_PROMPT": "0",
}
ENV_ALLOWLIST = frozenset({"PATH", "HOME", "LC_ALL", *GIT_SETTINGS})
UNSUPPORTED_PLATFORM = "verify_resolver_unsupported_platform"
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


LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class GitTool:
    """The verified git, held open, and the from-scratch environment every call gets.

    `path` is the verified real path. It is only argv[0]: the image that runs is the one behind `fd`.
    """

    path: str
    fd: int
    env: Mapping[str, str]

    def argv(self, args: Sequence[str]) -> list[str]:
        """The full argv for one git call (D3 rules 1 and 3)."""
        argv = [self.path, "--no-pager", "--no-replace-objects"]
        for override in OVERRIDES:
            argv += ["-c", override]
        return argv + list(args)


@dataclass
class Run:
    """One verification run: the held git, its private HOME, the git version and this run's fetch proofs.

    Call `close()` when the run ends. Collecting the object does the same.
    """

    proofs: dict[tuple[config.Identity, tuple[str, ...]], bool] = field(default_factory=dict)
    version: tuple[int, int] | None = None
    tool: GitTool | None = None
    tool_failed: bool = False
    cleanups: list[weakref.finalize] = field(default_factory=list, repr=False)

    def close(self) -> None:
        for cleanup in self.cleanups:
            cleanup()


@dataclass(frozen=True)
class _Result:
    code: int
    out: str


@dataclass(frozen=True)
class _Home:
    """A private HOME and the identities recorded when the resolver created it and its parent."""

    parent: str
    parent_id: config.Identity
    name: str
    home_id: config.Identity

    @property
    def path(self) -> str:
        return os.path.join(self.parent, self.name)


def platform_supported() -> bool:
    """Whether the resolver may run git here: POSIX with held-descriptor execution."""
    return os.name == "posix" and proc.held_exec_supported()


def unavailable_reason() -> str | None:
    """The fixed reason every expansion abstains on this platform, or None where the resolver runs."""
    return None if platform_supported() else UNSUPPORTED_PLATFORM


def _trusted_node(path: str, metadata: os.stat_result) -> bool:
    """Owned by root and writable by neither group nor others. `path` is for tests that fake a root."""
    return metadata.st_uid == 0 and not metadata.st_mode & 0o022


def root_chain(path: str) -> bool:
    """True when `path` (already real) and every directory above it up to `/` is a trusted, non-link node."""
    current = path
    while True:
        try:
            metadata = os.lstat(current)
        except (OSError, ValueError):
            return False
        if stat.S_ISLNK(metadata.st_mode) or not _trusted_node(current, metadata):
            return False
        parent = os.path.dirname(current)
        if parent == current:
            return True
        current = parent


def find_git() -> str | None:
    """The real path of the first existing candidate, when it is a root-owned regular file in a root-owned chain."""
    for candidate in GIT_CANDIDATES:
        if not os.path.lexists(candidate):
            continue
        real = os.path.realpath(candidate)
        try:
            regular = stat.S_ISREG(os.lstat(real).st_mode)
        except (OSError, ValueError):
            return None
        return real if regular and root_chain(real) else None
    return None


def _hold(real: str) -> int | None:
    """Open the verified file and prove the descriptor is that file, still in a trusted chain."""
    try:
        fd = proc.high_fd(os.open(real, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW))
    except (OSError, ValueError):
        return None
    try:
        held = os.fstat(fd)
        named = os.lstat(real)
        if (
            stat.S_ISREG(held.st_mode)
            and _trusted_node(real, held)
            and (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino)
            and root_chain(real)
        ):
            return fd
    except (OSError, ValueError):
        pass
    os.close(fd)
    return None


def git_environment(home: str, exec_dir: str | None = None) -> dict[str, str]:
    """The from-scratch environment every git call runs with (D3 rule 2). Only `ENV_ALLOWLIST` keys appear."""
    return {
        "PATH": os.pathsep.join(((exec_dir,) if exec_dir else ()) + SYSTEM_DIRS),
        "HOME": home,
        "LC_ALL": "C",
        **GIT_SETTINGS,
    }


def _make_home() -> _Home:
    """A 0700 parent made with mkdtemp under `SYSTEM_TEMP`, and an empty 0700 HOME made inside it."""
    parent = tempfile.mkdtemp(prefix="brigade-verify-git-", dir=SYSTEM_TEMP)
    parent_meta = os.lstat(parent)
    path = tempfile.mkdtemp(prefix="home-", dir=parent)
    home_meta = os.lstat(path)
    return _Home(
        parent=parent,
        parent_id=(parent_meta.st_dev, parent_meta.st_ino),
        name=os.path.basename(path),
        home_id=(home_meta.st_dev, home_meta.st_ino),
    )


def _matches(metadata: os.stat_result, identity: config.Identity) -> bool:
    return (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and (metadata.st_dev, metadata.st_ino) == identity
    )


def remove_home(home: _Home) -> bool:
    """Remove the HOME and its parent only while both are still the directories the resolver made.

    The parent is opened by walking from `/` without following any link, both
    identities are compared (device, inode, owner, directory type), and the
    HOME is removed relative to the held parent by the descriptor-based
    rmtree, which never follows a link. Any mismatch leaves everything in
    place and logs it. Nothing raises.
    """
    try:
        if not dirfd.tree_removal_available():
            LOG.warning("verify resolver left its private HOME in place: no link-safe tree removal here")
            return False
        parent_fd = dirfd.open_directory_chain(Path(home.parent), writable=False)
        try:
            if not _matches(os.fstat(parent_fd), home.parent_id):
                LOG.warning("verify resolver left its private HOME in place: its parent was replaced")
                return False
            if not _matches(dirfd.stat_child(parent_fd, home.name), home.home_id):
                LOG.warning("verify resolver left its private HOME in place: it was replaced")
                return False
            dirfd.remove_tree_child(parent_fd, home.name)
        finally:
            os.close(parent_fd)
        top_fd = dirfd.open_directory_chain(Path(os.path.dirname(home.parent)), writable=False)
        try:
            name = os.path.basename(home.parent)
            if _matches(dirfd.stat_child(top_fd, name), home.parent_id):
                dirfd.rmdir_child(top_fd, name)
        finally:
            os.close(top_fd)
        return True
    except Exception as exc:  # noqa: BLE001 - cleanup never raises past the run
        LOG.warning("verify resolver left its private HOME in place: %s", type(exc).__name__)
        return False


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _private_home(run: Run) -> str:
    """Make the run's private HOME and register its checked removal."""
    home = _make_home()
    run.cleanups.append(weakref.finalize(run, remove_home, home))
    return home.path


def _tool(run: Run) -> GitTool | None:
    """Verify and hold git once per run, then check its exec directory for the helpers it launches."""
    if run.tool is not None or run.tool_failed:
        return run.tool
    run.tool_failed = True
    real = find_git()
    fd = None if real is None else _hold(real)
    if real is None or fd is None:
        return None
    run.cleanups.append(weakref.finalize(run, _close_quietly, fd))
    home = _private_home(run)
    bootstrap = GitTool(real, fd, git_environment(home))
    exec_dir = _output(bootstrap, home, "--exec-path")
    if exec_dir is None or not os.path.isabs(exec_dir):
        return None
    exec_real = os.path.realpath(exec_dir)
    if not os.path.isdir(exec_real) or not root_chain(exec_real):
        return None
    run.tool, run.tool_failed = GitTool(real, fd, git_environment(home, exec_real)), False
    return run.tool


def _git(tool: GitTool, cwd: str, *args: str) -> _Result | None:
    """Run one hardened git call from the held descriptor. None on a launch failure, a timeout or bad output."""
    try:
        result = proc.run_held(
            tool.argv(args),
            tool.fd,
            timeout=TIMEOUT_SECONDS,
            env=dict(tool.env),
            cwd=Path(cwd),
            supervise_group=True,
        )
    except OSError:
        return None
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

    Where `platform_supported()` is false the resolver returns None for every abbreviation and never runs
    git. `unavailable_reason()` gives the fixed reason to report.
    """
    if not platform_supported():
        return lambda abbreviation: None
    current = Run() if run is None else run

    def resolve(abbreviation: str) -> str | None:
        try:
            return _resolve(workspace, current, abbreviation)
        except Exception:  # noqa: BLE001 - rule 16: any failure returns None and never signals success
            return None

    return resolve


__all__ = [
    "UNSUPPORTED_PLATFORM",
    "GitTool",
    "Run",
    "commit_resolver",
    "find_git",
    "git_environment",
    "https_url",
    "platform_supported",
    "refused_key",
    "remove_home",
    "root_chain",
    "unavailable_reason",
]
