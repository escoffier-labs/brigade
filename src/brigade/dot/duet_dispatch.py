"""Shared Dot and Duet dispatch admission. Reservations survive restarts and uncertain replies."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .contracts import require
from .fleet_tools import FleetTools

# Must match brigade.dot.events.TERMINAL; events is not imported here because it pulls in the event app.
TERMINAL = frozenset({"succeeded", "failed", "interrupted", "cancelled", "completed"})


class LimitedFleetTools(FleetTools):
    _owned_requests_only = False

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        require(self.runtime.dispatch_limits is not None, "invalid_config")
        with self.state.transaction() as connection:
            # Keep the existing table names for persisted Duet state. The listeners use separate state directories.
            connection.execute("""CREATE TABLE IF NOT EXISTS duet_dispatch (
                id INTEGER PRIMARY KEY, request_id TEXT UNIQUE, repository TEXT NOT NULL,
                open INTEGER NOT NULL, busy INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS duet_dispatch_attempts (
                ts REAL NOT NULL)""")

    @staticmethod
    def _journal_repository(connection: sqlite3.Connection, request_id: str) -> str | None:
        # Dot can enable limits after it has already submitted work.
        old = connection.execute(
            "SELECT summary FROM operations WHERE request_id=? AND tool='fleet_submit' ORDER BY id DESC LIMIT 1",
            (request_id,),
        ).fetchone()
        if old is None:
            return None
        value = json.loads(old["summary"]).get("repository")
        return value if isinstance(value, str) else None

    @staticmethod
    def _utc_day_start(now: datetime) -> float:
        return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    @staticmethod
    def _daily_attempts(connection: sqlite3.Connection, since: float) -> int:
        row = connection.execute("SELECT COUNT(*) FROM duet_dispatch_attempts WHERE ts>=?", (since,)).fetchone()
        return int(row[0])

    def dispatch_usage(self) -> dict[str, int]:
        with self.state.transaction() as connection:
            open_slots, unassigned = connection.execute(
                "SELECT COUNT(*), COUNT(*) - COUNT(request_id) FROM duet_dispatch WHERE open=1"
            ).fetchone()
            return {
                "open": open_slots,
                "attempts_used_today": self._daily_attempts(connection, self._utc_day_start(self.clock())),
                "open_without_request_id": unassigned,
            }

    def _fleet(self, action: str, payload: dict[str, Any], *, timeout: float, mutating: bool = False) -> dict[str, Any]:
        limits = self.runtime.dispatch_limits
        assert limits is not None
        launching = action in {"submit", "followup"}
        observing = action in {"status", "wait"}
        drives_work = not self._owned_requests_only and action in {"answer", "approve"}
        duet_interrupt = self._owned_requests_only and action == "interrupt"
        if not (launching or observing or drives_work or duet_interrupt):
            return super()._fleet(action, payload, timeout=timeout, mutating=mutating)

        reservation = None
        request_id = payload.get("request_id")
        with self.state.transaction() as connection:
            row = None
            if request_id is not None:
                row = connection.execute("SELECT * FROM duet_dispatch WHERE request_id=?", (request_id,)).fetchone()
            if self._owned_requests_only and (observing or action in {"followup", "interrupt"}) and row is None:
                return {"error": "unknown_request"}
            repository = payload["repository"] if action == "submit" else (row["repository"] or None) if row else None
            if repository is None and request_id is not None and (drives_work or action == "followup"):
                repository = self._journal_repository(connection, request_id)
            check_repository = launching or drives_work or self._owned_requests_only
            if limits.repositories is not None and check_repository:
                if repository is not None and repository not in limits.repositories:
                    return {"error": "repository_not_allowed"}
                if launching and repository is None:
                    return {"error": "unknown_request" if self._owned_requests_only else "repository_unknown"}
            if drives_work and repository is None:
                return {"error": "repository_unknown"}
            if launching:
                if len(payload["prompt"].encode("utf-8")) > limits.prompt_max_bytes:
                    return {"error": "prompt_too_large"}
                if row is not None and row["busy"]:
                    return {"error": "request_busy"}
                open_count = connection.execute("SELECT COUNT(*) FROM duet_dispatch WHERE open=1").fetchone()[0]
                if (row is None or not row["open"]) and open_count >= limits.max_open:
                    return {"error": "open_limit"}
                now = self.clock()
                midnight = self._utc_day_start(now)
                used = self._daily_attempts(connection, midnight)
                if used >= limits.daily_limit:
                    return {"error": "daily_limit"}
                connection.execute("INSERT INTO duet_dispatch_attempts VALUES (?)", (now.timestamp(),))
                connection.execute("DELETE FROM duet_dispatch_attempts WHERE ts<?", (midnight,))
                if row is None:
                    reservation = connection.execute(
                        "INSERT INTO duet_dispatch(request_id,repository,open,busy) VALUES (?,?,1,1)",
                        (request_id, repository or ""),
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
            elif observing and row is not None and result.get("ok") and nested.get("state") in TERMINAL:
                if row["busy"]:
                    # Started while a followup was launching: this terminal state may describe the
                    # previous turn, so it must not release the new turn's reservation.
                    return result
                # A status concurrent with a followup cannot release its admission reservation.
                connection.execute(
                    "UPDATE duet_dispatch SET open=0 WHERE request_id=? AND busy=0 AND generation=?",
                    (request_id, row["generation"]),
                )
        return result


class DuetFleetTools(LimitedFleetTools):
    _owned_requests_only = True

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        with self.state.transaction() as connection:
            # Completion notices are sent with the node credential, which a Duet listener never
            # holds. Tracked rows would retry forever and starve the poller, so Duet tracks none
            # (including rows BridgeState recovers from the journal) and polls with fleet_status.
            connection.execute("DELETE FROM tracked_requests")

    def fleet_submit(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = super().fleet_submit(*args, **kwargs)
        request_id = result.get("request_id")
        if isinstance(request_id, str):
            # finish_operation tracks every successful submit; Duet cannot deliver those notices.
            with self.state.transaction() as connection:
                connection.execute("DELETE FROM tracked_requests WHERE request_id=?", (request_id,))
        return result
