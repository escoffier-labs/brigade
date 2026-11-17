"""Bounded retention sweeps for Fleet Hub tables that otherwise grow forever.

The events journal, interactive session presence rows and cloud lease history
accumulate for the life of a Hub database. Each sweep drains at most
``RETENTION_SWEEP_MAX_BATCHES`` batches of ``RETENTION_SWEEP_BATCH`` rows per
tick, selecting candidates in a plain read and deleting only those keys under a
short ``BEGIN IMMEDIATE``, with the liveness condition repeated in the DELETE so
a row that became live in between survives. Live state is never deleted: for a
run the latest event, the first event, the newest seat event and the newest
harness event always stay, and an unreleased, unexpired lease is never removed.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .fleet_hub_event_bounds import (
    CLOUD_LEASES_RETENTION_DAYS,
    EVENTS_RETENTION_DAYS,
    INTERACTIVE_SESSIONS_RETENTION_DAYS,
    RETENTION_SWEEP_BATCH,
    RETENTION_SWEEP_MAX_BATCHES,
)

_LOG = logging.getLogger("brigade.fleet_hub_retention")

_DAY_SECONDS = 86_400
# SQLite builds before 3.32 cap host parameters at 999. Chunk key lists below
# that floor so the default 1,000-row batch still works there.
_MAX_DELETE_KEYS = 900

# Readers order a run's rows by (sequence, received_at, digest): started_at
# takes the first, and _last_known_seat plus the Deck seat/harness backfill
# take the newest row carrying the field. A row may go only when it is neither
# the latest nor the first, and it is not the newest row with a non-empty seat
# or harness. Those four rows per (node_id, run_id) always stay. The non-empty
# test matches the readers: not null, not blank after trim(), not a lone dash.
# /status compares the seat to '-' untrimmed and the Deck trims first, so the
# newest seat row under each rule is kept.
_EVENTS_LIVENESS_SQL = """
    received_at < ?
    AND EXISTS (
        SELECT 1 FROM events AS newer
        WHERE newer.node_id = events.node_id AND newer.run_id = events.run_id
          AND (newer.sequence, newer.received_at, newer.digest) > (events.sequence, events.received_at, events.digest)
    )
    AND EXISTS (
        SELECT 1 FROM events AS older
        WHERE older.node_id = events.node_id AND older.run_id = events.run_id
          AND (older.sequence, older.received_at, older.digest) < (events.sequence, events.received_at, events.digest)
    )
    AND (
        NOT (seat IS NOT NULL AND trim(seat) != '' AND seat != '-')
        OR EXISTS (
            SELECT 1 FROM events AS newer_seat
            WHERE newer_seat.node_id = events.node_id AND newer_seat.run_id = events.run_id
              AND newer_seat.seat IS NOT NULL AND trim(newer_seat.seat) != '' AND newer_seat.seat != '-'
              AND (newer_seat.sequence, newer_seat.received_at, newer_seat.digest)
                  > (events.sequence, events.received_at, events.digest)
        )
    )
    AND (
        NOT (seat IS NOT NULL AND trim(seat) != '' AND trim(seat) != '-')
        OR EXISTS (
            SELECT 1 FROM events AS newer_deck_seat
            WHERE newer_deck_seat.node_id = events.node_id AND newer_deck_seat.run_id = events.run_id
              AND newer_deck_seat.seat IS NOT NULL AND trim(newer_deck_seat.seat) != ''
              AND trim(newer_deck_seat.seat) != '-'
              AND (newer_deck_seat.sequence, newer_deck_seat.received_at, newer_deck_seat.digest)
                  > (events.sequence, events.received_at, events.digest)
        )
    )
    AND (
        NOT (harness IS NOT NULL AND trim(harness) != '' AND trim(harness) != '-')
        OR EXISTS (
            SELECT 1 FROM events AS newer_harness
            WHERE newer_harness.node_id = events.node_id AND newer_harness.run_id = events.run_id
              AND newer_harness.harness IS NOT NULL AND trim(newer_harness.harness) != ''
              AND trim(newer_harness.harness) != '-'
              AND (newer_harness.sequence, newer_harness.received_at, newer_harness.digest)
                  > (events.sequence, events.received_at, events.digest)
        )
    )
"""

_EVENTS_CANDIDATE_SQL = f"SELECT rowid FROM events WHERE {_EVENTS_LIVENESS_SQL} ORDER BY received_at, digest LIMIT ?"
_EVENTS_DELETE_SQL = f"DELETE FROM events WHERE rowid IN ({{keys}}) AND {_EVENTS_LIVENESS_SQL}"

_SESSIONS_LIVENESS_SQL = "heartbeat_at < ?"
_SESSIONS_CANDIDATE_SQL = (
    f"SELECT rowid FROM interactive_sessions WHERE {_SESSIONS_LIVENESS_SQL} ORDER BY heartbeat_at LIMIT ?"
)
_SESSIONS_DELETE_SQL = f"DELETE FROM interactive_sessions WHERE rowid IN ({{keys}}) AND {_SESSIONS_LIVENESS_SQL}"

# Released rows age from released_at. An unreleased row ages from expires_at
# and is only eligible once it is expired, so a live lease is never removed.
_LEASES_LIVENESS_SQL = "((released_at IS NOT NULL AND released_at < ?) OR (released_at IS NULL AND expires_at < ?))"
_LEASES_CANDIDATE_SQL = f"SELECT lease_id FROM cloud_leases WHERE {_LEASES_LIVENESS_SQL} ORDER BY lease_id LIMIT ?"
_LEASES_DELETE_SQL = f"DELETE FROM cloud_leases WHERE lease_id IN ({{keys}}) AND {_LEASES_LIVENESS_SQL}"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _delete(conn: sqlite3.Connection, statements: list[tuple[str, tuple[object, ...]]]) -> int:
    """Run bounded deletes in one short write transaction when the caller has none."""
    opened = not conn.in_transaction
    if opened:
        conn.execute("BEGIN IMMEDIATE")
    try:
        removed = 0
        for sql, params in statements:
            removed += max(conn.execute(sql, params).rowcount, 0)
        if opened:
            conn.commit()
    except BaseException:
        if opened:
            conn.rollback()
        raise
    return removed


def _candidate_keys(conn: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> list[Any]:
    """Read candidate keys without a write transaction, so the writer lock stays free."""
    return [row[0] for row in conn.execute(sql, params)]


def _delete_keys(conn: sqlite3.Connection, sql: str, keys: list[Any], params: tuple[object, ...]) -> int:
    """Delete exactly ``keys``, re-checking liveness, in one short write transaction.

    Key lists are chunked under SQLite's 999-variable floor and the chunks share
    one transaction, so the writer lock covers only this batch.
    """
    if not keys:
        return 0
    statements = []
    for start in range(0, len(keys), _MAX_DELETE_KEYS):
        chunk = keys[start : start + _MAX_DELETE_KEYS]
        placeholders = ",".join("?" for _ in chunk)
        statements.append((sql.format(keys=placeholders), (*chunk, *params)))
    return _delete(conn, statements)


def _drain(
    conn: sqlite3.Connection,
    *,
    batch: int,
    candidates: Callable[[], list[Any]],
    delete: Callable[[list[Any]], int],
) -> int:
    """Delete batches until one comes back short or the per-tick cap is reached.

    The reader holds no transaction and each delete commits on its own, so the
    writer lock is released between batches and other writers can interleave.
    """
    total = 0
    for _ in range(RETENTION_SWEEP_MAX_BATCHES):
        keys = candidates()
        if not keys:
            break
        total += delete(keys)
        if len(keys) < batch:
            break
    return total


def _delete_events(conn: sqlite3.Connection, keys: list[Any], cutoff: str) -> int:
    return _delete_keys(conn, _EVENTS_DELETE_SQL, keys, (cutoff,))


def _delete_sessions(conn: sqlite3.Connection, keys: list[Any], cutoff: str) -> int:
    return _delete_keys(conn, _SESSIONS_DELETE_SQL, keys, (cutoff,))


def _delete_leases(conn: sqlite3.Connection, keys: list[Any], cutoff: str, now: float) -> int:
    return _delete_keys(conn, _LEASES_DELETE_SQL, keys, (cutoff, now))


def sweep_events(
    conn: sqlite3.Connection,
    *,
    now: float,
    retention_days: int = EVENTS_RETENTION_DAYS,
    limit: int = RETENTION_SWEEP_BATCH,
) -> int:
    """Delete old events that no reader still needs for their run."""
    cutoff = _iso(now - retention_days * _DAY_SECONDS)
    return _drain(
        conn,
        batch=limit,
        candidates=lambda: _candidate_keys(conn, _EVENTS_CANDIDATE_SQL, (cutoff, limit)),
        delete=lambda keys: _delete_events(conn, keys, cutoff),
    )


def sweep_interactive_sessions(
    conn: sqlite3.Connection,
    *,
    now: float,
    retention_days: int = INTERACTIVE_SESSIONS_RETENTION_DAYS,
    limit: int = RETENTION_SWEEP_BATCH,
) -> int:
    """Delete session rows whose last heartbeat is older than the window."""
    cutoff = _iso(now - retention_days * _DAY_SECONDS)
    return _drain(
        conn,
        batch=limit,
        candidates=lambda: _candidate_keys(conn, _SESSIONS_CANDIDATE_SQL, (cutoff, limit)),
        delete=lambda keys: _delete_sessions(conn, keys, cutoff),
    )


def sweep_cloud_leases(
    conn: sqlite3.Connection,
    *,
    now: float,
    retention_days: int = CLOUD_LEASES_RETENTION_DAYS,
    limit: int = RETENTION_SWEEP_BATCH,
) -> int:
    """Delete released leases and expired unreleased leases older than the window."""
    cutoff_epoch = now - retention_days * _DAY_SECONDS
    cutoff = _iso(cutoff_epoch)
    return _drain(
        conn,
        batch=limit,
        candidates=lambda: _candidate_keys(conn, _LEASES_CANDIDATE_SQL, (cutoff, cutoff_epoch, limit)),
        delete=lambda keys: _delete_leases(conn, keys, cutoff, cutoff_epoch),
    )


def sweep_retention(conn: sqlite3.Connection, *, now: float | None = None) -> dict[str, int]:
    """Run every bounded sweep once and return the rows removed per table.

    One failing sweep is logged and does not stop the others or the sealed
    content purge that follows in ``maintain()``.
    """
    from .fleet_hub import FleetHubError

    moment = time.time() if now is None else now
    results = {"events": 0, "interactive_sessions": 0, "cloud_leases": 0}
    for table, sweep in (
        ("events", sweep_events),
        ("interactive_sessions", sweep_interactive_sessions),
        ("cloud_leases", sweep_cloud_leases),
    ):
        try:
            results[table] = sweep(conn, now=moment)
        except (sqlite3.Error, FleetHubError) as exc:
            _LOG.warning("retention sweep for %s failed, next tick retries: %s", table, exc)
    return results
