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


def rows(target: Path, rel: str):
    """Stream bounded ledger rows without bounding total ledger growth."""
    path = safe_path(target, rel)
    try:
        parent = dirfd.open_directory_chain(path.parent)
    except FileNotFoundError:
        return
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if dirfd.posix_available():
            flags = dirfd.file_flags(flags | os.O_NONBLOCK)
        try:
            fd = dirfd.open_child_file(parent, path.name, flags, 0)
        except FileNotFoundError:
            return
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("unsafe ledger")
            while line := handle.readline(MAX_DRAFT_BYTES + 1):
                if len(line) > MAX_DRAFT_BYTES or not line.endswith(b"\n"):
                    raise ValueError("malformed ledger row")
                yield line
    finally:
        os.close(parent)


def append(target: Path, rel: str, data: bytes) -> None:
    """Append a ledger row without replacing an inode or locking other writers."""
    path = safe_path(target, rel)
    parent = dirfd.open_directory_chain(path.parent, create=True)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
        if dirfd.posix_available():
            flags = dirfd.file_flags(flags | os.O_NONBLOCK)
        fd = dirfd.open_child_file(parent, path.name, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("unsafe ledger")
            if os.write(fd, data) != len(data):
                raise OSError("short ledger append")
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def contains(target: Path, rel: str, block: bytes) -> bool:
    """Recognize a published block with bounded memory as a destination grows."""
    path = safe_path(target, rel)
    try:
        parent = dirfd.open_directory_chain(path.parent)
    except FileNotFoundError:
        return False
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if dirfd.posix_available():
            flags = dirfd.file_flags(flags | os.O_NONBLOCK)
        try:
            fd = dirfd.open_child_file(parent, path.name, flags, 0)
        except FileNotFoundError:
            return False
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("unsafe append destination")
            window = b""
            remaining = info.st_size
            while remaining:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                window += chunk
                if block in window:
                    return True
                window = window[-(len(block) - 1) :] if len(block) > 1 else b""
            return not block
    finally:
        os.close(parent)
