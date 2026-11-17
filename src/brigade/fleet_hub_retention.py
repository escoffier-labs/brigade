"""Bounded retention sweeps for Fleet Hub tables that otherwise grow forever.

The events journal, interactive session presence rows and cloud lease history
all accumulate for the life of a Hub database. Each sweep below deletes at most
``RETENTION_SWEEP_BATCH`` rows in one transaction, so a large backlog is
reclaimed over several runs instead of holding the writer lock for one long
delete. Every sweep is safe to run repeatedly and returns the number of rows it
removed.

Live state is never deleted: the latest event for each ``(node_id, run_id)``
stays even when the whole run is old, and an unreleased lease that has not
expired stays regardless of its age.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

from .fleet_hub_event_bounds import (
    CLOUD_LEASES_RETENTION_DAYS,
    EVENTS_RETENTION_DAYS,
    INTERACTIVE_SESSIONS_RETENTION_DAYS,
    RETENTION_SWEEP_BATCH,
)

_DAY_SECONDS = 86_400

# A row stays while a newer row exists for the same run, so the latest event of
# every run survives. Ties follow the /status ordering: sequence, received_at,
# digest.
_EVENTS_SQL = """
DELETE FROM events WHERE rowid IN (
    SELECT old.rowid FROM events AS old
    WHERE old.received_at < ?
      AND EXISTS (
        SELECT 1 FROM events AS newer
        WHERE newer.node_id = old.node_id
          AND newer.run_id = old.run_id
          AND (
            newer.sequence > old.sequence
            OR (newer.sequence = old.sequence AND newer.received_at > old.received_at)
            OR (
              newer.sequence = old.sequence
              AND newer.received_at = old.received_at
              AND newer.digest > old.digest
            )
          )
      )
    ORDER BY old.received_at, old.digest
    LIMIT ?
)
"""

_SESSIONS_SQL = """
DELETE FROM interactive_sessions WHERE rowid IN (
    SELECT rowid FROM interactive_sessions
    WHERE heartbeat_at < ?
    ORDER BY heartbeat_at
    LIMIT ?
)
"""

# Released rows age from released_at. An unreleased row ages from expires_at
# and is only eligible once it is expired, so a live lease is never removed.
_LEASES_SQL = """
DELETE FROM cloud_leases WHERE lease_id IN (
    SELECT lease_id FROM cloud_leases
    WHERE (released_at IS NOT NULL AND released_at < ?)
       OR (released_at IS NULL AND expires_at < ?)
    ORDER BY lease_id
    LIMIT ?
)
"""


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _delete(conn: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> int:
    """Run one bounded delete in its own transaction when the caller has none."""
    opened = not conn.in_transaction
    if opened:
        conn.execute("BEGIN IMMEDIATE")
    try:
        removed = conn.execute(sql, params).rowcount
        if opened:
            conn.commit()
    except BaseException:
        if opened:
            conn.rollback()
        raise
    return max(removed, 0)


def sweep_events(
    conn: sqlite3.Connection,
    *,
    now: float,
    retention_days: int = EVENTS_RETENTION_DAYS,
    limit: int = RETENTION_SWEEP_BATCH,
) -> int:
    """Delete old events that are not the latest row for their run."""
    cutoff = _iso(now - retention_days * _DAY_SECONDS)
    return _delete(conn, _EVENTS_SQL, (cutoff, limit))


def sweep_interactive_sessions(
    conn: sqlite3.Connection,
    *,
    now: float,
    retention_days: int = INTERACTIVE_SESSIONS_RETENTION_DAYS,
    limit: int = RETENTION_SWEEP_BATCH,
) -> int:
    """Delete session rows whose last heartbeat is older than the window."""
    cutoff = _iso(now - retention_days * _DAY_SECONDS)
    return _delete(conn, _SESSIONS_SQL, (cutoff, limit))


def sweep_cloud_leases(
    conn: sqlite3.Connection,
    *,
    now: float,
    retention_days: int = CLOUD_LEASES_RETENTION_DAYS,
    limit: int = RETENTION_SWEEP_BATCH,
) -> int:
    """Delete released leases and expired unreleased leases older than the window."""
    cutoff = _iso(now - retention_days * _DAY_SECONDS)
    return _delete(conn, _LEASES_SQL, (cutoff, now - retention_days * _DAY_SECONDS, limit))


def sweep_retention(conn: sqlite3.Connection, *, now: float | None = None) -> dict[str, int]:
    """Run every bounded sweep once and return the rows removed per table."""
    moment = time.time() if now is None else now
    return {
        "events": sweep_events(conn, now=moment),
        "interactive_sessions": sweep_interactive_sessions(conn, now=moment),
        "cloud_leases": sweep_cloud_leases(conn, now=moment),
    }
