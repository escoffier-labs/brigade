"""Detached graph refresh for verify receipts."""

from __future__ import annotations

import os
import secrets
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, suppress
from pathlib import Path
from threading import Thread

from . import engine_paths, inbox_lock, localio, nt_process, proc, proc_group, runguard

_START_LOCK_TIMEOUT = 5.0
# Cold interpreter startup on Windows can be delayed by antivirus scanning.
_WINDOWS_START_WINDOW = 60.0
_RESERVATION_MTIME_TOLERANCE = 2.0
_EXIT_REASON_LIMIT = 160
_SYNC_TIMEOUT = 120.0
_WORKER = (
    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
    "from brigade.code_graph_background import _worker; _worker(*sys.argv[1:])"
)


def index_ready(db: Path) -> bool:
    """A created schema is not proof of a committed sync."""
    if not db.is_file():
        return False
    try:
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=0)) as connection:
            return connection.execute("SELECT 1 FROM meta WHERE key = 'synced_at'").fetchone() is not None
    except sqlite3.Error:
        return False


def _lock_path(target: Path) -> Path:
    return target / engine_paths.CODE_GRAPH_DB.parent / "verify-sync.lock"


def last_exit_reason(target: Path) -> str | None:
    status = localio.read_json_dict(_lock_path(target).with_suffix(".exit.json")) or {}
    reason = status.get("reason")
    if not isinstance(reason, str):
        return None
    return "".join(char for char in reason if char.isprintable())[:_EXIT_REASON_LIMIT].strip() or None


def _record_exit(lock: Path, reason: str) -> None:
    with suppress(OSError):
        localio.write_json(lock.with_suffix(".exit.json"), {"reason": reason})


def _ignored_for_refresh(target: Path, db: Path, lock: Path, reservation: Path) -> bool:
    state_paths = (lock.parent, lock, reservation, lock.with_suffix(".exit.json"))
    paths = {candidate.relative_to(target) for path in state_paths for candidate in (path, path.resolve())}
    for path in (db.parent, db):
        if path.resolve().is_relative_to(target):
            paths.add(path.resolve().relative_to(target))
    expected = {os.fsencode(path.as_posix()) for path in paths}
    result = subprocess.run(
        ["git", "-C", str(target), "check-ignore", "--stdin", "-z"],
        input=b"\0".join(sorted(expected)) + b"\0",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=1,
        check=False,
    )
    return result.returncode == 0 and set(result.stdout.rstrip(b"\0").split(b"\0")) == expected


def _process_start_time(pid: int) -> int | None:
    if sys.platform == "win32":
        return nt_process.process_creation_time(pid)
    return proc_group.process_start_time(pid)


def _reservation_live(claim: dict, reservation: Path | None = None) -> bool:
    pid, created, started = claim.get("pid"), claim.get("created"), claim.get("started")
    if type(pid) is not int or pid <= 0 or not isinstance(created, (int, float)):
        return False
    windows = sys.platform == "win32"
    window = _WINDOWS_START_WINDOW if windows else _START_LOCK_TIMEOUT
    now = time.time()
    if not now - window < created < now + window or (started is not None and type(started) is not int):
        return False
    actual_start = _process_start_time(pid) if started is not None else None
    if actual_start is not None and actual_start != started:
        return False
    if created > now:
        # A small clock rollback can put both the claim and its mtime in the
        # future. Only retain it when Windows also confirms the launcher's identity.
        if not windows or actual_start is None or reservation is None:
            return False
        try:
            modified = reservation.stat().st_mtime
        except OSError:
            return False
        if not now < modified < now + window or abs(modified - created) > _RESERVATION_MTIME_TOLERANCE:
            return False
    # Windows can read creation time even for a process that has already exited.
    return (not windows and actual_start is not None) or runguard._pid_is_active(pid)


def status_or_start(target: Path, db: Path, binary: str | None = None) -> dict[str, str] | None:
    """Observe a live refresh or launch one without waiting for its work."""
    lock = _lock_path(target)
    reservation = lock.with_suffix(".json")
    try:
        lock_exists = lock.is_file()
        if not lock_exists:
            if binary is None:
                return None
            # Before creating the lock, confirm that this path is ignored.
            if not _ignored_for_refresh(target, db, lock, reservation):
                return {"status": "launch_failed", "reason": "index or lock path is not ignored"}
        with inbox_lock.held_file_lock(lock, deadline_seconds=0):
            if _reservation_live(localio.read_json_dict(reservation) or {}, reservation):
                return {"status": "busy", "reason": "background sync starting"}
            if binary is None:
                return None
            if lock_exists and not _ignored_for_refresh(target, db, lock, reservation):
                return {"status": "launch_failed", "reason": "index or lock path is not ignored"}
            if reservation.exists():
                # Replacing a claim does not establish that its worker failed.
                # Keep the lock file as the stable inode for other openers.
                reservation.unlink()
            # Prevent the engine's first-sync setup from editing .gitignore.
            db.parent.mkdir(parents=True, exist_ok=True)
            token = secrets.token_hex(16)
            child, _ = proc.spawn_detached(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    _WORKER,
                    str(Path(__file__).resolve().parents[1]),
                    str(lock),
                    str(Path(binary).absolute()),
                    str(db.resolve()),
                    str(target),
                    token,
                ],
                cwd=Path(sys.executable).resolve().parent,
            )
            started = _process_start_time(child.pid)
            Thread(target=child.wait, daemon=True).start()
            localio.write_json(
                reservation,
                {"pid": child.pid, "created": time.time(), "started": started, "token": token},
            )
            return {"status": "started"}
    except inbox_lock.InboxLockTimeout:
        return {"status": "busy", "reason": "background sync lock held"}
    except proc.DetachedLaunchError as exc:
        return {"status": "launch_failed", "reason": str(exc), "failure_kind": "detach_refused"}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"status": "launch_failed", "reason": str(exc)}


def _worker(lock_path: str, binary: str, db: str, target: str, token: str = "") -> None:
    lock = Path(lock_path)
    reservation = lock.with_suffix(".json")
    try:
        # The parent publishes the token under this lock. The PID is only a
        # startup liveness hint: a venv launcher can have a different PID from
        # the interpreter. During sync the kernel lock establishes liveness.
        with inbox_lock.held_file_lock(lock, deadline_seconds=_START_LOCK_TIMEOUT):
            claim = localio.read_json_dict(reservation)
            if not token or claim is None or claim.get("token") != token:
                return
            try:
                reservation.unlink()
                creationflags = 0
                if sys.platform == "win32":
                    creationflags = subprocess.CREATE_NO_WINDOW
                result = subprocess.run(
                    [binary, "--db", db, "sync", target],
                    timeout=_SYNC_TIMEOUT,
                    check=False,
                    creationflags=creationflags,
                )
            except subprocess.TimeoutExpired:
                _record_exit(lock, "graph sync timed out")
            except OSError as exc:
                _record_exit(lock, f"worker I/O failed (errno {exc.errno})")
            else:
                if result.returncode:
                    _record_exit(lock, f"graph sync exited with code {result.returncode}")
                else:
                    with suppress(OSError):
                        lock.with_suffix(".exit.json").unlink(missing_ok=True)
    except OSError:
        # A worker without the lock cannot update shared refresh state.
        return
