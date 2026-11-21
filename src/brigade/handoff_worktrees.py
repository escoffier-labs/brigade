"""Memory Handoff inboxes across git worktrees.

A handoff written into a linked worktree's inbox dies with that worktree, and
an ingester that only watches the main checkout never sees it. These helpers
let ingest find sibling worktree inboxes (bounded by `git worktree list`, never
a directory walk), let worktree removal deliver pending handoffs to the main
checkout first, and tell hooks which inbox outlives the worktree.

Every path git reports is checked against the configured repository's own
common git dir, and every inbox component is opened through held, no-follow
directory descriptors, so neither repo metadata nor a planted link can steer
discovery or rescue outside that repository.

Rescue claims a quiet source (unmodified for QUIESCENCE_SECONDS) with an
atomic no-replace rename, copies and publishes it, then makes a bounded
final check (size snapshot, hash, mtime, under the ingest cap and
FINAL_CHECK_SECONDS) while the claim is still named. It unlinks the claim
only if that check matches the delivered copy. Otherwise the claim is renamed
back under a recovery name and the worktree is kept. Residual risk: a write
that lands between the final check and the unlink is lost. That needs a
writer stalled longer than the quiescence window with a descriptor still
open. Closing it needs an atomic publication contract for handoff writers
(write a private temp, fsync, publish with a no-replace rename), tracked in escoffier-labs/brigade#2397.

Known conservative limit: git does not look inside an embedded repository
(an untracked nested repo, at any depth and in ignored trees too) or an
initialized submodule, so a worktree holding one is always kept and
reported as `embedded-repository`, whether or not it holds a handoff.

Git is pointed at the held worktree and admin directories. On Linux that
is through `/proc/self/fd`, so renaming another tree into either path does
not redirect it. Other platforms pass the paths and compare both
directories with the held descriptors after git exits, which catches a
swap that is still in place but not one that is swapped back before git
exits. The admin dir is opened while it is verified and then held, and
removal renames the tree to a private name through its held parent before
deleting anything, so a rename cannot redirect either step. An actor able
to rename those directories can already delete the handoff directly, so
rescue guards against accidental loss, not that.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING

from . import dirfd
from .selection import WRITER_INBOXES

if TYPE_CHECKING:
    from .ingest import _TrackedIndex

# Linked worktrees considered per repository; records past this are never
# resolved or stat'ed.
MAX_WORKTREES = 256
# `git worktree list` output larger than this is refused (fail closed).
MAX_WORKTREE_LIST_BYTES = 1 << 20
# Directory entries read from one inbox. A source inbox past this is refused;
# a destination index past it dedupes only the entries it read.
MAX_INBOX_ENTRIES = 8192
MAX_HANDOFF_BYTES = 1 << 20
_GIT_TIMEOUT_SECONDS = 20
# Git state that would point `git -C <dir>` at another repository.
GIT_LOCATION_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_CEILING_DIRECTORIES",
)
CLAUDE_INBOX = WRITER_INBOXES["claude"]
# Ingesters list only `*.md`, so a partial rescue copy is never read.
_TEMP_PREFIX = ".brigade-rescue-"
_TEMP_SUFFIX = ".tmp"
# A source being rescued is renamed to a private claim name first. No
# ingester or writer lists it, and a writer that returns to the original
# path creates a new file instead of changing the claimed one.
_CLAIM_PREFIX = ".brigade-claim-"
# Oversized late bytes kept under a name ingest skips. Like a claim, it is an
# unresolved handoff that must keep its worktree until an operator acts.
_OVERSIZE_PREFIX = ".late-oversize-"
_CLAIM_SUFFIX = ".md.claimed"
# A `.git` file holds one `gitdir:` line. Its read is bounded separately
# from the handoff size cap.
_GIT_MARKER_BYTES = 64 * 1024
# A handoff modified more recently than this is still being written. It is
# left in place and its worktree is kept.
QUIESCENCE_SECONDS = 5.0
# Ceiling for the final pre-unlink check. Bytes are bounded by the ingest cap.
FINAL_CHECK_SECONDS = 10.0


# Environment that could point git at another repository, work tree or
# config, or change how pathspecs match.
_GIT_STRIP_ENV = (
    *GIT_LOCATION_ENV,
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_LITERAL_PATHSPECS",
    "GIT_GLOB_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
)


def sanitized_git_env() -> dict[str, str]:
    """Process environment for git with injected config and locations removed.

    System and global config are off too, so only the repository's own
    config applies, and callers pin the work tree explicitly where it counts.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _GIT_STRIP_ENV and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    }
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["LC_ALL"] = "C"
    return env


_git_env = sanitized_git_env


def _git(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            env=_git_env(),
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _git_path(path: Path, flag: str) -> Path | None:
    out = _git(path, "rev-parse", "--path-format=absolute", flag)
    value = out.rstrip("\n") if out is not None else ""
    if not value or "\n" in value:
        return None
    try:
        return Path(value).resolve(strict=True)
    except OSError:
        return None


def git_common_dir(path: Path) -> Path | None:
    """Resolved common git dir of the repository containing `path`."""
    return _git_path(path, "--git-common-dir")


def main_checkout(path: Path, *, repo: Path | None = None) -> Path | None:
    """Return the main working tree when `path` sits in a linked worktree.

    The common dir must be a real `.git` directory whose parent checkout
    reports that same common dir as its own git dir, and `path`'s git dir must
    be one of its `worktrees/<name>` admin dirs. With `repo`, the common dir
    must also be `repo`'s. Anything else returns None.
    """
    git_dir = _git_path(path, "--git-dir")
    common = git_common_dir(path)
    if git_dir is None or common is None or git_dir == common:
        return None
    if common.name != ".git" or git_dir.parent != common / "worktrees":
        return None
    main = common.parent
    try:
        if not stat.S_ISDIR((main / ".git").lstat().st_mode):
            return None
    except OSError:
        return None
    if _git_path(main, "--git-dir") != common or git_common_dir(main) != common:
        return None
    if repo is not None and git_common_dir(repo) != common:
        return None
    return main


def checkout_prefix(path: Path) -> tuple[str, ...] | None:
    """Components of `path` below its checkout's top level, () at the top.

    None when git cannot say or the prefix is not a plain relative path.
    """
    out = _git(path, "rev-parse", "--show-prefix")
    if out is None:
        return None
    value = out.rstrip("\n").rstrip("/")
    if not value:
        return ()
    try:
        return inbox_components(value)
    except ValueError:
        return None


def open_main_checkout_for(path: Path, *, repo: Path | None = None) -> tuple[Path, int] | None:
    """`main_checkout(path)` mapped to `path`'s subdirectory, as (path, held fd).

    Every prefix component is walked from the main checkout root with held
    no-follow descriptors. The returned descriptor is that walked directory;
    callers write relative to it and close it. None when there is no
    verified main checkout or a component is missing or a link.
    """
    main = main_checkout(path, repo=repo)
    prefix = checkout_prefix(path)
    if main is None or prefix is None:
        return None
    try:
        root_fd = dirfd.open_directory_chain(main, writable=False)
    except OSError:
        return None
    try:
        mapped_fd = _walk(root_fd, prefix)
    except OSError:
        return None
    finally:
        os.close(root_fd)
    if mapped_fd is None:
        return None
    return main.joinpath(*prefix), mapped_fd


def main_checkout_for(path: Path, *, repo: Path | None = None) -> Path | None:
    """Path form of `open_main_checkout_for`, for display and the Stop hook."""
    held = open_main_checkout_for(path, repo=repo)
    if held is None:
        return None
    os.close(held[1])
    return held[0]


def _bounded_git_output(
    cwd: Path,
    args: list[str],
    max_bytes: int,
    timeout: float = _GIT_TIMEOUT_SECONDS,
    pass_fds: tuple[int, ...] = (),
) -> bytes | None:
    """Stdout of a git command, or None on failure, timeout or overflow."""
    try:
        proc = subprocess.Popen(
            ["git", "-C", str(cwd), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_git_env(),
            pass_fds=pass_fds,
        )
    except OSError:
        return None
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    try:
        assert proc.stdout is not None
        data = proc.stdout.read(max_bytes + 1)
        overflow = len(data) > max_bytes
        if overflow:
            proc.kill()
        proc.stdout.close()
        returncode = proc.wait()
    finally:
        timer.cancel()
    if overflow or returncode != 0:
        return None
    return data


def _worktree_records(raw: bytes, limit: int) -> list[bytes]:
    """Worktree paths from `--porcelain -z` output, at most `limit` records."""
    paths: list[bytes] = []
    for field in raw.split(b"\0"):
        if field.startswith(b"worktree "):
            paths.append(field[len(b"worktree ") :])
            if len(paths) >= limit:
                break
    return paths


def linked_worktrees(root: Path, *, limit: int = MAX_WORKTREES, max_bytes: int = MAX_WORKTREE_LIST_BYTES) -> list[Path]:
    """Linked working trees of `root`'s repository other than `root` itself.

    Output over `max_bytes` is refused outright. Only the first `limit`
    records are considered, and each must resolve to a directory whose own
    common git dir is `root`'s.
    """
    common = git_common_dir(root)
    if common is None:
        return []
    raw = _bounded_git_output(root, ["worktree", "list", "--porcelain", "-z"], max_bytes)
    if raw is None:
        print(f"brigade: git worktree list failed or exceeded {max_bytes} bytes for {root}", file=sys.stderr)
        return []
    # Compare against the checkout's top level, so a subdirectory root does
    # not count its own checkout as a sibling.
    own = _git_path(root, "--show-toplevel")
    if own is None:
        return []
    found: list[Path] = []
    for value in _worktree_records(raw, limit):
        candidate = Path(os.fsdecode(value))
        if not candidate.is_absolute():
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved == own or not resolved.is_dir():
            continue
        # Worktree metadata can name any directory. Keep only checkouts that
        # really belong to this repository.
        if git_common_dir(resolved) != common:
            continue
        found.append(resolved)
    return found


def inbox_components(rel: str) -> tuple[str, ...]:
    """Validate a repo-relative inbox path and split it into components.

    Rejects absolute, drive-qualified and UNC paths, `.`/`..`, separators
    other than `/`, control characters and components that change under
    Unicode normalization.
    """
    if not isinstance(rel, str) or not rel or "\\" in rel or "\x00" in rel:
        raise ValueError("inbox path is not a plain relative path")
    windows = PureWindowsPath(rel)
    if rel.startswith("/") or windows.drive or windows.root:
        raise ValueError("inbox path is absolute")
    parts = tuple(rel.split("/"))
    for part in parts:
        if part in ("", ".", "..") or ":" in part:
            raise ValueError("inbox path component is not contained")
        if unicodedata.normalize("NFC", part) != part or unicodedata.normalize("NFKC", part) != part:
            raise ValueError("inbox path component is ambiguous under Unicode normalization")
        if any(unicodedata.category(char).startswith("C") for char in part):
            raise ValueError("inbox path component has control characters")
    return parts


def _walk(root_fd: int, parts: tuple[str, ...], *, create: bool = False) -> int | None:
    """Open `parts` under a held root without following any link.

    Returns None when a component is missing and `create` is False. A link,
    junction, reparse point or non-directory component raises OSError.
    """
    current = os.dup(root_fd)
    try:
        for part in parts:
            try:
                child = dirfd.open_child_directory(current, part)
            except FileNotFoundError:
                if not create:
                    return None
                try:
                    dirfd.mkdir_child(current, part, 0o755)
                except FileExistsError:
                    pass
                child = dirfd.open_child_directory(current, part)
            os.close(current)
            current = child
        result, current = current, -1
        return result
    finally:
        if current >= 0:
            os.close(current)


def contained_inbox(worktree: Path, rel: str) -> Path | None:
    """`worktree/rel` when every component is a real directory, else None."""
    try:
        parts = inbox_components(rel)
        root_fd = dirfd.open_directory_chain(worktree.resolve(strict=True), writable=False)
    except (OSError, ValueError):
        return None
    try:
        inbox_fd = _walk(root_fd, parts)
    except OSError:
        print(f"brigade: refusing linked inbox {worktree / rel}: a component is not a plain directory", file=sys.stderr)
        return None
    finally:
        os.close(root_fd)
    if inbox_fd is None:
        return None
    os.close(inbox_fd)
    return worktree.joinpath(*parts)


def handoff_inbox(target: Path) -> Path:
    """Inbox a session at `target` should write to.

    A linked worktree points at the main checkout's inbox, which survives the
    worktree's deletion and is the inbox an ingester already watches.
    """
    main = main_checkout_for(target)
    return (main if main is not None else target) / CLAUDE_INBOX


def handoff_inboxes(target: Path) -> tuple[Path, ...]:
    """Every inbox a handoff for `target` may land in (own first)."""
    own = target / CLAUDE_INBOX
    preferred = handoff_inbox(target)
    return (own,) if preferred == own else (own, preferred)


def is_handoff_path(target: Path, path: Path) -> bool:
    """True when resolved `path` sits in an inbox a session at `target` may use."""
    own = (target / CLAUDE_INBOX).resolve()
    if path.is_relative_to(own):
        return True
    preferred = handoff_inbox(target).resolve()
    return preferred != own and path.is_relative_to(preferred)


def stop_hint(target: Path) -> str:
    """Where the Stop hook tells a session to write its handoff."""
    preferred = handoff_inbox(target)
    if preferred == target / CLAUDE_INBOX:
        if main_checkout(target) is not None:
            return (
                f"`{CLAUDE_INBOX}/` (the matching main checkout directory could not be verified, so this "
                "worktree's inbox is used and must be delivered before the worktree is removed)"
            )
        return f"`{CLAUDE_INBOX}/`"
    return f"`{preferred}/` (the main checkout, so it outlives this worktree)"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_text(value: object, limit: int = 160) -> str:
    """Printable ASCII rendering of an untrusted name for logs and receipts."""
    text = str(value).encode("unicode_escape", "backslashreplace").decode("ascii")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def safe_path(value: object, limit: int = 160) -> str:
    """Escaped path for warnings that never loses its final name.

    Only the directory part is shortened, from the middle, so an operator
    still sees which file to act on.
    """
    text = str(value).encode("unicode_escape", "backslashreplace").decode("ascii")
    head, sep, name = text.rpartition("/")
    if len(text) <= limit or not sep:
        return text
    room = max(limit - len(name) - 4, 16)
    return head[: room // 2] + "..." + head[-(room - room // 2) :] + "/" + name


@dataclass(frozen=True)
class Rescue:
    source: str
    state: str  # delivered | duplicate | failed
    dest: str | None = None
    reason: str = ""  # stable reason code


@dataclass(frozen=True)
class RemovalHandle:
    """Held descriptors for removing the worktree a rescue verified.

    ``root_fd`` is the worktree, ``admin_fd`` its verified admin git dir, and
    ``worktrees_fd`` the held parent of ``admin_name``. The caller closes all
    three. Removal goes through these, never through a path.
    """

    path: Path
    root_fd: int
    admin_fd: int
    worktrees_fd: int
    admin_name: str


def _read_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
    )


def _write_flags() -> int:
    return (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
    )


def _read_fd(fd: int) -> bytes:
    """Read a held regular file from offset 0, bounded."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise OSError("not a regular file")
    if info.st_size > MAX_HANDOFF_BYTES:
        raise OSError("handoff exceeds size limit")
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = MAX_HANDOFF_BYTES + 1
    while remaining > 0:
        chunk = os.read(fd, min(remaining, 65536))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > MAX_HANDOFF_BYTES:
        raise OSError("handoff exceeds size limit")
    return data


def _read_child(parent: int, name: str) -> tuple[bytes, os.stat_result]:
    """Read a regular file through a no-follow handle; return bytes and fstat."""
    fd = dirfd.open_child_file(parent, name, _read_flags())
    try:
        return _read_fd(fd), os.fstat(fd)
    finally:
        os.close(fd)


def _child_exists(parent: int, name: str) -> bool:
    try:
        dirfd.stat_child(parent, name)
    except FileNotFoundError:
        return False
    return True


def _digest_index(parent: int, skip: tuple[set[str], set[tuple[int, int]]] | None = None) -> set[str]:
    """sha256 of the `*.md` files directly under a held directory (bounded).

    `skip` holds names and identities to leave out of the index.
    """
    skip_names = {n.casefold() for n in skip[0]} if skip else set()
    skip_ids = skip[1] if skip else set()
    known: set[str] = set()
    for name in dirfd.child_names(parent, MAX_INBOX_ENTRIES):
        if not is_handoff_filename(name) or name.casefold() in skip_names:
            continue
        try:
            data, info = _read_child(parent, name)
        except OSError:
            continue
        if (info.st_dev, info.st_ino) in skip_ids:
            continue
        known.add(digest(data))
    return known


def _known_digests(dest_fd: int) -> set[str]:
    """Content already archived in the destination's `processed/`.

    Only archived content counts as delivered. A pending destination file
    can still change before ingest reads it, so a matching pending copy is
    no proof of delivery. Rescue publishes its own copy, and ingest's in-run
    dedupe handles the duplicate.
    """
    known: set[str] = set()
    try:
        processed = dirfd.open_child_directory(dest_fd, "processed", writable=False)
    except FileNotFoundError:
        return known
    try:
        known |= _digest_index(processed)
    finally:
        os.close(processed)
    return known


def _tracked(inbox: Path) -> _TrackedIndex | None:
    """Names and identities git tracks in `inbox`; None when unknown.

    A confirmed non-repository yields empty sets. Broken git metadata or a
    failing git command yields None, never "nothing tracked".
    """
    from .ingest import _git_tracked_files

    return _git_tracked_files(inbox)


_INBOX_PARTS = tuple(dict.fromkeys(tuple(rel.split("/")) for rel in WRITER_INBOXES.values()))
# Untracked-file listing used to find inboxes in a worktree being removed.
MAX_UNTRACKED_LIST_BYTES = 8 << 20
UNTRACKED_LIST_SECONDS = 10.0


def _canonical_inbox(parts: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """(prefix, canonical writer inbox) when `parts` ends in one, ignoring case."""
    for inbox in _INBOX_PARTS:
        tail = parts[-len(inbox) :]
        if len(tail) == len(inbox) and tuple(p.casefold() for p in tail) == tuple(p.casefold() for p in inbox):
            return parts[: -len(inbox)], inbox
    return None


def _split_inbox(parts: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split `prefix + writer inbox` components into (prefix, canonical inbox)."""
    found = _canonical_inbox(parts)
    if found is None:
        raise OSError("not a writer inbox path")
    return found


def _verified_git_dir(worktree: Path, root_fd: int) -> tuple[Path, int, int | None] | None:
    """The admin git dir of `worktree`, held open, only if its `.git` is genuine.

    `.git` is read without following links from the held root. In a main
    checkout it must be the common git dir itself. In a linked worktree it
    must be a regular file whose `gitdir:` names the admin dir, and the admin
    dir's `gitdir` entry, read through the opened admin dir, must name this
    very `.git` file by identity. Anything else (a symlinked marker pointing
    at another worktree, for example) is refused.

    The admin dir is opened first and its identity comes from that descriptor
    rather than from a path stat, so a rename that swaps a replacement into
    the path cannot make the check compare the replacement with itself. A
    relative `gitdir:` is walked from the held worktree root, not joined to a
    path. A linked admin dir is returned with its `worktrees` parent held and
    its entry identity checked, so the registration can be removed through
    descriptors. The caller closes both descriptors.
    """
    git_dir = _git_path(worktree, "--git-dir")
    common = git_common_dir(worktree)
    if git_dir is None or common is None:
        return None
    try:
        marker = dirfd.stat_child(root_fd, ".git")
    except OSError:
        return None
    if git_dir == common:
        if not stat.S_ISDIR(marker.st_mode):
            return None
        # In a main checkout the marker is the admin dir itself.
        admin_fd = _open_admin_dir(root_fd, git_dir, worktree)
        if admin_fd is None:
            return None
        try:
            if _identity(os.fstat(admin_fd)) != _identity(marker):
                raise OSError("marker changed during verification")
        except OSError:
            os.close(admin_fd)
            return None
        return git_dir, admin_fd, None
    if git_dir.parent != common / "worktrees" or not stat.S_ISREG(marker.st_mode):
        return None
    try:
        marker_fd = dirfd.open_child_file(root_fd, ".git", _read_flags())
        try:
            opened = os.fstat(marker_fd)
            if _identity(opened) != _identity(marker) or opened.st_size > _GIT_MARKER_BYTES:
                return None
            data = os.read(marker_fd, _GIT_MARKER_BYTES + 1)
        finally:
            os.close(marker_fd)
        text = data.decode("utf-8").strip()
        if not text.startswith("gitdir:"):
            return None
        named = Path(text[len("gitdir:") :].strip())
        admin_fd = _open_admin_dir(root_fd, named, worktree)
        if admin_fd is None:
            return None
        try:
            backlink = admin_backlink(admin_fd)
            if backlink is None:
                raise OSError("admin backlink unreadable")
            linked_fd = _open_backlink(admin_fd, backlink)
            try:
                linked = os.fstat(linked_fd)
            finally:
                os.close(linked_fd)
            if not stat.S_ISREG(linked.st_mode) or _identity(linked) != _identity(opened):
                raise OSError("admin backlink mismatch")
            worktrees_fd = _open_worktrees_dir(git_dir, admin_fd)
            if worktrees_fd is None:
                raise OSError("admin entry changed")
        except (OSError, ValueError):
            os.close(admin_fd)
            return None
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return git_dir, admin_fd, worktrees_fd


def _open_worktrees_dir(git_dir: Path, admin_fd: int) -> int | None:
    """The held `worktrees` parent whose entry is the verified admin dir."""
    try:
        fd = dirfd.open_directory_chain(git_dir.parent, writable=False)
    except OSError:
        return None
    try:
        entry = dirfd.stat_child(fd, git_dir.name)
        if _identity(entry) != _identity(os.fstat(admin_fd)):
            raise OSError("admin entry changed")
    except OSError:
        os.close(fd)
        return None
    return fd


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _open_admin_dir(root_fd: int, named: Path, worktree: Path) -> int | None:
    """Open the admin dir a `.git` marker names, never following a final link.

    An absolute target is walked from the filesystem anchor. A relative one is
    walked from the held worktree root, so it cannot be redirected by a path
    that no longer names that root. Windows has no descriptor walk for that,
    so it keeps the pre-PR resolved path.
    """
    try:
        if named.is_absolute():
            return dirfd.open_directory_chain(named, writable=False)
        if os.name == "nt":
            return dirfd.open_directory_chain(Path(os.path.normpath(worktree / named)), writable=False)
        return _open_relative_directory(root_fd, named)
    except (OSError, ValueError):
        return None


def _open_relative_directory(parent_fd: int, rel: Path) -> int:
    """Walk `rel` from a held directory without following any component."""
    fd = os.dup(parent_fd)
    try:
        for part in rel.parts:
            if part in ("", "."):
                continue
            if part == "..":
                child = os.open("..", dirfd.directory_flags() | getattr(os, "O_PATH", 0), dir_fd=fd)
            else:
                child = dirfd.open_child_directory(fd, part, writable=False)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_backlink(admin_fd: int, backlink: Path) -> int:
    """Open the backlink target read from a held admin dir, never a link."""
    if backlink.is_absolute():
        return os.open(backlink, _read_flags())
    return os.open(backlink, _read_flags(), dir_fd=admin_fd)


def admin_backlink(admin_fd: int) -> Path | None:
    """The worktree `.git` path recorded in a held admin dir's `gitdir` file."""
    try:
        fd = dirfd.open_child_file(admin_fd, "gitdir", _read_flags())
        try:
            if os.fstat(fd).st_size > _GIT_MARKER_BYTES:
                return None
            data = os.read(fd, _GIT_MARKER_BYTES + 1)
        finally:
            os.close(fd)
        text = data.decode("utf-8").strip()
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not text or "\0" in text:
        return None
    return Path(text)


def _proc_fd_paths() -> bool:
    """Whether `/proc/self/fd/<n>` names an open directory (Linux)."""
    return dirfd.pin_available()


def _pinned_git(worktree: Path, git_dir: Path, args: list[str], *, root_fd: int, admin_fd: int) -> bytes:
    """Bounded git output with the git dir and work tree pinned.

    Explicit --git-dir and --work-tree (and core.worktree) override any
    core.worktree from repository, worktree or included config, and the
    environment is sanitized. On Linux both point at the held descriptors
    through `/proc/self/fd`, so renaming another tree into either path
    cannot redirect git. Elsewhere git gets the paths and both directories
    are compared with the held descriptors after git exits. Raises on
    failure, over a bound, or on an identity mismatch.
    """
    fds: tuple[int, ...]
    if _proc_fd_paths():
        work, admin = f"/proc/self/fd/{root_fd}", f"/proc/self/fd/{admin_fd}"
        cwd, fds = Path(work), (root_fd, admin_fd)
    else:
        work, admin = str(worktree), str(git_dir)
        cwd, fds = worktree, ()
    raw = _bounded_git_output(
        cwd,
        [
            f"--git-dir={admin}",
            f"--work-tree={work}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"core.worktree={work}",
            *args,
        ],
        MAX_UNTRACKED_LIST_BYTES,
        timeout=UNTRACKED_LIST_SECONDS,
        pass_fds=fds,
    )
    if raw is None:
        raise OSError("git listing failed or exceeded its budget")
    if not fds:
        _same_directory(worktree, root_fd)
        _same_directory(git_dir, admin_fd)
    return raw


def _same_directory(path: Path, fd: int) -> None:
    """Raise unless `path` still names the directory held open as `fd`."""
    now, held = os.stat(path), os.fstat(fd)
    if (now.st_dev, now.st_ino) != (held.st_dev, held.st_ino):
        raise OSError("directory changed during git listing")


def _worktree_listing(
    worktree: Path, root_fd: int, git_dir: Path, admin_fd: int
) -> tuple[list[tuple[str, ...]], list[str]]:
    """(inbox prefixes holding untracked files, embedded repositories).

    Git lists untracked files, ignored ones included, without following
    links. Pathspecs keep the output to writer inbox files, so a large
    ignored tree (node_modules, .venv) does not count against the budget.
    Git does not descend into an embedded repository, so those (untracked
    nested repos and initialized submodules) are listed separately and make
    the caller keep the worktree. Any git failure or bound raises.
    """
    return _list_with(worktree, git_dir, root_fd, admin_fd)


def _list_with(worktree: Path, git_dir: Path, root_fd: int, admin_fd: int) -> tuple[list[tuple[str, ...]], list[str]]:
    def git(args: list[str]) -> bytes:
        return _pinned_git(worktree, git_dir, args, root_fd=root_fd, admin_fd=admin_fd)

    # `**/` also matches at the top level, so `.claude/memory-handoffs/x.md`
    # in the checkout root is listed.
    # Matched without regard to case, so `.CLAUDE/memory-handoffs` is found
    # too. The directory names are kept as listed for the no-follow walk.
    pathspecs = [f":(glob,icase)**/{'/'.join(inbox)}/**" for inbox in _INBOX_PARTS]
    raw = git(["ls-files", "--others", "-z", "--", *pathspecs])
    prefixes: set[tuple[str, ...]] = set()
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        parent = tuple(os.fsdecode(entry).split("/"))[:-1]
        if _canonical_inbox(parent) is not None:
            prefixes.add(parent)
    # Directory entries in an untracked listing are repository boundaries.
    nested = git(["ls-files", "--others", "-z", "--", ":(glob)**/"])
    embedded = [os.fsdecode(e).rstrip("/") for e in nested.split(b"\0") if e.endswith(b"/")]
    stage = git(["ls-files", "-s", "-z"])
    for record in stage.split(b"\0"):
        meta, _, path = record.partition(b"\t")
        if meta.startswith(b"160000 ") and path:
            name = os.fsdecode(path)
            if _is_present_directory(root_fd, name):
                embedded.append(name)
    return sorted(prefixes), sorted(set(embedded))


def _is_present_directory(root_fd: int, rel: str) -> bool:
    """Whether a gitlink path is populated, checked without following links.

    A populated gitlink is a boundary git will not list inside, with or
    without a `.git` marker. A missing path is not. Anything unverifiable
    counts as present.
    """
    try:
        fd = _walk(root_fd, inbox_components(rel))
    except (OSError, ValueError):
        return True
    if fd is None:
        return False
    os.close(fd)
    return True


class _Dest:
    """Destination root held open for the whole rescue transaction."""

    def __init__(self, root: Path | None, root_fd: int | None = None) -> None:
        # `root` is only for tracking checks and messages. All writes go
        # through `root_fd`, opened once (or handed over) and never reopened.
        self.root = root
        self.root_fd = os.dup(root_fd) if root_fd is not None else None
        self.inboxes: dict[tuple[str, ...], tuple[int, set[str], frozenset[str]]] = {}

    def inbox(self, parts: tuple[str, ...]) -> tuple[int, set[str], frozenset[str]]:
        """Held inbox fd, digests ingest will see, and tracked names (casefolded).

        Ingest skips git-tracked names, so a rescue must never publish under
        one. Unknown tracking raises instead of guessing.
        """
        if parts not in self.inboxes:
            if self.root is None:
                raise OSError("no verified main checkout")
            if self.root_fd is None:
                self.root_fd = dirfd.open_directory_chain(self.root, writable=False)
            # The checkout subdirectory must already be a real directory in
            # the main checkout. Only the inbox under it may be created.
            prefix, inbox = _split_inbox(parts)
            base = _walk(self.root_fd, prefix)
            if base is None:
                raise OSError("destination subdirectory missing")
            try:
                fd = _walk(base, inbox, create=True)
            finally:
                os.close(base)
            assert fd is not None
            try:
                tracked = _tracked(self.root.joinpath(*parts))
                if tracked is None:
                    raise OSError("destination git tracking unknown")
                known = _known_digests(fd)
            except BaseException:
                os.close(fd)
                raise
            self.inboxes[parts] = (fd, known, frozenset(n.casefold() for n in tracked[0]))
        return self.inboxes[parts]

    def verify(self, parts: tuple[str, ...]) -> None:
        """The held inbox is still the directory its path names, reached without links.

        Run before a source is removed: if the destination path was swapped
        after it was opened, the copy is not where ingest will look.
        """
        if self.root is None or parts not in self.inboxes:
            raise OSError("destination not held")
        current = dirfd.open_directory_chain(self.root.joinpath(*parts), writable=False)
        try:
            held, now = os.fstat(self.inboxes[parts][0]), os.fstat(current)
        finally:
            os.close(current)
        if (held.st_dev, held.st_ino) != (now.st_dev, now.st_ino):
            raise OSError("destination moved")

    def close(self) -> None:
        for fd, _, _ in self.inboxes.values():
            os.close(fd)
        if self.root_fd is not None:
            os.close(self.root_fd)


def _rename_noreplace(parent: int, source: str, destination: str) -> None:
    """Atomically rename one child of a held directory without replacing."""
    dirfd.rename_child_noreplace(parent, source, parent, destination)


def _restore_claim(src_fd: int, claim: str, name: str) -> bool:
    """Put a claimed handoff back as a pending `.md` without replacing anything."""
    stem = name[: -len(".md")]
    for target in (name, f"{stem}-restored-{uuid.uuid4().hex[:8]}.md"):
        try:
            _rename_noreplace(src_fd, claim, target)
        except FileExistsError:
            continue
        except OSError:
            return False
        try:
            dirfd.fsync_directory(src_fd)
        except OSError:
            pass
        return True
    return False


def is_handoff_filename(name: str) -> bool:
    """The one handoff filename rule ingest and rescue share.

    Any `.md` file except TEMPLATE.md, dot-prefixed ones included. Rescue's
    private claim, temp and recovery names do not end in `.md`.
    """
    return Path(name).suffix == ".md" and name != "TEMPLATE.md"


_is_candidate = is_handoff_filename


def _quiet(info: os.stat_result) -> bool:
    return time.time() - info.st_mtime >= QUIESCENCE_SECONDS


def _publish_bytes(dest_fd: int, raw: bytes, names: tuple[str, ...], forbidden: frozenset[str]) -> str:
    """Publish `raw` under the first free name, never replacing anything.

    The bytes go to a temp name the ingesters ignore, are fsynced and read
    back, then linked or renamed into place without replacement.
    """
    sha = digest(raw)
    temp = f"{_TEMP_PREFIX}{uuid.uuid4().hex}{_TEMP_SUFFIX}"
    out = dirfd.open_child_file(dest_fd, temp, _write_flags(), 0o644)
    created = True
    try:
        try:
            view = memoryview(raw)
            while view:
                view = view[os.write(out, view) :]
            os.fsync(out)
        finally:
            os.close(out)
        copied, _ = _read_child(dest_fd, temp)
        if digest(copied) != sha:
            raise OSError("copy verification failed")
        for final in dict.fromkeys(names):
            # A tracked name is skipped by ingest even when its file is gone.
            if final.casefold() in forbidden or _child_exists(dest_fd, final):
                continue
            try:
                dirfd.replace_children(dest_fd, temp, final, replace=False)
            except FileExistsError:
                continue
            created = False
            dirfd.fsync_directory(dest_fd)
            return final
        raise FileExistsError(errno.EEXIST, "no free destination name", names[0])
    finally:
        if created:
            try:
                dirfd.unlink_child(dest_fd, temp)
            except OSError:
                pass


def _stable_since_delivery(held: int, delivered: os.stat_result, sha: str) -> bool:
    """Bounded final check on the still-named claim before it is unlinked.

    Takes one size snapshot and hashes exactly that many bytes, under the
    ingest size cap and FINAL_CHECK_SECONDS. True only when size, mtime and
    hash all match the delivered copy and nothing changed during the read.
    A growing or oversized inode, or a missed deadline, is not stable.
    """
    started = time.monotonic()
    try:
        snapshot = os.fstat(held)
        if (snapshot.st_size, snapshot.st_mtime_ns) != (delivered.st_size, delivered.st_mtime_ns):
            return False
        if snapshot.st_size > MAX_HANDOFF_BYTES:
            return False
        os.lseek(held, 0, os.SEEK_SET)
        hasher = hashlib.sha256()
        remaining = snapshot.st_size
        while remaining > 0:
            if time.monotonic() - started > FINAL_CHECK_SECONDS:
                return False
            chunk = os.read(held, min(remaining, 1 << 16))
            if not chunk:
                return False
            hasher.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(held)
    except OSError:
        return False
    if time.monotonic() - started > FINAL_CHECK_SECONDS:
        return False
    return hasher.hexdigest() == sha and (after.st_size, after.st_mtime_ns) == (snapshot.st_size, snapshot.st_mtime_ns)


def _keep_claim(src_fd: int, held: int, claim: str, name: str, forbidden: frozenset[str]) -> str | None:
    """Rename a claim that changed after delivery back to a recovery name.

    Never replaces anything. Within the ingest cap it becomes a pending `.md`
    (the original name if free, else `<stem>-late-<id>.md`). Larger content
    gets a name ingest skips. Returns the name, or None if every rename failed
    (the claim then stays and keeps the worktree as a stale claim).
    """
    stem = name[: -len(".md")]
    try:
        oversize = os.fstat(held).st_size > MAX_HANDOFF_BYTES
    except OSError:
        oversize = True
    if oversize:
        names: tuple[str, ...] = (f"{_OVERSIZE_PREFIX}{uuid.uuid4().hex}.md.recovered",)
    else:
        names = (name, f"{stem}-late-{uuid.uuid4().hex[:8]}.md")
    for target in names:
        if target.casefold() in forbidden:
            continue
        try:
            _rename_noreplace(src_fd, claim, target)
        except FileExistsError:
            continue
        except OSError:
            return None
        try:
            dirfd.fsync_directory(src_fd)
        except OSError:
            pass
        return target
    return None


def _rescue_one(
    src_fd: int,
    name: str,
    label: str,
    dest: _Dest,
    parts: tuple[str, ...],
    tracked: _TrackedIndex,
) -> Rescue | None:
    source = f"{label}/{name}"
    tracked_names, tracked_ids = tracked.names, tracked.identities
    if name.casefold() in {n.casefold() for n in tracked_names}:
        return None
    try:
        before = dirfd.stat_child(src_fd, name)
    except OSError:
        return Rescue(source, "failed", reason="rescue-io-error")
    if not stat.S_ISREG(before.st_mode):
        return Rescue(source, "failed", reason="not-regular-file")
    if (before.st_dev, before.st_ino) in tracked_ids:
        return None
    if not _quiet(before):
        return Rescue(source, "failed", reason="source-too-recent")
    stem = name[: -len(".md")]
    # Claim the source atomically. From here on only the claimed object is
    # read, compared and removed, whatever happens at the original path.
    claim = f"{_CLAIM_PREFIX}{uuid.uuid4().hex}{_CLAIM_SUFFIX}"
    try:
        _rename_noreplace(src_fd, name, claim)
    except OSError:
        return Rescue(source, "failed", reason="claim-failed")
    claimed = True
    held: int | None = None
    try:
        held = dirfd.open_child_file(src_fd, claim, _read_flags())
        opened = os.fstat(held)
        named = dirfd.stat_child(src_fd, claim)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or (opened.st_dev, opened.st_ino) in tracked_ids
        ):
            return Rescue(source, "failed", reason="source-changed")
        if not _quiet(opened):
            return Rescue(source, "failed", reason="source-too-recent")
        raw = _read_fd(held)
        delivered = os.fstat(held)
        sha = digest(raw)
        if delivered.st_size != len(raw):
            return Rescue(source, "failed", reason="source-changed")
        dest_fd, known, dest_tracked = dest.inbox(parts)
        published: str | None = None
        if sha in known:
            state = "duplicate"
        else:
            published = _publish_bytes(
                dest_fd,
                raw,
                (name, f"{stem}-{sha[:12]}.md", f"{stem}-{uuid.uuid4().hex[:8]}.md"),
                dest_tracked,
            )
            known.add(sha)
            state = "delivered"
        # Final check while the claim is still named. Unlink only if stable.
        if not _stable_since_delivery(held, delivered, sha):
            kept = _keep_claim(src_fd, held, claim, name, frozenset(n.casefold() for n in tracked_names))
            if kept is None:
                return Rescue(source, "failed", reason="late-write-claim-kept")
            claimed = False
            return Rescue(source, "failed", dest=str(Path(label) / kept), reason="late-write-kept")
        # The delivered copy must still sit where ingest will look for it,
        # checked last, right before the source is removed.
        try:
            dest.verify(parts)
        except OSError:
            return Rescue(source, "failed", reason="destination-moved")
        dirfd.unlink_child(src_fd, claim)
        claimed = False
        try:
            dirfd.fsync_directory(src_fd)
        except OSError:
            pass  # the unlink not reaching disk only re-delivers a duplicate
        dest_path = str(dest.root.joinpath(*parts, published)) if dest.root and published else None
        return Rescue(source, state, dest=dest_path)
    except OSError:
        return Rescue(source, "failed", reason="rescue-io-error")
    finally:
        if held is not None:
            os.close(held)
        if claimed and not _restore_claim(src_fd, claim, name):
            print(
                f"brigade: claimed handoff left as {safe_text(claim)} in {safe_text(label)}",
                file=sys.stderr,
            )


def _tracked_pinned(
    worktree: Path,
    git_dir: Path,
    root_fd: int,
    admin_fd: int,
    parts: tuple[str, ...],
    src_fd: int,
) -> _TrackedIndex | None:
    """Tracked names for one inbox, from Git pinned to the held directories.

    The query follows `/proc/self/fd` for both the work tree and the admin
    dir, so a swap of the inbox path while it runs cannot make it answer for
    another tree. None when the query is unavailable or fails.
    """
    from .ingest import _TrackedIndex

    if not _proc_fd_paths():
        return None
    parent = "/".join(parts)
    try:
        raw = _pinned_git(
            worktree,
            git_dir,
            ["ls-files", "-s", "-z", "--", f":(literal){parent}/"],
            root_fd=root_fd,
            admin_fd=admin_fd,
        )
    except OSError:
        return None
    names: set[str] = set()
    identities: set[tuple[int, int]] = set()
    unread: set[str] = set()
    for record in raw.split(b"\0"):
        _meta, _, path = record.partition(b"\t")
        if not path:
            continue
        directory, _, name = os.fsdecode(path).rpartition("/")
        if directory != parent:
            continue
        names.add(name)
        try:
            identities.add(_identity(dirfd.stat_child(src_fd, name)))
        except FileNotFoundError:
            # Its own spelling still protects it.
            unread.add(name)
        except OSError:
            return None
    return _TrackedIndex(names, identities, unread, False)


def _tracked_for_inbox(
    worktree: Path,
    git_dir: Path,
    root_fd: int,
    admin_fd: int,
    parts: tuple[str, ...],
    src_fd: int,
) -> _TrackedIndex | None:
    """Tracked names for an inbox, pinned where the platform allows it."""
    if not _proc_fd_paths():
        # Windows keeps the pre-PR path query. Other platforms are not
        # followed by a rescue-backed removal, which refuses instead.
        return _tracked(worktree.joinpath(*parts))
    return _tracked_pinned(worktree, git_dir, root_fd, admin_fd, parts, src_fd)


def _rescue_inbox(worktree: Path, root_fd: int, admin_fd: int, git_dir: Path, rel: str, dest: _Dest) -> list[Rescue]:
    label = str(worktree / rel)
    try:
        parts = inbox_components(rel)
        prefix, inbox = _split_inbox(parts)
        src_fd = _walk(root_fd, parts)
    except (OSError, ValueError):
        return [Rescue(label, "failed", reason="inbox-refused")]
    # Deliver into the canonical inbox name ingest watches.
    dest_parts = (*prefix, *inbox)
    if src_fd is None:
        return []
    try:
        names = dirfd.child_names(src_fd, MAX_INBOX_ENTRIES + 1)
        if len(names) > MAX_INBOX_ENTRIES:
            return [Rescue(label, "failed", reason="inbox-too-large")]
        # A claim left by an interrupted rescue, or an oversize recovery file,
        # still holds handoff bytes. Name each so an operator can resolve it.
        held = [
            Rescue(
                f"{label}/{n}",
                "failed",
                reason="stale-claim" if n.startswith(_CLAIM_PREFIX) else "recovery-file-pending",
            )
            for n in sorted(names)
            if n.startswith((_CLAIM_PREFIX, _OVERSIZE_PREFIX))
        ]
        if held:
            return held
        candidates = sorted(n for n in names if _is_candidate(n))
        if not candidates:
            return []
        tracked = _tracked_for_inbox(worktree, git_dir, root_fd, admin_fd, parts, src_fd)
        if tracked is None:
            return [Rescue(label, "failed", reason="tracking-unverifiable")]
        try:
            dest.inbox(dest_parts)
        except OSError:
            return [Rescue(label, "failed", reason="destination-unavailable")]
        results = [r for r in (_rescue_one(src_fd, n, label, dest, dest_parts, tracked) for n in candidates) if r]
        # A writer may have created a handoff (or returned to a claimed path)
        # during the pass. Anything still pending keeps the worktree.
        tracked_names = {n.casefold() for n in tracked[0]}
        left = [
            n
            for n in dirfd.child_names(src_fd, MAX_INBOX_ENTRIES + 1)
            if n.startswith((_CLAIM_PREFIX, _OVERSIZE_PREFIX))
            or (_is_candidate(n) and n.casefold() not in tracked_names)
        ]
        if left and not any(r.state == "failed" for r in results):
            results.append(Rescue(label, "failed", reason="pending-after-rescue"))
        return results
    except OSError:
        return [Rescue(label, "failed", reason="inbox-unreadable")]
    finally:
        os.close(src_fd)


def rescue_pending_handoffs(
    worktree: Path,
    dest_root: Path | None,
    *,
    dest_fd: int | None = None,
    hold_out: list[RemovalHandle] | None = None,
) -> list[Rescue]:
    """Move pending handoffs from `worktree`'s inboxes into `dest_root`'s.

    Source and destination directories are held open for the whole
    transaction. Each copy is written to a temp name the ingesters ignore,
    fsynced, verified and published without replacing anything. The source
    is unlinked only if it is still the file that was copied. Content already
    in the destination inbox or its `processed/` archive is not copied twice.
    Every problem is a `failed` result, never an exception, so the caller can
    keep the worktree. With no verified `dest_root`, pending handoffs fail.

    When `hold_out` is given and the path still names the rescued tree at the
    end, a :class:`RemovalHandle` holding the worktree, its admin dir and the
    admin entry's parent is appended to it. The caller owns and closes those
    descriptors, and removing through them is the only way to bind removal to
    the identity rescue verified.
    """
    try:
        resolved = worktree.resolve(strict=True)
    except FileNotFoundError:
        return []
    except OSError:
        return [Rescue(str(worktree), "failed", reason="worktree-unresolvable")]
    if dest_root is not None and dest_root.resolve() == resolved:
        return []
    try:
        root_fd = dirfd.open_directory_chain(resolved, writable=False)
    except OSError:
        return [Rescue(str(worktree), "failed", reason="worktree-unopenable")]
    dest = _Dest(dest_root, dest_fd)
    results: list[Rescue] = []
    admin_fd = worktrees_fd = None
    try:
        verified = _verified_git_dir(resolved, root_fd)
        if verified is None:
            return [Rescue(str(worktree), "failed", reason="inbox-scan-failed")]
        git_dir, admin_fd, worktrees_fd = verified
        try:
            prefixes, embedded = _worktree_listing(resolved, root_fd, git_dir, admin_fd)
        except OSError:
            return [Rescue(str(worktree), "failed", reason="inbox-scan-failed")]
        if embedded:
            # Git does not look inside these, so pending handoffs there are
            # invisible. Keep the worktree rather than recurse.
            return [Rescue(str(resolved / path), "failed", reason="embedded-repository") for path in embedded]
        # Each listed inbox directory, under the name git reported for it.
        for inbox_dir in prefixes:
            results.extend(_rescue_inbox(resolved, root_fd, admin_fd, git_dir, "/".join(inbox_dir), dest))
        # The caller removes the worktree, so the path must still name the
        # tree that was rescued. Removal re-checks the held identity again
        # right before it deletes anything.
        try:
            _same_directory(resolved, root_fd)
        except OSError:
            results.append(Rescue(str(worktree), "failed", reason="worktree-moved"))
        else:
            if hold_out is not None and worktrees_fd is not None:
                hold_out.append(
                    RemovalHandle(
                        path=resolved,
                        root_fd=os.dup(root_fd),
                        admin_fd=os.dup(admin_fd),
                        worktrees_fd=os.dup(worktrees_fd),
                        admin_name=git_dir.name,
                    )
                )
    finally:
        if admin_fd is not None:
            os.close(admin_fd)
        if worktrees_fd is not None:
            os.close(worktrees_fd)
        dest.close()
        os.close(root_fd)
    return results
