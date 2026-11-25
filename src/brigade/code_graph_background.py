"""Detached graph refresh for verify receipts."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from threading import Thread

from . import engine_paths, inbox_lock, localio, proc, proc_group

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


def _ignored_for_refresh(target: Path, db: Path, lock: Path, reservation: Path) -> bool:
    paths = {path.resolve().relative_to(target) for path in (lock.parent, lock, reservation)}
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
    return (
        type(pid) is int
        and pid > 0
        and isinstance(created, (int, float))
        and 0 <= time.time() - created < _START_TIMEOUT
        and type(started) is int
        and proc_group.process_start_time(pid) == started
    )


def status_or_start(target: Path, db: Path, binary: str | None = None) -> dict[str, str] | None:
    """Observe a live refresh or launch one without waiting for its work."""
    lock = target / engine_paths.CODE_GRAPH_DB.parent / "verify-sync.lock"
    reservation = lock.with_suffix(".json")
    if binary is None and not lock.is_file():
        return None
    try:
        if binary is not None and not _ignored_for_refresh(target, db, lock, reservation):
            return {"status": "launch_failed", "reason": "index or lock path is not ignored"}
        with inbox_lock.held_file_lock(lock, deadline_seconds=0):
            if _reservation_live(localio.read_json_dict(reservation) or {}):
                return {"status": "busy", "reason": "background sync starting"}
            if binary is None:
                return None
            # Prevent the engine's first-sync setup from editing .gitignore.
            db.parent.mkdir(parents=True, exist_ok=True)
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
                ],
                cwd=Path(sys.executable).resolve().parent,
            )
            Thread(target=child.wait, daemon=True).start()
            localio.write_json(
                reservation,
                {"pid": child.pid, "created": time.time(), "started": proc_group.process_start_time(child.pid)},
            )
            return {"status": "started"}
    except inbox_lock.InboxLockTimeout:
        return {"status": "busy", "reason": "background sync lock held"}
    except (OSError, ValueError, proc.DetachedLaunchError, subprocess.TimeoutExpired) as exc:
        return {"status": "launch_failed", "reason": str(exc)}


def wait_for_refresh(target: Path, db: Path, timeout: float) -> bool:
    """Let a stale-baseline run see the refreshed graph within its post-sync budget."""
    deadline = time.monotonic() + timeout
    while (refresh := status_or_start(target, db)) and refresh["status"] == "busy":
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))
    return True


def _worker(lock_path: str, binary: str, db: str, target: str) -> None:
    lock = Path(lock_path)
    reservation = lock.with_suffix(".json")
    try:
        # The launcher holds this lock until it has published the child's PID.
        # Once acquired here, the kernel releases it on either completion or death.
        with inbox_lock.held_file_lock(lock, deadline_seconds=_START_TIMEOUT):
            if (localio.read_json_dict(reservation) or {}).get("pid") != os.getpid():
                return
            reservation.unlink()
            subprocess.run([binary, "--db", db, "sync", target], timeout=_SYNC_TIMEOUT, check=False)
    except (OSError, TimeoutError, subprocess.TimeoutExpired):
        return
