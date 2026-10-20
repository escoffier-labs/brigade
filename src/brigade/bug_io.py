"""Bounded regular-file reads and durable append-only bug-report state.

POSIX retains held no-follow parent descriptors. The Windows fallback uses
the journal's checked path open, with regular-file and identity checks. As
with that fallback, ancestor checks cannot exclude a concurrent path swap.
"""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import memory_proposal_io as private_io
from . import run_journal


def _supports_dirfd() -> bool:
    return os.name == "posix" and os.open in os.supports_dir_fd and bool(getattr(os, "O_NOFOLLOW", 0))


def _unlinked(info: os.stat_result) -> bool:
    return not stat.S_ISLNK(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400)


def _parents(path: Path, *, create: bool = False) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if create:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
        info = current.lstat()
        if not _unlinked(info) or not stat.S_ISDIR(info.st_mode):
            raise OSError("state parent must be an unlinked directory")


def _regular(info: os.stat_result, *, private: bool = False) -> None:
    if not _unlinked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError("file must be regular and single-link")
    if private and os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise OSError("state file must be private and operator-owned")


@contextmanager
def _fallback_file(path: Path, flags: int, *, private: bool = False) -> Iterator[int]:
    try:
        try:
            _regular(path.lstat(), private=private)
        except FileNotFoundError:
            if not flags & os.O_CREAT:
                raise
        fd = run_journal._open_nofollow(path, flags | getattr(os, "O_NONBLOCK", 0), 0o600)
        try:
            _regular(os.fstat(fd), private=private)
            _regular(path.lstat(), private=private)
            run_journal._verify_fd_identity(path, fd)
            yield fd
        finally:
            os.close(fd)
    except run_journal.RunJournalError as exc:
        raise OSError("unsafe report file") from exc


def read_bytes(path: Path, *, limit: int) -> bytes:
    if _supports_dirfd():
        return private_io.read_bytes(path, limit=limit) or b""
    _parents(path.parent)
    with _fallback_file(path, os.O_RDONLY) as fd:
        before = os.fstat(fd)
        if before.st_size > limit:
            raise ValueError("file size limit exceeded")
        raw = bytearray()
        while chunk := os.read(fd, min(65536, limit + 1 - len(raw))):
            raw.extend(chunk)
            if len(raw) > limit:
                raise ValueError("file size limit exceeded")
        after = os.fstat(fd)
        _regular(after)
        _regular(path.lstat())
        run_journal._verify_fd_identity(path, fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise OSError("file changed during exact read")
        return bytes(raw)


def _append(fd: int, raw: bytes) -> None:
    with os.fdopen(os.dup(fd), "ab") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def append_bytes(path: Path, raw: bytes) -> None:
    """Append while the caller holds the submission lock, never replace history."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if _supports_dirfd():
        with private_io.directory(path.parent, create=True, private=True) as parent:
            fd = os.open(path.name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
            try:
                private_io._regular(fd, private=True)
                os.fchmod(fd, 0o600)
                _append(fd, raw)
                os.fsync(parent)
            finally:
                os.close(fd)
        return
    _parents(path.parent, create=True)
    with _fallback_file(path, flags, private=True) as fd:
        _append(fd, raw)
