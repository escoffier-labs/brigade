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
"""

from __future__ import annotations

import ctypes
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

from . import dirfd
from .selection import WRITER_INBOXES

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
_RENAME_NOREPLACE = 1
# A handoff modified more recently than this is still being written. It is
# left in place and its worktree is kept.
QUIESCENCE_SECONDS = 5.0
# Ceiling for the final pre-unlink check. Bytes are bounded by the ingest cap.
FINAL_CHECK_SECONDS = 10.0


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in GIT_LOCATION_ENV}
    env["LC_ALL"] = "C"
    return env


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


def _bounded_git_output(cwd: Path, args: list[str], max_bytes: int) -> bytes | None:
    """Stdout of a git command, or None on failure, timeout or overflow."""
    try:
        proc = subprocess.Popen(
            ["git", "-C", str(cwd), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_git_env(),
        )
    except OSError:
        return None
    timer = threading.Timer(_GIT_TIMEOUT_SECONDS, proc.kill)
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
    try:
        own = root.resolve(strict=True)
    except OSError:
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
    main = main_checkout(target)
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
        return f"`{CLAUDE_INBOX}/`"
    return f"`{preferred}/` (the main checkout, so it outlives this worktree)"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_text(value: object, limit: int = 160) -> str:
    """Printable ASCII rendering of an untrusted name for logs and receipts."""
    text = str(value).encode("unicode_escape", "backslashreplace").decode("ascii")
    return text if len(text) <= limit else text[: limit - 3] + "..."


@dataclass(frozen=True)
class Rescue:
    source: str
    state: str  # delivered | duplicate | failed
    dest: str | None = None
    reason: str = ""  # stable reason code


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
        if not name.endswith(".md") or name.startswith(".") or name.casefold() in skip_names:
            continue
        try:
            data, info = _read_child(parent, name)
        except OSError:
            continue
        if (info.st_dev, info.st_ino) in skip_ids:
            continue
        known.add(digest(data))
    return known


def _known_digests(dest_fd: int, tracked: tuple[set[str], set[tuple[int, int]]]) -> set[str]:
    """Content ingest will see or has already archived at the destination.

    A git-tracked file in the inbox is never ingested, so its bytes do not
    count as delivered.
    """
    known = _digest_index(dest_fd, tracked)
    try:
        processed = dirfd.open_child_directory(dest_fd, "processed", writable=False)
    except FileNotFoundError:
        return known
    try:
        known |= _digest_index(processed)
    finally:
        os.close(processed)
    return known


def _tracked(inbox: Path) -> tuple[set[str], set[tuple[int, int]]] | None:
    """Names and identities git tracks in `inbox`; None when unknown.

    A confirmed non-repository yields empty sets. Broken git metadata or a
    failing git command yields None, never "nothing tracked".
    """
    from .ingest import _git_tracked_files

    return _git_tracked_files(inbox)


class _Dest:
    """Destination root held open for the whole rescue transaction."""

    def __init__(self, root: Path | None) -> None:
        self.root = root
        self.root_fd: int | None = None
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
                self.root_fd = dirfd.open_directory_chain(self.root.resolve(strict=True), writable=False)
            fd = _walk(self.root_fd, parts, create=True)
            assert fd is not None
            try:
                tracked = _tracked(self.root.resolve(strict=True).joinpath(*parts))
                if tracked is None:
                    raise OSError("destination git tracking unknown")
                known = _known_digests(fd, tracked)
            except BaseException:
                os.close(fd)
                raise
            self.inboxes[parts] = (fd, known, frozenset(n.casefold() for n in tracked[0]))
        return self.inboxes[parts]

    def close(self) -> None:
        for fd, _, _ in self.inboxes.values():
            os.close(fd)
        if self.root_fd is not None:
            os.close(self.root_fd)


def _renameat2_noreplace(parent: int, source: str, destination: str) -> bool:
    """Linux renameat2(RENAME_NOREPLACE). False when the call is unavailable."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        function = ctypes.CDLL(None, use_errno=True).renameat2
    except (OSError, AttributeError):
        return False
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(parent, os.fsencode(source), parent, os.fsencode(destination), _RENAME_NOREPLACE) == 0:
        return True
    code = ctypes.get_errno()
    if code in (errno.ENOSYS, errno.EINVAL):
        return False
    raise OSError(code, os.strerror(code), destination)


def _rename_noreplace(parent: int, source: str, destination: str) -> None:
    """Atomically rename one child of a held directory without replacing.

    Linux uses renameat2(RENAME_NOREPLACE) and Windows a no-replace handle
    rename. Other POSIX systems link the source to `destination`, which fails
    if anything exists there, then unlink the source name only while it is
    still the linked object, so a file a writer put back is left pending.
    """
    if os.name == "posix":
        if _renameat2_noreplace(parent, source, destination):
            return
        os.link(source, destination, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        linked = dirfd.stat_child(parent, destination)
        try:
            current = dirfd.stat_child(parent, source)
        except FileNotFoundError:
            return
        if (current.st_dev, current.st_ino) == (linked.st_dev, linked.st_ino):
            os.unlink(source, dir_fd=parent)
        return
    dirfd.replace_children(parent, source, destination, replace=False)


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


def _is_candidate(name: str) -> bool:
    return name.endswith(".md") and not name.startswith(".") and name != "TEMPLATE.md"


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
    tracked: tuple[set[str], set[tuple[int, int]]],
) -> Rescue | None:
    source = f"{label}/{name}"
    tracked_names, tracked_ids = tracked
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


def _rescue_inbox(worktree: Path, root_fd: int, rel: str, dest: _Dest) -> list[Rescue]:
    label = str(worktree / rel)
    try:
        parts = inbox_components(rel)
        src_fd = _walk(root_fd, parts)
    except (OSError, ValueError):
        return [Rescue(label, "failed", reason="inbox-refused")]
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
        tracked = _tracked(worktree.joinpath(*parts))
        if tracked is None:
            return [Rescue(label, "failed", reason="tracking-unverifiable")]
        try:
            dest.inbox(parts)
        except OSError:
            return [Rescue(label, "failed", reason="destination-unavailable")]
        results = [r for r in (_rescue_one(src_fd, n, label, dest, parts, tracked) for n in candidates) if r]
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


def rescue_pending_handoffs(worktree: Path, dest_root: Path | None) -> list[Rescue]:
    """Move pending handoffs from `worktree`'s inboxes into `dest_root`'s.

    Source and destination directories are held open for the whole
    transaction. Each copy is written to a temp name the ingesters ignore,
    fsynced, verified and published without replacing anything. The source
    is unlinked only if it is still the file that was copied. Content already
    in the destination inbox or its `processed/` archive is not copied twice.
    Every problem is a `failed` result, never an exception, so the caller can
    keep the worktree. With no verified `dest_root`, pending handoffs fail.
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
    dest = _Dest(dest_root)
    results: list[Rescue] = []
    try:
        for rel in dict.fromkeys(WRITER_INBOXES.values()):
            results.extend(_rescue_inbox(resolved, root_fd, rel, dest))
    finally:
        dest.close()
        os.close(root_fd)
    return results
