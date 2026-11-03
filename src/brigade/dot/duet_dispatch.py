"""Duet-only dispatch admission. Reservations survive restarts and uncertain replies."""

from __future__ import annotations

from datetime import timezone
from typing import Any

from .contracts import require
from .fleet_tools import FleetTools

# Must match brigade.dot.events.TERMINAL; events is not imported here because it pulls in the event app.
TERMINAL = frozenset({"succeeded", "failed", "interrupted", "cancelled", "completed"})


class DuetFleetTools(FleetTools):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        require(self.runtime.dispatch_limits is not None, "invalid_config")
        with self.state.transaction() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS duet_dispatch (
                id INTEGER PRIMARY KEY, request_id TEXT UNIQUE, repository TEXT NOT NULL,
                open INTEGER NOT NULL, busy INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS duet_dispatch_attempts (
                ts REAL NOT NULL)""")

    def _fleet(self, action: str, payload: dict[str, Any], *, timeout: float, mutating: bool = False) -> dict[str, Any]:
        limits = self.runtime.dispatch_limits
        assert limits is not None
        reservation = None
        launching = action in {"submit", "followup"}
        with self.state.transaction() as connection:
            row = None
            if action in {"status", "wait", "followup", "interrupt"}:
                row = connection.execute(
                    "SELECT * FROM duet_dispatch WHERE request_id=?", (payload["request_id"],)
                ).fetchone()
                if row is None:
                    return {"error": "unknown_request"}
            repository = payload["repository"] if action == "submit" else row["repository"] if row else None
            if limits.repositories is not None and repository is not None and repository not in limits.repositories:
                return {"error": "repository_not_allowed"}
            if launching:
                if len(payload["prompt"].encode("utf-8")) > limits.prompt_max_bytes:
                    return {"error": "prompt_too_large"}
                if row is not None and row["busy"]:
                    return {"error": "request_busy"}
                open_count = connection.execute("SELECT COUNT(*) FROM duet_dispatch WHERE open=1").fetchone()[0]
                if (row is None or not row["open"]) and open_count >= limits.max_open:
                    return {"error": "open_limit"}
                now = self.clock()
                # Clock normally returns UTC; normalize before defining the daily boundary.
                midnight = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                used = connection.execute(
                    "SELECT COUNT(*) FROM duet_dispatch_attempts WHERE ts>=?", (midnight.timestamp(),)
                ).fetchone()[0]
                if used >= limits.daily_limit:
                    return {"error": "daily_limit"}
                connection.execute("INSERT INTO duet_dispatch_attempts VALUES (?)", (now.timestamp(),))
                connection.execute("DELETE FROM duet_dispatch_attempts WHERE ts<?", (midnight.timestamp(),))
                if row is None:
                    reservation = connection.execute(
                        "INSERT INTO duet_dispatch(repository,open,busy) VALUES (?,1,1)", (repository,)
                    ).lastrowid
                else:
                    reservation = row["id"]
                    connection.execute(
                        "UPDATE duet_dispatch SET open=1,busy=1,generation=generation+1 WHERE id=?", (reservation,)
                    )
        # Do not hold the SQLite write lock across fleet IO. A crash retains the reservation.
        result = super()._fleet(action, payload, timeout=timeout, mutating=mutating)
        nested = result.get("result") or {}
        request_id = result.get("request_id") or nested.get("request_id") or payload.get("request_id")
        with self.state.transaction() as connection:
            if launching:
                if result.get("ok") and isinstance(request_id, str):
                    connection.execute(
                        "UPDATE duet_dispatch SET request_id=?,open=?,busy=0 WHERE id=?",
                        (request_id, int(nested.get("state") not in TERMINAL), reservation),
                    )
                elif result.get("error") == "fleet_error":
                    connection.execute(
                        "UPDATE duet_dispatch SET open=?,busy=0 WHERE id=?",
                        (int(bool(row and row["open"])), reservation),
                    )
                # Unknown outcomes keep both the open slot and busy flag until operator reconciliation.
            elif action in {"status", "wait"} and result.get("ok") and nested.get("state") in TERMINAL:
                assert row is not None
                # A status concurrent with a followup cannot release its admission reservation.
                connection.execute(
                    "UPDATE duet_dispatch SET open=0 WHERE request_id=? AND busy=0 AND generation=?",
                    (request_id, row["generation"]),
                )
        return result
