"""Latest-status projection for Fleet Hub events.

Split from ``fleet_hub`` to keep that module under the size ratchet. The
projection is one row per (node_id, run_id); Hub ``received_at`` is the
authority for active TTL and stale-history aging.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import fleet_command_deck

ACTIVE_EVENT_TTL_SECONDS = 30 * 60
STALE_HISTORY_AFTER_SECONDS = 24 * 60 * 60
BOARD_LIMIT = 200
BOARD_OFFSET_MAX = 1_000_000

_LATEST_COLUMNS = (
    "node_id, run_id, repo, seat, harness, state, ts, sequence, digest, "
    "exit_status, capability_fingerprint, repo_identity, received_at"
)
_LATEST_PARTITION = (
    "SELECT e.node_id, e.run_id, e.repo, e.seat, e.harness, e.state, e.ts, e.sequence, "
    "e.digest, e.exit_status, e.capability_fingerprint, e.repo_identity, e.received_at, "
    "ROW_NUMBER() OVER ("
    "  PARTITION BY CASE WHEN e.harness = 'grokbot' THEN 'grokbot' ELSE 'node:' || e.node_id END, e.run_id "
    "  ORDER BY e.sequence DESC, e.received_at DESC, e.digest DESC"
    ") AS rn FROM events e"
)


@dataclass(frozen=True)
class BoardStatus:
    """Bounded latest-state page for the machine and repo boards."""

    runs: list[dict[str, Any]]
    more: bool
    offset: int
    limit: int


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


def _latest_sql(*, received_after: bool = False) -> str:
    inner = _LATEST_PARTITION
    if received_after:
        inner += " WHERE e.received_at >= ?"
    return f"SELECT {_LATEST_COLUMNS} FROM ({inner}) WHERE rn = 1"


def board_latest_sql(*, windowed: bool) -> str:
    """Latest-state-per-run page used by the HTML boards.

    Default boards window the inner scan by ``received_at`` so cost follows
    the horizon, not the journal. ``all=1`` skips the window and still
    applies LIMIT/OFFSET. Tests pin this shape; do not drop either bound.
    """
    return f"{_latest_sql(received_after=windowed)} ORDER BY received_at DESC, node_id, run_id LIMIT ? OFFSET ?"


def clamp_board_offset(raw: object) -> int:
    """Non-negative board page offset; hostile or blank values become 0."""
    try:
        value = int(str(raw).strip() or 0)
    except (TypeError, ValueError):
        return 0
    if value < 0:
        return 0
    return min(value, BOARD_OFFSET_MAX)


def clamp_board_limit(raw: int, *, default: int = BOARD_LIMIT) -> int:
    if raw < 1:
        return default
    return min(raw, BOARD_LIMIT)


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
    rows = conn.execute(f"{_latest_sql()} ORDER BY node_id, run_id").fetchall()
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    result = []
    for row in rows:
        state = row[5]
        if not include_all and (
            fleet_command_deck.is_terminal_state(state)
            or not _received_at_is_active(row[12], now=current, ttl_seconds=active_ttl_seconds)
        ):
            continue
        result.append(
            _status_entry(
                conn,
                row,
                now=current,
                age_stale=include_all,
                stale_history_after_seconds=stale_history_after_seconds,
            )
        )
    return result


def board_status(
    conn: sqlite3.Connection,
    *,
    include_all: bool = False,
    offset: int = 0,
    limit: int = BOARD_LIMIT,
    now: datetime | None = None,
    history_window_seconds: int = STALE_HISTORY_AFTER_SECONDS,
    stale_history_after_seconds: int = STALE_HISTORY_AFTER_SECONDS,
) -> BoardStatus:
    """Latest-state-per-run for the HTML boards, windowed and LIMITed.

    Default (``include_all=False``) keeps live runs and terminal history
    inside ``history_window_seconds`` (deck ``stale_history_after_seconds``,
    default 24h) by filtering ``events.received_at`` before the window
    function. ``include_all=True`` (``?all=1``) drops the time window so
    older terminals remain visible, still paginated. Neither path rewrites
    or deletes events.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    page_limit = clamp_board_limit(limit)
    page_offset = clamp_board_offset(offset)
    windowed = not include_all
    sql = board_latest_sql(windowed=windowed)
    params: list[Any] = []
    if windowed:
        cutoff = datetime.fromtimestamp(
            current.timestamp() - max(1, int(history_window_seconds)),
            tz=timezone.utc,
        ).isoformat()
        params.append(cutoff)
    params.extend((page_limit + 1, page_offset))
    rows = conn.execute(sql, params).fetchall()
    more = len(rows) > page_limit
    mapped = [
        _status_entry(
            conn,
            row,
            now=current,
            age_stale=include_all,
            stale_history_after_seconds=stale_history_after_seconds,
        )
        for row in rows[:page_limit]
    ]
    return BoardStatus(runs=mapped, more=more, offset=page_offset, limit=page_limit)


def _status_entry(
    conn: sqlite3.Connection,
    row: tuple[Any, ...],
    *,
    now: datetime,
    age_stale: bool,
    stale_history_after_seconds: int,
) -> dict[str, Any]:
    state = row[5]
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
    if age_stale and not fleet_command_deck.is_terminal_state(state):
        age = _received_at_age_seconds(row[12], now=now)
        if age is not None and age > stale_history_after_seconds:
            entry["original_state"] = state
            entry["state"] = "run.stale"
    return entry


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
