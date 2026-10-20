"""Local metadata-only GitHub budgets, shared by readers using the same file."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class BudgetLedger:
    """Serialize reads across threads/processes with a SQLite write transaction.

    Hold the transaction across admission, the bounded runner call and recording.
    This is a local queue, not a fleet-wide credential or rate-limit authority.
    Admission prunes rows older than an hour. No bodies, tokens, URLs or identities
    are stored.
    The caller supplies a trusted local database path outside version control.
    """

    def __init__(self, path: Path):
        self.path = path
        with self.serialized() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS reads (
                    id INTEGER PRIMARY KEY, at REAL NOT NULL,
                    credential TEXT NOT NULL, route TEXT NOT NULL,
                    status INTEGER, state TEXT NOT NULL, etag_hit INTEGER NOT NULL,
                    remaining INTEGER, reset INTEGER, attempted INTEGER NOT NULL,
                    fallback INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS reads_budget ON reads(credential, at);
                CREATE TABLE IF NOT EXISTS holds (
                    credential TEXT NOT NULL, route TEXT NOT NULL,
                    endpoint TEXT NOT NULL, until REAL NOT NULL, state TEXT NOT NULL,
                    PRIMARY KEY (credential, route, endpoint)
                );
                """
            )

    @contextmanager
    def serialized(self, *, timeout: float = 910) -> Iterator[sqlite3.Connection]:
        # A reader permits three runner calls of at most 300 seconds each.
        # Constructors and observations must also tolerate that transaction.
        conn = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def admission(
        self, conn: sqlite3.Connection, credential: str, route: str, endpoint: str, now: float, cap: int
    ) -> tuple[str, float] | None:
        conn.execute("DELETE FROM reads WHERE at <= ?", (now - 3600,))
        conn.execute("DELETE FROM holds WHERE until <= ?", (now,))
        hold = conn.execute(
            "SELECT state, until FROM holds WHERE credential=? AND route=? AND endpoint IN ('', ?) "
            "ORDER BY until DESC LIMIT 1",
            (credential, route, endpoint),
        ).fetchone()
        if hold:
            return hold["state"], hold["until"]
        count, first = conn.execute(
            "SELECT count(*), min(at) FROM reads WHERE credential=? AND attempted=1 AND etag_hit=0 AND at>?",
            (credential, now - 3600),
        ).fetchone()
        if count >= cap:
            return "budget-exhausted", (first if first is not None else now) + 3600
        return None

    def hold(
        self, conn: sqlite3.Connection, credential: str, route: str, endpoint: str, until: float, state: str
    ) -> None:
        conn.execute(
            "INSERT INTO holds VALUES (?, ?, ?, ?, ?) ON CONFLICT(credential, route, endpoint) "
            "DO UPDATE SET until=max(until, excluded.until), state=excluded.state",
            (credential, route, endpoint, until, state),
        )

    def record(
        self,
        conn: sqlite3.Connection,
        *,
        now: float,
        credential: str,
        route: str,
        status: int | None,
        state: str,
        etag_hit: bool = False,
        remaining: int | None = None,
        reset: int | None = None,
        attempted: bool = True,
        fallback: bool = False,
    ) -> None:
        conn.execute(
            "INSERT INTO reads(at, credential, route, status, state, etag_hit, remaining, reset, attempted, fallback) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, credential, route, status, state, etag_hit, remaining, reset, attempted, fallback),
        )

    def summary(self, conn: sqlite3.Connection, now: float) -> dict[str, Any]:
        rows = conn.execute("SELECT * FROM reads WHERE at>? ORDER BY id", (now - 3600,)).fetchall()
        result: dict[str, Any] = {
            "window_seconds": 3600,
            "requests": 0,
            "etag_hits": 0,
            "inconclusive": 0,
            "denied": 0,
            "fallbacks": 0,
            "budget_exhausted": 0,
            "statuses": {},
            "routes": {},
            "credentials": {},
        }
        for row in rows:
            result["requests"] += row["attempted"]
            result["etag_hits"] += row["etag_hit"]
            result["inconclusive"] += row["state"] == "inconclusive"
            result["denied"] += row["attempted"] and row["status"] in {401, 403} and row["state"] != "rate-limited"
            result["fallbacks"] += row["fallback"]
            result["budget_exhausted"] += row["state"] == "budget-exhausted"
            credential = result["credentials"].setdefault(
                row["credential"], {"requests": 0, "remaining": None, "reset": None, "state": row["state"]}
            )
            credential["requests"] += row["attempted"]
            credential["state"] = row["state"]
            for key in ("remaining", "reset"):
                if row[key] is not None:
                    credential[key] = row[key]
            if row["attempted"]:
                status = str(row["status"]) if row["status"] is not None else "transport-error"
                result["statuses"][status] = result["statuses"].get(status, 0) + 1
                result["routes"][row["route"]] = result["routes"].get(row["route"], 0) + 1
        result["etag_ratio"] = result["etag_hits"] / result["requests"] if result["requests"] else 0.0
        return result
