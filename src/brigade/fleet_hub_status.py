"""Latest-status projection for Fleet Hub events.

Split from ``fleet_hub`` to keep that module under the size ratchet. The
projection is one row per (node_id, run_id); Hub ``received_at`` is the
authority for active TTL and stale-history aging.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from . import fleet_command_deck
from .fleet_event_freshness import observation_state

ACTIVE_EVENT_TTL_SECONDS = 30 * 60
STALE_HISTORY_AFTER_SECONDS = 24 * 60 * 60
BOARD_LIMIT = 200
BOARD_OFFSET_MAX = 1_000_000
MACHINE_RUN_LIMIT = 100
MACHINE_PAGE_RUN_LIMIT = 2_000

_LATEST_COLUMNS = (
    "node_id, run_id, repo, seat, harness, state, ts, sequence, digest, "
    "exit_status, capability_fingerprint, repo_identity, received_at, context"
)
_LATEST_PARTITION = (
    "SELECT e.node_id, e.run_id, e.sequence, e.digest, "
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


def _latest_sql(
    *,
    received_after: bool = False,
    node_ids: list[str] | None = None,
    resolve_last_known_seat: bool = False,
    resolve_started_at: bool = False,
) -> str:
    inner = _LATEST_PARTITION
    predicates = []
    if received_after:
        predicates.append("(e.harness = 'grokbot' OR e.received_at >= ?)")
    if node_ids:
        placeholders = ",".join("?" for _ in node_ids)
        predicates.append(f"(e.harness = 'grokbot' OR e.node_id IN ({placeholders}))")
    if predicates:
        inner = inner.replace(" FROM events e", f" FROM events e WHERE {' AND '.join(predicates)}")
    columns = _LATEST_COLUMNS
    if resolve_last_known_seat:
        columns += (
            ", CASE WHEN latest.seat IS NOT NULL AND trim(latest.seat) != '' AND trim(latest.seat) != '-' "
            "THEN latest.seat ELSE (SELECT e2.seat FROM events e2 "
            "WHERE e2.run_id = latest.run_id AND ((latest.harness = 'grokbot' AND e2.harness = 'grokbot') "
            "OR (COALESCE(latest.harness, '') != 'grokbot' AND e2.node_id = latest.node_id)) "
            "AND e2.seat IS NOT NULL AND trim(e2.seat) != '' AND e2.seat != '-' "
            "ORDER BY e2.sequence DESC, e2.received_at DESC, e2.digest DESC LIMIT 1) END AS last_known_seat"
        )
    if resolve_started_at:
        columns += (
            ", CASE WHEN latest.harness = 'grokbot' THEN "
            "(SELECT e2.ts FROM events e2 WHERE e2.run_id = latest.run_id AND e2.harness = 'grokbot' "
            "ORDER BY e2.sequence ASC, e2.received_at ASC, e2.digest ASC LIMIT 1) ELSE "
            "(SELECT e2.ts FROM events e2 WHERE e2.node_id = latest.node_id AND e2.run_id = latest.run_id "
            "AND (e2.harness IS NULL OR e2.harness != 'grokbot') "
            "ORDER BY e2.sequence ASC, e2.received_at ASC, e2.digest ASC LIMIT 1) END AS started_at"
        )
    outer = (
        f"SELECT {columns} FROM ({inner}) AS ranked "
        "JOIN events AS latest USING (node_id, run_id, sequence, digest) WHERE rn = 1"
    )
    if received_after:
        outer += " AND received_at >= ?"
    if node_ids:
        outer += f" AND node_id IN ({','.join('?' for _ in node_ids)})"
    return outer


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
        entry = _status_entry(
            conn,
            row,
            now=current,
            age_stale=include_all,
            stale_history_after_seconds=stale_history_after_seconds,
        )
        if not include_all and entry["state"] == "run.stale":
            continue
        result.append(entry)
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
    default 24h). Grokbot revisions are globally ranked before the horizon
    is applied so an older revision cannot replace a newer one. ``include_all=True`` (``?all=1``) drops the time window so
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
        params.extend((cutoff, cutoff))
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


def board_status_for_nodes(
    conn: sqlite3.Connection,
    node_ids: list[str],
    *,
    include_all: bool = False,
    now: datetime | None = None,
    history_window_seconds: int = STALE_HISTORY_AFTER_SECONDS,
    stale_history_after_seconds: int = STALE_HISTORY_AFTER_SECONDS,
    resolve_last_known_seat: bool = False,
    predicate: Callable[[dict[str, Any]], bool] | None = None,
    priority: Callable[[dict[str, Any]], tuple[Any, ...]] | None = None,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Return bounded matching runs first, with identities whose history was truncated."""
    if not node_ids:
        return [], set()
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    windowed = not include_all
    sql = _latest_sql(
        received_after=windowed,
        node_ids=node_ids,
        resolve_last_known_seat=resolve_last_known_seat,
        resolve_started_at=True,
    )
    params: list[Any] = []
    if windowed:
        cutoff = datetime.fromtimestamp(
            current.timestamp() - max(1, int(history_window_seconds)), tz=timezone.utc
        ).isoformat()
        params.append(cutoff)
    params.extend(node_ids)
    if windowed:
        params.append(cutoff)
    params.extend(node_ids)
    per_node: dict[str, list[tuple[tuple[Any, ...], dict[str, Any]]]] = {}
    truncated: set[str] = set()
    for row in conn.execute(sql, params):
        entry = _status_entry(
            conn,
            row,
            now=current,
            age_stale=include_all,
            stale_history_after_seconds=stale_history_after_seconds,
            resolve_last_known_seat=resolve_last_known_seat,
            resolve_started_at=True,
        )
        node_id = str(entry["node_id"])
        matches = predicate is None or predicate(entry)
        match_priority = priority(entry) if matches and priority is not None else ()
        age = _received_at_age_seconds(entry.get("received_at"), now=current)
        order = (0 if matches else 1, match_priority, age if age is not None else float("inf"), str(entry["run_id"]))
        ranked = per_node.setdefault(node_id, [])
        ranked.append((order, entry))
        ranked.sort(key=lambda item: item[0])
        if len(ranked) > MACHINE_RUN_LIMIT:
            ranked.pop()
            truncated.add(node_id)

    page_rows = sorted(
        (
            (rank, order, node_id, entry)
            for node_id, ranked in per_node.items()
            for rank, (order, entry) in enumerate(ranked)
        ),
        key=lambda item: (item[1][0], item[0], item[1][1:], item[2]),
    )
    if len(page_rows) > MACHINE_PAGE_RUN_LIMIT:
        truncated.update(row[2] for row in page_rows[MACHINE_PAGE_RUN_LIMIT:])
    return [row[3] for row in page_rows[:MACHINE_PAGE_RUN_LIMIT]], truncated


def board_machine_page(
    conn: sqlite3.Connection,
    *,
    include_all: bool = False,
    offset: int = 0,
    limit: int = BOARD_LIMIT,
    now: datetime | None = None,
    history_window_seconds: int = STALE_HISTORY_AFTER_SECONDS,
    stale_history_after_seconds: int = STALE_HISTORY_AFTER_SECONDS,
    predicate: Callable[[dict[str, Any]], bool] | None = None,
    resolve_last_known_seat: bool = True,
    priority: Callable[[dict[str, Any]], tuple[Any, ...]] | None = None,
) -> tuple[set[str], list[str], bool]:
    """Return complete matching identities and a prioritized bounded page.

    The query streams the latest projection once, retaining only one ranking
    value per matching node before choosing the requested page.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    windowed = not include_all
    page_offset = clamp_board_offset(offset)
    page_limit = clamp_board_limit(limit)
    sql = _latest_sql(received_after=windowed, resolve_last_known_seat=resolve_last_known_seat, resolve_started_at=True)
    params: list[str] = []
    if windowed:
        cutoff = datetime.fromtimestamp(
            current.timestamp() - max(1, int(history_window_seconds)),
            tz=timezone.utc,
        ).isoformat()
        params.extend((cutoff, cutoff))
    node_priorities: dict[str, tuple[Any, ...]] = {}
    for row in conn.execute(sql, params):
        entry = _status_entry(
            conn,
            row,
            now=current,
            age_stale=include_all,
            stale_history_after_seconds=stale_history_after_seconds,
            resolve_last_known_seat=resolve_last_known_seat,
            resolve_started_at=True,
        )
        node_id = entry["node_id"]
        if node_id and (predicate is None or predicate(entry)):
            key = priority(entry) if priority is not None else ()
            current_key = node_priorities.get(str(node_id))
            if current_key is None or key < current_key:
                node_priorities[str(node_id)] = key
    ordered_ids = sorted(node_priorities, key=lambda item: (node_priorities[item], item))
    page_ids = ordered_ids[page_offset : page_offset + page_limit]
    more = len(ordered_ids) > page_offset + page_limit
    return set(node_priorities), page_ids, more


def _status_entry(
    conn: sqlite3.Connection,
    row: tuple[Any, ...],
    *,
    now: datetime,
    age_stale: bool,
    stale_history_after_seconds: int,
    resolve_last_known_seat: bool = True,
    resolve_started_at: bool = False,
) -> dict[str, Any]:
    state = row[5]
    seat = row[14] if resolve_last_known_seat and len(row) > 14 else row[3]
    if resolve_last_known_seat and len(row) <= 14:
        seat = _last_known_seat(conn, row[0], row[1], row[3], harness=row[4])
    entry = {
        "node_id": row[0],
        "run_id": row[1],
        "repo": row[2],
        "seat": seat,
        "harness": row[4],
        "state": state,
        "ts": row[6],
        "sequence": row[7],
        "digest": row[8],
        "exit_status": row[9],
        "capability_fingerprint": row[10],
        "repo_identity": row[11],
        "received_at": row[12],
        "context": json.loads(row[13]) if row[13] is not None else None,
    }
    if resolve_started_at:
        entry["started_at"] = row[-1]
    if not fleet_command_deck.is_terminal_state(state):
        projected = observation_state(state, row[4], row[6], now=now)
        if projected != state:
            entry["original_state"] = state
            entry["state"] = projected
    if age_stale and entry["state"] != "external.unknown" and not fleet_command_deck.is_terminal_state(state):
        age = _received_at_age_seconds(row[12], now=now)
        if age is not None and age > stale_history_after_seconds:
            entry["original_state"] = state
            entry["state"] = "run.stale"
    return entry


def _last_known_seat(conn: sqlite3.Connection, node_id: str, run_id: str, latest: Any, *, harness: Any = None) -> Any:
    """Keep the last non-empty seat when a terminal event drops it."""
    if isinstance(latest, str) and latest.strip() and latest.strip() != "-":
        return latest
    identity = "harness = 'grokbot' AND run_id = ?" if harness == "grokbot" else "node_id = ? AND run_id = ?"
    params = (run_id,) if harness == "grokbot" else (node_id, run_id)
    row = conn.execute(
        f"SELECT seat FROM events WHERE {identity} "
        "AND seat IS NOT NULL AND trim(seat) != '' AND seat != '-' "
        "ORDER BY sequence DESC, received_at DESC, digest DESC LIMIT 1",
        params,
    ).fetchone()
    return row[0] if row else latest
