"""Platform-specific coverage for pid liveness call sites."""

import errno
import os

import pytest

from brigade import nt_process, update_cmd
from brigade.tools_cmd import runtimes
from brigade.work_cmd import verify_reaper


def _forbid_kill(pid: int, signal: int) -> None:
    raise AssertionError(f"os.kill({pid}, {signal}) must not probe liveness on Windows")


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        (lambda: update_cmd._pid_alive(4242), True),
        (lambda: runtimes._process_alive(4242), True),
        (lambda: [process.pid for process in verify_reaper._survivor_mocks({4242})], [4242]),
    ],
    ids=["update-lock", "tool-runtime", "verify-reaper"],
)
def test_windows_call_sites_use_nt_process(monkeypatch, probe, expected):
    calls: list[int] = []

    def active(pid: int) -> bool:
        calls.append(pid)
        return True

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(os, "kill", _forbid_kill)
    monkeypatch.setattr(nt_process, "pid_is_active", active)

    assert probe() == expected
    assert calls == [4242]


@pytest.mark.parametrize(
    ("error", "expected"),
    [(None, True), (ProcessLookupError(), False), (PermissionError(), True)],
    ids=["live", "missing", "permission-denied"],
)
def test_update_pid_alive_keeps_posix_semantics(monkeypatch, error, expected):
    calls: list[tuple[int, int]] = []

    def kill(pid: int, signal: int) -> None:
        calls.append((pid, signal))
        if error is not None:
            raise error

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(nt_process, "pid_is_active", lambda pid: pytest.fail("Windows probe used on POSIX"))

    assert update_cmd._pid_alive(4242) is expected
    assert calls == [(4242, 0)]


def test_update_pid_alive_keeps_other_posix_errors(monkeypatch):
    error = OSError(errno.EIO, "probe failed")
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "kill", lambda pid, signal: (_ for _ in ()).throw(error))

    with pytest.raises(OSError) as raised:
        update_cmd._pid_alive(4242)
    assert raised.value is error


@pytest.mark.parametrize("error", [ProcessLookupError(), PermissionError(), OSError(errno.EIO, "probe failed")])
def test_runtime_pid_alive_keeps_posix_errors_inactive(monkeypatch, error):
    calls: list[tuple[int, int]] = []

    def kill(pid: int, signal: int) -> None:
        calls.append((pid, signal))
        raise error

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "kill", kill)

    assert runtimes._process_alive(4242) is False
    assert calls == [(4242, 0)]


@pytest.mark.parametrize("error", [ProcessLookupError(), PermissionError(), OSError(errno.EIO, "probe failed")])
def test_reaper_survivors_keep_posix_errors_inactive(monkeypatch, error):
    calls: list[tuple[int, int]] = []

    def kill(pid: int, signal: int) -> None:
        calls.append((pid, signal))
        raise error

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "kill", kill)

    assert verify_reaper._survivor_mocks({4242}) == []
    assert calls == [(4242, 0)]
