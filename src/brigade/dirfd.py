"""Cross-platform descriptor-relative no-follow primitives.

POSIX uses ``dir_fd`` plus ``O_NOFOLLOW``. Windows delegates to
``work_cmd.nt_dirfd``. Callers that already monkeypatch
``authority_store`` availability helpers keep those seams: this module is
the implementation, and ``authority_store`` re-branches on its own names.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any


def _nt_dirfd() -> Any:
    """Load the Windows helper without importing the work_cmd package facade."""
    return importlib.import_module("brigade.work_cmd.nt_dirfd")


def posix_available() -> bool:
    """Return whether POSIX openat/O_NOFOLLOW primitives can hold a parent."""
    return (
        os.name == "posix"
        and bool(getattr(os, "O_NOFOLLOW", 0))
        and bool(getattr(os, "O_DIRECTORY", 0))
        and os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and os.rename in os.supports_dir_fd
    )


def nt_available() -> bool:
    """Return whether Windows handle-relative no-follow operations can be used."""
    if sys.platform != "win32":
        return False
    return bool(_nt_dirfd().available())


def available() -> bool:
    return posix_available() or nt_available()


def unavailable(kind: str) -> OSError:
    return OSError(f"descriptor-relative {kind} are unavailable")


def validate_component(name: str) -> str:
    """Reject empty, dotted, separator-bearing, or NUL names so walks stay contained."""
    if not isinstance(name, str) or not name:
        raise OSError("path component is empty")
    if name in {".", ".."}:
        raise OSError("path component is not contained")
    if "/" in name or "\\" in name or ":" in name or "\x00" in name:
        raise OSError("path component is not a single contained name")
    return name


def _posix_open_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    return os.open(path, flags)


def _nt_open_directory(path: Path) -> int:
    return _nt_dirfd().open_root_directory(path)


def _posix_open_file(path: Path, flags: int, mode: int) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if flags & os.O_CREAT:
        return os.open(path, flags | nofollow | getattr(os, "O_CLOEXEC", 0), mode)
    return os.open(path, flags | nofollow | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))


def _nt_open_file(path: Path, flags: int, mode: int) -> int:
    return _nt_dirfd().open_path_file(path, flags, mode)


def _posix_open_child_directory(parent: int, name: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    return os.open(name, flags, dir_fd=parent)


def _nt_open_child_directory(parent: int, name: str) -> int:
    return _nt_dirfd().open_child_directory(parent, name)


def _posix_mkdir_child(parent: int, name: str) -> None:
    os.mkdir(name, 0o700, dir_fd=parent)


def _nt_mkdir_child(parent: int, name: str) -> None:
    _nt_dirfd().mkdir_child(parent, name)


def _posix_open_child_file(parent: int, name: str, flags: int, mode: int) -> int:
    if flags & os.O_CREAT:
        return os.open(name, flags, mode, dir_fd=parent)
    return os.open(name, flags, dir_fd=parent)


def _nt_open_child_file(parent: int, name: str, flags: int, mode: int) -> int:
    return _nt_dirfd().open_file(parent, name, flags, mode)


def _posix_replace_children(parent: int, source: str, destination: str) -> None:
    os.replace(source, destination, src_dir_fd=parent, dst_dir_fd=parent)


def _nt_replace_children(parent: int, source: str, destination: str) -> None:
    _nt_dirfd().replace_children(parent, source, destination)


def _posix_unlink_child(parent: int, name: str) -> None:
    os.unlink(name, dir_fd=parent)


def _nt_unlink_child(parent: int, name: str) -> None:
    _nt_dirfd().unlink_child(parent, name)


def _posix_stat_child(parent: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=parent, follow_symlinks=False)


def _nt_stat_child(parent: int, name: str) -> os.stat_result:
    return _nt_dirfd().stat_child(parent, name)


def open_directory_nofollow(path: Path) -> int:
    """Open a directory without following a final symlink or reparse point."""
    if posix_available():
        return _posix_open_directory(path)
    if nt_available():
        return _nt_open_directory(path)
    raise unavailable("directory operations")


def open_file_nofollow(path: Path, flags: int, mode: int = 0o600) -> int:
    """Open a file path without following a final symlink or reparse point."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        return _posix_open_file(path, flags, mode)
    if nt_available():
        return _nt_open_file(path, flags, mode)
    raise OSError("no-follow file open is unavailable")


def open_child_directory(parent: int, name: str) -> int:
    validate_component(name)
    if posix_available():
        return _posix_open_child_directory(parent, name)
    if nt_available():
        return _nt_open_child_directory(parent, name)
    raise unavailable("directory operations")


def mkdir_child(parent: int, name: str) -> None:
    validate_component(name)
    if posix_available():
        _posix_mkdir_child(parent, name)
        return
    if nt_available():
        _nt_mkdir_child(parent, name)
        return
    raise unavailable("directory operations")


def open_child_file(parent: int, name: str, flags: int, mode: int = 0o600) -> int:
    validate_component(name)
    if posix_available():
        return _posix_open_child_file(parent, name, flags, mode)
    if nt_available():
        return _nt_open_child_file(parent, name, flags, mode)
    raise unavailable("import inbox operations")


def replace_children(parent: int, source: str, destination: str) -> None:
    validate_component(source)
    validate_component(destination)
    if posix_available():
        _posix_replace_children(parent, source, destination)
        return
    if nt_available():
        _nt_replace_children(parent, source, destination)
        return
    raise unavailable("import inbox operations")


def unlink_child(parent: int, name: str) -> None:
    validate_component(name)
    if posix_available():
        _posix_unlink_child(parent, name)
        return
    if nt_available():
        _nt_unlink_child(parent, name)
        return
    raise unavailable("import inbox operations")


def stat_child(parent: int, name: str) -> os.stat_result:
    validate_component(name)
    if posix_available():
        return _posix_stat_child(parent, name)
    if nt_available():
        return _nt_stat_child(parent, name)
    raise unavailable("import inbox validation")


def fsync_directory(descriptor: int) -> None:
    """Flush a held descriptor; directory fsync is best-effort on Windows."""
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if sys.platform == "win32" and getattr(exc, "winerror", None) in {1, 5}:
            return
        raise
