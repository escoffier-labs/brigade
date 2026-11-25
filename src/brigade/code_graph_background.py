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

from . import engine_paths, inbox_lock, localio, proc, proc_group, runguard

_START_TIMEOUT = 5.0
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
    return reason if isinstance(reason, str) else None


def _record_exit(lock: Path, reason: str) -> None:
    with suppress(OSError):
        localio.write_json(lock.with_suffix(".exit.json"), {"reason": reason})


def _ignored_for_refresh(target: Path, db: Path, lock: Path, reservation: Path) -> bool:
    state_paths = (lock.parent, lock, reservation, lock.with_suffix(".exit.json"))
    paths = {path.resolve().relative_to(target) for path in state_paths}
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


def _reservation_live(claim: dict) -> bool:
    pid, created, started = claim.get("pid"), claim.get("created"), claim.get("started")
    now = time.time()
    if type(pid) is not int or pid <= 0 or not isinstance(created, (int, float)):
        return False
    if not now - _START_TIMEOUT < created <= now or (started is not None and type(started) is not int):
        return False
    actual_start = proc_group.process_start_time(pid)
    if started is None or actual_start is None:
        # Platforms without /proc can still recognize a live startup reservation.
        return runguard._pid_is_active(pid)
    return actual_start == started


def status_or_start(target: Path, db: Path, binary: str | None = None) -> dict[str, str] | None:
    """Observe a live refresh or launch one without waiting for its work."""
    lock = _lock_path(target)
    reservation = lock.with_suffix(".json")
    if binary is None and not lock.is_file():
        return None
    try:
        if binary is not None and not _ignored_for_refresh(target, db, lock, reservation):
            return {"status": "launch_failed", "reason": "index or lock path is not ignored"}
        with inbox_lock.held_file_lock(lock, deadline_seconds=0):
            if _reservation_live(localio.read_json_dict(reservation) or {}):
                return {"status": "busy", "reason": "background sync starting"}
            if reservation.exists() and (binary is not None or _ignored_for_refresh(target, db, lock, reservation)):
                # Keep the unlocked file as the stable lock inode for other openers.
                reservation.unlink()
                if last_exit_reason(target) is None:
                    _record_exit(lock, "stale startup reservation")
            if binary is None:
                return None
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
            started = proc_group.process_start_time(child.pid)
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
        with inbox_lock.held_file_lock(lock, deadline_seconds=_START_TIMEOUT):
            claim = localio.read_json_dict(reservation)
            if not token:
                _record_exit(lock, "missing reservation token")
                return
            if claim is None:
                _record_exit(lock, "startup reservation missing or unreadable")
                return
            if claim.get("token") != token:
                _record_exit(lock, "reservation token mismatch")
                return
            reservation.unlink()
            result = subprocess.run([binary, "--db", db, "sync", target], timeout=_SYNC_TIMEOUT, check=False)
            if result.returncode:
                _record_exit(lock, f"graph sync exited with code {result.returncode}")
            else:
                with suppress(OSError):
                    lock.with_suffix(".exit.json").unlink(missing_ok=True)
    except inbox_lock.InboxLockTimeout:
        _record_exit(lock, "startup lock timed out")
    except subprocess.TimeoutExpired:
        _record_exit(lock, "graph sync timed out")
    except OSError as exc:
        _record_exit(lock, f"worker I/O failed (errno {exc.errno})")
