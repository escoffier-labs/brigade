"""Windows pid liveness queries a process handle, never ``os.kill(pid, 0)``.

``signal.CTRL_C_EVENT`` is 0 on Windows, so CPython turns ``os.kill(pid, 0)``
into ``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)``. A live process in another
console answers ``ERROR_INVALID_PARAMETER`` (87), which reads as "not active"
and lets a second writer claim a live holder's lock. Issue #2287.

The same lock path needs its metadata reads to share delete access: Windows
refuses to rename a directory while a handle inside it lacks
``FILE_SHARE_DELETE``, and ``runguard`` releases a lock by renaming it.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from brigade import nt_process, runguard

STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87
ERROR_FILE_NOT_FOUND = 2


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


class _Entry:
    """Win32 callable that also accepts the ``argtypes``/``restype`` assignments."""

    def __init__(self, func: Callable[..., Any]) -> None:
        self._func = func
        self.argtypes: Any = None
        self.restype: Any = None

    def __call__(self, *args: Any) -> Any:
        return self._func(*args)


class BindingKernel32:
    """Kernel32 stand-in that keeps the bindings ``load_kernel32`` installs."""

    def __init__(self, *, opens: bool = True, create_handle: int = 0xC0FFEE) -> None:
        self.opens = opens
        self.create_handle = create_handle
        self.open_calls: list[tuple[int, bool, int]] = []
        self.create_calls: list[tuple[Any, ...]] = []
        self.close_calls: list[int] = []
        self.OpenProcess = _Entry(self._open_process)  # noqa: N815 - Win32 name
        self.GetExitCodeProcess = _Entry(self._exit_code)  # noqa: N815 - Win32 name
        self.CreateFileW = _Entry(self._create_file)  # noqa: N815 - Win32 name
        self.CloseHandle = _Entry(self._close_handle)  # noqa: N815 - Win32 name

    def _open_process(self, access: int, inherit: bool, pid: int) -> int | None:
        self.open_calls.append((access, inherit, pid))
        return 0xC0FFEE if self.opens else None

    def _exit_code(self, handle: int, code: Any) -> int:
        code._obj.value = STILL_ACTIVE
        return 1

    def _create_file(self, *args: Any) -> int | None:
        self.create_calls.append(args)
        return self.create_handle

    def _close_handle(self, handle: int) -> int:
        self.close_calls.append(handle)
        return 1


class _NtOs:
    """``os`` stand-in for ``runguard.os`` that reports nt without switching pathlib."""

    name = "nt"

    def __getattr__(self, item: str) -> Any:
        return getattr(os, item)


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


def test_kernel32_binding_is_loaded_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """A contended acquirer probes in a retry loop; the loader must run once."""
    fake = BindingKernel32()
    loads: list[Any] = []

    def fake_windll(*args: Any, **kwargs: Any) -> BindingKernel32:
        loads.append((args, kwargs))
        return fake

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(nt_process.ctypes, "WinDLL", fake_windll, raising=False)
    monkeypatch.setattr(nt_process, "_kernel32", None)

    for _ in range(25):
        assert nt_process.pid_is_active(4242) is True
    assert len(loads) == 1, "the binding must be built once, not per probe"
    assert len(fake.open_calls) == 25
    assert fake.OpenProcess.argtypes is not None, "the cached binding keeps its argtypes"
    assert nt_process.load_kernel32() is fake
    assert len(loads) == 1, "a second lookup must reuse the cached binding"


def test_lock_metadata_read_shares_delete_so_the_holder_can_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows denies a directory rename while an inside handle lacks delete sharing."""
    target = tmp_path / "pid"
    target.write_text("4242\n", encoding="utf-8")
    fake = BindingKernel32()
    descriptors: list[int] = []

    class FakeMsvcrt:
        def open_osfhandle(self, handle: int, flags: int) -> int:
            assert handle == fake.create_handle
            assert flags == os.O_RDONLY
            descriptor = os.open(target, os.O_RDONLY)
            descriptors.append(descriptor)
            return descriptor

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(nt_process, "load_kernel32", lambda: fake)
    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt())

    assert nt_process.read_text_sharing_delete(target) == "4242\n"

    name, access, share, security, disposition, flags, template = fake.create_calls[0]
    assert access == GENERIC_READ
    assert share == FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, (
        "without FILE_SHARE_DELETE a reader blocks the holder's release rename"
    )
    assert disposition == OPEN_EXISTING
    assert security is None and template is None
    assert name == "\\\\?\\" + os.path.abspath(str(target)), "long paths must still open"
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


@pytest.mark.parametrize(
    ("code", "expected"),
    [(ERROR_FILE_NOT_FOUND, FileNotFoundError), (ERROR_ACCESS_DENIED, PermissionError)],
    ids=["missing", "denied"],
)
def test_shared_read_raises_the_plain_read_error_for_a_missing_or_denied_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, expected: type[OSError]
) -> None:
    fake = BindingKernel32(create_handle=0)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(nt_process, "load_kernel32", lambda: fake)
    monkeypatch.setattr(nt_process, "_last_error", lambda: code)

    with pytest.raises(expected):
        nt_process.read_text_sharing_delete(tmp_path / "pid")


def test_shared_read_falls_back_to_a_plain_read_on_an_unexpected_open_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binding problem must never change a lock-liveness verdict."""
    target = tmp_path / "owner.json"
    target.write_text('{"pid": 7}', encoding="utf-8")
    fake = BindingKernel32(create_handle=0)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(nt_process, "load_kernel32", lambda: fake)
    monkeypatch.setattr(nt_process, "_last_error", lambda: 1)

    assert nt_process.read_text_sharing_delete(target) == '{"pid": 7}'


def test_shared_read_closes_the_handle_when_the_descriptor_wrap_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "pid"
    target.write_text("4242\n", encoding="utf-8")
    fake = BindingKernel32()

    class FakeMsvcrt:
        def open_osfhandle(self, handle: int, flags: int) -> int:
            raise OSError(errno.EMFILE, "too many open files")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(nt_process, "load_kernel32", lambda: fake)
    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt())

    assert nt_process.read_text_sharing_delete(target) == "4242\n"
    assert fake.close_calls == [fake.create_handle]


def test_shared_read_delegates_to_a_plain_read_off_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "pid"
    target.write_text("9\n", encoding="utf-8")
    monkeypatch.setattr(nt_process, "load_kernel32", lambda: pytest.fail("POSIX reads must not touch Win32"))

    assert nt_process.read_text_sharing_delete(target) == "9\n"


def test_windows_branch_waits_for_a_live_same_process_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Threads of one process share a pid, and that pid is live, not stale.

    The concurrent-acquirer test passed on Windows only because ``os.kill``
    judged every holder dead there, so waiters reclaimed live locks. With a
    correct probe they must wait for the release.
    """
    lock_path = tmp_path / "tasks.json.lock"
    monkeypatch.setattr(runguard, "os", _NtOs())
    monkeypatch.setattr(nt_process, "load_kernel32", lambda: BindingKernel32())
    monkeypatch.setattr(nt_process, "_last_error", lambda: 0)
    ownership = runguard._publish_lock(lock_path, run_dir=None)
    try:
        assert runguard._pid_is_active(os.getpid()) is True
        assert runguard._claim_stale_lock(lock_path) is None, "a live same-process owner is not stale"
        with pytest.raises(runguard.RunLockError, match="another brigade run appears active"):
            runguard._acquire_lock(lock_path)
    finally:
        runguard._release_lock(lock_path, ownership)
    assert not lock_path.exists()
    second = runguard._acquire_lock(lock_path)
    runguard._release_lock(lock_path, second)
    assert not lock_path.exists()


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
