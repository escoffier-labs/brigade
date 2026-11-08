"""Bound reflect replica decoding without limiting ordinary larger sync replicas."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

MAX_META_BYTES = 8 * 1024 * 1024
# Nominal memory per deferred log entry beside its key: the compact event and dict slot.
REPLAY_ENTRY_OVERHEAD = 512
_LIMITS: ContextVar[tuple[int, int] | None] = ContextVar("hub_replica_limits", default=None)


class ReplicaLimitError(OSError):
    """A fixed diagnostic, without snapshot or credential bytes."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"hub reflect replica {kind} limit exceeded")
        self.kind = kind


@contextmanager
def bounded(*, max_cards: int, max_bytes: int) -> Iterator[None]:
    token = _LIMITS.set((max_cards, max_bytes))
    try:
        yield
    finally:
        _LIMITS.reset(token)


def within_count(count: int) -> bool:
    limits = _LIMITS.get()
    return limits is None or count <= limits[0]


def check_count(count: int) -> None:
    if not within_count(count):
        raise ReplicaLimitError("cards")


def check_replay(used: int) -> None:
    """Refuse a log replay whose deferred metadata outgrows the per-replica byte bound."""
    limits = _LIMITS.get()
    if limits is not None and used > limits[1]:
        raise ReplicaLimitError("replay")


def active() -> bool:
    return _LIMITS.get() is not None


def decode_heads(raw: bytes) -> Any:
    """The allocation boundary, reached only after verified count and byte limits."""
    return json.loads(raw)


def read_heads(path: Path, meta: dict[str, Any]) -> Any:
    limits = _LIMITS.get()
    assert limits is not None
    declared = meta.get("cards")
    if declared is not None:
        if type(declared) is not int or declared < 0:
            raise OSError("invalid replica card count")
        check_count(declared)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError("replica heads is not a regular file")
        if info.st_size > limits[1]:
            raise ReplicaLimitError("bytes")
        count = depth = total = 0
        quoted = escaped = key = False
        started = closed = False
        digest = hashlib.sha256()
        while chunk := file.read(64 * 1024):
            total += len(chunk)
            if total > limits[1]:
                raise ReplicaLimitError("bytes")
            digest.update(chunk)
            for char in chunk:
                if quoted:
                    if escaped:
                        escaped = False
                    elif char == 92:
                        escaped = True
                    elif char == 34:
                        quoted = False
                    continue
                if char in (32, 9, 10, 13):
                    continue
                if closed or (not started and char != 123):
                    raise OSError("invalid replica heads object")
                if char == 34:
                    quoted = True
                    if depth == 1 and key:
                        count += 1
                        check_count(count)
                        key = False
                elif char in (123, 91):
                    depth += 1
                    if not started:
                        started = key = True
                elif char in (125, 93):
                    depth -= 1
                    if depth == 0:
                        closed = True
                elif char == 44 and depth == 1:
                    key = True
        if not closed or quoted or depth != 0:
            raise OSError("invalid replica heads object")
        fingerprint = digest.hexdigest()
        if declared is not None and declared != count:
            raise OSError("replica card count mismatch")
        if meta.get("heads_sha256") not in (None, fingerprint):
            raise OSError("replica snapshot digest mismatch")
        file.seek(0)
        raw = file.read(limits[1] + 1)
        if len(raw) > limits[1]:
            raise ReplicaLimitError("bytes")
        # Bind the bounded scan to the exact bytes parsed, even after an in-place edit.
        if hashlib.sha256(raw).hexdigest() != fingerprint:
            raise OSError("replica heads changed during inspection")
    return decode_heads(raw)
