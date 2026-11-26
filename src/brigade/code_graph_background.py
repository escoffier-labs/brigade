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
from tempfile import TemporaryFile
from threading import Thread

from . import engine_paths, inbox_lock, localio, nt_process, proc, proc_group, runguard

_START_LOCK_TIMEOUT = 5.0
# Cold interpreter startup on Windows can be delayed by antivirus scanning.
_WINDOWS_START_WINDOW = 60.0
_RESERVATION_MTIME_TOLERANCE = 2.0
_EXIT_REASON_LIMIT = 160
_SYNC_TIMEOUT = 120.0
_ENGINE_LOCK_REFUSED = "another sync is already running for this database"
_SYNC_STILL_RUNNING = f"graph sync still running after {_SYNC_TIMEOUT:g}s"
_WORKER = (
    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
    "from brigade.code_graph_background import _worker; _worker(*sys.argv[1:])"
)


def may_run_killable_foreground_sync() -> bool:
    # Engines up to v0.32.0 cannot recover a killed writer's lock on Windows.
    # Drop this workaround once Brigade requires an engine release with the fix.
    return sys.platform != "win32"


def engine_lock_path(db: Path) -> Path:
    return db.with_name(db.name + ".lock")


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


def _refresh_state(lock: Path) -> dict:
    return localio.read_json_dict(lock.with_suffix(".exit.json")) or {}


def last_exit_reason(target: Path) -> str | None:
    state = _refresh_state(_lock_path(target))
    reason = state.get("reason")
    if state.get("in_progress") or not isinstance(reason, str):
        return None
    return "".join(char for char in reason if char.isprintable())[:_EXIT_REASON_LIMIT].strip() or None


def last_engine_lock_refusal(target: Path, db: Path) -> str | None:
    state = _refresh_state(_lock_path(target))
    if state.get("lock_refused_db") == str(db.resolve()):
        return _ENGINE_LOCK_REFUSED
    return None


def _record_refresh_state(lock: Path, reason: str, **metadata: str | bool) -> None:
    with suppress(OSError):
        localio.write_json(lock.with_suffix(".exit.json"), {"reason": reason, **metadata})


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


def status_or_start(
    target: Path,
    db: Path,
    binary: str | None = None,
    *,
    initial_wait: float | None = None,
) -> dict[str, str] | None:
    """Observe or launch a refresh, optionally waiting for an in-session first build."""
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
            argv = [
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
            ]
            cwd = Path(sys.executable).resolve().parent
            in_session = False
            try:
                child, _ = proc.spawn_detached(argv, cwd=cwd)
            except proc.DetachedLaunchError:
                if initial_wait is None or may_run_killable_foreground_sync() or db.is_file():
                    raise
                # Breakaway was refused. Keep the worker in the ambient job
                # and give it no console or pipes connected to verification.
                child = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.STDOUT,
                    **proc.detached_launch_kwargs(job_breakaway=False),
                )
                in_session = True
            started = _process_start_time(child.pid)
            if not in_session:
                Thread(target=child.wait, daemon=True).start()
            localio.write_json(
                reservation,
                {"pid": child.pid, "created": time.time(), "started": started, "token": token},
            )
        if not in_session:
            return {"status": "started"}
        # Let the worker claim its reservation before waiting. Its lock stays
        # held until the engine exits, even when verification returns first.
        try:
            child.wait(timeout=initial_wait)
        except subprocess.TimeoutExpired:
            Thread(target=child.wait, daemon=True).start()
            return {
                "status": "started",
                "mode": "in_session",
                "reason": "initial graph sync still running",
            }
        return {"status": "completed", "mode": "in_session"}
    except inbox_lock.InboxLockTimeout:
        reason = "background sync lock held"
        if not may_run_killable_foreground_sync() and _refresh_state(lock).get("in_progress"):
            reason = _SYNC_STILL_RUNNING
        return {"status": "busy", "reason": reason}
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
                argv = [binary, "--db", db, "sync", target]
                lock_refused = False
                if may_run_killable_foreground_sync():
                    returncode = subprocess.run(argv, timeout=_SYNC_TIMEOUT, check=False, creationflags=0).returncode
                else:
                    if _refresh_state(lock).get("in_progress"):
                        with suppress(OSError):
                            lock.with_suffix(".exit.json").unlink(missing_ok=True)
                    returncode, lock_refused = _wait_for_windows_sync(argv, lock)
            except subprocess.TimeoutExpired:
                _record_refresh_state(lock, "graph sync timed out")
            except OSError as exc:
                _record_refresh_state(lock, f"worker I/O failed (errno {exc.errno})")
            else:
                if lock_refused:
                    _record_refresh_state(lock, _ENGINE_LOCK_REFUSED, lock_refused_db=db)
                elif returncode:
                    _record_refresh_state(lock, f"graph sync exited with code {returncode}")
                else:
                    with suppress(OSError):
                        lock.with_suffix(".exit.json").unlink(missing_ok=True)
    except OSError:
        # A worker without the lock cannot update shared refresh state.
        return


def _wait_for_windows_sync(argv: list[str], lock: Path) -> tuple[int, bool]:
    # A file lets the engine write diagnostics without a pipe reader that
    # needs cleanup when the wait expires.
    with TemporaryFile() as stderr:
        child = subprocess.Popen(argv, stderr=stderr, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            returncode = child.wait(timeout=_SYNC_TIMEOUT)
        except subprocess.TimeoutExpired:
            _record_refresh_state(lock, _SYNC_STILL_RUNNING, in_progress=True)
            returncode = child.wait()
        stderr.seek(0)
        lock_refused = returncode != 0 and _ENGINE_LOCK_REFUSED.encode() in stderr.read(8192)
        return returncode, lock_refused
