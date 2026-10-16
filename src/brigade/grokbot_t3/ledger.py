"""Pack-owned SQLite ledger of dispatched requests. Prompts are stored only as SHA-256."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import TERMINAL_STATES

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


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self._conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

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
    ) -> None:
        ts = _ts(now)
        self._conn.execute(
            "INSERT INTO requests (request_id, caller, repository, host, seat, title, prompt_sha256, "
            "created_at, updated_at, last_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (request_id, caller, repository, host, seat, title, prompt_sha256, ts, ts, state),
        )

    def get(self, request_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone()
        return dict(row) if row is not None else None

    def update_state(self, request_id: str, state: str, now: datetime) -> None:
        self._conn.execute(
            "UPDATE requests SET notified_state = CASE WHEN last_state = ? THEN notified_state ELSE NULL END, "
            "last_state = ?, updated_at = ? WHERE request_id = ?",
            (state, state, _ts(now), request_id),
        )

    def mark_notified(self, request_id: str, state: str, now: datetime) -> None:
        self._conn.execute(
            "UPDATE requests SET notified_state = ?, notified_at = ? WHERE request_id = ?",
            (state, _ts(now), request_id),
        )

    def open_count(self) -> int:
        placeholders = ", ".join("?" for _ in _TERMINAL)
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM requests WHERE last_state NOT IN ({placeholders})", _TERMINAL
        ).fetchone()
        return int(row[0])

    def submits_since(self, since: datetime) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM requests WHERE created_at >= ?", (_ts(since),)).fetchone()
        return int(row[0])

    def recent(self, caller: str | None, limit: int) -> list[dict[str, Any]]:
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
        placeholders = ", ".join("?" for _ in _TERMINAL)
        rows = self._conn.execute(
            f"SELECT * FROM requests WHERE last_state NOT IN ({placeholders}) "
            "OR notified_state IS NULL OR notified_state != last_state ORDER BY created_at, rowid",
            _TERMINAL,
        ).fetchall()
        return [dict(row) for row in rows]
