"""This node's memory-used fraction, for the T3 observer's health report (#2710).

``memory_used()`` returns ``1 - available/total`` in 0..1, rounded to four decimals, or None when
the platform has no supported source or any read fails. It has no dependencies and never raises:
a missing reading is simply left out of the report, and placement treats it as unknown.

Linux reads ``MemTotal`` and ``MemAvailable`` from ``/proc/meminfo``. Windows asks
``GlobalMemoryStatusEx`` through ctypes. Every other platform reports None. Add a platform here
only when a node on it needs to be placed by memory.
"""

from __future__ import annotations

import math
import sys

_MEMINFO = "/proc/meminfo"


def _fraction(total: float, available: float) -> float | None:
    """Used fraction from two byte counts, or None when they cannot describe a machine."""
    if not (math.isfinite(total) and math.isfinite(available)) or total <= 0 or available < 0:
        return None
    used = 1.0 - available / total
    if not math.isfinite(used):
        return None
    return round(min(1.0, max(0.0, used)), 4)


def parse_meminfo(text: str) -> float | None:
    """Used fraction from ``/proc/meminfo`` text. None when a field is missing or malformed."""
    values: dict[str, float] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name in ("MemTotal", "MemAvailable"):
            parts = rest.split()
            if not parts:
                return None
            try:
                values[name] = float(int(parts[0]))
            except ValueError:
                return None
    if "MemTotal" not in values or "MemAvailable" not in values:
        return None
    return _fraction(values["MemTotal"], values["MemAvailable"])


def _linux() -> float | None:
    with open(_MEMINFO, encoding="ascii", errors="replace") as handle:
        return parse_meminfo(handle.read())


def _windows() -> float | None:
    import ctypes

    class _MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_uint32),
            ("dwMemoryLoad", ctypes.c_uint32),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(_MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
        return None
    return _fraction(float(status.ullTotalPhys), float(status.ullAvailPhys))


def memory_used() -> float | None:
    """This machine's memory-used fraction in 0..1, or None. Never raises."""
    try:
        if sys.platform.startswith("linux"):
            return _linux()
        if sys.platform == "win32":
            return _windows()
    except Exception:
        return None
    return None
