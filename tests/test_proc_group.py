"""Owned-group liveness checks used by the scanner drain (#2286)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from brigade import proc, proc_group
from tests._child_publish import PUBLISH_SOURCE, process_is_running, wait_for_text

pytestmark = pytest.mark.skipif(not proc_group.drain_supported(), reason="requires /proc and waitid(WNOWAIT)")


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
