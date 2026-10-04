"""Bounded descriptor reads and private storage for reviewed memory edits.

Fail closed without directory-descriptor support. The operator boundary is
same-UID local access, not protection from a malicious process with that UID.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

MAX_CARD_BYTES = 256 * 1024
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_CORPUS_BYTES = 16 * 1024 * 1024
MAX_FILES = 1024
MAX_INDEXES = 32
MAX_LIST = 64
MAX_REASON_BYTES = 4096


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def relative(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024 or "\\" in value:
        raise ValueError("invalid relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value or value == ".":
        raise ValueError("path must be canonical and relative without traversal")
    return value


@contextmanager
def directory(path: Path, *, create: bool = False, private: bool = False) -> Iterator[int]:
    """Walk from / using O_NOFOLLOW; never resolve an untrusted component."""
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise OSError("safe directory descriptors unavailable")
    absolute = Path(os.path.abspath(path))
    if ".." in path.parts:
        raise OSError("unsafe directory traversal")
    fd = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in absolute.parts[1:]:
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        if private:
            metadata = os.fstat(fd)
            if metadata.st_uid != os.getuid():
                raise OSError("state directory has another owner")
            os.fchmod(fd, 0o700)
        yield fd
    finally:
        os.close(fd)


def _regular(fd: int, *, private: bool = False) -> os.stat_result:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise OSError("file must be regular and single-link")
    if private and (metadata.st_uid != os.getuid() or metadata.st_mode & 0o077):
        raise OSError("state file must be private and operator-owned")
    return metadata


def read_bytes(
    path: Path, *, limit: int = MAX_INPUT_BYTES, absent: bool = False, private: bool = False
) -> bytes | None:
    try:
        with directory(path.parent) as parent:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                before = _regular(fd, private=private)
                if before.st_size > limit:
                    raise ValueError("file size limit exceeded")
                parts: list[bytes] = []
                size = 0
                while chunk := os.read(fd, min(65536, limit + 1 - size)):
                    parts.append(chunk)
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("file size limit exceeded")
                after = _regular(fd, private=private)
                live = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ) or (after.st_dev, after.st_ino) != (live.st_dev, live.st_ino):
                    raise OSError("file changed during exact read")
                return b"".join(parts)
            finally:
                os.close(fd)
    except FileNotFoundError:
        if absent:
            return None
        raise


def file_mode(path: Path, *, absent: bool = False) -> int | None:
    try:
        with directory(path.parent) as parent:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                metadata = _regular(fd)
                live = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                if (metadata.st_dev, metadata.st_ino, metadata.st_mode) != (live.st_dev, live.st_ino, live.st_mode):
                    raise OSError("file changed during mode inspection")
                return metadata.st_mode & 0o777
            finally:
                os.close(fd)
    except FileNotFoundError:
        if absent:
            return None
        raise


def text(path: Path, *, limit: int = MAX_INPUT_BYTES, absent: bool = False, private: bool = False) -> str | None:
    data = read_bytes(path, limit=limit, absent=absent, private=private)
    return data.decode("utf-8", errors="strict") if data is not None else None


def read_object(path: Path, *, private: bool = False) -> dict[str, Any]:
    value = json.loads(text(path, limit=MAX_STATE_BYTES, private=private) or "")
    if not isinstance(value, dict):
        raise ValueError("stored state must be an object")
    return value


def write_exclusive(path: Path, data: bytes) -> None:
    if len(data) > MAX_STATE_BYTES:
        raise ValueError("state size limit exceeded")
    with directory(path.parent, create=True, private=True) as parent:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            _regular(fd, private=True)
            with os.fdopen(os.dup(fd), "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.fsync(parent)
        finally:
            os.close(fd)


def protect_file(path: Path) -> None:
    """Prepare a shared ledger/lock without following or changing linked files."""
    with directory(path.parent, create=True, private=True) as parent:
        fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
        try:
            metadata = _regular(fd)
            if metadata.st_uid != os.getuid():
                raise OSError("state file has another owner")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
