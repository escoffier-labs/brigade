"""Writer exclusion for the canonical import inbox: two locks, one order.

Canonical read-modify-write windows on ``.brigade/work/imports/inbox.jsonl``
are serialized by two adjacent lock files with distinct lifetimes:

``inbox.jsonl.lock`` (the *run lock*) is held by a scanner launcher only,
across its whole run window (pre-run snapshot through stamping or rollback),
so outside writers cannot interleave with the run.

``inbox.jsonl.writer.lock`` (the *writer lock*) serializes each individual
canonical write: imports, backfill, promote, dismiss, rollback, and scanner
stamping each hold it for their critical section.

Who takes what is selected by an environment marker that carries no
capability: the launcher exports ``BRIGADE_SCANNER_RUN_ID`` to its children.
A process that sees the marker is inside-a-run and takes ONLY the writer
lock, which it opens itself; siblings therefore exclude each other on their
own open file descriptions, and no child can release the launcher's run lock.
A process without the marker acquires the run lock first (blocking flock /
retrying msvcrt lock, so it waits while a run window is open) and then the
writer lock. The marker is deliberately not a capability: a child that forges
it takes only the writer lock like any other child, one that strips it waits
on the run lock like any outsider, and neither breaks exclusion because the
launcher's run-lock descriptor is never shared with anyone. The launcher
itself holds the run lock and takes the writer lock around each of its own
stamping/rollback sections.

Global lock order everywhere: task-ledger lock, then run lock, then writer
lock. Every canonical writer follows it, so no holder of an inbox lock ever
waits on the task ledger.

Both lock files are defended against same-UID tampering in exactly the same
hardened way: on POSIX they are opened through a held no-follow directory
descriptor with ``O_NOFOLLOW`` (symlinks refused) and ``O_NONBLOCK``, then
verified by ``fstat`` to be a regular file with exactly one link (FIFOs and
hard links refused). The held device/inode identity is re-checked against the
path before every protected write, so replacing either locked inode mid-run
fails the holder loudly instead of letting later writers flock a different
object. On Windows both locks use the same validated regular-file/single-link
open plus an ``msvcrt`` byte-range lock acquired on that handle; Windows
cannot request no-follow at open time, so a symlink planted in the
open-to-validation window remains a documented residual there. A same-UID
attacker can additionally ignore the advisory locks entirely; launch-time
inbox revalidation detects that case.

Each lock is reentrant per process so nested writer paths inside one
acquisition cannot self-deadlock; nested acquisitions share the outer
cross-process acquisition.
"""

from __future__ import annotations

import contextlib
import os
import stat
import threading
from collections.abc import Iterator
from pathlib import Path

from . import helpers

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows does not provide flock.
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX does not provide msvcrt.
    msvcrt = None  # type: ignore[assignment]


_PROCESS_LOCK = threading.Lock()
_LOCK_DEPTH: dict[str, int] = {}
_ACTIVE_LOCKS: dict[str, "_HeldInboxLock"] = {}

#: Marker (not a capability) set by the scanner launcher for its children:
#: a process that sees it is inside a scanner run and skips the run lock,
#: taking only the writer lock for canonical writes. Forging or stripping it
#: cannot break exclusion; see the module docstring.
SCANNER_RUN_ENV = "BRIGADE_SCANNER_RUN_ID"


def inside_scanner_run() -> bool:
    """Whether this process carries the launcher's inside-a-run marker."""
    return bool(os.environ.get(SCANNER_RUN_ENV))


def inbox_lock_path(target: Path) -> Path:
    """Return the run-window lock file beside the canonical inbox."""
    return _imports_sibling(target, "inbox.jsonl.lock")


def inbox_writer_lock_path(target: Path) -> Path:
    """Return the per-write serialization lock file beside the canonical inbox."""
    return _imports_sibling(target, "inbox.jsonl.writer.lock")


def _imports_sibling(target: Path, name: str) -> Path:
    target_root = target.expanduser().resolve()
    inbox = helpers._imports_path(target_root)
    return inbox.with_name(name)


class _HeldInboxLock:
    """One cross-process acquisition of an inbox lock file."""

    def __init__(self, key: str, parent: int | None, name: str, fd: int) -> None:
        self.key = key
        self.parent = parent
        self.name = name
        self.fd = fd
        self.identity = self._identity(os.fstat(fd))

    @staticmethod
    def _identity(metadata: os.stat_result) -> tuple[int, int] | None:
        try:
            device, inode = metadata.st_dev, metadata.st_ino
        except AttributeError:  # pragma: no cover - defensive.
            return None
        if not isinstance(device, int) or not isinstance(inode, int) or device <= 0 or inode <= 0:
            return None
        return device, inode

    def _named_identity(self) -> tuple[int, int] | None:
        if self.parent is not None:
            named = os.stat(self.name, dir_fd=self.parent, follow_symlinks=False)
        else:  # pragma: no cover - Windows fallback below is exercised there.
            named = os.stat(self.key, follow_symlinks=False)
        return self._identity(named)

    def verify(self) -> None:
        """Refuse when the lock path no longer names the locked inode.

        Called immediately before protected writes: a replacement of the lock
        file mid-run would otherwise let later writers exclude each other on
        a different inode while this holder keeps writing unexcluded.
        """
        if self.identity is None:  # pragma: no cover - platforms without ids.
            return
        try:
            named = self._named_identity()
        except FileNotFoundError as exc:
            raise OSError("import inbox lock vanished while held") from exc
        except OSError as exc:
            raise OSError(f"import inbox lock could not be re-checked while held: {exc}") from exc
        if named != self.identity:
            raise OSError("import inbox lock was replaced while held")

    def release(self) -> None:
        try:
            if fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows only.
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        finally:
            os.close(self.fd)
            if self.parent is not None:
                os.close(self.parent)


def verify_inbox_lock(target: Path) -> None:
    """Re-check this process's held run lock; refuse when the inode was replaced."""
    entry = _ACTIVE_LOCKS.get(str(inbox_lock_path(target)))
    if entry is None:
        raise OSError("import inbox run lock is not held by this process")
    entry.verify()


def verify_inbox_writer_lock(target: Path) -> None:
    """Re-check this process's held writer lock; refuse when the inode was replaced."""
    entry = _ACTIVE_LOCKS.get(str(inbox_writer_lock_path(target)))
    if entry is None:
        raise OSError("import inbox writer lock is not held by this process")
    entry.verify()


def verify_canonical_write_locks(target: Path) -> None:
    """Re-check whichever locks this writer holds before protected writes.

    Inside-a-run writers (marker present) hold only the writer lock, whose
    dev/ino identity is re-checked here; outsiders hold run then writer and
    get both re-checked.
    """
    if not inside_scanner_run():
        verify_inbox_lock(target)
    verify_inbox_writer_lock(target)


@contextlib.contextmanager
def scanner_inbox_run_lock(target: Path) -> Iterator[_HeldInboxLock]:
    """Hold the workspace's run-wide inbox exclusion for the block.

    Only a scanner launcher holds this across its run window; honest Brigade
    writers in other processes serialize behind it (reentrant per process so
    nested paths inside one holder cannot self-deadlock).
    """
    with _held_inbox_lock(inbox_lock_path(target)) as held:
        yield held


@contextlib.contextmanager
def inbox_writer_lock(target: Path) -> Iterator[_HeldInboxLock]:
    """Hold the per-canonical-write inbox exclusion for the block.

    Serializes one read-modify-write section across all writers: outside
    writers (run lock first), self-importing scanner children (this lock
    only), and the launcher's stamping/rollback sections (under its run lock).
    Reentrant per process like the run lock.
    """
    with _held_inbox_lock(inbox_writer_lock_path(target)) as held:
        yield held


@contextlib.contextmanager
def _held_inbox_lock(key_path: Path) -> Iterator[_HeldInboxLock]:
    key = str(key_path)
    with _PROCESS_LOCK:
        depth = _LOCK_DEPTH.get(key, 0) + 1
        _LOCK_DEPTH[key] = depth
    owned: _HeldInboxLock | None = None
    try:
        if depth == 1:
            owned = _acquire_cross_process(key)
            _ACTIVE_LOCKS[key] = owned
        yield _ACTIVE_LOCKS[key]
    finally:
        with _PROCESS_LOCK:
            remaining = _LOCK_DEPTH.pop(key, 1) - 1
            if remaining > 0:
                _LOCK_DEPTH[key] = remaining
                release_owned = False
            else:
                release_owned = True
        if release_owned and owned is not None:
            owned.release()


def _open_lock_parent_posix(lock_path: Path) -> tuple[int, str]:
    """Walk to the lock's parent by descriptors, rejecting symlinked parts."""
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    anchor = lock_path.anchor or "."
    parent = os.open(anchor, directory_flags)
    try:
        for component in lock_path.relative_to(lock_path.anchor).parts[:-1]:
            try:
                child = os.open(component, directory_flags, dir_fd=parent)
            except FileNotFoundError:
                os.mkdir(component, 0o700, dir_fd=parent)
                child = os.open(component, directory_flags, dir_fd=parent)
            os.close(parent)
            parent = child
        return parent, lock_path.name
    except BaseException:
        os.close(parent)
        raise


def _validate_lock_descriptor(fd: int) -> None:
    """Refuse unless the descriptor proves a regular file with exactly one link.

    Platforms whose ``fstat`` cannot prove mode or link count are failed
    closed, matching the inbox descriptor validation posture.
    """
    metadata = os.fstat(fd)
    mode = getattr(metadata, "st_mode", None)
    nlink = getattr(metadata, "st_nlink", None)
    if mode is None or not stat.S_ISREG(mode) or nlink != 1:
        raise OSError("import inbox lock is not a single-link regular file")


def _acquire_posix(key: str) -> tuple[int, str, int]:
    parent, name = _open_lock_parent_posix(Path(key))
    try:
        fd = os.open(
            name,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent,
        )
        try:
            _validate_lock_descriptor(fd)
            if fcntl is None:  # pragma: no cover - POSIX without flock.
                raise OSError("flock is unavailable for the import inbox lock")
            fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(fd)
            raise
    except BaseException:
        os.close(parent)
        raise
    return parent, name, fd


def _acquire_fallback(key: str) -> tuple[int | None, str, int]:
    """Acquire without dirfds; Windows residual documented in the module doc."""
    lock_path = Path(key)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOINHERIT", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(str(lock_path), flags, 0o600)
    try:
        _validate_lock_descriptor(fd)
        if msvcrt is not None:  # pragma: no cover - Windows only.
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
                    break
                except OSError:
                    continue
        elif fcntl is not None:  # pragma: no cover - POSIX without dirfd.
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:  # pragma: no cover - neither primitive available.
            raise OSError("no cross-process lock primitive is available")
    except BaseException:
        os.close(fd)
        raise
    return None, str(lock_path), fd


def _acquire_cross_process(key: str) -> _HeldInboxLock:
    parent_fd: int | None
    if os.name == "posix" and os.open in getattr(os, "supports_dir_fd", set()):
        parent_fd, name, fd = _acquire_posix(key)
    else:
        parent_fd, name, fd = _acquire_fallback(key)
    held = _HeldInboxLock(key, parent_fd, name, fd)
    try:
        named = held._named_identity()
    except BaseException:
        held.release()
        raise
    if held.identity is not None and named != held.identity:
        held.release()
        raise OSError("import inbox lock was replaced while being locked")
    return held
