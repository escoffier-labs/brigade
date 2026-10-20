"""Durability helpers for local Fleet Hub spools."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any


class AppendRollbackError(OSError):
    """An append failed and its rollback could not be synced."""


def append_record(fd: int, data: bytes, path: Path, dir_fd: int | None) -> None:
    original_size = os.fstat(fd).st_size
    try:
        # A killed writer may leave an incomplete final JSON record. Separate it
        # from the next append so tolerant event readers can recover that event.
        if original_size:
            os.lseek(fd, original_size - 1, os.SEEK_SET)
            if os.read(fd, 1) != b"\n":
                data = b"\n" + data
        remaining = memoryview(data)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("spool append made no progress")
            remaining = remaining[written:]
        os.fsync(fd)
        fsync_parent(path, dir_fd)
    except OSError:
        try:
            os.ftruncate(fd, original_size)
            os.fsync(fd)
            fsync_parent(path, dir_fd)
        except OSError as rollback_error:
            raise AppendRollbackError("spool append rollback could not be synced") from rollback_error
        raise


def ensure_directory(path: Path) -> None:
    """Create missing directory components and persist their parent entries."""
    for directory in reversed(path.parents):
        if _create_directory(directory):
            _sync_parent(directory)
    if _create_directory(path):
        _sync_parent(path)


def _create_directory(path: Path) -> bool:
    try:
        path.mkdir(mode=0o700)
    except OSError:
        # Windows mkdir on an existing drive/share root may return access
        # denied instead of EEXIST. Accept only a confirmed directory, as
        # pathlib's mkdir(exist_ok=True) does, and preserve other failures.
        if not path.is_dir():
            raise
        return False
    return os.name == "posix"


def _sync_parent(path: Path) -> None:
    fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_parent(path: Path, dir_fd: int | None) -> None:
    """Sync a spool's directory entry after a mutation on POSIX."""
    if dir_fd is not None:
        os.fsync(dir_fd)
    elif os.name == "posix":
        fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def parse_records(text: str, *, strict: bool) -> list[dict[str, Any]]:
    """Event delivery skips damaged records; durable message queues fail closed."""
    records = []
    for line in text.splitlines():
        if not line.strip():
            continue
        error = "fleet spool contains an invalid record"
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            error = "fleet spool contains malformed JSON"
        else:
            if isinstance(record, dict):
                records.append(record)
                continue
        if strict:
            raise ValueError(error)
        logging.getLogger("brigade.fleet").warning("%s; skipping event record", error)
    return records
