"""Private spool locking helpers using the fleet client's shared state.

Resolve client bindings at call time to retain platform and test overrides
without importing the client while its compatibility exports are loading.
"""

from __future__ import annotations

import errno
import math
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def _validate_spool_lock_timeout(timeout: float | None) -> None:
    from . import fleet_client as client

    if timeout is None:
        return
    try:
        valid = (
            not isinstance(timeout, bool)
            and isinstance(timeout, (int, float))
            and math.isfinite(timeout)
            and timeout >= 0
        )
    except OverflowError:
        valid = False
    if not valid:
        raise client.FleetClientError("invalid fleet spool lock timeout")


def _spool_lock_name(path: Path) -> str:
    """The lock file `_spool_lock(path)` takes: the one name every holder of that spool must use."""
    return path.name + ".lock"


@contextmanager
def _spool_lock(path: Path, *, timeout: float | None = None) -> Iterator[int | None]:
    """Guard a spool across processes and threads.

    Yields a validated directory descriptor for relative I/O, or None on Windows.
    An optional finite, nonnegative timeout shares one budget across both locks.
    None preserves blocking acquisition; zero attempts acquisition without waiting.
    """
    from . import fleet_client as client

    client._validate_spool_lock_timeout(timeout)
    deadline = None if timeout is None else client.time.monotonic() + timeout
    dir_fd = client._ensure_private_dir(path.parent)
    lock_name = _spool_lock_name(path)
    try:
        if deadline is None:
            client._SPOOL_PROCESS_LOCK.acquire()
        elif not client._SPOOL_PROCESS_LOCK.acquire(
            timeout=min(max(0.0, deadline - client.time.monotonic()), client.threading.TIMEOUT_MAX)
        ):
            raise client.FleetClientError("fleet spool lock timed out")
        try:
            if dir_fd is not None:
                fd = client.os.open(
                    lock_name,
                    client.os.O_RDWR | client.os.O_CREAT | client._O_NOFOLLOW | client._O_CLOEXEC,
                    0o600,
                    dir_fd=dir_fd,
                )
            else:  # pragma: no cover - Windows
                fd = client.os.open(
                    str(path.with_name(lock_name)), client.os.O_RDWR | client.os.O_CREAT | client._O_CLOEXEC, 0o600
                )
            locked = False
            try:
                if deadline is not None and (client.fcntl is not None or client.msvcrt is not None):
                    while True:
                        try:
                            if client.fcntl is not None:
                                client.fcntl.flock(fd, client.fcntl.LOCK_EX | client.fcntl.LOCK_NB)
                            elif client.msvcrt is not None:  # pragma: no cover - Windows
                                client.msvcrt.locking(fd, client.msvcrt.LK_NBLCK, 1)
                            locked = True
                            break
                        except OSError as exc:
                            if exc.errno not in {
                                errno.EACCES,
                                errno.EAGAIN,
                                errno.EWOULDBLOCK,
                                errno.EINTR,
                                errno.EDEADLK,
                            }:
                                raise
                            remaining = deadline - client.time.monotonic()
                            if remaining <= 0:
                                raise client.FleetClientError("fleet spool lock timed out") from None
                            client.time.sleep(min(0.05, remaining))
                elif client.fcntl is not None:
                    client.fcntl.flock(fd, client.fcntl.LOCK_EX)
                    locked = True
                elif client.msvcrt is not None:  # pragma: no cover - Windows
                    while True:
                        try:
                            client.msvcrt.locking(fd, client.msvcrt.LK_LOCK, 1)
                            locked = True
                            break
                        except OSError:
                            continue
                yield dir_fd
            finally:
                try:
                    if locked:
                        if client.fcntl is not None:
                            client.fcntl.flock(fd, client.fcntl.LOCK_UN)
                        elif client.msvcrt is not None:  # pragma: no cover - Windows
                            client.msvcrt.locking(fd, client.msvcrt.LK_UNLCK, 1)
                finally:
                    client.os.close(fd)
        finally:
            client._SPOOL_PROCESS_LOCK.release()
    finally:
        if dir_fd is not None:
            client.os.close(dir_fd)
