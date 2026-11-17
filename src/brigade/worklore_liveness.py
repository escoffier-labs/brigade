"""Derived owner liveness for one Worklore ownership snapshot.

The Hub already records the facts that decide liveness: interactive presence,
repo claims, the campaign lease deadline, and terminal execution events. This
module reads those facts and derives one fixed state word on every read. It
stores nothing and exposes only the state word, a basis word, and a freshness
word. Claim targets, session ids, process ids, paths, and capabilities of other
nodes never leave this module.

Every fact is scoped to this item and this ownership. A session or claim counts
only for the snapshot's repository identity, and a linked run counts only when
this owner node linked it after the current generation began. Another node's
run, a previous owner's run, or another repository's session or claim can
therefore never make this item read as live, terminal, or idle.

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
# An older Hub sends "unassessed". Readers accept it so an older Hub stays
# readable; the Hub itself only emits ``FRESHNESS_VALUES``.
LEGACY_FRESHNESS_VALUES = frozenset({"unassessed"})
STALE_AFTER_SECONDS = 1800
# Same item-status set as worklore_ownership.TERMINAL_STATUSES. Copied rather
# than imported because worklore_ownership imports this module.
_TERMINAL_STATUSES = frozenset({"completed", "canceled", "archived"})
# One more than the per-node active claim quota in
# fleet_hub_claim_cardinality.MAX_ACTIVE_CLAIMS_PER_NODE. Live claims (a
# deadline after now) sort before every expired claim, so this bound always
# covers all of them.
_CLAIM_ROW_LIMIT = 1025
_CASEFOLD_FUNCTION = "brigade_casefold"
# The node's newest session row for one repository. The CASE clause is the
# exact predicate fleet_hub_sessions.count_live_by_node uses to exclude a
# finished v2 cloud report; keep the two in step.
_SESSIONS_SQL = (
    "SELECT state, expires_at, heartbeat_at, ended_at, "
    "CASE WHEN coalesce(json_extract(cloud_context_json, '$.version'), 0) = 2 "
    "AND coalesce(json_extract(cloud_context_json, '$.reported_state'), '') "
    "IN ('completed', 'failed', 'cancelled') THEN 1 ELSE 0 END "
    "FROM interactive_sessions "
    "WHERE node_id = ? AND brigade_casefold(repo_identity) = ? "
    "ORDER BY coalesce(ended_at, heartbeat_at) DESC, heartbeat_at DESC, started_at DESC, harness, session_id "
    "LIMIT 1"
)
# Any live session for the repository, whatever the newest row is. A second
# session that ended later must never hide one that is still live.
_LIVE_SESSION_SQL = (
    "SELECT MAX(heartbeat_at) FROM interactive_sessions "
    "WHERE node_id = ? AND brigade_casefold(repo_identity) = ? AND state = 'active' AND expires_at > ? "
    "AND NOT (coalesce(json_extract(cloud_context_json, '$.version'), 0) = 2 "
    "AND coalesce(json_extract(cloud_context_json, '$.reported_state'), '') "
    "IN ('completed', 'failed', 'cancelled'))"
)
_CLAIMS_SQL = (
    "SELECT target, expires_at, renewed_at, acquired_at FROM claims "
    "WHERE owner_node = ? AND expires_at > ? ORDER BY expires_at DESC, target LIMIT ?"
)
# Only this owner node's link, and only a link recorded after the ownership
# event that began the current generation.
_LINK_SQL = (
    "SELECT node_id, run_id FROM work_events "
    "WHERE work_id = ? AND event_type = 'execution-linked' AND node_id = ? AND seq > ? "
    "ORDER BY seq DESC LIMIT 1"
)
_RUN_SQL = "SELECT state, ts FROM events WHERE node_id = ? AND run_id = ? ORDER BY sequence DESC, ts DESC LIMIT 1"


@dataclass(frozen=True)
class OwnerFacts:
    """Only the booleans and timestamp that decide owner liveness.

    Every field is scoped: session and claim facts belong to the snapshot's
    repository identity, and ``run_terminal`` belongs to this owner node in the
    current ownership generation.
    """

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


def _read_all(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    """One bounded multi-row read; an older database without the table has no fact."""
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def _register_casefold(conn: sqlite3.Connection) -> None:
    """Register the Unicode-aware fold the rest of the Hub uses in SQL.

    ``fleet_hub_sessions`` registers the same function for its filters, and a
    SQLite ``NOCASE`` collation folds ASCII only. Registering it twice on one
    connection replaces it with the same behavior.
    """
    conn.create_function(
        _CASEFOLD_FUNCTION, 1, lambda value: "" if value is None else str(value).casefold(), deterministic=True
    )


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
    """Newest valid TEXT stamp. Non-string values carry no comparable fact."""
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


def _repo_identity(snapshot: Mapping[str, Any]) -> str | None:
    """The checkpointed repository, or the campaign member's repository before the first checkpoint."""
    if not isinstance(snapshot, Mapping):
        return None
    for key in ("repo_identity", "campaign_repo"):
        raw = snapshot.get(key)
        if isinstance(raw, str) and raw:
            return raw
    return None


def _names_repo(target: object, repo: str) -> bool:
    """Whether a claim target names this repository.

    A target is the repository key with an optional ``@<worktree>`` suffix
    (see ``fleet_claim_target``) or an issue suffix, so only the base key is
    compared, case-insensitively.
    """
    if not isinstance(target, str) or not target:
        return False
    base = target.split("#", 1)[0].split("@", 1)[0]
    return bool(base) and base.casefold() == repo.casefold()


def _ended_after(ended_at: object, ownership_updated: object) -> bool:
    ended, updated = _parse_stamp(ended_at), _parse_stamp(ownership_updated)
    return ended is not None and updated is not None and ended > updated


def _session_facts(
    conn: sqlite3.Connection,
    owner_node: str,
    repo: str,
    *,
    now_epoch: float,
    ownership_updated: object,
) -> tuple[bool, bool, tuple[object, ...]]:
    """Live and counted-end facts from the node's sessions for this repository.

    Any live session makes the owner live. Only when none is live does the
    newest row decide a counted end, so a crashed session that is newer than an
    ended one hides the end.
    """
    _register_casefold(conn)
    alive = _read(conn, _LIVE_SESSION_SQL, (owner_node, repo.casefold(), now_epoch))
    if alive is not None and alive[0] is not None:
        return True, False, (alive[0],)
    row = _read(conn, _SESSIONS_SQL, (owner_node, repo.casefold()))
    if row is None:
        return False, False, ()
    state, _expires_at, heartbeat_at, ended_at, _finished_cloud = row
    ended = state == "ended" and _ended_after(ended_at, ownership_updated)
    stamps: tuple[object, ...] = (heartbeat_at, ended_at) if ended else (heartbeat_at,)
    return False, bool(ended), stamps


def _claim_facts(
    conn: sqlite3.Connection, owner_node: str, repo: str, *, now_epoch: float
) -> tuple[bool, tuple[object, ...]]:
    """Live claims this node holds for this repository only."""
    live = False
    stamps: list[object] = []
    rows = _read_all(conn, _CLAIMS_SQL, (owner_node, now_epoch, _CLAIM_ROW_LIMIT))
    for target, _expires_at, renewed_at, acquired_at in rows:
        if not _names_repo(target, repo):
            continue
        live = True
        stamps.extend((renewed_at, acquired_at))
    return live, tuple(stamps)


def _linked_run(
    conn: sqlite3.Connection, work_id: str, owner_node: str, generation_start: int | None
) -> tuple[bool, str | None]:
    """Terminality of the newest run this owner linked in the current generation.

    ``generation_start`` is the work event sequence at which the current
    ownership generation began. Without it no link is admitted, which can only
    under-report a terminal run and never make a live owner read terminal.
    """
    if generation_start is None:
        return False, None
    link = _read(conn, _LINK_SQL, (work_id, owner_node, generation_start))
    if link is None or not isinstance(link[0], str) or not isinstance(link[1], str):
        return False, None
    event = _read(conn, _RUN_SQL, (link[0], link[1]))
    if event is None:
        return False, None
    terminal = isinstance(event[0], str) and is_terminal_state(event[0])
    return terminal, event[1] if isinstance(event[1], str) else None


def owner_facts(
    conn: sqlite3.Connection,
    snapshot: Mapping[str, Any],
    work_id: str,
    *,
    now: str | datetime,
    generation_start: int | None = None,
) -> OwnerFacts:
    """Read the scoped fact set for one ownership snapshot. Never raises for missing data."""
    raw_owner = snapshot.get("owner_node") if isinstance(snapshot, Mapping) else None
    owner_node = raw_owner if isinstance(raw_owner, str) and raw_owner else None
    item_terminal = _item_terminal(conn, work_id)
    if owner_node is None:
        return OwnerFacts(item_terminal=item_terminal)
    now_stamp = _parse_stamp(now)
    if now_stamp is None:
        return OwnerFacts(owner_node=owner_node, item_terminal=item_terminal)
    now_epoch = now_stamp.timestamp()

    has_live_session = session_ended = has_live_claim = False
    stamps: list[object] = []
    repo = _repo_identity(snapshot)
    if repo is not None:
        has_live_session, session_ended, session_stamps = _session_facts(
            conn, owner_node, repo, now_epoch=now_epoch, ownership_updated=snapshot.get("updated_at")
        )
        has_live_claim, claim_stamps = _claim_facts(conn, owner_node, repo, now_epoch=now_epoch)
        stamps.extend(session_stamps)
        stamps.extend(claim_stamps)
    # Without a repository no session or claim was checked, so a terminal run
    # cannot prove the owner is gone. Withhold it and read unknown.
    run_terminal, run_stamp = False, None
    if repo is not None:
        run_terminal, run_stamp = _linked_run(conn, work_id, owner_node, generation_start)
    if run_stamp is not None:
        stamps.append(run_stamp)

    return OwnerFacts(
        owner_node=owner_node,
        item_terminal=item_terminal,
        has_live_session=has_live_session,
        session_ended=session_ended,
        has_live_claim=has_live_claim,
        lease_expired=_deadline_lapsed(snapshot.get("expires_at"), now_stamp),
        run_terminal=run_terminal,
        newest_fact_at=_latest(tuple(stamps)),
    )


def _verdict(facts: OwnerFacts) -> tuple[str, str]:
    """Shared precedence: first match wins, in the documented order."""
    if not facts.owner_node or facts.item_terminal:
        return "unknown", "none"
    # A lapsed lease wins: the rest of the system already treats the item as
    # reclaimable, so no session or claim may mask it.
    if facts.lease_expired:
        return "expired-observation", "lease"
    if facts.has_live_session:
        return "live-owner", "session"
    if facts.has_live_claim:
        return "live-owner", "claim"
    if facts.run_terminal:
        return "terminal-execution", "run"
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
