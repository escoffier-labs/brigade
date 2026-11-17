"""Derived owner liveness for one Worklore ownership snapshot.

The Hub already records the facts that decide liveness: interactive presence,
repo claims, the campaign lease deadline, and terminal execution events. This
module reads those facts and derives one fixed state word on every read. It
stores nothing and exposes only the state word, a basis word, and a freshness
word. Claim targets, session ids, process ids, paths, and capabilities of other
nodes never leave this module.

Every missing table, NULL, or malformed timestamp fails to ``unknown`` instead
of raising. Reads are parameterized and bounded.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .fleet_command_deck import is_terminal_state, snapshot_freshness

LIVENESS_VALUES = frozenset({"live-owner", "idle-owner", "expired-observation", "terminal-execution", "unknown"})
BASIS_VALUES = frozenset({"session", "claim", "lease", "run", "none"})
FRESHNESS_VALUES = frozenset({"recent", "stale", "future", "unknown"})
STALE_AFTER_SECONDS = 1800
# Same item-status set as worklore_ownership.TERMINAL_STATUSES. Copied rather
# than imported because worklore_ownership imports this module.
_TERMINAL_STATUSES = frozenset({"completed", "canceled", "archived"})
_SESSIONS_SQL = (
    "SELECT MAX(CASE WHEN state = 'active' AND expires_at > ? THEN 1 ELSE 0 END), "
    "MAX(CASE WHEN state = 'ended' AND ended_at IS NOT NULL THEN 1 ELSE 0 END), "
    "MAX(heartbeat_at), MAX(ended_at) "
    "FROM interactive_sessions WHERE node_id = ?"
)
_CLAIMS_SQL = (
    "SELECT MAX(CASE WHEN expires_at > ? THEN 1 ELSE 0 END), MAX(renewed_at), MAX(acquired_at) "
    "FROM claims WHERE owner_node = ?"
)
_LINK_SQL = (
    "SELECT node_id, run_id FROM work_events "
    "WHERE work_id = ? AND event_type = 'execution-linked' ORDER BY seq DESC LIMIT 1"
)
_RUN_SQL = "SELECT state, ts FROM events WHERE node_id = ? AND run_id = ? ORDER BY sequence DESC, ts DESC LIMIT 1"


@dataclass(frozen=True)
class OwnerFacts:
    """Only the booleans and timestamp that decide owner liveness."""

    owner_node: str | None = None
    item_terminal: bool = False
    has_live_session: bool = False
    session_ended: bool = False
    has_live_claim: bool = False
    lease_expired: bool = False
    run_terminal: bool = False
    newest_fact_at: str | None = None


def _read(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> tuple[Any, ...] | None:
    """One bounded read; an older database without the table has no fact."""
    try:
        return conn.execute(sql, params).fetchone()
    except sqlite3.OperationalError:
        return None


def _parse_stamp(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else None
    if not isinstance(raw, str) or not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None


def _latest(stamps: tuple[object, ...]) -> str | None:
    best: tuple[datetime, str] | None = None
    for raw in stamps:
        parsed = _parse_stamp(raw)
        if parsed is not None and isinstance(raw, str) and (best is None or parsed > best[0]):
            best = (parsed, raw)
    return None if best is None else best[1]


def _deadline_lapsed(deadline: object, now: datetime | None) -> bool:
    # Same rule as worklore_ownership.expired: only a valid explicit deadline
    # with a timezone lapses. Copied because that module imports this one.
    if not isinstance(deadline, str) or now is None:
        return False
    try:
        stamp = datetime.fromisoformat(deadline)
    except ValueError:
        return False
    return stamp.tzinfo is not None and stamp <= now


def _item_terminal(conn: sqlite3.Connection, work_id: str) -> bool:
    row = _read(conn, "SELECT status FROM work_items WHERE work_id = ?", (work_id,))
    return row is not None and isinstance(row[0], str) and row[0] in _TERMINAL_STATUSES


def owner_facts(
    conn: sqlite3.Connection,
    snapshot: Mapping[str, Any],
    work_id: str,
    *,
    now: str | datetime,
) -> OwnerFacts:
    """Read the fixed fact set for one ownership snapshot. Never raises for missing data."""
    raw_owner = snapshot.get("owner_node") if isinstance(snapshot, Mapping) else None
    owner_node = raw_owner if isinstance(raw_owner, str) and raw_owner else None
    item_terminal = _item_terminal(conn, work_id)
    if owner_node is None:
        return OwnerFacts(item_terminal=item_terminal)
    now_stamp = _parse_stamp(now)
    if now_stamp is None:
        return OwnerFacts(owner_node=owner_node, item_terminal=item_terminal)
    now_epoch = now_stamp.timestamp()

    sessions = _read(conn, _SESSIONS_SQL, (now_epoch, owner_node))
    has_live_session = bool(sessions and sessions[0])
    session_ended = bool(sessions and sessions[1])
    claims = _read(conn, _CLAIMS_SQL, (now_epoch, owner_node))
    has_live_claim = bool(claims and claims[0])

    run_terminal = False
    run_stamp: str | None = None
    link = _read(conn, _LINK_SQL, (work_id,))
    if link is not None and isinstance(link[0], str) and isinstance(link[1], str):
        event = _read(conn, _RUN_SQL, (link[0], link[1]))
        if event is not None:
            run_terminal = isinstance(event[0], str) and is_terminal_state(event[0])
            run_stamp = event[1] if isinstance(event[1], str) else None

    session_stamps = (sessions[2], sessions[3]) if sessions else ()
    claim_stamps = (claims[1], claims[2]) if claims else ()
    return OwnerFacts(
        owner_node=owner_node,
        item_terminal=item_terminal,
        has_live_session=has_live_session,
        session_ended=session_ended,
        has_live_claim=has_live_claim,
        lease_expired=_deadline_lapsed(snapshot.get("expires_at"), now_stamp),
        run_terminal=run_terminal,
        newest_fact_at=_latest((*session_stamps, *claim_stamps, run_stamp)),
    )


def _verdict(facts: OwnerFacts) -> tuple[str, str]:
    """Shared precedence: first match wins, in the documented order."""
    if not facts.owner_node or facts.item_terminal:
        return "unknown", "none"
    if not (
        facts.has_live_session
        or facts.session_ended
        or facts.has_live_claim
        or facts.lease_expired
        or facts.run_terminal
        or facts.newest_fact_at is not None
    ):
        return "unknown", "none"
    if facts.run_terminal and not facts.has_live_session and not facts.has_live_claim:
        return "terminal-execution", "run"
    if facts.has_live_session:
        return "live-owner", "session"
    if facts.has_live_claim:
        return "live-owner", "claim"
    if facts.lease_expired and not facts.session_ended:
        return "expired-observation", "lease"
    if facts.session_ended:
        return "idle-owner", "session"
    return "unknown", "none"


def derive_owner_liveness(facts: OwnerFacts) -> str:
    """Pure verdict: one of the five liveness state words."""
    return _verdict(facts)[0]


def derive_owner_liveness_basis(facts: OwnerFacts) -> str:
    """Pure basis word for the rule that fired: session, claim, lease, run, or none."""
    return _verdict(facts)[1]


def freshness_for(
    stamp: object,
    *,
    now: object,
    stale_after_seconds: int = STALE_AFTER_SECONDS,
) -> str:
    """Freshness of a fact stamp: recent, stale, future, or unknown.

    Reuses the deck's ``snapshot_freshness`` rule (future when the stamp is
    ahead of now, stale past the threshold, recent otherwise).
    """
    observed = _parse_stamp(stamp)
    current = _parse_stamp(now)
    if observed is None or current is None:
        return "unknown"
    return snapshot_freshness(observed, now=current, stale_after_seconds=stale_after_seconds)[1]
