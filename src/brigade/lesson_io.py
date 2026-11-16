"""Descriptor-bound lesson files, on POSIX and native Windows handles."""

from __future__ import annotations

import hashlib
import os
import stat
import time
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Iterator

from . import dirfd, inbox_lock
from .memory_proposal_io import json_bytes as json_bytes, relative as relative


def regular(fd: int) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError("lesson file must be regular and single-link")
    return info


@contextmanager
def directory(path: Path, *, create: bool = False, private: bool = False) -> Iterator[int]:
    fd = dirfd.open_directory_chain(Path(os.path.abspath(path)), create=create)
    try:
        if private and os.name == "posix":
            if os.fstat(fd).st_uid != os.getuid():
                raise OSError("lesson directory has another owner")
            os.fchmod(fd, 0o700)
        yield fd
    finally:
        os.close(fd)


def read_bytes(path: Path, *, limit: int = 1024 * 1024, absent: bool = False, private: bool = False) -> bytes | None:
    try:
        with directory(path.parent) as parent:
            fd = dirfd.open_child_file(parent, path.name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            try:
                before = regular(fd)
                if private and os.name == "posix" and (before.st_uid != os.getuid() or before.st_mode & 0o077):
                    raise OSError("lesson state must be private and operator-owned")
                data = bytearray()
                while chunk := os.read(fd, min(65536, limit + 1 - len(data))):
                    data.extend(chunk)
                    if len(data) > limit:
                        raise ValueError("lesson file size limit exceeded")
                after = regular(fd)
                live = dirfd.stat_child(parent, path.name)
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ) or (live.st_dev, live.st_ino) != (after.st_dev, after.st_ino):
                    raise OSError("lesson file changed during read")
                return bytes(data)
            finally:
                os.close(fd)
    except FileNotFoundError:
        if absent:
            return None
        raise


def text(path: Path, **kwargs) -> str | None:
    data = read_bytes(path, **kwargs)
    return data.decode("utf-8") if data is not None else None


def recover_publication(path: Path, data: bytes) -> None:
    """Remove only the second name left by interrupted no-replace publication."""
    if not dirfd.posix_available():
        return
    key = hashlib.sha256(data).hexdigest()
    with held_file_lock(path.parent / f".lesson-{key}.lock", deadline_seconds=1):
        with directory(path.parent, private=True) as parent:
            temporary = f".lesson-{key}.tmp"
            try:
                draft = dirfd.stat_child(parent, path.name)
                staged = dirfd.stat_child(parent, temporary)
            except FileNotFoundError:
                return
            if (
                stat.S_ISREG(draft.st_mode)
                and stat.S_ISREG(staged.st_mode)
                and draft.st_nlink == staged.st_nlink == 2
                and (draft.st_dev, draft.st_ino) == (staged.st_dev, staged.st_ino)
            ):
                dirfd.unlink_child(parent, temporary)
                dirfd.fsync_directory(parent)


def write(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    # Serialize matching exclusive writes so recovery can remove abandoned
    # temporaries even when a retried draft gets a new timestamped filename.
    key = hashlib.sha256(data).hexdigest()
    lock = path.parent / f".lesson-{key}.lock"
    with held_file_lock(lock, deadline_seconds=1) if exclusive else nullcontext():
        with directory(path.parent, create=True, private=True) as parent:
            temporary = f".lesson-{key if exclusive else uuid.uuid4().hex}.tmp"
            if exclusive:
                try:
                    dirfd.unlink_child(parent, temporary)
                except FileNotFoundError:
                    pass
            try:
                fd = dirfd.open_child_file(parent, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                try:
                    regular(fd)
                    with os.fdopen(os.dup(fd), "wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                finally:
                    os.close(fd)
                if not exclusive:
                    # Refuse linked destinations even though replace would not follow them.
                    read_bytes(path, absent=True)
                dirfd.replace_children(parent, temporary, path.name, replace=not exclusive)
                dirfd.fsync_directory(parent)
            finally:
                try:
                    dirfd.unlink_child(parent, temporary)
                except FileNotFoundError:
                    pass


@contextmanager
def held_file_lock(path: Path, *, deadline_seconds: float) -> Iterator[None]:
    with directory(path.parent, create=True, private=True) as parent:
        fd = dirfd.open_child_lock_file(parent, path.name)
        try:
            info = regular(fd)
            deadline = time.monotonic() + deadline_seconds
            if inbox_lock.fcntl is not None:
                inbox_lock._flock_with_deadline(fd, str(path), deadline, deadline_seconds)
            else:
                inbox_lock._msvcrt_lock_with_deadline(fd, str(path), deadline, deadline_seconds)
            yield
            live = dirfd.stat_child(parent, path.name)
            if (info.st_dev, info.st_ino) != (live.st_dev, live.st_ino):
                raise OSError("lesson lock changed while held")
        finally:
            os.close(fd)
