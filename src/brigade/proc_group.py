"""Owned-process-group draining for supervised children.

A supervised run can leave descendants that are still finishing the run's own
output when the direct child exits. Draining waits, bounded, for them instead
of reaping the group mid-write, and keeps the leader's pid and process group id
reserved for the whole wait, so a recycled id can never be probed or signalled
(#2286).
"""

from __future__ import annotations

import errno
import os
import signal
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


class ProcessGroupUnreadable(OSError):
    """The process table could not be read, so group membership cannot be verified."""


def group_has_live_members(pgid: int) -> bool:
    """Whether any non-zombie process belongs to ``pgid``'s group or session.

    The leader starts a new session, so its pid is both the group and the
    session id. A descendant that moves to its own group with ``setpgid`` keeps
    that session id and still counts. A zombie cannot write, so a drained but
    deliberately unreaped leader does not count as a live member.
    """
    records = _member_records(pgid)
    if records is None:
        # The group's fate cannot be observed, so it is treated as alive.
        return True
    return bool(records)


def group_member_pids(pgid: int) -> list[int] | None:
    """Live (non-zombie) pids in ``pgid``'s group or session, or None when /proc is unreadable."""
    records = _member_records(pgid)
    if records is None:
        return None
    return [pid for pid, _ in records]


def _member_records(pgid: int) -> list[tuple[int, int]] | None:
    """(pid, starttime) for every live member, or None when the process table cannot be read."""
    if pgid <= 0:
        return []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    try:
        return _members(entries, pgid)
    except OSError:
        # A member whose stat cannot be read makes the whole group unknown, never empty.
        return None


def _members(entries: list[str], pgid: int) -> list[tuple[int, int]]:
    if pgid <= 0:
        return []
    target = str(pgid).encode()
    members: list[tuple[int, int]] = []
    for entry in entries:
        if not entry.isdigit():
            continue
        fields = _stat_fields(int(entry))
        if fields is None or len(fields) <= 19:
            continue
        # The process name may contain ")", so the split in ``_stat_fields`` uses the last one.
        # Field 22 is starttime, which pins this pid to one process across the scan and the signal.
        if target not in (fields[2], fields[3]) or fields[0] == b"Z":
            continue
        try:
            started = int(fields[19])
        except ValueError:
            continue
        members.append((int(entry), started))
    return members


def _stat_fields(pid: int) -> list[bytes] | None:
    try:
        rest = Path(f"/proc/{pid}/stat").read_bytes().rpartition(b")")[2]
    except OSError as exc:
        if isinstance(exc, (FileNotFoundError, ProcessLookupError)) or exc.errno in (errno.ENOENT, errno.ESRCH):
            return None
        raise ProcessGroupUnreadable(f"cannot read the process table for {pid}") from exc
    return rest.split()


def kill_group_members(pgid: int, sig: int) -> int:
    """Signal every live member of ``pgid``'s group or session; return how many were signalled.

    A descendant that called ``setpgid`` keeps the leader's session id, so a group kill alone misses
    it. Each candidate gets a pidfd before its stat is re-read. The signal goes through that
    descriptor only when the re-read still names the same group or session, the state is not a
    zombie, and starttime matches the scan, so a pid recycled in between can never be signalled.
    Raises ``ProcessGroupUnreadable`` when the process table cannot be read.
    """
    if pgid <= 0:
        return 0
    records = _member_records(pgid)
    if records is None:
        raise ProcessGroupUnreadable("the process table is unreadable")
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or pidfd_send_signal is None:
        return 0
    target = str(pgid).encode()
    signalled = 0
    for pid, started in records:
        try:
            descriptor = pidfd_open(pid)
        except OSError:
            continue
        try:
            fields = _stat_fields(pid)
            if fields is None or len(fields) <= 19:
                continue
            try:
                started_now = int(fields[19])
            except ValueError:
                continue
            if started_now != started or fields[0] == b"Z" or target not in (fields[2], fields[3]):
                continue
            try:
                pidfd_send_signal(descriptor, sig)
            except OSError:
                continue
            signalled += 1
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return signalled


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
