"""The stale-recovery gate that serializes run-lock recoverers.

Split out of ``runguard`` to keep that module under the size ceiling. The
gate's errors are ``runguard.RunLockError``; ``runguard`` imports this module,
so the error helpers import it at call time.
"""

from __future__ import annotations

import contextlib
import enum
import errno
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runguard import RunLockError


def _recovery_gate_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.recovery-gate")


class _RecoveryGate(enum.Enum):
    BUSY = "busy"


def _try_lock_descriptor(descriptor: int) -> bool:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            # The CRT reports every failed non-blocking LockFile as EACCES, so a lock violation and a
            # gate that can never be locked look alike here; the busy-timeout message names both causes.
            if exc.errno != errno.EACCES:
                raise
            return False
        return True
    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock_descriptor(descriptor: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextlib.contextmanager
def _stale_recovery_gate(path: Path) -> Iterator[bool]:
    """Hold the recovery gate for the lock at ``path``; yield False when another recoverer holds it.

    The gate is an OS file lock on a sibling file, so the kernel drops it when
    its holder exits. It never needs aging out by pid, so no recoverer can reap
    it from under a live one. Each call opens its own descriptor, which makes
    threads of one process exclude each other too. The gate file stays beside
    the lock: unlinking it would let two recoverers lock different inodes.
    """
    gate = _recovery_gate_path(path)
    try:
        descriptor = os.open(gate, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    except OSError as exc:
        raise _recovery_gate_error("open", gate, path, exc) from exc
    try:
        try:
            acquired = _try_lock_descriptor(descriptor)
        except OSError as exc:
            raise _recovery_gate_error("lock", gate, path, exc) from exc
        if not acquired:
            yield False
            return
        try:
            yield True
        finally:
            _unlock_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _recovery_gate_error(action: str, gate: Path, path: Path, exc: OSError) -> RunLockError:
    from .runguard import RunLockError

    return RunLockError(
        f"could not {action} run lock recovery gate {gate}: {exc}. Stale lock recovery cannot proceed: "
        f"remove {gate} or fix its permissions (file locking must work here); the dead lock is {path}"
    )


def _recovery_gate_busy_error(path: Path) -> RunLockError:
    from .runguard import RunLockError

    gate = _recovery_gate_path(path)
    return RunLockError(
        f"stale lock recovery in progress: {path}. Another process holds the recovery gate {gate}; retry. "
        f"If this persists, either a stopped process holds {gate} (resume or end it), or {gate} can never "
        "be locked because file locking does not work here, for example on a share without byte-range "
        "locks (move the workspace to a local disk)"
    )
