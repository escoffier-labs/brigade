"""Pack-owned SQLite ledger of dispatched requests. Prompts are stored only as SHA-256."""

from __future__ import annotations

import hashlib
import json
import re
import os
import sqlite3
import stat
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import runtime_config
from .contracts import REOPENING_STATE, REOPENING_TTL_SECONDS, TERMINAL_STATES, T3DispatchError

IN_DOUBT_TTL = timedelta(hours=2)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    caller TEXT NOT NULL,
    repository TEXT NOT NULL,
    host TEXT NOT NULL,
    seat TEXT NOT NULL,
    title TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_state TEXT NOT NULL,
    notified_state TEXT,
    notified_at TEXT
)
"""
_TERMINAL = tuple(sorted(TERMINAL_STATES))
_PUBLIC = "request_id, caller, repository, host, seat, title, last_state AS state, created_at, updated_at"


def _ts(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")


def wake_target_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _prepare_storage(path: Path) -> None:
    runtime_config.require_posix_storage()
    try:
        path.parent.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        parent = path.parent.stat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o022:
            raise T3DispatchError("unavailable") from None
    else:
        os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)


class Ledger:
    def __init__(self, path: Path):
        _prepare_storage(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), timeout=10, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS in_doubt (caller TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS intake_claims "
            "(issue_key TEXT PRIMARY KEY, request_id TEXT, claimed_at TEXT NOT NULL)"
        )
        # Seed legacy rows once, atomically with creation of the attempt ledger.
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            existing = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'attempts'"
            ).fetchone()
            self._conn.execute("CREATE TABLE IF NOT EXISTS attempts (attempted_at TEXT NOT NULL)")
            if existing is None:
                self._conn.execute("INSERT INTO attempts SELECT created_at FROM requests")
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(requests)")}
            for name, definition in {
                "interaction_mode": "TEXT NOT NULL DEFAULT 'default'",
                "status_failures": "INTEGER NOT NULL DEFAULT 0",
                "status_poll_delay": "INTEGER NOT NULL DEFAULT 0",
                "wake_attempts": "INTEGER NOT NULL DEFAULT 0",
                "delivered_targets": "TEXT NOT NULL DEFAULT '[]'",
                "last_polled_seq": "INTEGER NOT NULL DEFAULT 0",
                "state_revision": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in columns:
                    self._conn.execute(f"ALTER TABLE requests ADD COLUMN {name} {definition}")
            for row in self._conn.execute("SELECT request_id, delivered_targets FROM requests").fetchall():
                targets = json.loads(row["delivered_targets"])
                migrated = set()
                for target in targets:
                    if re.fullmatch(r"[0-9a-f]{64}", target):
                        migrated.add(target)
                    else:
                        # Legacy identifiers encoded both the URL and key-file path.
                        url = json.loads(target)[0] if target.startswith("[") else target
                        migrated.add(wake_target_id(url))
                if migrated != set(targets):
                    self._conn.execute(
                        "UPDATE requests SET delivered_targets = ? WHERE request_id = ?",
                        (json.dumps(sorted(migrated)), row["request_id"]),
                    )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def claim_intake(self, issue_key: str, now: datetime) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO intake_claims (issue_key, claimed_at) VALUES (?, ?)", (issue_key, _ts(now))
            )
            return cursor.rowcount > 0

    def attach_intake(self, issue_key: str, request_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE intake_claims SET request_id = ? WHERE issue_key = ?", (request_id, issue_key))

    def release_intake(self, issue_key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM intake_claims WHERE issue_key = ?", (issue_key,))

    def record_submit(
        self,
        *,
        request_id: str,
        caller: str,
        repository: str,
        host: str,
        seat: str,
        title: str,
        prompt_sha256: str,
        state: str,
        now: datetime,
        interaction_mode: str = "default",
        release_reservation: int | None = None,
    ) -> None:
        with self._lock, self._conn:
            # The connection autocommits, so open the transaction explicitly or the insert and release split.
            self._conn.execute("BEGIN IMMEDIATE")
            ts = _ts(now)
            self._conn.execute(
                "INSERT INTO requests (request_id, caller, repository, host, seat, title, prompt_sha256, "
                "created_at, updated_at, last_state, interaction_mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, caller, repository, host, seat, title, prompt_sha256, ts, ts, state, interaction_mode),
            )
            if release_reservation is not None:
                # Same transaction as the insert, so the slot is never counted zero or two times.
                self._conn.execute("DELETE FROM in_doubt WHERE rowid = ?", (release_reservation,))

    def get(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone()
            return dict(row) if row is not None else None

    def update_state(
        self,
        request_id: str,
        state: str,
        now: datetime,
        *,
        reset_status_failures: bool = True,
        expected_revision: int | None = None,
        owner_write: bool = False,
    ) -> bool:
        """Set state and advance its revision, optionally requiring an observed revision. Return whether applied.

        Only the follow-up that set the provisional reopening marker (owner_write) may replace it while it is
        fresh; pollers may replace it once it is older than REOPENING_TTL_SECONDS (the follow-up crashed).
        """
        guard = " AND state_revision = ?" if expected_revision is not None else ""
        params: tuple[Any, ...] = (expected_revision,) if expected_revision is not None else ()
        if not owner_write:
            guard += " AND (last_state != ? OR updated_at <= ?)"
            params += (REOPENING_STATE, _ts(now - timedelta(seconds=REOPENING_TTL_SECONDS)))
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE requests SET notified_state = CASE WHEN last_state = ? THEN notified_state ELSE NULL END, "
                "wake_attempts = CASE WHEN last_state = ? THEN wake_attempts ELSE 0 END, "
                "delivered_targets = CASE WHEN last_state = ? THEN delivered_targets ELSE '[]' END, "
                "status_failures = CASE WHEN ? THEN 0 ELSE status_failures END, "
                "status_poll_delay = CASE WHEN ? THEN 0 ELSE status_poll_delay END, "
                "state_revision = state_revision + 1, last_state = ?, updated_at = ? WHERE request_id = ?" + guard,
                (state, state, state, reset_status_failures, reset_status_failures, state, _ts(now), request_id)
                + params,
            )
            return cursor.rowcount > 0

    def restore_state(
        self, request_id: str, state: str, notified_state: str | None, now: datetime, *, expected_revision: int
    ) -> bool:
        """Undo a pre-launch reopening marker after a definite refusal, keeping the earlier wake as delivered."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE requests SET last_state = ?, notified_state = ?, updated_at = ?, "
                "state_revision = state_revision + 1 WHERE request_id = ? AND state_revision = ?",
                (state, notified_state, _ts(now), request_id, expected_revision),
            )
            return cursor.rowcount > 0

    def mark_notified(self, request_id: str, state: str, now: datetime) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET notified_state = ?, notified_at = ? WHERE request_id = ? AND last_state = ?",
                (state, _ts(now), request_id, state),
            )

    def open_count(self, now: datetime | None = None) -> int:
        """Open requests plus unexpired in-doubt reservations."""
        current = _ts(now or datetime.now(timezone.utc))
        with self._lock:
            placeholders = ", ".join("?" for _ in _TERMINAL)
            row = self._conn.execute(
                f"SELECT COUNT(*) FROM requests WHERE last_state NOT IN ({placeholders})", _TERMINAL
            ).fetchone()
            doubt = self._conn.execute("SELECT COUNT(*) FROM in_doubt WHERE expires_at > ?", (current,)).fetchone()
            return int(row[0]) + int(doubt[0])

    def record_in_doubt(self, caller: str, now: datetime) -> int:
        """Hold an open slot for a submit whose launch is not yet known to be tracked."""
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO in_doubt (caller, created_at, expires_at) VALUES (?, ?, ?)",
                (caller, _ts(now), _ts(now + IN_DOUBT_TTL)),
            )
            return int(cursor.lastrowid or 0)

    def release_in_doubt(self, reservation: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM in_doubt WHERE rowid = ?", (reservation,))

    def submits_since(self, since: datetime) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM requests WHERE created_at >= ?", (_ts(since),)).fetchone()
            return int(row[0])

    def recent(self, caller: str | None, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            if caller is None:
                rows = self._conn.execute(
                    f"SELECT {_PUBLIC} FROM requests ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT {_PUBLIC} FROM requests WHERE caller = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (caller, limit),
                ).fetchall()
            return [dict(row) for row in rows]

    def pending_notifications(self) -> list[dict[str, Any]]:
        with self._lock:
            placeholders = ", ".join("?" for _ in _TERMINAL)
            rows = self._conn.execute(
                f"SELECT * FROM requests WHERE last_state NOT IN ({placeholders}) "
                "OR notified_state IS NULL OR notified_state != last_state "
                # Terminal rows need no status call, so deliver them first; then poll the least recently polled.
                f"ORDER BY CASE WHEN last_state IN ({placeholders}) THEN 0 ELSE 1 END, last_polled_seq, created_at, rowid",
                _TERMINAL + _TERMINAL,
            ).fetchall()
            return [dict(row) for row in rows]

    def record_attempt(self, now: datetime) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO attempts (attempted_at) VALUES (?)", (_ts(now),))

    def attempts_since(self, since: datetime) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM attempts WHERE attempted_at >= ?", (_ts(since),)).fetchone()
            return int(row[0])

    def record_status_poll(self, request_id: str, *, failed: bool) -> int:
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET status_failures = "
                + ("status_failures + 1" if failed else "0")
                + " WHERE request_id = ?",
                (request_id,),
            )
            row = self._conn.execute(
                "SELECT status_failures FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            return int(row[0])

    def mark_polled(self, request_id: str) -> None:
        """Move a row to the back of the polling order."""
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET last_polled_seq = (SELECT COALESCE(MAX(last_polled_seq), 0) + 1 FROM requests) "
                "WHERE request_id = ?",
                (request_id,),
            )

    def status_poll_due(self, request_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT status_poll_delay FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row[0] <= 0:
                return True
            self._conn.execute(
                "UPDATE requests SET status_poll_delay = status_poll_delay - 1 WHERE request_id = ?", (request_id,)
            )
            return False

    def defer_status_poll(self, request_id: str, ticks: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE requests SET status_poll_delay = ? WHERE request_id = ?", (ticks, request_id))

    def record_wake_attempt(self, request_id: str, state: str, delivered: set[str]) -> int:
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET wake_attempts = wake_attempts + 1, delivered_targets = ? WHERE request_id = ? AND last_state = ?",
                (json.dumps(sorted(delivered)), request_id, state),
            )
            row = self._conn.execute(
                "SELECT wake_attempts FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            return int(row[0])
