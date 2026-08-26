"""Run-wide writer exclusion for the canonical import inbox.

Scanner runs and every ``work_cmd`` inbox writer serialize on one adjacent
lock file (``<target>/.brigade/work/imports/inbox.jsonl.lock``), so
concurrent ``brigade import``, promote, dismiss, and scan transactions cannot
interleave their read-modify-write windows with scanner stamping or rollback.
The lock is reentrant per process so nested writer paths inside one run
cannot self-deadlock.

The lock file itself is defended against same-UID tampering: on POSIX it is
opened through a held no-follow directory descriptor with ``O_NOFOLLOW``
(symlinks refused) and ``O_NONBLOCK``, then verified by ``fstat`` to be a
regular file with exactly one link (FIFOs and hard links refused). The held
device/inode identity is re-checked against the path before every protected
write, so replacing the locked inode mid-run fails the holder loudly instead
of letting later writers flock a different object. On Windows the same
regular-file/single-link validation runs on the opened handle and exclusion
uses an ``msvcrt`` byte-range lock; Windows cannot request no-follow at open
time, so a symlink planted in the open-to-validation window remains a
documented residual there. A same-UID attacker can additionally ignore the
advisory lock entirely; launch-time inbox revalidation detects that case.

Self-importing scanner children are handed their launcher's held lock
descriptor through ``BRIGADE_INBOX_LOCK_FD`` so they join the run's exclusion
window instead of deadlocking against it. POSIX children adopt by taking a
nonblocking ``flock`` on the inherited open file description; Windows cannot
pass the descriptor this way, so a self-importing scanner child there still
serializes behind the run window and must avoid canonical writer calls until
the run releases it (documented residual).
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

#: Environment variable used to hand the launcher's held lock to scanner
#: children, so a self-importing scanner child joins its own run's exclusion
#: window instead of deadlocking against it or racing it.
INHERITED_LOCK_ENV = "BRIGADE_INBOX_LOCK_FD"


def inbox_lock_path(target: Path) -> Path:
    """Return the lock file beside the canonical inbox."""
    target_root = target.expanduser().resolve()
    inbox = helpers._imports_path(target_root)
    return inbox.with_name(inbox.name + ".lock")


class _HeldInboxLock:
    """One cross-process acquisition of the inbox lock file."""

    def __init__(self, key: str, parent: int | None, name: str, fd: int, *, adopted: bool = False) -> None:
        self.key = key
        self.parent = parent
        self.name = name
        self.fd = fd
        self.adopted = adopted
        self.child_fd: int | None = None
        if not adopted and os.name == "posix":
            try:
                duplicate = os.dup(fd)
                os.set_inheritable(duplicate, True)
                self.child_fd = duplicate
            except OSError:  # pragma: no cover - defensive; inheritance optional.
                self.child_fd = None
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
        if self.adopted:
            # The lock belongs to the launcher's open file description; a child
            # must neither unlock it (shared OFD) nor close the inherited fd.
            return
        try:
            if fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows only.
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        finally:
            os.close(self.fd)
            if self.child_fd is not None:
                try:
                    os.close(self.child_fd)
                except OSError:  # pragma: no cover - defensive.
                    pass
            if self.parent is not None:
                os.close(self.parent)


def child_lock_env(target: Path) -> dict[str, str]:
    """Environment exposing this process's held lock to scanner children."""
    entry = _ACTIVE_LOCKS.get(str(inbox_lock_path(target)))
    if entry is None or entry.child_fd is None:
        return {}
    return {INHERITED_LOCK_ENV: str(entry.child_fd)}


def child_lock_pass_fds(target: Path) -> tuple[int, ...]:
    """File descriptors to keep open across a scanner child exec."""
    entry = _ACTIVE_LOCKS.get(str(inbox_lock_path(target)))
    if entry is None or entry.child_fd is None:
        return ()
    return (entry.child_fd,)


def _adopt_inherited_lock(key: str) -> _HeldInboxLock | None:
    """Join an enclosing scanner run's lock window passed by our launcher.

    Returns ``None`` when no usable inheritance exists; callers then take the
    ordinary exclusive path. A mismatched or unusable inherited fd is ignored
    rather than trusted, so a crafted environment can never bypass exclusion.
    """
    raw = os.environ.get(INHERITED_LOCK_ENV)
    if not raw:
        return None
    try:
        fd = int(raw)
    except ValueError:
        return None
    try:
        metadata = os.fstat(fd)
    except OSError:
        return None
    mode = getattr(metadata, "st_mode", None)
    nlink = getattr(metadata, "st_nlink", None)
    if mode is None or not stat.S_ISREG(mode) or nlink != 1:
        return None
    held = _HeldInboxLock(key, None, str(Path(key)), fd, adopted=True)
    try:
        named = held._named_identity()
    except OSError:
        return None
    if held.identity is not None and named != held.identity:
        # The inherited fd names some other workspace's lock; ignore it and
        # fall through to a normal acquisition for this target.
        return None
    if fcntl is not None:
        try:
            # Same open file description as the launcher's lock: succeeds
            # instantly while the run holds it, and fails when it does not.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return None
    elif msvcrt is not None:  # pragma: no cover - Windows only.
        # Byte-range locks are system-wide per region even for duplicated
        # handles, so re-locking from the child would block on the launcher;
        # trust the inherited window instead (documented residual).
        pass
    else:  # pragma: no cover - no locking primitive available.
        return None
    return held


def verify_inbox_lock(target: Path) -> None:
    """Re-check this process's held lock; refuse when the inode was replaced."""
    entry = _ACTIVE_LOCKS.get(str(inbox_lock_path(target)))
    if entry is None:
        raise OSError("import inbox run lock is not held by this process")
    entry.verify()


@contextlib.contextmanager
def scanner_inbox_run_lock(target: Path) -> Iterator[_HeldInboxLock]:
    """Hold the workspace's inbox writer exclusion for the block.

    Honest Brigade writers in any process serialize on an adjacent
    ``flock``/``msvcrt`` lock file (reentrant per process so nested writer
    paths inside one run cannot self-deadlock). Nested acquisitions share the
    outer cross-process acquisition.
    """
    key = str(inbox_lock_path(target))
    with _PROCESS_LOCK:
        depth = _LOCK_DEPTH.get(key, 0) + 1
        _LOCK_DEPTH[key] = depth
    owned: _HeldInboxLock | None = None
    try:
        if depth == 1:
            owned = _adopt_inherited_lock(key)
            if owned is None:
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
