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
A process without the marker acquires the run lock first (deadline-bounded
flock / msvcrt lock retry: it waits while a run window is open, but raises
``InboxLockTimeout`` instead of hanging forever behind a stale holder) and
then the writer lock under the same bound. The marker is deliberately not a capability: a child that forges
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

Residuals tracked in escoffier-labs/brigade#1215: a descendant that calls
``setsid()`` or double-forks escapes process-group reaping (``descendants_reaped``
records that the group was reaped, not that no descendant survives); reentrancy
is process-wide rather than thread-owned; canonical writers outside ``work_cmd``
(operator migration, trust gate, session ops, issue ops, actions dispatch) still
read the inbox before taking the locks; ``InboxLockTimeout`` escapes a few CLI
paths as a traceback instead of a bounded nonzero result; and the Windows
``LK_NBLCK`` deadline path has no native contention test yet.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import threading
import time
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
    msvcrt = None  # type: ignore[assignment,attr-defined]


_PROCESS_LOCK = threading.Lock()
_ACTIVE_LOCKS: dict[str, "_ActiveInboxLock"] = {}


class _ActiveInboxLock:
    """One process-wide registry entry: ownership and depth together.

    Keeping the cross-process acquisition and the reentrancy count in a single
    entry means an inner thread context can never lose the fd to an outer
    context that exits first, the descriptor closes exactly once at outermost
    exit, and concurrent first acquisitions serialize on ``ready`` instead of
    observing a half-initialized entry.
    """

    def __init__(self) -> None:
        self.owner: _HeldInboxLock | None = None
        self.depth = 0
        self.ready = threading.Event()
        self.error: BaseException | None = None


#: Default bounded window for one cross-process inbox lock acquisition. A
#: holder that outlives its legitimacy (an escaped descendant keeping the
#: writer lock after its direct child exited) must fail launchers and outside
#: writers loudly instead of blocking them forever.
DEFAULT_LOCK_DEADLINE_SECONDS = 30.0

#: Poll interval while a bounded acquisition waits for a busy lock file.
_LOCK_RETRY_INTERVAL_SECONDS = 0.05

#: Marker (not a capability) set by the scanner launcher for its children:
#: a process that sees it is inside a scanner run and skips the run lock,
#: taking only the writer lock for canonical writes. Forging or stripping it
#: cannot break exclusion; see the module docstring.
SCANNER_RUN_ENV = "BRIGADE_SCANNER_RUN_ID"


class InboxLockTimeout(TimeoutError):
    """Typed failure when an inbox lock stayed busy past its acquisition deadline."""


@contextlib.contextmanager
def held_file_lock(path: Path, *, deadline_seconds: float) -> Iterator[None]:
    """Hold one cross-platform advisory lock file for the block.

    This public primitive uses the same hardened open and deadline-bounded
    ``flock`` / ``msvcrt`` acquisition as the inbox locks, without joining
    their process-wide reentrancy registry.  It is suitable for adjacent
    state-file locks that need independent cross-process exclusion.
    """
    held = _acquire_cross_process(str(path), deadline_seconds=deadline_seconds)
    try:
        yield
    finally:
        held.release()


def _resolve_deadline(deadline_seconds: float | None) -> tuple[float, float]:
    """Return ``(requested_seconds, absolute_monotonic_deadline)``."""
    requested = DEFAULT_LOCK_DEADLINE_SECONDS if deadline_seconds is None else deadline_seconds
    return requested, time.monotonic() + requested


def _wait_for_retry(key: str, deadline: float, requested_seconds: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise InboxLockTimeout(f"import inbox lock stayed busy for {requested_seconds:g}s: {key}")
    time.sleep(min(_LOCK_RETRY_INTERVAL_SECONDS, remaining))


def _flock_with_deadline(fd: int, key: str, deadline: float, requested_seconds: float) -> None:
    if fcntl is None:  # pragma: no cover - callers guarantee availability.
        raise OSError("fcntl is unavailable for the import inbox lock")
    nonblocking = fcntl.LOCK_EX | getattr(fcntl, "LOCK_NB", 0)
    while True:
        try:
            fcntl.flock(fd, nonblocking)
            return
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, getattr(errno, "EWOULDBLOCK", errno.EAGAIN)):
                raise
        _wait_for_retry(key, deadline, requested_seconds)


def _msvcrt_lock_with_deadline(fd: int, key: str, deadline: float, requested_seconds: float) -> None:
    if msvcrt is None:  # pragma: no cover - callers guarantee availability.
        raise OSError("msvcrt is unavailable for the import inbox lock")
    retryable = (errno.EACCES, errno.EAGAIN, errno.EDEADLOCK, getattr(errno, "EWOULDBLOCK", errno.EAGAIN))
    while True:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            return
        except OSError as exc:
            if exc.errno not in retryable:
                raise
        _wait_for_retry(key, deadline, requested_seconds)


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

    def _verify_live_descriptor(self) -> None:
        """Refuse unless the held descriptor is still a valid single-link lock file.

        ``fstat`` of the live descriptor fails closed on a closed or otherwise
        stale entry and proves the descriptor still names the locked inode;
        the path-only check below cannot see that difference.
        """
        metadata = os.fstat(self.fd)
        mode = getattr(metadata, "st_mode", None)
        nlink = getattr(metadata, "st_nlink", None)
        if mode is None or not stat.S_ISREG(mode) or nlink != 1:
            raise OSError("import inbox lock descriptor is not a single-link regular file")
        live = self._identity(metadata)
        if live is not None and live != self.identity:
            raise OSError("import inbox lock descriptor no longer names the locked inode")

    def verify(self) -> None:
        """Refuse when the lock path no longer names the locked inode.

        Called immediately before protected writes: a replacement of the lock
        file mid-run would otherwise let later writers exclude each other on
        a different inode while this holder keeps writing unexcluded.
        """
        if self.identity is None:  # pragma: no cover - platforms without ids.
            return
        self._verify_live_descriptor()
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
    """Re-check this process's held run lock; refuse when it is not really held.

    The registry lookup happens under the module lock so an entry released by
    an outermost exit is never observed, and the entry itself is re-validated
    against its live descriptor.
    """
    with _PROCESS_LOCK:
        entry = _ACTIVE_LOCKS.get(str(inbox_lock_path(target)))
    if entry is None or entry.owner is None:
        raise OSError("import inbox run lock is not held by this process")
    entry.owner.verify()


def verify_inbox_writer_lock(target: Path) -> None:
    """Re-check this process's held writer lock; refuse when it is not really held."""
    with _PROCESS_LOCK:
        entry = _ACTIVE_LOCKS.get(str(inbox_writer_lock_path(target)))
    if entry is None or entry.owner is None:
        raise OSError("import inbox writer lock is not held by this process")
    entry.owner.verify()


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
def scanner_inbox_run_lock(target: Path, *, deadline_seconds: float | None = None) -> Iterator[_HeldInboxLock]:
    """Hold the workspace's run-wide inbox exclusion for the block.

    Only a scanner launcher holds this across its run window; honest Brigade
    writers in other processes serialize behind it (reentrant per process so
    nested paths inside one holder cannot self-deadlock). Acquisition is
    deadline-bounded: a lock held past ``deadline_seconds`` (default
    :data:`DEFAULT_LOCK_DEADLINE_SECONDS`) raises :class:`InboxLockTimeout`.
    """
    with _held_inbox_lock(inbox_lock_path(target), deadline_seconds=deadline_seconds) as held:
        yield held


@contextlib.contextmanager
def inbox_writer_lock(target: Path, *, deadline_seconds: float | None = None) -> Iterator[_HeldInboxLock]:
    """Hold the per-canonical-write inbox exclusion for the block.

    Serializes one read-modify-write section across all writers: outside
    writers (run lock first), self-importing scanner children (this lock
    only), and the launcher's stamping/rollback sections (under its run lock).
    Reentrant per process like the run lock, and deadline-bounded like it.
    """
    with _held_inbox_lock(inbox_writer_lock_path(target), deadline_seconds=deadline_seconds) as held:
        yield held


@contextlib.contextmanager
def _held_inbox_lock(key_path: Path, *, deadline_seconds: float | None = None) -> Iterator[_HeldInboxLock]:
    key = str(key_path)
    with _PROCESS_LOCK:
        entry = _ACTIVE_LOCKS.get(key)
        fresh = entry is None
        if fresh:
            entry = _ActiveInboxLock()
            _ACTIVE_LOCKS[key] = entry
        assert entry is not None
        entry.depth += 1
    try:
        if fresh and entry is not None:
            # Initialization runs outside the module lock (the cross-process
            # acquisition blocks), but the entry exists and later joiners wait
            # on ``ready``, so no one can observe a missing owner.
            try:
                owned = _acquire_cross_process(key, deadline_seconds=deadline_seconds)
            except BaseException as exc:
                with _PROCESS_LOCK:
                    entry.error = exc
                    if _ACTIVE_LOCKS.get(key) is entry:
                        _ACTIVE_LOCKS.pop(key, None)
                entry.ready.set()
                raise
            with _PROCESS_LOCK:
                entry.owner = owned
            entry.ready.set()
            yield owned
        else:
            assert entry is not None
            entry.ready.wait()
            if entry.error is not None:
                raise entry.error
            assert entry.owner is not None
            yield entry.owner
    finally:
        release_owned: _HeldInboxLock | None = None
        drop = False
        with _PROCESS_LOCK:
            entry.depth -= 1
            if entry.depth <= 0 and _ACTIVE_LOCKS.get(key) is entry:
                # Drop the registry entry atomically with the decision to
                # release: verify_* must never observe a lock this process no
                # longer holds, and the entry must be gone before the fd closes.
                drop = True
                release_owned = entry.owner
                _ACTIVE_LOCKS.pop(key, None)
        if drop and release_owned is not None:
            release_owned.release()


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
                try:
                    os.mkdir(component, 0o700, dir_fd=parent)
                except FileExistsError:
                    # A concurrent lock opener created the same component.
                    # Re-open it below with the normal no-follow validation.
                    pass
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


def _acquire_posix(key: str, deadline: float, requested_seconds: float) -> tuple[int, str, int]:
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
            _flock_with_deadline(fd, key, deadline, requested_seconds)
        except BaseException:
            os.close(fd)
            raise
    except BaseException:
        os.close(parent)
        raise
    return parent, name, fd


def _acquire_fallback(key: str, deadline: float, requested_seconds: float) -> tuple[int | None, str, int]:
    """Acquire without dirfds; Windows residual documented in the module doc."""
    lock_path = Path(key)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOINHERIT", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(str(lock_path), flags, 0o600)
    try:
        _validate_lock_descriptor(fd)
        if msvcrt is not None:  # pragma: no cover - Windows only.
            _msvcrt_lock_with_deadline(fd, key, deadline, requested_seconds)
        elif fcntl is not None:  # pragma: no cover - POSIX without dirfd.
            _flock_with_deadline(fd, key, deadline, requested_seconds)
        else:  # pragma: no cover - neither primitive available.
            raise OSError("no cross-process lock primitive is available")
    except BaseException:
        os.close(fd)
        raise
    return None, str(lock_path), fd


def _acquire_cross_process(key: str, *, deadline_seconds: float | None = None) -> _HeldInboxLock:
    parent_fd: int | None
    requested_seconds, deadline = _resolve_deadline(deadline_seconds)
    if os.name == "posix" and os.open in getattr(os, "supports_dir_fd", set()):
        parent_fd, name, fd = _acquire_posix(key, deadline, requested_seconds)
    else:
        parent_fd, name, fd = _acquire_fallback(key, deadline, requested_seconds)
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
