"""Handle-relative no-follow directory operations for Windows.

POSIX import-inbox publication uses ``openat`` with ``O_NOFOLLOW|O_DIRECTORY``
and ``renameat`` so a parent cannot be swapped for a symlink mid-write.
Windows has no ``dir_fd`` / ``O_NOFOLLOW``. This module provides the same
containment with ``CreateFileW`` / ``NtCreateFile`` plus
``FILE_FLAG_OPEN_REPARSE_POINT`` / ``FILE_OPEN_REPARSE_POINT``: each component
is opened as itself, and a junction or symlink is rejected instead of followed.

Handles are exported as Python fds via ``msvcrt.open_osfhandle`` so callers can
``os.fstat`` / ``os.read`` / ``os.write`` / ``os.close`` them the same way.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_DELETE = 0x00010000
_SYNCHRONIZE = 0x00100000
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_LIST_DIRECTORY = 0x0001
_FILE_ADD_FILE = 0x0002
_FILE_ADD_SUBDIRECTORY = 0x0004
_FILE_TRAVERSE = 0x0020
_FILE_WRITE_DATA = 0x0002
_FILE_APPEND_DATA = 0x0004

_FILE_OPEN = 0x00000001
_FILE_CREATE = 0x00000002
_FILE_DIRECTORY_FILE = 0x00000001
_FILE_NON_DIRECTORY_FILE = 0x00000040
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_OPEN_REPARSE_POINT = 0x00200000

_OBJ_CASE_INSENSITIVE = 0x00000040

_FileRenameInformation = 10
_FileDispositionInformation = 13

_STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
_STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A
_STATUS_OBJECT_NAME_COLLISION = 0xC0000035
_STATUS_ACCESS_DENIED = 0xC0000022
_STATUS_OBJECT_NAME_INVALID = 0xC0000033
_STATUS_OBJECT_PATH_INVALID = 0xC0000039
_STATUS_NOT_A_DIRECTORY = 0xC0000103
_STATUS_FILE_IS_A_DIRECTORY = 0xC00000BA
_STATUS_STOPPED_ON_SYMLINK = 0x8000002D
_STATUS_IO_REPARSE_TAG_NOT_HANDLED = 0xC0000279
_STATUS_REPARSE_POINT_NOT_RESOLVED = 0xC0000280

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_DIRECTORY_ACCESS = (
    _FILE_LIST_DIRECTORY
    | _FILE_ADD_FILE
    | _FILE_ADD_SUBDIRECTORY
    | _FILE_TRAVERSE
    | _FILE_READ_ATTRIBUTES
    | _DELETE
    | _SYNCHRONIZE
)
_SHARE_ALL = _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class _OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", wintypes.LPVOID),
        ("SecurityQualityOfService", wintypes.LPVOID),
    ]


class _IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [
        ("Status", ctypes.c_void_p),
        ("Information", ctypes.c_void_p),
    ]


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


class _FILE_DISPOSITION_INFORMATION(ctypes.Structure):
    _fields_ = [("DeleteFile", wintypes.BOOLEAN)]


def _make_unicode(name: str) -> tuple[_UNICODE_STRING, Any]:
    buf = ctypes.create_unicode_buffer(name)
    us = _UNICODE_STRING()
    us.Length = len(name) * 2
    us.MaximumLength = len(buf) * 2
    us.Buffer = ctypes.cast(buf, wintypes.LPWSTR)
    return us, buf


def _make_rename_class(nchars: int) -> type[ctypes.Structure]:
    class FILE_RENAME_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", wintypes.BOOLEAN),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.ULONG),
            ("FileName", wintypes.WCHAR * max(nchars, 1)),
        ]

    return FILE_RENAME_INFORMATION


def available() -> bool:
    """Return whether handle-relative no-follow operations can be used here."""
    return sys.platform == "win32"


def validate_component(name: str) -> str:
    """Reject empty, dotted, or separator-bearing names so walks stay contained."""
    if not isinstance(name, str) or not name:
        raise OSError("path component is empty")
    if name in {".", ".."}:
        raise OSError("path component is not contained")
    if "/" in name or "\\" in name or ":" in name or "\x00" in name:
        raise OSError("path component is not a single contained name")
    return name


def open_root_directory(path: Path | str) -> int:
    """Open ``path`` as a directory without following a final reparse point."""
    api = _require_api()
    handle = api.CreateFileW(
        _wide_path(path),
        _DIRECTORY_ACCESS,
        _SHARE_ALL,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if _is_invalid_handle(handle):
        raise _win_error()
    try:
        _reject_reparse(api, handle, expected_directory=True)
        return _handle_to_fd(api, handle, os.O_RDONLY)
    except BaseException:
        api.CloseHandle(handle)
        raise


def open_child_directory(parent: int, name: str) -> int:
    """Open ``name`` under ``parent`` as a directory without following a reparse point."""
    api = _require_api()
    handle = _nt_create(
        api,
        parent,
        name,
        access=_DIRECTORY_ACCESS,
        disposition=_FILE_OPEN,
        options=_FILE_DIRECTORY_FILE | _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT,
        attributes=_FILE_ATTRIBUTE_DIRECTORY,
    )
    try:
        _reject_reparse(api, handle, expected_directory=True)
        return _handle_to_fd(api, handle, os.O_RDONLY)
    except BaseException:
        api.CloseHandle(handle)
        raise


def mkdir_child(parent: int, name: str) -> None:
    """Create ``name`` as a real directory under the held parent handle."""
    api = _require_api()
    handle = _nt_create(
        api,
        parent,
        name,
        access=_DIRECTORY_ACCESS,
        disposition=_FILE_CREATE,
        options=_FILE_DIRECTORY_FILE | _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT,
        attributes=_FILE_ATTRIBUTE_DIRECTORY,
    )
    try:
        _reject_reparse(api, handle, expected_directory=True)
    finally:
        api.CloseHandle(handle)


def open_file(parent: int, name: str, flags: int, mode: int = 0o600) -> int:
    """Open or create ``name`` under ``parent`` without following a reparse point."""
    del mode
    api = _require_api()
    write = bool(flags & (os.O_WRONLY | os.O_RDWR))
    create = bool(flags & os.O_CREAT)
    exclusive = bool(flags & os.O_EXCL)
    if create and exclusive:
        disposition = _FILE_CREATE
    elif create:
        raise OSError("non-exclusive create is not used by import inbox publication")
    else:
        disposition = _FILE_OPEN
    access = _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
    if write:
        access |= _GENERIC_WRITE | _DELETE | _FILE_WRITE_DATA | _FILE_APPEND_DATA
    else:
        access |= _GENERIC_READ
    handle = _nt_create(
        api,
        parent,
        name,
        access=access,
        disposition=disposition,
        options=_FILE_NON_DIRECTORY_FILE | _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT,
        attributes=_FILE_ATTRIBUTE_NORMAL,
    )
    fd_flags = os.O_WRONLY if write and not (flags & os.O_RDWR) else os.O_RDONLY
    if flags & os.O_RDWR:
        fd_flags = os.O_RDWR
    try:
        _reject_reparse(api, handle, expected_directory=False)
        return _handle_to_fd(api, handle, fd_flags)
    except BaseException:
        api.CloseHandle(handle)
        raise


def replace_children(parent: int, source: str, destination: str) -> None:
    """Rename ``source`` to ``destination`` relative to the held parent handle."""
    api = _require_api()
    validate_component(source)
    validate_component(destination)
    handle = _nt_create(
        api,
        parent,
        source,
        access=_DELETE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        disposition=_FILE_OPEN,
        options=_FILE_NON_DIRECTORY_FILE | _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT,
        attributes=_FILE_ATTRIBUTE_NORMAL,
    )
    try:
        _reject_reparse(api, handle, expected_directory=False)
        info, length = _rename_information(api, parent, destination, replace=True)
        _nt_set_info(api, handle, info, length, _FileRenameInformation)
    finally:
        api.CloseHandle(handle)


def unlink_child(parent: int, name: str) -> None:
    """Unlink ``name`` relative to the held parent without following a reparse point."""
    api = _require_api()
    handle = _nt_create(
        api,
        parent,
        name,
        access=_DELETE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        disposition=_FILE_OPEN,
        options=_FILE_NON_DIRECTORY_FILE | _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT,
        attributes=_FILE_ATTRIBUTE_NORMAL,
    )
    try:
        _reject_reparse(api, handle, expected_directory=False)
        info = api.FILE_DISPOSITION_INFORMATION()
        info.DeleteFile = True
        _nt_set_info(api, handle, ctypes.byref(info), ctypes.sizeof(info), _FileDispositionInformation)
    finally:
        api.CloseHandle(handle)


def stat_child(parent: int, name: str) -> os.stat_result:
    """Return ``lstat``-equivalent metadata for ``name`` under the held parent."""
    descriptor = open_file(parent, name, os.O_RDONLY)
    try:
        return os.fstat(descriptor)
    finally:
        os.close(descriptor)


def _require_api() -> Any:
    if sys.platform != "win32":
        raise OSError("Windows handle-relative directory operations are unavailable")
    return _api()


def _wide_path(path: Path | str) -> str:
    text = os.fspath(path)
    if text.startswith("\\\\?\\") or text.startswith("\\\\.\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def _is_invalid_handle(handle: Any) -> bool:
    if handle is None:
        return True
    try:
        value = int(handle)
    except (TypeError, ValueError):
        return True
    return value in {0, -1} or value == _INVALID_HANDLE_VALUE


def _win_error() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]


def _handle_to_fd(api: Any, handle: Any, flags: int) -> int:
    import msvcrt

    raw = int(handle)
    try:
        return msvcrt.open_osfhandle(raw, flags)  # type: ignore[attr-defined]
    except OSError:
        api.CloseHandle(handle)
        raise


def _handle_from_fd(fd: int) -> int:
    import msvcrt

    return int(msvcrt.get_osfhandle(fd))  # type: ignore[attr-defined]


def _reject_reparse(api: Any, handle: Any, *, expected_directory: bool) -> None:
    info = api.BY_HANDLE_FILE_INFORMATION()
    if not api.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise _win_error()
    attributes = int(info.dwFileAttributes)
    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise OSError("path component is a reparse point")
    is_directory = bool(attributes & _FILE_ATTRIBUTE_DIRECTORY)
    if expected_directory and not is_directory:
        raise OSError("path component is not a directory")
    if not expected_directory and is_directory:
        raise OSError("path component is a directory")


def _nt_create(
    api: Any,
    parent: int,
    name: str,
    *,
    access: int,
    disposition: int,
    options: int,
    attributes: int,
) -> Any:
    validate_component(name)
    unicode_name, buffer = api.make_unicode(name)
    obj = api.OBJECT_ATTRIBUTES()
    obj.Length = ctypes.sizeof(api.OBJECT_ATTRIBUTES)
    obj.RootDirectory = _handle_from_fd(parent)
    obj.ObjectName = ctypes.pointer(unicode_name)
    obj.Attributes = _OBJ_CASE_INSENSITIVE
    obj.SecurityDescriptor = None
    obj.SecurityQualityOfService = None
    handle = ctypes.c_void_p()
    iosb = api.IO_STATUS_BLOCK()
    status = api.NtCreateFile(
        ctypes.byref(handle),
        access,
        ctypes.byref(obj),
        ctypes.byref(iosb),
        None,
        attributes,
        _SHARE_ALL,
        disposition,
        options,
        None,
        0,
    )
    del buffer
    _raise_ntstatus(status)
    if _is_invalid_handle(handle.value):
        raise OSError("NtCreateFile returned an invalid handle")
    return handle.value


def _nt_set_info(api: Any, handle: Any, info: Any, length: int, klass: int) -> None:
    iosb = api.IO_STATUS_BLOCK()
    status = api.NtSetInformationFile(handle, ctypes.byref(iosb), info, length, klass)
    _raise_ntstatus(status)


def _rename_information(api: Any, parent: int, destination: str, *, replace: bool) -> tuple[Any, int]:
    encoded = destination.encode("utf-16-le")
    nchars = len(destination)
    rename_class = api.make_rename_class(nchars)
    info = rename_class()
    info.ReplaceIfExists = bool(replace)
    info.RootDirectory = _handle_from_fd(parent)
    info.FileNameLength = len(encoded)
    info.FileName = destination
    return ctypes.byref(info), ctypes.sizeof(info)


def _raise_ntstatus(status: int) -> None:
    code = status & 0xFFFFFFFF
    if status >= 0 and code != _STATUS_STOPPED_ON_SYMLINK:
        return
    if code in {_STATUS_OBJECT_NAME_NOT_FOUND, _STATUS_OBJECT_PATH_NOT_FOUND}:
        raise FileNotFoundError("path component does not exist")
    if code == _STATUS_OBJECT_NAME_COLLISION:
        raise FileExistsError("path component already exists")
    if code == _STATUS_ACCESS_DENIED:
        raise PermissionError("path component access denied")
    if code in {_STATUS_OBJECT_NAME_INVALID, _STATUS_OBJECT_PATH_INVALID}:
        raise OSError("path component is not a single contained name")
    if code == _STATUS_NOT_A_DIRECTORY:
        raise NotADirectoryError("path component is not a directory")
    if code == _STATUS_FILE_IS_A_DIRECTORY:
        raise IsADirectoryError("path component is a directory")
    if code in {_STATUS_STOPPED_ON_SYMLINK, _STATUS_IO_REPARSE_TAG_NOT_HANDLED, _STATUS_REPARSE_POINT_NOT_RESOLVED}:
        raise OSError("path component is a reparse point")
    raise OSError(f"Windows no-follow directory operation failed: NTSTATUS=0x{code:08X}")


def _bind_api_namespace(*, kernel32: Any = None, ntdll: Any = None) -> SimpleNamespace:
    """Bind NT structures and optional DLL entry points.

    Class-body assignment of enclosing locals raises ``NameError`` on Windows
    (``OBJECT_ATTRIBUTES = OBJECT_ATTRIBUTES`` inside a nested class). Keep the
    binder as a SimpleNamespace so the Windows path can load.
    """
    if kernel32 is not None:
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
        ]
        kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    if ntdll is not None:
        ntdll.NtCreateFile.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            ctypes.POINTER(_OBJECT_ATTRIBUTES),
            ctypes.POINTER(_IO_STATUS_BLOCK),
            wintypes.LPVOID,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.LPVOID,
            wintypes.ULONG,
        ]
        ntdll.NtCreateFile.restype = ctypes.c_long
        ntdll.NtSetInformationFile.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_IO_STATUS_BLOCK),
            wintypes.LPVOID,
            wintypes.ULONG,
            wintypes.ULONG,
        ]
        ntdll.NtSetInformationFile.restype = ctypes.c_long
    return SimpleNamespace(
        OBJECT_ATTRIBUTES=_OBJECT_ATTRIBUTES,
        IO_STATUS_BLOCK=_IO_STATUS_BLOCK,
        BY_HANDLE_FILE_INFORMATION=_BY_HANDLE_FILE_INFORMATION,
        FILE_DISPOSITION_INFORMATION=_FILE_DISPOSITION_INFORMATION,
        CreateFileW=None if kernel32 is None else kernel32.CreateFileW,
        CloseHandle=None if kernel32 is None else kernel32.CloseHandle,
        GetFileInformationByHandle=None if kernel32 is None else kernel32.GetFileInformationByHandle,
        NtCreateFile=None if ntdll is None else ntdll.NtCreateFile,
        NtSetInformationFile=None if ntdll is None else ntdll.NtSetInformationFile,
        make_unicode=_make_unicode,
        make_rename_class=_make_rename_class,
    )


def _api() -> Any:
    global _API
    if _API is not None:
        return _API
    if sys.platform != "win32":
        raise OSError("Windows handle-relative directory operations are unavailable")
    import msvcrt  # noqa: F401  # fail closed if the CRT helper is missing

    _API = _bind_api_namespace(
        kernel32=ctypes.WinDLL("kernel32", use_last_error=True),
        ntdll=ctypes.WinDLL("ntdll"),
    )
    return _API


_API: Any = None
