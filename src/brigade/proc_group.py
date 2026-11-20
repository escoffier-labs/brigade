"""Owned-process-group draining for supervised children.

A supervised run can leave descendants that are still finishing the run's own
output when the direct child exits. Draining waits, bounded, for them instead
of reaping the group mid-write, and keeps the leader's pid and process group id
reserved for the whole wait, so a recycled id can never be probed or signalled
(#2286).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path


def drain_supported() -> bool:
    """Whether the leader's exit can be observed without reaping it."""
    return (
        os.name == "posix" and hasattr(os, "waitid") and bool(getattr(os, "WNOWAIT", 0)) and Path("/proc/self").is_dir()
    )


def observe_leader_exit(process: subprocess.Popen[bytes]) -> None:
    """Block until the direct child exits without reaping it, keeping its pid and pgid reserved."""
    try:
        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
    except OSError:
        # Already reaped, or the platform refused the observation: treat the
        # child as gone and let the drain fail closed if the group is alive.
        pass


def group_has_live_members(pgid: int) -> bool:
    """Whether any non-zombie process belongs to ``pgid``'s group or session.

    The leader starts a new session, so its pid is both the group and the
    session id. A descendant that moves to its own group with ``setpgid`` keeps
    that session id and still counts. A zombie cannot write, so a drained but
    deliberately unreaped leader does not count as a live member.
    """
    try:
        entries = os.listdir("/proc")
    except OSError:
        return True
    target = str(pgid).encode()
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            rest = Path(f"/proc/{entry}/stat").read_bytes().rpartition(b")")[2]
        except OSError:
            continue
        # The process name may contain ")", so the split above uses the last one.
        fields = rest.split()
        if len(fields) > 3 and target in (fields[2], fields[3]) and fields[0] != b"Z":
            return True
    return False


def wait_for_group(process: subprocess.Popen[bytes], timeout: float) -> bool:
    """Wait bounded for the group to have no live members; True when some outlived ``timeout``."""
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        if not group_has_live_members(process.pid):
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(0.02, remaining))
