"""Windows primitives the run lock needs: process liveness and share-delete files.

``signal.CTRL_C_EVENT`` is 0 on Windows, so CPython implements
``os.kill(pid, 0)`` as ``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)`` instead of
probing existence. A live process outside the caller's console answers
``ERROR_INVALID_PARAMETER`` (87), which reads exactly like "no such process" and
used to let a second writer judge a live lock holder stale (issue #2287).

The same lock path needs two more Windows behaviours (issue #2747). NTFS refuses
to rename a directory while any handle to a file inside it is open, even one
opened with ``FILE_SHARE_DELETE``, and ``runguard`` releases a lock by renaming
its directory while waiters poll ``pid`` and ``owner.json`` inside it. So those
reads go through :func:`read_text_sharing_delete`, which shares delete access,
and the release removes the names with :func:`delete_file_sharing_readers`, which
uses POSIX-semantics delete (Windows 10 1709 and later) so the unlink does not
wait for the last reader to close.

This module is imported on every platform; only these helpers touch Windows
APIs, and they run on the Windows code path alone.
"""

from __future__ import annotations

import ctypes
import errno
import locale
import os
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
ERROR_INVALID_PARAMETER = 87

GENERIC_READ = 0x80000000
DELETE = 0x00010000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

# FILE_INFO_BY_HANDLE_CLASS.FileDispositionInfoEx, and the flags that make it
# unlink the name immediately instead of waiting for the last open handle.
FILE_DISPOSITION_INFO_EX_CLASS = 21
FILE_DISPOSITION_FLAG_DELETE = 0x00000001
FILE_DISPOSITION_FLAG_POSIX_SEMANTICS = 0x00000002

ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_ACCESS_DENIED = 5
ERROR_SHARING_VIOLATION = 32
ERROR_INVALID_FUNCTION = 1
ERROR_NOT_SUPPORTED = 50
ERROR_CALL_NOT_IMPLEMENTED = 120

# SetFileInformationByHandle answers one of these when FileDispositionInfoEx
# cannot apply: ERROR_INVALID_PARAMETER before Windows 10 1709, and the others on
# filesystems without POSIX delete (FAT, some network shares). Nothing was
# removed in that case, which is what lets the caller fall back.
_POSIX_DELETE_UNSUPPORTED = frozenset(
    {ERROR_INVALID_FUNCTION, ERROR_INVALID_PARAMETER, ERROR_NOT_SUPPORTED, ERROR_CALL_NOT_IMPLEMENTED}
)
_SHARE_ALL = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
_BINARY = getattr(os, "O_BINARY", 0)
_HANDLE_SIZE = ctypes.sizeof(wintypes.HANDLE)
_INVALID_HANDLE_VALUE: int = 2 ** (_HANDLE_SIZE * 8) - 1

_kernel32: Any = None


class _FileDispositionInfoEx(ctypes.Structure):
    """``FILE_DISPOSITION_INFO_EX``: the flags for a ``FileDispositionInfoEx`` call."""

    _fields_ = [("Flags", wintypes.DWORD)]


def load_kernel32() -> Any:
    """Return kernel32 with the calls this module makes bound to explicit types.

    The binding is built once per process: a contended lock release retries near
    every millisecond, and each call to :func:`delete_file_sharing_readers` needs
    the library.
    """
    global _kernel32
    if sys.platform != "win32":
        raise OSError("Windows process primitives are unavailable on this platform")
    if _kernel32 is not None:
        return _kernel32
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.SetFileInformationByHandle.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32 = kernel32
    return kernel32


def _last_error() -> int:
    return ctypes.get_last_error()  # type: ignore[attr-defined]


def pid_is_active(pid: int) -> bool:
    """Whether ``pid`` names a live process, read through a query handle.

    Only ``ERROR_INVALID_PARAMETER`` (87) proves absence. ``ERROR_ACCESS_DENIED``
    (5) and every unrecognized open failure fail closed to active: judging a
    live holder dead admits a second writer, which is the bug this probe exists
    to prevent. A nonpositive pid cannot name a process, so it is answered
    before the call rather than through argument conversion.
    """
    if pid <= 0:
        return False
    kernel32 = load_kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return _last_error() != ERROR_INVALID_PARAMETER
    try:
        code = wintypes.DWORD(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def open_shared_delete(path: Path | str) -> int:
    """Open ``path`` for reading with every share mode and return the descriptor.

    A Windows reader that does not share delete access blocks the holder from
    unlinking that name (issue #2747), so lock metadata is always read through
    this open. Every error a plain ``os.open`` would raise is raised unchanged;
    anything else falls back to ``os.open``, because a binding problem must never
    change a lock-liveness verdict.
    """
    target = Path(path)
    if sys.platform != "win32":
        return os.open(target, os.O_RDONLY | _BINARY)
    try:
        return _shared_descriptor(target)
    except OSError:
        raise
    except Exception:  # noqa: BLE001 - the plain open is the pre-existing behavior
        return os.open(target, os.O_RDONLY | _BINARY)


def read_text_sharing_delete(path: Path | str) -> str:
    """Read ``path`` as text through :func:`open_shared_delete`."""
    descriptor = open_shared_delete(path)
    try:
        stream = os.fdopen(descriptor, "r", encoding=locale.getencoding())
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        return stream.read()


def delete_file_sharing_readers(path: Path | str) -> bool:
    """Delete ``path`` even while another handle holds it open.

    ``FileDispositionInfoEx`` with ``FILE_DISPOSITION_FLAG_DELETE`` and
    ``FILE_DISPOSITION_FLAG_POSIX_SEMANTICS`` unlinks the name immediately, so a
    reader that opened it through :func:`open_shared_delete` no longer keeps the
    directory entry occupied. Returns ``False`` when this Windows build or
    filesystem does not support that, in which case nothing was removed and the
    caller falls back to its earlier behaviour. The reparse point itself is
    opened, so deleting a link removes the link rather than its target.
    """
    target = Path(path)
    kernel32 = load_kernel32()
    handle = _open_handle(target, DELETE, flags=FILE_FLAG_OPEN_REPARSE_POINT)
    try:
        disposition = _FileDispositionInfoEx(FILE_DISPOSITION_FLAG_DELETE | FILE_DISPOSITION_FLAG_POSIX_SEMANTICS)
        deleted = kernel32.SetFileInformationByHandle(
            handle,
            FILE_DISPOSITION_INFO_EX_CLASS,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        )
        if deleted:
            return True
        code = _last_error()
        if code in _POSIX_DELETE_UNSUPPORTED:
            return False
        raise _windows_error(target, code)
    finally:
        kernel32.CloseHandle(handle)


def _shared_descriptor(path: Path) -> int:
    handle = _open_handle(path, GENERIC_READ)
    try:
        import msvcrt

        return msvcrt.open_osfhandle(handle, os.O_RDONLY | _BINARY)  # type: ignore[attr-defined]
    except BaseException:
        load_kernel32().CloseHandle(handle)
        raise


def _open_handle(path: Path, access: int, *, flags: int = 0) -> Any:
    kernel32 = load_kernel32()
    handle = kernel32.CreateFileW(_long_path(path), access, _SHARE_ALL, None, OPEN_EXISTING, flags, None)
    if not handle or handle == _INVALID_HANDLE_VALUE:
        raise _windows_error(path, _last_error())
    return handle


def _long_path(path: Path) -> str:
    """Absolute Windows path with the ``\\\\?\\`` prefix, so long paths still open."""
    text = os.path.abspath(str(path))
    if text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def _windows_error(path: Path | str, code: int) -> OSError:
    if code in {ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND}:
        error: OSError = FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
    elif code in {ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION}:
        error = PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
    else:
        error = OSError(errno.EIO, f"could not open {path}: Windows error {code}", str(path))
    error.winerror = code  # type: ignore[attr-defined]
    return error
