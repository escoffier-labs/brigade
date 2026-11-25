"""Detached graph refresh for verify receipts."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

from . import engine_paths, inbox_lock, localio, proc, runguard

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


def status_or_start(target: Path, db: Path, binary: str | None = None) -> str | None:
    """Observe a live refresh or launch one without waiting for its work."""
    lock = target / engine_paths.CODE_GRAPH_DB.parent / "verify-sync.lock"
    reservation = lock.with_suffix(".json")
    if binary is None and not lock.is_file():
        return None
    try:
        if binary is not None and (
            any(localio.check_git_ignored(target, path) != "yes" for path in (lock.parent, lock, reservation))
            or localio.check_git_ignored(target, db.parent) not in ("yes", "outside-target")
            or localio.check_git_ignored(target, db) not in ("yes", "outside-target")
        ):
            return None
        with inbox_lock.held_file_lock(lock, deadline_seconds=0):
            claim = localio.read_json_dict(reservation) or {}
            pid, deadline = claim.get("pid"), claim.get("deadline")
            if isinstance(pid, int) and isinstance(deadline, (int, float)):
                if time.monotonic() < deadline and runguard._pid_is_active(pid):
                    return "background sync already running"
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
                ],
                cwd=target,
            )
            localio.write_json(reservation, {"pid": child.pid, "deadline": time.monotonic() + _START_TIMEOUT})
            return "background sync started"
    except inbox_lock.InboxLockTimeout:
        return "background sync lock held"
    except (OSError, proc.DetachedLaunchError):
        return None


def _worker(lock_path: str, binary: str, db: str) -> None:
    lock = Path(lock_path)
    reservation = lock.with_suffix(".json")
    try:
        # The launcher holds this lock until it has published the child's PID.
        # Once acquired here, the kernel releases it on either completion or death.
        with inbox_lock.held_file_lock(lock, deadline_seconds=_START_TIMEOUT):
            if (localio.read_json_dict(reservation) or {}).get("pid") != os.getpid():
                return
            reservation.unlink()
            subprocess.run([binary, "--db", db, "sync"], timeout=_SYNC_TIMEOUT, check=False)
    except (OSError, TimeoutError, subprocess.TimeoutExpired):
        return
