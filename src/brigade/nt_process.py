"""Windows primitives runguard needs from Win32: process liveness and lock reads.

``signal.CTRL_C_EVENT`` is 0 on Windows, so CPython implements
``os.kill(pid, 0)`` as ``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)`` instead of
probing existence. A live process outside the caller's console answers
``ERROR_INVALID_PARAMETER`` (87), which reads exactly like "no such process" and
used to let a second writer judge a live lock holder stale (issue #2287).

``OpenProcess`` with ``PROCESS_QUERY_LIMITED_INFORMATION`` plus
``GetExitCodeProcess`` asks the kernel a different question and answers it for
any process this user may query, across consoles and sessions.

``read_text_sharing_delete`` serves the same lock path: Windows refuses to
rename a directory while a handle inside it was opened without
``FILE_SHARE_DELETE``, and CPython opens files with read and write sharing only.
``runguard`` releases a lock by renaming its directory, so any waiter polling
``pid`` or ``owner.json`` with a plain read can hold that rename off, and a
release that cannot win in its retry budget strands the lock while its owner is
still alive.

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
ERROR_ACCESS_DENIED = 5
ERROR_FILE_NOT_FOUND = 2
ERROR_INVALID_PARAMETER = 87
ERROR_PATH_NOT_FOUND = 3

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_kernel32: Any = None


def load_kernel32() -> Any:
    """Return kernel32 with the calls this module makes bound to explicit types.

    The binding is built once per process. ``ctypes.WinDLL`` loads the library
    and the ``argtypes`` assignments resolve each entry point, and
    :func:`pid_is_active` runs in the retry loop of every acquirer contending
    for a lock, so paying that per probe puts a loader call in the hot path.
    """
    global _kernel32
    if sys.platform != "win32":
        raise OSError("Windows process liveness is unavailable on this platform")
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


def read_text_sharing_delete(path: Path | str) -> str:
    """Read ``path`` so the read never blocks a rename of its directory.

    Only Windows needs the special open. Every error a plain read would raise is
    raised unchanged; anything else falls back to ``Path.read_text``, because a
    binding problem must never change a liveness verdict.
    """
    target = Path(path)
    if sys.platform != "win32":
        return target.read_text()
    try:
        return _read_shared(target)
    except (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError):
        raise
    except Exception:  # noqa: BLE001 - the plain read is the pre-existing behavior
        return target.read_text()


def _read_shared(path: Path) -> str:
    kernel32 = load_kernel32()
    handle = kernel32.CreateFileW(
        _long_path(path),
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if not handle or handle == _INVALID_HANDLE_VALUE:
        raise _open_error(path, _last_error())
    try:
        import msvcrt

        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)  # type: ignore[attr-defined]
    except Exception:
        kernel32.CloseHandle(handle)
        raise
    try:
        stream = os.fdopen(descriptor, "r", encoding=locale.getencoding())
    except Exception:
        os.close(descriptor)
        raise
    with stream:
        return stream.read()


def _long_path(path: Path) -> str:
    """Absolute Windows path with the ``\\\\?\\`` prefix, so long paths still open."""
    text = os.path.abspath(str(path))
    if text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def _open_error(path: Path | str, code: int) -> OSError:
    if code in {ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND}:
        return FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
    if code == ERROR_ACCESS_DENIED:
        return PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
    error = OSError(errno.EIO, f"could not open {path}: Windows error {code}", str(path))
    error.winerror = code  # type: ignore[attr-defined]
    return error
