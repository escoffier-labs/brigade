"""Shared no-follow file operations retained for connector isolation."""

from __future__ import annotations
import os
import stat
import sys
import uuid
from pathlib import Path
from . import dirfd as dirfd_mod


def remove_regular_file(path: Path) -> None:
    """Unlink one regular file without following a symlink or reparse point."""
    parent = _open_parent_nofollow(path, create=False)
    try:
        if os.name == "posix":
            existing = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISLNK(existing.st_mode):
                raise OSError("output is a symlink")
            if not stat.S_ISREG(existing.st_mode):
                raise OSError("output is not a regular file")
            os.unlink(path.name, dir_fd=parent)
            return
        if _path_is_symlink(path):
            raise OSError("output is a reparse point")
        from .work_cmd import nt_dirfd

        nt_dirfd.unlink_child(parent, path.name)
    finally:
        os.close(parent)


def _path_is_symlink(path: Path) -> bool:
    """Return whether a final path component is a link or Windows reparse point."""
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_point)


def _open_parent_nofollow(path: Path, *, create: bool) -> int:
    """Hold ``path.parent`` while rejecting every POSIX symlink component."""
    parent = path.parent.absolute()
    if os.name != "posix" or not getattr(os, "O_NOFOLLOW", 0):
        return _open_windows_parent_nofollow(parent, create=create)
    flags = dirfd_mod.directory_flags()
    root_flags = dirfd_mod.root_directory_flags()
    descriptor = os.open(parent.anchor, root_flags)
    try:
        for component in parent.parts[1:]:
            if component in {"", ".", ".."}:
                raise OSError("unsafe output directory")
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o755, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_windows_parent_nofollow(parent: Path, *, create: bool) -> int:
    """Hold a Windows parent directory through the reparse-point-safe adapter."""
    if sys.platform != "win32":
        raise OSError("no-follow directory operations are unavailable")
    from .work_cmd import nt_dirfd

    anchor = Path(parent.anchor)
    try:
        relative_parts = parent.relative_to(anchor).parts
    except ValueError:
        raise OSError("unsafe output directory") from None
    if not relative_parts:
        descriptor = nt_dirfd.open_root_directory(anchor, writable=create)
        return descriptor
    descriptor = nt_dirfd.open_root_directory(anchor, writable=False)
    try:
        for index, component in enumerate(relative_parts):
            want_writable = bool(create and index == len(relative_parts) - 1)
            try:
                child = nt_dirfd.open_child_directory(descriptor, component, writable=want_writable)
            except FileNotFoundError:
                if not create:
                    raise
                _mkdir_windows_child(descriptor, component)
                child = nt_dirfd.open_child_directory(descriptor, component, writable=want_writable)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _mkdir_windows_child(parent_fd: int, component: str) -> None:
    """Create one Windows child under the already-held parent descriptor.

    The held handle is reopened with mutation access through
    ``nt_dirfd.reopen_directory_writable`` (NtCreateFile with RootDirectory set
    to the held descriptor and an empty ObjectName), so the mkdir parent is the
    same directory object the caller holds instead of a fresh re-walk of the
    prefix by name from the anchor. The reopened handle is closed immediately.
    Reparse rejection still fires on every open, so a remaining failure lands
    as an error rather than a traversal.
    """
    from .work_cmd import nt_dirfd

    nt_dirfd.validate_component(component)
    mkdir_parent = nt_dirfd.reopen_directory_writable(parent_fd)
    try:
        nt_dirfd.mkdir_child(mkdir_parent, component)
    finally:
        os.close(mkdir_parent)


def _read_regular_text(path: Path, *, max_bytes: int | None = None) -> str:
    """Read one regular file without traversing a symlink or reparse point.

    The open never blocks on a FIFO. With ``max_bytes`` a larger file is refused.
    """
    parent = _open_parent_nofollow(path, create=False)
    descriptor = -1
    try:
        if os.name == "posix":
            descriptor = os.open(
                path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent,
            )
        else:
            from .work_cmd import nt_dirfd

            descriptor = nt_dirfd.open_file(parent, path.name, os.O_RDONLY)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("output is not a regular file")
        if max_bytes is not None and metadata.st_size > max_bytes:
            raise OSError("file is too large")
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            data = handle.read() if max_bytes is None else handle.read(max_bytes + 1)
        if max_bytes is not None and len(data) > max_bytes:
            raise OSError("file is too large")
        return data.decode("utf-8")
    finally:
        if descriptor != -1:
            os.close(descriptor)
        os.close(parent)


def _write_text_nofollow_atomic(
    path: Path, data: str, *, mode: int, replace_symlink: bool = False, replace: bool = True
) -> None:
    """Atomically publish a regular file without following its destination link."""
    parent = _open_parent_nofollow(path, create=True)
    temporary = f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        if os.name == "posix":
            descriptor = os.open(temporary, flags | getattr(os, "O_NOFOLLOW", 0), mode, dir_fd=parent)
        else:
            from .work_cmd import nt_dirfd

            descriptor = nt_dirfd.open_file(parent, temporary, flags, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            if os.name == "posix":
                os.fchmod(handle.fileno(), mode)
        if os.name == "posix" and replace:
            try:
                existing = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and stat.S_ISLNK(existing.st_mode) and not replace_symlink:
                raise OSError("output is a symlink")
            if existing is not None and not (stat.S_ISREG(existing.st_mode) or stat.S_ISLNK(existing.st_mode)):
                raise OSError("output is not a regular file")
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        elif os.name == "posix":
            os.link(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.unlink(temporary, dir_fd=parent)
            os.fsync(parent)
        else:
            from .work_cmd import nt_dirfd

            if replace and not replace_symlink and _path_is_symlink(path):
                raise OSError("output is a reparse point")
            nt_dirfd.replace_children(parent, temporary, path.name, replace=replace)
    except BaseException:
        try:
            if os.name == "posix":
                os.unlink(temporary, dir_fd=parent)
            else:
                from .work_cmd import nt_dirfd

                nt_dirfd.unlink_child(parent, temporary)
        except FileNotFoundError:
            pass
        raise
    finally:
        if descriptor != -1:
            os.close(descriptor)
        os.close(parent)
