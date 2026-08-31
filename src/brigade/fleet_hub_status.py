"""Latest-status projection for Fleet Hub events.

Split from ``fleet_hub`` to keep that module under the size ratchet. The
projection is one row per (node_id, run_id); Hub ``received_at`` is the
authority for active TTL and stale-history aging.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import fleet_command_deck

ACTIVE_EVENT_TTL_SECONDS = 30 * 60
STALE_HISTORY_AFTER_SECONDS = 24 * 60 * 60


def _received_at_is_active(value: object, *, now: datetime, ttl_seconds: int) -> bool:
    if not isinstance(value, str):
        return False
    try:
        received = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    return (now - received.astimezone(timezone.utc)).total_seconds() <= ttl_seconds


def _received_at_age_seconds(value: object, *, now: datetime) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        received = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    return (now - received.astimezone(timezone.utc)).total_seconds()


def latest_status(
    conn: sqlite3.Connection,
    *,
    include_all: bool = False,
    now: datetime | None = None,
    active_ttl_seconds: int = ACTIVE_EVENT_TTL_SECONDS,
    stale_history_after_seconds: int = STALE_HISTORY_AFTER_SECONDS,
) -> list[dict[str, Any]]:
    """Latest event per run; non-terminal runs unless include_all.

    Normal fleet runs are keyed by ``(node_id, run_id)``. Grok Bot jobs are
    hub-owned, so their lifecycle revisions are keyed globally by ``run_id``
    even when the feed node and claimant differ. Ties on sequence resolve to
    the most recently received, then the larger digest, so the view never
    shows a run twice.

    History views (``include_all``) age old nonterminal rows to synthetic
    ``run.stale`` from Hub ``received_at``, keeping the recorded state in
    ``original_state``. Event history itself is not rewritten.
    """
    rows = conn.execute(
        "SELECT node_id, run_id, repo, seat, harness, state, ts, sequence, digest, exit_status, "
        "capability_fingerprint, repo_identity, received_at FROM ("
        "  SELECT e.*, ROW_NUMBER() OVER ("
        "    PARTITION BY CASE WHEN harness = 'grokbot' THEN 'grokbot' ELSE 'node:' || node_id END, run_id "
        "    ORDER BY sequence DESC, received_at DESC, digest DESC"
        "  ) AS rn FROM events e"
        ") WHERE rn = 1 ORDER BY node_id, run_id"
    ).fetchall()
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    result = []
    for row in rows:
        state = row[5]
        if not include_all and (
            fleet_command_deck.is_terminal_state(state)
            or not _received_at_is_active(row[12], now=current, ttl_seconds=active_ttl_seconds)
        ):
            continue
        entry = {
            "node_id": row[0],
            "run_id": row[1],
            "repo": row[2],
            "seat": _last_known_seat(conn, row[0], row[1], row[3]),
            "harness": row[4],
            "state": state,
            "ts": row[6],
            "sequence": row[7],
            "digest": row[8],
            "exit_status": row[9],
            "capability_fingerprint": row[10],
            "repo_identity": row[11],
            "received_at": row[12],
        }
        if include_all and not fleet_command_deck.is_terminal_state(state):
            age = _received_at_age_seconds(row[12], now=current)
            if age is not None and age > stale_history_after_seconds:
                entry["original_state"] = state
                entry["state"] = "run.stale"
        result.append(entry)
    return result


def _last_known_seat(conn: sqlite3.Connection, node_id: str, run_id: str, latest: Any) -> Any:
    """Keep the last non-empty seat when a terminal event drops it."""
    if isinstance(latest, str) and latest.strip() and latest.strip() != "-":
        return latest
    row = conn.execute(
        "SELECT seat FROM events WHERE node_id = ? AND run_id = ? "
        "AND seat IS NOT NULL AND trim(seat) != '' AND seat != '-' "
        "ORDER BY sequence DESC, received_at DESC, digest DESC LIMIT 1",
        (node_id, run_id),
    ).fetchone()
    return row[0] if row else latest
