"""Restrictive descriptor-bound storage for offline trial evidence."""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import dirfd, run_dirfd


def _check_directory(metadata: os.stat_result, *, private: bool) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError("paired output component is not a plain directory")
    if os.name == "posix":
        if private and (metadata.st_uid != os.getuid() or metadata.st_mode & 0o022):
            raise OSError("paired output must be owned by current user and not writable by others")
        if not private and metadata.st_mode & 0o022 and not metadata.st_mode & stat.S_ISVTX:
            raise OSError("paired output ancestor is writable by others")


@contextmanager
def safe_output(path: Path) -> Iterator[run_dirfd.BoundRunDir]:
    # Walk lexical components before any resolve: even a symlink to an otherwise
    # valid owned directory is refused. Windows uses handle-relative primitives.
    if ".." in path.parts:
        raise OSError("paired output must not contain parent traversal")
    path = Path(os.path.abspath(path.expanduser()))
    if path == Path(path.anchor):
        raise OSError("paired output must not be a filesystem anchor")
    fd = dirfd.open_directory_nofollow(Path(path.anchor))
    try:
        parts = path.parts[1:]
        for index, part in enumerate(parts):
            created = False
            try:
                child = dirfd.open_child_directory(fd, part)
            except FileNotFoundError:
                dirfd.mkdir_child(fd, part, 0o700)
                created = True
                child = dirfd.open_child_directory(fd, part)
            os.close(fd)
            fd = child
            _check_directory(os.fstat(fd), private=created or index == len(parts) - 1)
        metadata = os.fstat(fd)
        bound = run_dirfd.BoundRunDir(path, fd, (metadata.st_dev, metadata.st_ino))
        fd = -1
        try:
            yield bound
        finally:
            bound.close()
    finally:
        if fd >= 0:
            os.close(fd)


def write(bound: run_dirfd.BoundRunDir, path: Path, data: bytes) -> None:
    parts = path.relative_to(bound.path).parts
    # Atomic replacement alone would safely replace a symlink, but planted
    # destinations must be refused rather than silently accepted.
    bound.dir_fd(*parts[:-1], create=True)
    try:
        metadata = bound.stat(parts[:-1], parts[-1])
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError("paired artifact must be a single-link regular file")
    bound.write_bytes_atomic(parts[:-1], parts[-1], data)


def inspect_tree(bound: run_dirfd.BoundRunDir, parts: tuple[str, ...] = ()) -> None:
    parent = bound.dir_fd(*parts)
    for name in dirfd.child_names(parent, 100000):
        metadata = dirfd.stat_child(parent, name)
        if stat.S_ISDIR(metadata.st_mode):
            _check_directory(metadata, private=True)
            inspect_tree(bound, (*parts, name))
        elif not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError("paired output contains a link or special file")
