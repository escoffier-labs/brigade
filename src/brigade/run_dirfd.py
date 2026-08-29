"""Descriptor-bound run directory walks and atomic writes.

Phase 1 is dormant: no production caller activates a binding. ``localio``
consults ``active_binding_for`` first; without a binding it keeps today's
pathname writes. Authorization is lexical against the exact bound path, then
resolution walks held no-follow dirfds. Pathname re-resolution never
authorizes an operation.
"""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from . import dirfd

MAX_READ_BYTES = 8 * 1024 * 1024

_active: ContextVar[BoundRunDir | None] = ContextVar("brigade_bound_run_dir", default=None)


def _close_descriptor(descriptor: int) -> None:
    if descriptor < 0:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


def _lexical_relative(path: Path, root: Path) -> tuple[str, ...] | None:
    path_parts = Path(os.fspath(path)).parts
    root_parts = Path(os.fspath(root)).parts
    if not path_parts or not root_parts:
        return None
    if len(path_parts) <= len(root_parts):
        return None
    if path_parts[: len(root_parts)] != root_parts:
        return None
    return path_parts[len(root_parts) :]


class BoundRunDir:
    """A no-follow handle on a run directory plus its (dev, ino) identity."""

    def __init__(self, path: Path, root_fd: int, identity: tuple[int, int]) -> None:
        self.path = path
        self.identity = identity
        self._root_fd = root_fd
        self._dirs: dict[tuple[str, ...], int] = {(): root_fd}

    def _require_open(self) -> int:
        root = self._dirs.get(())
        if root is None or root < 0:
            raise OSError("bound run directory is closed")
        return root

    def dir_fd(self, *components: str, create: bool = False) -> int:
        fd = self._require_open()
        current: tuple[str, ...] = ()
        for component in components:
            dirfd.validate_component(component)
            current = (*current, component)
            cached = self._dirs.get(current)
            if cached is not None:
                fd = cached
                continue
            try:
                child = dirfd.open_child_directory(fd, component)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    dirfd.mkdir_child(fd, component)
                except FileExistsError:
                    pass
                child = dirfd.open_child_directory(fd, component)
            self._dirs[current] = child
            fd = child
        return fd

    def read_bytes(self, *parts: str, max_bytes: int = MAX_READ_BYTES) -> bytes | None:
        if not parts:
            raise OSError("path component is empty")
        *directories, name = parts
        for component in directories:
            dirfd.validate_component(component)
        dirfd.validate_component(name)
        parent = self.dir_fd(*directories)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = dirfd.open_child_file(parent, name, flags)
        except FileNotFoundError:
            return None
        try:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise OSError("bound read exceeds byte limit")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            _close_descriptor(descriptor)

    def write_text_atomic(self, components: tuple[str, ...], name: str, data: str) -> None:
        self.write_bytes_atomic(components, name, data.encode("utf-8"))

    def write_bytes_atomic(self, components: tuple[str, ...], name: str, data: bytes) -> None:
        for component in components:
            dirfd.validate_component(component)
        dirfd.validate_component(name)
        parent = self.dir_fd(*components, create=True)
        temporary = f".{name}.{uuid4().hex}.tmp"
        descriptor = -1
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            descriptor = dirfd.open_child_file(parent, temporary, flags, 0o600)
            with os.fdopen(os.dup(descriptor), "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.close(descriptor)
            descriptor = -1
            dirfd.replace_children(parent, temporary, name)
            dirfd.fsync_directory(parent)
        except BaseException:
            if descriptor != -1:
                _close_descriptor(descriptor)
                descriptor = -1
            try:
                dirfd.unlink_child(parent, temporary)
            except FileNotFoundError:
                pass
            raise

    def open_file(self, components: tuple[str, ...], name: str, flags: int, mode: int = 0o600) -> int:
        for component in components:
            dirfd.validate_component(component)
        dirfd.validate_component(name)
        parent = self.dir_fd(*components)
        extra = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        return dirfd.open_child_file(parent, name, flags | extra, mode)

    def replace(self, components: tuple[str, ...], source: str, destination: str) -> None:
        parent = self.dir_fd(*components)
        dirfd.replace_children(parent, source, destination)

    def stat(self, components: tuple[str, ...], name: str) -> os.stat_result:
        parent = self.dir_fd(*components)
        return dirfd.stat_child(parent, name)

    def still_bound(self) -> bool:
        try:
            held = os.fstat(self._require_open())
        except OSError:
            return False
        if (held.st_dev, held.st_ino) != self.identity:
            return False
        try:
            named = self.path.lstat()
        except OSError:
            return False
        if stat.S_ISLNK(named.st_mode):
            return False
        return (named.st_dev, named.st_ino) == self.identity

    def close(self) -> None:
        descriptors = list(self._dirs.values())
        self._dirs.clear()
        self._root_fd = -1
        for descriptor in descriptors:
            _close_descriptor(descriptor)


def bind_run_dir(run_dir: Path) -> BoundRunDir | None:
    """Hold a no-follow directory handle bound to the contained identity.

    Returns None when the identity cannot be established. That includes
    platforms without an equivalent no-follow primitive.
    """
    if not dirfd.available():
        return None
    path = Path(run_dir)
    try:
        descriptor = dirfd.open_directory_nofollow(path)
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            _close_descriptor(descriptor)
            return None
        try:
            named = path.lstat()
        except OSError:
            _close_descriptor(descriptor)
            return None
        if stat.S_ISLNK(named.st_mode) or (named.st_dev, named.st_ino) != (metadata.st_dev, metadata.st_ino):
            _close_descriptor(descriptor)
            return None
    except OSError:
        _close_descriptor(descriptor)
        return None
    return BoundRunDir(path, descriptor, (metadata.st_dev, metadata.st_ino))


@contextmanager
def bound_run_dir(run_dir: Path) -> Iterator[BoundRunDir]:
    bound = bind_run_dir(run_dir)
    if bound is None:
        raise OSError("run directory could not be bound")
    token = _active.set(bound)
    try:
        yield bound
    finally:
        _active.reset(token)
        bound.close()


def active_binding_for(path: Path) -> tuple[BoundRunDir, tuple[str, ...], str] | None:
    """Authorize only lexical descendants of the exact bound run path."""
    bound = _active.get()
    if bound is None:
        return None
    rest = _lexical_relative(Path(path), bound.path)
    if rest is None:
        return None
    *directories, name = rest
    for component in directories:
        dirfd.validate_component(component)
    dirfd.validate_component(name)
    return bound, tuple(directories), name


def read_bytes(path: Path, *, max_bytes: int = MAX_READ_BYTES) -> bytes:
    found = active_binding_for(path)
    if found is None:
        return Path(path).read_bytes()
    bound, components, name = found
    payload = bound.read_bytes(*components, name, max_bytes=max_bytes)
    if payload is None:
        raise FileNotFoundError(path)
    return payload
