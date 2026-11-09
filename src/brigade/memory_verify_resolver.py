"""Hardened git commit resolver for the memory verification pass (verify slice 9a).

`commit_resolver(workspace, run)` returns the `Resolver` that
`memory_verify_contract.verify_card(resolve_commit=...)` takes. It follows D3's
"Commit resolver contract" rule by rule. The numbers below are D3's.

1. Every git call goes through `_git`, which launches through
   `proc.run_held`: a fixed timeout, no stdin, `--no-pager`, no prompt, and
   the whole process group killed on a timeout. Supervised runs also reap
   helpers left after git exits.
2. git is the first of `GIT_CANDIDATES` that exists, resolved with
   `realpath`. It must be a native (ELF) regular file, and it and every
   directory from it up to `/` must be owned by root, writable by neither
   group nor others, and neither setuid nor setgid. git's exec directory
   must meet the same rule, and must be the canonical path git reports.
   `git-remote-https` and `git-remote-http` there must pass before any
   fetch at every hop of the pathname git launches: each hop in a
   canonical, root-chained directory and the final one a native root-owned
   file. The
   verified git is opened once per run and executed through that held
   descriptor (`fexecve`), so swapping the path after the check changes
   nothing. git launches its helpers by pathname, so for them the residual
   trust is a root-owned, root-chained native file, the same level as git.
   The environment is built from scratch and holds only `ENV_ALLOWLIST`:
   `PATH` (git's exec directory and those fixed system directories that pass
   the same chain rule, as real paths), `HOME` (an empty directory inside a
   resolver-owned 0700 parent), `LC_ALL=C` and the four `GIT_*` settings D3
   names. Platforms
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
    porcelain output (or `ls-remote` before git 2.41) proves it current. The
    run keeps the object id each fetch proved. A later card re-reads every
    ref with `show-ref` and abstains for the rest of the run when one moved.
12. `rev-parse --disambiguate` must list exactly one object.
13. That object must be a commit.
14. Its id must be 40 or 64 lowercase hex characters.
15. It must be an ancestor of at least one proven object id, never a ref name.
16. Anything else returns None.

The private HOME is removed through held descriptors, iteratively and within
fixed depth and entry bounds, and each name is checked again just before its
`unlink` or `rmdir`. One race remains and is accepted: Linux has no unlink or
rmdir bound to an inode, so a process running as the same user could move an
empty directory or a single entry into the private 0700 directory under a
checked name between that check and the syscall. Such a process can already
delete the user's files, and nothing outside the private directory is ever
opened, traversed or removed.

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

    # Per repository and ref list: each allowed ref mapped to the object id its fetch proved in this run, or None
    # when the proof failed or a proven ref later moved.
    proofs: dict[tuple[config.Identity, tuple[str, ...]], dict[str, str] | None] = field(default_factory=dict)
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


# Mode bits a trusted node must not carry: group or world write, setuid and setgid.
UNSAFE_MODE = 0o022 | stat.S_ISUID | stat.S_ISGID
# git launches these by pathname from its exec directory for an https fetch. held execution cannot cover them.
HELPERS = ("git-remote-https", "git-remote-http")
REQUIRED_HELPER = "git-remote-https"
ELF_MAGIC = b"\x7fELF"


def _root_owned(path: str, metadata: os.stat_result) -> bool:
    """Owned by root. `path` lets tests fake a root without changing the mode rules."""
    return metadata.st_uid == 0


def _trusted_node(path: str, metadata: os.stat_result) -> bool:
    """Owned by root, writable by neither group nor others, and neither setuid nor setgid."""
    return _root_owned(path, metadata) and not metadata.st_mode & UNSAFE_MODE


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


def native_file(real: str) -> bool:
    """A regular file that starts with the ELF magic, read without following a link."""
    try:
        fd = os.open(real, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except (OSError, ValueError):
        return False
    try:
        return stat.S_ISREG(os.fstat(fd).st_mode) and os.read(fd, len(ELF_MAGIC)) == ELF_MAGIC
    except OSError:
        return False
    finally:
        os.close(fd)


def trusted_executable(path: str) -> str | None:
    """The real path behind `path` when it is a native, root-owned file in a root-owned chain, else None."""
    real = os.path.realpath(path)
    return real if native_file(real) and root_chain(real) else None


def find_git() -> str | None:
    """The real path of the first existing candidate, when it passes `trusted_executable`."""
    for candidate in GIT_CANDIDATES:
        if os.path.lexists(candidate):
            return trusted_executable(candidate)
    return None


# The most symlink hops a helper pathname may take before reaching its file.
MAX_LINK_HOPS = 8


def trusted_launch_path(path: str) -> bool:
    """True when the exact pathname git will exec is safe at every hop, not only at its canonical target.

    git launches a helper by this pathname, so a link anywhere along it that another user could retarget after
    the check would let that user choose the binary. Each hop must sit in a canonical directory (no symlinked
    directory on the way) whose chain up to `/` is root-owned and trusted, and the final hop must be a native,
    root-owned regular file. Links are followed one hop at a time, at most `MAX_LINK_HOPS`.
    """
    current = os.path.normpath(path)
    for _ in range(MAX_LINK_HOPS + 1):
        directory = os.path.dirname(current)
        if not os.path.isabs(current) or os.path.realpath(directory) != directory or not root_chain(directory):
            return False
        try:
            metadata = os.lstat(current)
        except (OSError, ValueError):
            return False
        if stat.S_ISLNK(metadata.st_mode):
            try:
                target = os.readlink(current)
            except OSError:
                return False
            current = os.path.normpath(os.path.join(directory, target))
            continue
        return native_file(current) and _trusted_node(current, metadata)
    return False


def helpers_trusted(exec_dir: str) -> bool:
    """Every helper an https fetch can launch from `exec_dir` is a trusted executable. git-remote-https must exist.

    git runs these by pathname, so the held descriptor does not cover them. The residual trust is a root-owned,
    root-chained native helper, the same level as git itself.
    """
    for name in HELPERS:
        path = os.path.join(exec_dir, name)
        if not os.path.lexists(path):
            if name == REQUIRED_HELPER:
                return False
            continue
        if not trusted_launch_path(path):
            return False
    return True


def trusted_dirs(directories: Sequence[str]) -> tuple[str, ...]:
    """The real paths of `directories` that exist and sit in a root-owned chain, deduplicated, in order."""
    kept: list[str] = []
    for directory in directories:
        real = os.path.realpath(directory)
        if real not in kept and os.path.isdir(real) and root_chain(real):
            kept.append(real)
    return tuple(kept)


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
        "PATH": os.pathsep.join(((exec_dir,) if exec_dir else ()) + trusted_dirs(SYSTEM_DIRS)),
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


_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _open_child_dir(parent_fd: int, name: str) -> int:
    """Open a child directory relative to a held parent, refusing a link."""
    return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)


# Bounds on one HOME removal. Past either one the removal stops and leaves the rest in place.
CLEAR_MAX_DEPTH = 64
CLEAR_MAX_ENTRIES = 10_000


@dataclass
class _Frame:
    """One held directory on the removal stack, with where it sits in its held parent."""

    fd: int
    names: list[str]
    parent_fd: int | None = None
    name: str = ""
    identity: config.Identity = (0, 0)


def _names(fd: int) -> list[str]:
    with os.scandir(fd) as entries:
        return [entry.name for entry in entries]


def _same_entry(parent_fd: int, name: str, identity: config.Identity) -> bool:
    """Re-stat `name` relative to its held parent, without following a link, and compare device and inode."""
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return (metadata.st_dev, metadata.st_ino) == identity


def _clear_held(fd: int) -> bool:
    """Remove everything inside the held directory `fd`. True when it is empty afterwards.

    Iterative, with an explicit stack of held descriptors, so depth never reaches Python's recursion limit.
    Every directory is opened relative to its held parent with O_NOFOLLOW and its descriptor must name the
    entry just read. Immediately before each `unlink` or `rmdir`, the name is checked again with `stat` relative
    to the held parent and must still have the verified device and inode, or that entry is skipped and logged. Deeper than
    `CLEAR_MAX_DEPTH` or more than `CLEAR_MAX_ENTRIES` entries stops the removal with the rest left in place.
    """
    stack = [_Frame(fd, _names(fd))]
    seen = 0
    clean = True
    try:
        while stack:
            frame = stack[-1]
            if not frame.names:
                stack.pop()
                if frame.parent_fd is None:
                    continue
                os.close(frame.fd)
                if _same_entry(frame.parent_fd, frame.name, frame.identity):
                    try:
                        os.rmdir(frame.name, dir_fd=frame.parent_fd)
                    except OSError:
                        clean = False
                else:
                    LOG.warning("verify resolver skipped a directory in its private HOME that changed")
                    clean = False
                continue
            name = frame.names.pop()
            seen += 1
            if seen > CLEAR_MAX_ENTRIES:
                LOG.warning("verify resolver left its private HOME in place: more than %d entries", CLEAR_MAX_ENTRIES)
                return False
            entry = os.stat(name, dir_fd=frame.fd, follow_symlinks=False)
            identity = (entry.st_dev, entry.st_ino)
            if not stat.S_ISDIR(entry.st_mode):
                if _same_entry(frame.fd, name, identity):
                    os.unlink(name, dir_fd=frame.fd)
                else:
                    LOG.warning("verify resolver skipped an entry in its private HOME that changed")
                    clean = False
                continue
            if len(stack) >= CLEAR_MAX_DEPTH:
                LOG.warning("verify resolver left its private HOME in place: deeper than %d", CLEAR_MAX_DEPTH)
                return False
            child = _open_child_dir(frame.fd, name)
            opened = os.fstat(child)
            if (opened.st_dev, opened.st_ino) != identity:
                os.close(child)
                LOG.warning("verify resolver skipped a directory in its private HOME that changed")
                clean = False
                continue
            stack.append(_Frame(child, _names(child), frame.fd, name, identity))
        return clean
    finally:
        for frame in stack:
            if frame.parent_fd is not None:
                _close_quietly(frame.fd)


def remove_home(home: _Home) -> bool:
    """Remove the HOME and its parent only while both are still the directories the resolver made.

    The parent is opened by walking from `/` without following any link and must keep its identity (device,
    inode, owner, directory type). The HOME is opened relative to that held parent with O_NOFOLLOW and its
    descriptor must keep the HOME's identity. Its contents are removed through the held descriptor, so a HOME
    swapped after the check is never touched. The empty HOME is then removed by name only after that name is
    re-checked against the same identity. Any mismatch leaves everything in place and logs it. Nothing raises.

    Residual race, accepted: Linux has no unlink or rmdir bound to an inode, so between the last identity
    re-check and the syscall, a process running as the same user could move an empty directory or a single
    entry into this private 0700 directory under the checked name, and that one entry would be removed. Such a
    process can already delete the user's files directly, so it gains nothing. Nothing outside the private
    directory is ever opened, traversed or removed.
    """
    try:
        parent_fd = dirfd.open_directory_chain(Path(home.parent), writable=False)
        try:
            if not _matches(os.fstat(parent_fd), home.parent_id):
                LOG.warning("verify resolver left its private HOME in place: its parent was replaced")
                return False
            home_fd = _open_child_dir(parent_fd, home.name)
            try:
                if not _matches(os.fstat(home_fd), home.home_id):
                    LOG.warning("verify resolver left its private HOME in place: it was replaced")
                    return False
                cleared = _clear_held(home_fd)
            finally:
                os.close(home_fd)
            if not cleared:
                LOG.warning("verify resolver left its private HOME in place: part of it could not be removed")
                return False
            if not _matches(os.stat(home.name, dir_fd=parent_fd, follow_symlinks=False), home.home_id):
                LOG.warning("verify resolver left its private HOME in place: it was replaced during removal")
                return False
            os.rmdir(home.name, dir_fd=parent_fd)
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
    # git launches helpers through the exec directory it reports, so that pathname must already be canonical.
    exec_real = os.path.realpath(exec_dir)
    if (
        exec_real != os.path.normpath(exec_dir)
        or not os.path.isdir(exec_real)
        or not root_chain(exec_real)
        or not helpers_trusted(exec_real)
    ):
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


def _fetch_proves(tool: GitTool, cwd: str, ref: str, run: Run) -> str | None:
    """D3 rule 11: fetch `ref` now with an explicit refspec and return the object id the fetch proved, or None."""
    version = _version(tool, cwd, run)
    if version is None:
        return None
    remote, branch = _remote(ref)
    refspec = f"+refs/heads/{branch}:{ref}"
    # An empty --refmap makes git ignore every configured remote.<name>.fetch mapping, so only the explicit
    # refspec below can update a ref.
    common = (
        "--refmap=",
        "--no-tags",
        "--no-prune-tags",
        "--prune",
        "--no-recurse-submodules",
        "--no-write-fetch-head",
    )
    if version >= PORCELAIN_VERSION:
        # Without --verbose, porcelain output leaves out refs that were already up to date.
        result = _git(tool, cwd, "fetch", "--porcelain", "--verbose", *common, remote, refspec)
        if result is None or result.code != 0:
            return None
        value = _ref_value(tool, cwd, ref)
        return value if value is not None and _porcelain_proves(result.out, ref, value) else None
    result = _git(tool, cwd, "fetch", *common, remote, refspec)
    if result is None or result.code != 0:
        return None
    remote_value = _ls_remote_value(tool, cwd, remote, branch)
    return remote_value if remote_value is not None and _ref_value(tool, cwd, ref) == remote_value else None


def _proven_ids(
    tool: GitTool, cwd: str, key: tuple[config.Identity, tuple[str, ...]], run: Run
) -> dict[str, str] | None:
    """The object id each allowed ref's fetch proved in this run, fetching on the run's first card.

    A later card first re-reads every ref with `show-ref`. A ref that no longer names its proven id moved after
    the proof (a local write the fetch did not make), so the run stops trusting that repository: the cache is
    dropped to None and every later card for it abstains. The run never refetches to re-prove.
    """
    if key not in run.proofs:
        proven: dict[str, str] = {}
        for ref in key[1]:
            value = _fetch_proves(tool, cwd, ref, run)
            if value is None:
                run.proofs[key] = None
                return None
            proven[ref] = value
        run.proofs[key] = proven
        return proven
    cached = run.proofs[key]
    if cached is None:
        return None
    if any(_ref_value(tool, cwd, ref) != value for ref, value in cached.items()):
        run.proofs[key] = None
        return None
    return cached


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
    proven = _proven_ids(tool, cwd, (workspace.identity, refs), run)
    if proven is None:
        return None
    listed = _output(tool, cwd, "rev-parse", f"--disambiguate={abbreviation}")
    if listed is None or len(listed.splitlines()) != 1:
        return None
    full = listed.strip("\n")
    if FULL_ID.fullmatch(full) is None or not full.startswith(abbreviation):
        return None
    if _output(tool, cwd, "cat-file", "-t", full) != "commit":
        return None
    # Reachability is checked against the object ids the fetch proved, never against a ref name that can move.
    for ref in refs:
        result = _git(tool, cwd, "merge-base", "--is-ancestor", full, proven[ref])
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
