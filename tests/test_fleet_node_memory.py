"""The node memory-used reading: never raises, never reports an impossible fraction (#2710)."""

from __future__ import annotations

import builtins
import ctypes
import io
import sys
import types

import pytest

from brigade import fleet_node_memory

MEMINFO = """MemTotal:       16000000 kB
MemFree:          500000 kB
MemAvailable:    4000000 kB
Buffers:          100000 kB
"""


def test_linux_text_is_one_minus_available_over_total():
    assert fleet_node_memory.parse_meminfo(MEMINFO) == 0.75


@pytest.mark.parametrize(
    "text",
    [
        "",
        "MemTotal: 100 kB\n",  # no MemAvailable (an old kernel)
        "MemAvailable: 100 kB\n",
        "MemTotal: 0 kB\nMemAvailable: 0 kB\n",
        "MemTotal: -5 kB\nMemAvailable: 1 kB\n",
        "MemTotal: lots kB\nMemAvailable: 1 kB\n",
        "MemTotal:\nMemAvailable: 1 kB\n",
        "MemTotal: 100 kB\nMemAvailable: -1 kB\n",
    ],
)
def test_malformed_or_impossible_text_is_none(text):
    assert fleet_node_memory.parse_meminfo(text) is None


def test_available_above_total_clamps_to_zero_and_the_result_is_rounded():
    assert fleet_node_memory.parse_meminfo("MemTotal: 100 kB\nMemAvailable: 150 kB\n") == 0.0
    assert fleet_node_memory.parse_meminfo("MemTotal: 3 kB\nMemAvailable: 1 kB\n") == 0.6667
    assert fleet_node_memory.parse_meminfo("MemTotal: 100 kB\nMemAvailable: 0 kB\n") == 1.0


@pytest.mark.parametrize(("total", "available"), [(float("nan"), 1.0), (1.0, float("inf")), (float("inf"), 1.0)])
def test_non_finite_inputs_are_refused(total, available):
    assert fleet_node_memory._fraction(total, available) is None


def test_memory_used_reads_proc_meminfo_on_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    seen = []

    def fake_open(path, *args, **kwargs):
        seen.append(path)
        return io.StringIO(MEMINFO)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert fleet_node_memory.memory_used() == 0.75
    assert seen == ["/proc/meminfo"]


def test_an_unreadable_source_is_none_never_an_exception(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    def refuse(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(builtins, "open", refuse)
    assert fleet_node_memory.memory_used() is None


@pytest.mark.parametrize("platform", ["darwin", "freebsd14", "sunos5", "cygwin"])
def test_an_unsupported_platform_is_none(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    assert fleet_node_memory.memory_used() is None


def _fake_windll(monkeypatch, *, total, available, ok=1, seen=None):
    """A stand-in for ctypes.windll whose GlobalMemoryStatusEx fills the struct it is handed."""

    def global_memory_status_ex(reference):
        status = reference._obj
        if seen is not None:
            seen.append(status.dwLength)
        status.ullTotalPhys = total
        status.ullAvailPhys = available
        return ok

    kernel32 = types.SimpleNamespace(GlobalMemoryStatusEx=global_memory_status_ex)
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(kernel32=kernel32), raising=False)


def test_windows_uses_global_memory_status_ex_with_the_struct_length_set(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    lengths: list[int] = []
    _fake_windll(monkeypatch, total=16 * 1024**3, available=4 * 1024**3, seen=lengths)
    assert fleet_node_memory.memory_used() == 0.75
    assert lengths == [64]  # MEMORYSTATUSEX is 64 bytes; GlobalMemoryStatusEx fails if dwLength is unset


def test_windows_failure_or_zero_total_is_none(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    _fake_windll(monkeypatch, total=1, available=1, ok=0)
    assert fleet_node_memory.memory_used() is None
    _fake_windll(monkeypatch, total=0, available=0)
    assert fleet_node_memory.memory_used() is None


def test_windows_api_error_is_none(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    def boom(reference):
        raise OSError("no kernel32")

    kernel32 = types.SimpleNamespace(GlobalMemoryStatusEx=boom)
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(kernel32=kernel32), raising=False)
    assert fleet_node_memory.memory_used() is None


def test_the_live_reading_on_this_host_is_none_or_a_valid_fraction():
    value = fleet_node_memory.memory_used()
    assert value is None or (isinstance(value, float) and 0.0 <= value <= 1.0)
