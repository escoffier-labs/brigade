"""Durability helpers for local Fleet Hub spools."""

from __future__ import annotations

import os
from pathlib import Path


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
