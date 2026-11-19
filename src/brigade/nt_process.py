"""Windows process liveness read from a query handle, never from ``os.kill``.

``signal.CTRL_C_EVENT`` is 0 on Windows, so CPython implements
``os.kill(pid, 0)`` as ``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)`` instead of
probing existence. A live process outside the caller's console answers
``ERROR_INVALID_PARAMETER`` (87), which reads exactly like "no such process" and
used to let a second writer judge a live lock holder stale (issue #2287).

``OpenProcess`` with ``PROCESS_QUERY_LIMITED_INFORMATION`` plus
``GetExitCodeProcess`` asks the kernel a different question and answers it for
any process this user may query, across consoles and sessions.

This module is imported on every platform; only :func:`pid_is_active` touches
Windows APIs, and it is called on the Windows code path alone.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Any

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
ERROR_INVALID_PARAMETER = 87


def load_kernel32() -> Any:
    """Return kernel32 with the liveness calls bound to explicit types."""
    if sys.platform != "win32":
        raise OSError("Windows process liveness is unavailable on this platform")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
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
