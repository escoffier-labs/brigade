"""Durability helpers for local Fleet Hub spools."""

from __future__ import annotations

import os
from pathlib import Path


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
