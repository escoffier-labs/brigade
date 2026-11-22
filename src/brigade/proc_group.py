"""Owned-process-group draining for supervised children.

A supervised run can leave descendants that are still finishing the run's own
output when the direct child exits. Draining waits, bounded, for them instead
of reaping the group mid-write, and keeps the leader's pid and process group id
reserved for the whole wait, so a recycled id can never be probed or signalled
(#2286).

A descendant that calls ``setsid`` leaves both the group and the session, so a
group drain cannot reach it (#2847). Each supervised run gets a random token in
its environment, which every descendant inherits, so ``stray_processes`` names
what the run left behind by that token even after it changes its working
directory. ``kill_strays`` signals each one through the same pinned pidfd
pattern as ``kill_group_members``. Only a token carrier is ever signalled, so a
same-user process the run did not start is left alone.
"""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import time
from collections.abc import Collection, Sequence
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
        if isinstance(exc, PermissionError) and not _owned_by_us(pid):
            # Under hidepid=1 another user's stat is unreadable. Such a process cannot be signalled
            # by us, so it is not a member we could drain.
            return None
        raise ProcessGroupUnreadable(f"cannot read the process table for {pid}") from exc
    return rest.split()


def _owned_by_us(pid: int) -> bool:
    """Whether ``/proc/<pid>`` belongs to this user. An unknown owner counts as ours, so it fails closed."""
    try:
        return os.stat(f"/proc/{pid}").st_uid == os.getuid()
    except FileNotFoundError:
        return False  # The process is gone, so there is nothing to drain.
    except OSError:
        return True


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


def stray_processes(
    key: str,
    token: str,
    *,
    not_before: int | None = None,
    pipes: Collection[int] = (),
    root: Path | None = None,
) -> list[tuple[int, int]] | None:
    """(pid, starttime) for every live process whose environment carries ``key=token``.

    This is the post-teardown scan for a process that left the group and the session, so neither a
    group kill nor a session drain could reach it. The token is a random per-run environment entry
    every descendant inherits, so it names the run's own processes without relying on ancestry,
    which a new session breaks, or on a working directory, which the run can leave. A process
    without the exact entry is never a stray, and only a token carrier is ever signalled (#2847).

    ``None`` means cleanup cannot be confirmed. That is the process table unreadable, or a
    same-user environ unreadable on a process that started at or after ``not_before`` and also
    holds one of ``pipes`` or runs inside ``root``. Those two signals never decide a kill. They
    only narrow the blind case, because unrelated same-user processes start during a run on a
    normal desktop and keep their environ unreadable by design (sshd, a compositor). A process that
    predates the run cannot be its descendant, so its blind environ is skipped.
    """
    wanted = f"{key}={token}".encode()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    target = Path(os.path.realpath(root)) if root is not None else None
    mine = os.getpid()
    found: list[tuple[int, int]] = []
    try:
        for entry in entries:
            if not entry.isdigit() or int(entry) == mine:
                continue
            fields = _stat_fields(int(entry))
            if fields is None or len(fields) <= 19 or fields[0] in (b"Z", b"X", b"x"):
                continue
            try:
                started = int(fields[19])
            except ValueError:
                continue
            carries = _carries_token(int(entry), wanted)
            if carries is None:
                if not_before is not None and started < not_before:
                    continue
                blind = _holds_pipe(int(entry), pipes) or (target is not None and _inside(target, int(entry)))
                if blind:
                    return None
                continue
            if carries:
                found.append((int(entry), started))
    except OSError:
        return None
    return found


def kill_strays(records: Sequence[tuple[int, int]], sig: int, key: str, token: str) -> int:
    """Signal each recorded process through its pidfd, after re-reading its identity and token.

    The pidfd pins the process itself, so a recycled number cannot redirect the signal. The stat
    re-read drops a pid whose starttime moved or which became a zombie since the scan, and the
    environ re-read drops a pid that no longer carries the run's token. Returns how many were
    signalled. Raises ``ProcessGroupUnreadable`` when the process table cannot be read.
    """
    wanted = f"{key}={token}".encode()
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or pidfd_send_signal is None:
        return 0
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
            if started_now != started or fields[0] in (b"Z", b"X", b"x"):
                continue
            if _carries_token(pid, wanted) is not True:
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


def _carries_token(pid: int, wanted: bytes) -> bool | None:
    """Whether ``pid`` holds the exact ``KEY=token`` entry. ``None`` when ours but unreadable."""
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return False
    except OSError:
        if _owned_by_us(pid):
            # A same-user process whose environ cannot be read is not confirmed quiet.
            return None
        return False  # Another user's process cannot be signalled by us, so it is not our stray.
    return wanted in environ.split(b"\0")


def _inside(root: Path, pid: int) -> bool:
    """Whether the process in ``pid`` runs with a working directory inside ``root``."""
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd")).is_relative_to(root)
    except OSError:
        return False


def _holds_pipe(pid: int, inodes: Collection[int]) -> bool:
    """Whether any descriptor of ``pid`` is one of the listed pipes."""
    if not inodes:
        return False
    try:
        names = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return False
    for name in names:
        try:
            target = os.readlink(f"/proc/{pid}/fd/{name}")
        except OSError:
            continue
        if target.startswith("pipe:[") and target.endswith("]"):
            try:
                if int(target[6:-1]) in inodes:
                    return True
            except ValueError:
                continue
    return False


def process_start_time(pid: int) -> int | None:
    """The process starttime in clock ticks since boot, or None when it cannot be read."""
    try:
        fields = _stat_fields(pid)
    except ProcessGroupUnreadable:
        return None
    if fields is None or len(fields) <= 19:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None
