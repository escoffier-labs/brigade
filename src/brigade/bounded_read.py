"""Bounded, nonblocking read of one regular file.

The instruction inventory reads files that other programs and agents write: a
path can be swapped for a FIFO between a `stat` and an `open`, and a plain open
of a FIFO without a writer blocks forever. This helper opens once, with
`O_NONBLOCK` where the platform has it, checks the opened descriptor with
`fstat`, refuses anything that is not a regular file, and reads at most a fixed
number of bytes. Size and bytes both come from that one descriptor. It never
raises: every failure comes back as a short reason code.

Limitation on a platform with no `O_NONBLOCK`: the type of the path is checked
with `stat` before the open, so a path swapped for a FIFO between that `stat`
and the `open` leaves a race the helper cannot close. If that FIFO has no writer
the open blocks. Such a platform is Windows, where a directory entry cannot be a
FIFO, so nothing there can be swapped in. A platform that lacks `O_NONBLOCK` but
has FIFOs is unsupported. When the open does return, the `fstat` of the opened
descriptor still refuses anything that is not a regular file.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

_CHUNK = 64 * 1024


@dataclass(frozen=True)
class BoundedRead:
    data: bytes | None  # None unless the whole file was read
    size: int  # bytes: len(data) when read, else the size the descriptor reported (0 when unknown)
    reason: str | None  # None | "missing" | "unreadable" | "not-regular" | "too-large" | "changed"


def _fingerprint(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns


def read_regular_file(path: Path | str, *, max_bytes: int, require_stable: bool = False) -> BoundedRead:
    """Read `path` when it is a regular file of at most `max_bytes` bytes.

    `require_stable` also fails with "changed" when the descriptor's size or
    modification time moves during the read, or when the path no longer names
    the file that was opened. Use it for configuration, where a half-written file
    must not be trusted. A file another program appends to can be read without it.
    """
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if not nonblock:
        # No O_NONBLOCK (Windows): a filesystem entry there cannot be a FIFO, so
        # check the type before opening instead.
        try:
            pre = os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            return BoundedRead(None, 0, "missing")
        except (OSError, ValueError):
            return BoundedRead(None, 0, "unreadable")
        if not stat.S_ISREG(pre.st_mode):
            return BoundedRead(None, 0, "not-regular")
    flags = os.O_RDONLY | nonblock | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except (FileNotFoundError, NotADirectoryError):
        return BoundedRead(None, 0, "missing")
    except (OSError, ValueError):
        return BoundedRead(None, 0, "unreadable")
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return BoundedRead(None, 0, "not-regular")
        if before.st_size > max_bytes:
            return BoundedRead(None, before.st_size, "too-large")
        data = bytearray()
        # One extra byte detects growth past the ceiling even when st_size was stale.
        while len(data) <= max_bytes:
            chunk = os.read(fd, min(_CHUNK, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > max_bytes:
            return BoundedRead(None, len(data), "too-large")
        if require_stable:
            after = os.fstat(fd)
            if _fingerprint(before) != _fingerprint(after) or len(data) != before.st_size:
                return BoundedRead(None, 0, "changed")
            now = os.stat(path)
            if (now.st_dev, now.st_ino) != (before.st_dev, before.st_ino):
                return BoundedRead(None, 0, "changed")
        return BoundedRead(bytes(data), len(data), None)
    except OSError:
        return BoundedRead(None, 0, "unreadable")
    finally:
        os.close(fd)
