"""Durability helpers for local Fleet Hub spools."""

from __future__ import annotations

import os
from pathlib import Path


class AppendRollbackError(OSError):
    """An append failed and its rollback could not be synced."""


def append_record(fd: int, data: bytes, path: Path, dir_fd: int | None) -> None:
    original_size = os.fstat(fd).st_size
    try:
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
    except FileExistsError:
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
