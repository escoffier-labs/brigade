"""Windows pid liveness queries a process handle, never ``os.kill(pid, 0)``.

``signal.CTRL_C_EVENT`` is 0 on Windows, so CPython turns ``os.kill(pid, 0)``
into ``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)``. A live process in another
console answers ``ERROR_INVALID_PARAMETER`` (87), which reads as "not active"
and lets a second writer claim a live holder's lock. Issue #2287.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import time
from typing import Any

import pytest

from brigade import nt_process, runguard

STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87


class FakeKernel32:
    """Kernel32 stand-in so the Windows branch runs on any host."""

    def __init__(self, *, opens: bool, exit_code: int = STILL_ACTIVE) -> None:
        self.opens = opens
        self.exit_code = exit_code
        self.handle = 0xC0FFEE
        self.open_calls: list[tuple[int, bool, int]] = []
        self.exit_code_calls: list[int] = []
        self.close_calls: list[int] = []

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int | None:  # noqa: N802 - Win32 name
        self.open_calls.append((access, inherit, pid))
        return self.handle if self.opens else None

    def GetExitCodeProcess(self, handle: int, code: Any) -> int:  # noqa: N802 - Win32 name
        self.exit_code_calls.append(handle)
        code._obj.value = self.exit_code
        return 1

    def CloseHandle(self, handle: int) -> int:  # noqa: N802 - Win32 name
        self.close_calls.append(handle)
        return 1


def _forbidden_os_kill(pid: int, sig: object) -> None:
    raise AssertionError(f"os.kill({pid}, {sig!r}) must not probe liveness on Windows")


def _use_fake_windows(monkeypatch: pytest.MonkeyPatch, fake: FakeKernel32, last_error: int) -> None:
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(os, "kill", _forbidden_os_kill)
    monkeypatch.setattr(nt_process, "load_kernel32", lambda: fake)
    monkeypatch.setattr(nt_process, "_last_error", lambda: last_error)


@pytest.mark.parametrize(
    ("opens", "exit_code", "last_error", "expected"),
    [
        (True, STILL_ACTIVE, 0, True),
        (True, 1, 0, False),
        (False, 0, ERROR_INVALID_PARAMETER, False),
        (False, 0, ERROR_ACCESS_DENIED, True),
        (False, 0, 0, True),
    ],
    ids=["live", "exited", "invalid-pid", "access-denied", "unknown-error"],
)
def test_windows_liveness_opens_a_query_handle_instead_of_os_kill(
    monkeypatch: pytest.MonkeyPatch,
    opens: bool,
    exit_code: int,
    last_error: int,
    expected: bool,
) -> None:
    fake = FakeKernel32(opens=opens, exit_code=exit_code)
    _use_fake_windows(monkeypatch, fake, last_error)

    assert runguard._pid_is_active(4242) is expected

    assert fake.open_calls == [(PROCESS_QUERY_LIMITED_INFORMATION, False, 4242)]
    if opens:
        assert fake.exit_code_calls == [fake.handle], "the exit code must come from the opened handle"
        assert fake.close_calls == [fake.handle], "every opened handle must be closed"
    else:
        assert fake.exit_code_calls == []
        assert fake.close_calls == []


def test_windows_liveness_close_survives_a_failing_exit_code_query(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeKernel32(opens=True)

    def failing_exit_code(handle: int, code: Any) -> int:
        fake.exit_code_calls.append(handle)
        return 0

    monkeypatch.setattr(fake, "GetExitCodeProcess", failing_exit_code)
    _use_fake_windows(monkeypatch, fake, 0)

    assert runguard._pid_is_active(4242) is True, "an unreadable exit code fails closed to active"
    assert fake.close_calls == [fake.handle]


def test_windows_liveness_rejects_nonpositive_pids_without_opening_a_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeKernel32(opens=True, exit_code=STILL_ACTIVE)
    _use_fake_windows(monkeypatch, fake, 0)

    assert runguard._pid_is_active(0) is False
    assert runguard._pid_is_active(-1) is False
    assert fake.open_calls == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (None, True),
        (ProcessLookupError(), False),
        (PermissionError(), True),
        (OSError(errno.ESRCH, "no such process"), False),
        (OSError(errno.EPERM, "operation not permitted"), True),
    ],
    ids=["live", "process-lookup", "permission", "esrch", "eperm"],
)
def test_posix_liveness_still_uses_os_kill(
    monkeypatch: pytest.MonkeyPatch,
    error: OSError | None,
    expected: bool,
) -> None:
    calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))
        if error is not None:
            raise error

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(nt_process, "load_kernel32", lambda: pytest.fail("the Windows probe must not run on POSIX"))

    assert runguard._pid_is_active(4242) is expected
    assert calls == [(4242, 0)]


def _wait_until(predicate: Any, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


@pytest.mark.skipif(os.name != "nt", reason="needs a real Windows process in its own console")
def test_windows_holder_in_another_console_reads_active_until_killed() -> None:
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_until(lambda: runguard._pid_is_active(holder.pid), timeout=60), (
            "a live holder in another console must read as active"
        )
        holder.kill()
        holder.wait(timeout=60)
        assert _wait_until(lambda: not runguard._pid_is_active(holder.pid), timeout=60), (
            "an exited holder must read as inactive"
        )
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=60)
