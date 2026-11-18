"""Hub record of whether each node's native T3 observer is currently reporting.

T3 session presence rows expire after their TTL, so a node whose observer is failing
looks exactly like a node with no sessions. This table records, per node, when the Hub
last accepted a successful observation, so readers can tell "zero sessions" from
"unknown". Both timestamps are the Hub's own clock at receipt: a node-supplied time
never decides freshness.

A node with no row here has never reported health. Session views leave such a node alone, but
placement treats its sessions as unknown (every host runs the observer, so a missing row means
the node is not reporting). The table is optional: it is created on the first report, never at hub startup.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from . import fleet_hub_policy
from .fleet_hub import FleetHubError, FleetHubForbidden
from .fleet_hub_routing import _bounded, _machine_for_node, _parse_stamp

# Equal to the T3 session presence TTL (fleet_openclaw_observer.TTL_SECONDS): once a node's
# sessions could have expired because it stopped reporting, its count is unknown. With a 120 s
# timer, a node is still known after two missed cycles (age 240 s) and unknown on the third (360 s).
STALE_AFTER_SECONDS = 300
_FIELDS = frozenset({"node_id", "ok"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_t3_observations (
    node_id TEXT NOT NULL PRIMARY KEY,
    last_success_at TEXT,
    last_attempt_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Optional table, created on first report. Not part of the hub startup schema."""
    conn.execute(_SCHEMA)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def observe_t3(conn: sqlite3.Connection, body: Any, caller_node: str) -> dict[str, Any]:
    """Record one observer attempt for the calling node. Success is stamped only when ``ok`` is true.

    Precondition: the caller holds no uncommitted writes it cannot lose. The policy handler has
    written nothing before this action, and each Hub request has its own connection.
    """
    payload = body if isinstance(body, Mapping) else None
    if payload is None:
        raise FleetHubError("t3 observation report must be a JSON object")
    unknown = sorted(str(key) for key in payload if key not in _FIELDS)
    if unknown:
        raise FleetHubError(f"t3 observation report has unknown field {unknown[0]!r}")
    node_id = _bounded(payload.get("node_id"), "node_id")
    caller = _bounded(caller_node, "caller_node")
    assert node_id is not None and caller is not None
    if node_id != caller:
        raise FleetHubForbidden(f"t3 observation node_id {node_id!r} does not match the caller ({caller})")
    ok = payload.get("ok")
    if type(ok) is not bool:
        raise FleetHubError("t3 observation field 'ok' must be a boolean")
    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    try:
        document = fleet_hub_policy.current_policy(conn)["document"]
        machine = _machine_for_node(document, node_id)
        if machine is None:
            raise FleetHubError(f"t3 observation node_id {node_id!r} is not a configured machine")
        ensure_schema(conn)
        stamp = _now().isoformat()
        # A failed attempt keeps the previous success time, so health never moves backward by itself.
        conn.execute(
            "INSERT INTO fleet_t3_observations (node_id, last_success_at, last_attempt_at, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(node_id) DO UPDATE SET "
            "last_success_at=CASE WHEN ? THEN excluded.last_success_at ELSE last_success_at END, "
            "last_attempt_at=excluded.last_attempt_at, updated_at=excluded.updated_at",
            (node_id, stamp if ok else None, stamp, stamp, 1 if ok else 0),
        )
        if opened:
            conn.commit()
        return {"node_id": node_id, "machine": machine, "ok": ok, "observed_at": stamp}
    except BaseException:
        if opened:
            conn.rollback()
        raise


def classify_nodes(
    conn: sqlite3.Connection, now: datetime | None = None, ttl: int = STALE_AFTER_SECONDS
) -> tuple[set[str], set[str]] | None:
    """Pure read: (fresh, stale) node ids among nodes that have a health row, or None if unreadable.

    Fresh means the last success is younger than ``ttl``. A missing table means no node ever
    reported, so both sets are empty. Never writes.
    """
    moment = now or _now()
    try:
        rows = conn.execute("SELECT node_id, last_success_at FROM fleet_t3_observations").fetchall()
    except sqlite3.OperationalError as exc:
        return (set(), set()) if "no such table" in str(exc) else None
    except sqlite3.Error:
        return None
    fresh: set[str] = set()
    stale: set[str] = set()
    for node_id, last_success in rows:
        try:
            success = _parse_stamp(last_success) if last_success is not None else None
        except (ValueError, TypeError, AttributeError):
            success = None  # An unparseable or non-text stamp is as unknown as a missing one.
        # Session rows are published before the health post, so at exactly ``ttl`` they have expired.
        known = success is not None and moment - success < timedelta(seconds=ttl)
        (fresh if known else stale).add(str(node_id))
    return fresh, stale


def stale_nodes(
    conn: sqlite3.Connection, now: datetime | None = None, ttl: int = STALE_AFTER_SECONDS
) -> set[str] | None:
    """Node ids with a health row whose last success is missing or at least ``ttl`` old, for session views.

    A node with no row is not listed. None means the read failed: treat everything as unknown.
    """
    classified = classify_nodes(conn, now, ttl)
    return None if classified is None else classified[1]


def fresh_nodes(
    conn: sqlite3.Connection, now: datetime | None = None, ttl: int = STALE_AFTER_SECONDS
) -> set[str] | None:
    """Node ids whose observer succeeded within ``ttl``, for placement. None means the read failed.

    Placement trusts a session count only for these nodes: no row, a failed attempt and a stale
    success are all unknown.
    """
    classified = classify_nodes(conn, now, ttl)
    return None if classified is None else classified[0]


def unknown_view(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, Any]:
    """The /sessions fields naming nodes whose T3 sessions are unknown. A failed read says so, never empty."""
    stale = stale_nodes(conn, now)
    if stale is None:
        return {"nodes_unknown": [], "nodes_unknown_error": True}
    return {"nodes_unknown": sorted(stale)}
