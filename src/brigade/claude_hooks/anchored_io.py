"""Descriptor-bound I/O for Claude session state, locks and timeout sidecars.

All child operations use the existing cross-platform no-follow primitives.
The directory chain is retained and revalidated, including across a locked
read/modify/write transaction. No pathname fallback is permitted.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .. import dirfd, inbox_lock

_COMPONENTS = (".brigade", "work", "claude-hooks", "sessions")
_ACTIVE: ContextVar[SessionDirectory | None] = ContextVar("claude_session_directory", default=None)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _plain_directory(info: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(info.st_mode)
        and not getattr(info, "st_reparse_tag", 0)
        and not (getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    )


class SessionDirectory:
    """An open target root and no-follow chain down to its session directory."""

    def __init__(self, target: Path, *, create: bool) -> None:
        self.target = target
        self.pid = os.getpid()
        self.chain: list[int] = []
        self.lock: tuple[str, int] | None = None
        try:
            self.chain.append(dirfd.open_directory_nofollow(target, writable=create))
            for name in _COMPONENTS:
                parent = self.chain[-1]
                try:
                    child = dirfd.open_child_directory(parent, name, writable=create)
                except FileNotFoundError:
                    if not create:
                        raise
                    self.validate()
                    try:
                        dirfd.mkdir_child(parent, name)
                    except FileExistsError:
                        pass
                    child = dirfd.open_child_directory(parent, name, writable=create)
                self.chain.append(child)
            self.validate()
        except BaseException:
            self.close()
            raise

    @property
    def fd(self) -> int:
        return self.chain[-1]

    def close(self) -> None:
        for fd in reversed(self.chain):
            os.close(fd)
        self.chain.clear()

    def validate(self) -> None:
        """Refuse renamed, linked or replaced components without following them."""
        root_info = os.lstat(self.target)
        if not _plain_directory(root_info) or _identity(root_info) != _identity(os.fstat(self.chain[0])):
            raise OSError("Claude hook target directory changed during session I/O")
        for index, name in enumerate(_COMPONENTS[: len(self.chain) - 1]):
            named = dirfd.stat_child(self.chain[index], name, allow_reparse=True)
            held = os.fstat(self.chain[index + 1])
            if not _plain_directory(named) or _identity(named) != _identity(held):
                raise OSError("Claude hook session directory changed during session I/O")
        if self.lock is not None:
            self.validate_file(*self.lock)

    def validate_file(self, name: str, fd: int) -> os.stat_result:
        """Require the named file to remain a single-link regular held inode."""
        held = os.fstat(fd)
        named = dirfd.stat_child(self.fd, name, allow_reparse=True)
        if (
            not stat.S_ISREG(held.st_mode)
            or held.st_nlink != 1
            or not stat.S_ISREG(named.st_mode)
            or bool(getattr(named, "st_reparse_tag", False))
            or getattr(named, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or _identity(named) != _identity(held)
        ):
            raise OSError("Claude hook session file is not a single-link regular held file")
        return held

    @contextlib.contextmanager
    def open_file(self, name: str, flags: int) -> Iterator[int]:
        dirfd.validate_component(name)
        self.validate()
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
        fd = dirfd.open_child_file(self.fd, name, flags, 0o600)
        try:
            self.validate_file(name, fd)
            self.validate()
            yield fd
        finally:
            os.close(fd)

    def read_bytes(self, name: str, max_bytes: int | None = None) -> bytes:
        with self.open_file(name, os.O_RDONLY) as fd:
            chunks: list[bytes] = []
            remaining = max_bytes
            while remaining is None or remaining > 0:
                chunk = os.read(fd, 65536 if remaining is None else min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
            self.validate_file(name, fd)
            self.validate()
            return b"".join(chunks)

    def write_json(self, name: str, payload: dict[str, Any]) -> None:
        data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        temporary = f".{dirfd.validate_component(name)}.{uuid.uuid4().hex}.tmp"
        created = False
        try:
            self.validate()
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
            fd = dirfd.open_child_file(self.fd, temporary, flags, 0o600)
            created = True
            try:
                self.validate_file(temporary, fd)
                self.validate()
                _write_all(fd, data)
                os.fsync(fd)
                self.validate_file(temporary, fd)
                self.validate()
                dirfd.replace_children(self.fd, temporary, name)
            finally:
                os.close(fd)
            dirfd.fsync_directory(self.fd)
        finally:
            if created:
                # Cleanup stays relative even when a parent was renamed.
                with contextlib.suppress(FileNotFoundError):
                    dirfd.unlink_child(self.fd, temporary)

    @contextlib.contextmanager
    def held_lock(self, name: str, *, deadline_seconds: float) -> Iterator[None]:
        self.validate()
        if os.name == "nt":
            fd = dirfd.open_child_lock_file(self.fd, name)
        else:
            fd = dirfd.open_child_file(self.fd, name, dirfd.file_flags(os.O_RDWR | os.O_CREAT | os.O_NONBLOCK), 0o600)
        try:
            self.validate_file(name, fd)
            deadline = time.monotonic() + deadline_seconds
            if os.name == "nt":
                inbox_lock._msvcrt_lock_with_deadline(fd, name, deadline, deadline_seconds)
            else:
                inbox_lock._flock_with_deadline(fd, name, deadline, deadline_seconds)
            # Reuse the existing fork cleanup so children cannot prolong locks.
            with inbox_lock._HELD_FILE_LOCK_FDS_GUARD:
                inbox_lock._HELD_FILE_LOCK_FDS.add(fd)
            self.lock = (name, fd)
            token = _ACTIVE.set(self)
            try:
                self.validate()
                yield
            finally:
                _ACTIVE.reset(token)
                self.lock = None
                with inbox_lock._HELD_FILE_LOCK_FDS_GUARD:
                    inbox_lock._HELD_FILE_LOCK_FDS.discard(fd)
        finally:
            os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("short Claude hook session write")
        remaining = remaining[written:]


@contextlib.contextmanager
def session_directory(target: Path, *, create: bool = False) -> Iterator[SessionDirectory]:
    target = target.expanduser().absolute()
    active = _ACTIVE.get()
    if active is not None and active.target == target and active.pid == os.getpid():
        active.validate()
        yield active
        return
    anchor = SessionDirectory(target.resolve(), create=create)
    try:
        yield anchor
    finally:
        anchor.close()


def _read_json(anchor: SessionDirectory, name: str) -> dict[str, Any] | None:
    try:
        value = json.loads(anchor.read_bytes(name))
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def read_json(target: Path, name: str) -> dict[str, Any] | None:
    try:
        with session_directory(target) as anchor:
            return _read_json(anchor, name)
    except OSError:
        return None


def write_json(target: Path, name: str, payload: dict[str, Any]) -> None:
    with session_directory(target, create=True) as anchor:
        anchor.write_json(name, payload)


def read_sidecar(path: Path, max_bytes: int) -> bytes:
    try:
        with session_directory(path.parents[4]) as anchor:
            return anchor.read_bytes(path.name, max_bytes)
    except OSError:
        return b""


def append_sidecar(path: Path, payload: bytes, *, max_bytes: int | None) -> bool:
    try:
        with session_directory(path.parents[4], create=True) as anchor:
            with anchor.open_file(path.name, os.O_WRONLY | os.O_CREAT | os.O_APPEND) as fd:
                if max_bytes is not None and os.fstat(fd).st_size >= max_bytes:
                    return False
                anchor.validate()
                anchor.validate_file(path.name, fd)
                _write_all(fd, payload)
        return True
    except OSError:
        return False


def iter_states(
    target: Path, *, modified_since: datetime | None = None, limit: int | None = None
) -> Iterator[dict[str, Any]]:
    try:
        with session_directory(target) as anchor:
            threshold = modified_since.timestamp() if modified_since is not None else None
            candidates: list[tuple[float, str]] = []
            # Both backends enumerate the held directory. Unsupported native
            # enumeration raises and is diagnosed below, never reopened by path.
            for name in dirfd.child_names(anchor.fd, sys.maxsize):
                if not name.endswith(".json"):
                    continue
                try:
                    info = dirfd.stat_child(anchor.fd, name, allow_reparse=True)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode) and (threshold is None or info.st_mtime >= threshold):
                    candidates.append((info.st_mtime, name))
            candidates.sort(reverse=True)
            if limit is not None:
                candidates = candidates[: max(limit, 0)]
            for _, name in candidates:
                value = _read_json(anchor, name)
                if value is not None:
                    yield value
    except FileNotFoundError:
        return
    except OSError as exc:
        print(f"brigade: Claude hook session listing refused: {exc}", file=sys.stderr)
