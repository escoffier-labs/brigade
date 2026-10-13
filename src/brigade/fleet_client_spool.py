"""Durability helpers for local Fleet Hub spools."""

from __future__ import annotations

import os
from pathlib import Path


def ensure_directory(path: Path) -> None:
    """Create missing directory components and persist their parent entries."""
    for directory in reversed(path.parents):
        _create_directory(directory)
    _create_directory(path)
    if os.name == "posix":
        for directory in (path, *path.parents):
            _sync_parent(directory)


def _create_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        return


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
