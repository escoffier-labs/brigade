"""Bounded, descriptor-relative reads for unattended inbox care."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from . import dirfd
from .memory_care_journal import safe_path

MAX_DRAFT_BYTES = 256 * 1024


def read(target: Path, rel: str, *, limit: int = MAX_DRAFT_BYTES) -> bytes | None:
    path = safe_path(target, rel)
    try:
        parent = dirfd.open_directory_chain(path.parent)
    except FileNotFoundError:
        return None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if dirfd.posix_available():
            flags = dirfd.file_flags(flags | os.O_NONBLOCK)
        try:
            fd = dirfd.open_child_file(parent, path.name, flags, 0)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                raise ValueError("not a bounded single-link regular file")
            data = handle.read(limit + 1)
            if len(data) > limit:
                raise ValueError("input size limit exceeded")
            return data
    finally:
        os.close(parent)
