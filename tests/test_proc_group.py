"""Owned-group liveness checks used by the scanner drain (#2286)."""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from brigade import proc, proc_group
from tests._child_publish import PUBLISH_SOURCE, process_is_running, wait_for_text

pytestmark = pytest.mark.skipif(not proc_group.drain_supported(), reason="requires /proc and waitid(WNOWAIT)")
requires_pidfd = pytest.mark.skipif(
    not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"),
    reason="pidfd signalling is not available",
)


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def test_member_with_parenthesis_in_its_name_still_counts_as_live(tmp_path):
    """A comm holding ``)`` must not shift the parsed stat fields."""
    ready = tmp_path / "ready"
    code = (
        PUBLISH_SOURCE + "import ctypes, time\n"
        "ctypes.CDLL(None).prctl(15, b'a) q r', 0, 0, 0)\n"
        f"publish({str(ready)!r}, 'ok')\n"
        "time.sleep(60)\n"
    )
    process = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    try:
        assert wait_for_text(ready, timeout=10) == "ok"
        assert proc_group.group_has_live_members(process.pid) is True
    finally:
        _stop(process)


def test_descendant_that_leaves_the_group_still_counts_as_live(tmp_path):
    """A descendant that calls setpgid keeps the leader's session id."""
    pid_path = tmp_path / "pid"
    grandchild = PUBLISH_SOURCE + (
        f"import os, time\nos.setpgid(0, 0)\npublish({str(pid_path)!r}, str(os.getpid()))\ntime.sleep(60)\n"
    )
    leader = (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        f"p = Path({str(pid_path)!r})\n"
        "while not p.exists():\n"
        "    time.sleep(0.02)\n"
    )
    process = subprocess.Popen([sys.executable, "-c", leader], start_new_session=True)
    escaped = None
    try:
        published = wait_for_text(pid_path, timeout=10)
        assert published is not None
        escaped = int(published)
        proc_group.observe_leader_exit(process)
        assert proc_group.group_has_live_members(process.pid) is True
    finally:
        if escaped is not None:
            try:
                os.kill(escaped, 9)
            except OSError:
                pass
        _stop(process)


@requires_pidfd
def test_kill_group_members_stops_a_descendant_that_left_the_group(tmp_path):
    """The bounded stop reaches a setpgid descendant that a group kill cannot."""
    pid_path = tmp_path / "pid"
    grandchild = PUBLISH_SOURCE + (
        f"import os, time\nos.setpgid(0, 0)\npublish({str(pid_path)!r}, str(os.getpid()))\ntime.sleep(60)\n"
    )
    leader = (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        f"p = Path({str(pid_path)!r})\n"
        "while not p.exists():\n"
        "    time.sleep(0.02)\n"
    )
    process = subprocess.Popen([sys.executable, "-c", leader], start_new_session=True)
    escaped = None
    try:
        published = wait_for_text(pid_path, timeout=10)
        assert published is not None
        escaped = int(published)
        proc_group.observe_leader_exit(process)  # An unreaped zombie leader is not a live member.
        assert proc_group.group_member_pids(process.pid) == [escaped]
        assert proc_group.kill_group_members(process.pid, signal.SIGKILL) == 1
        deadline = time.monotonic() + 5
        while process_is_running(escaped) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not process_is_running(escaped)
    finally:
        if escaped is not None:
            try:
                os.kill(escaped, signal.SIGKILL)
            except OSError:
                pass
        _stop(process)


def test_group_member_pids_ignores_a_non_positive_group(monkeypatch):
    calls = []
    monkeypatch.setattr(proc_group.os, "listdir", lambda path: calls.append(path) or [])
    assert proc_group.group_member_pids(0) == []
    assert calls == []


def test_group_member_pids_reports_an_unreadable_process_table(monkeypatch):
    real_listdir = os.listdir

    def listdir(path):
        if str(path) == "/proc":
            raise PermissionError(errno.EACCES, "denied")
        return real_listdir(path)

    monkeypatch.setattr(proc_group.os, "listdir", listdir)
    assert proc_group.group_member_pids(4242) is None


@requires_pidfd
def test_kill_group_members_skips_a_pid_whose_starttime_changed(monkeypatch):
    """A pid recycled between the scan and the read must never be signalled."""
    fields = [b"S", b"1", b"4242", b"4242"] + [b"0"] * 15 + [b"222"]
    monkeypatch.setattr(proc_group, "group_member_pids", lambda pgid: [os.getpid()], raising=False)
    monkeypatch.setattr(proc_group, "_member_records", lambda pgid: [(os.getpid(), 111)], raising=False)
    monkeypatch.setattr(proc_group, "_stat_fields", lambda pid: list(fields), raising=False)
    sent = []
    monkeypatch.setattr(proc_group.os, "kill", lambda pid, sig: sent.append(("kill", pid, sig)))
    monkeypatch.setattr(
        proc_group.signal, "pidfd_send_signal", lambda fd, sig: sent.append(("pidfd", sig)), raising=False
    )
    assert proc_group.kill_group_members(4242, signal.SIGKILL) == 0
    assert sent == []


def test_a_stat_read_error_makes_the_group_unreadable(monkeypatch):
    """Only a vanished process may be skipped. A read error means the group is unknown, not empty."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    target = f"/proc/{process.pid}/stat"
    real_read_bytes = Path.read_bytes

    def read_bytes(self):
        if str(self) == target:
            raise PermissionError(errno.EACCES, "denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    try:
        assert proc_group.group_member_pids(process.pid) is None
        assert proc_group.group_has_live_members(process.pid) is True
        with pytest.raises(proc_group.ProcessGroupUnreadable):
            proc_group.kill_group_members(process.pid, signal.SIGKILL)
    finally:
        process.kill()
        process.wait(timeout=10)


def test_another_users_unreadable_stat_is_skipped(monkeypatch):
    """Under hidepid=1 another user's stat is unreadable. It cannot be an arm descendant, so skip it."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    target = f"/proc/{process.pid}/stat"
    real_read_bytes = Path.read_bytes
    real_stat = os.stat

    def read_bytes(self):
        if str(self) == target:
            raise PermissionError(errno.EACCES, "denied")
        return real_read_bytes(self)

    def fake_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if str(path) == f"/proc/{process.pid}":
            fields = list(result)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(proc_group.os, "stat", fake_stat)
    try:
        assert proc_group.group_member_pids(process.pid) == []
    finally:
        process.kill()
        process.wait(timeout=10)


def test_clean_drain_records_descendants_reaped():
    """A group observed empty is confirmed gone, so the receipt says reaped."""
    result = proc.run(
        [sys.executable, "-c", "print('done')"],
        timeout=20,
        supervise_group=True,
        group_drain_seconds=5,
    )

    assert result.code == 0
    assert result.incomplete_process_group is False
    assert result.descendants_reaped is True
