"""Small server-owned evidence snapshots for concrete alert subjects."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from typing import Any

from .fleet_action_auth import ActionError, active_node, canonical, digest, subject


def resolve(conn: sqlite3.Connection, source: Any) -> dict[str, Any]:
    source = subject(source)
    kind = source["kind"]
    keys: tuple[str, ...]
    if kind == "run":
        row = conn.execute(
            "SELECT sequence, digest, state, ts, received_at, repo_identity, exit_status FROM events "
            "WHERE node_id=? AND run_id=? ORDER BY sequence DESC, received_at DESC, digest DESC LIMIT 1",
            (source["node_id"], source["run_id"]),
        ).fetchone()
        keys = ("sequence", "digest", "state", "evidence_at", "received_at", "repo_identity", "exit_status")
    elif kind == "work":
        row = conn.execute(
            "SELECT version, status, updated_at FROM work_items WHERE work_id=? AND archived_at IS NULL",
            (source["work_id"],),
        ).fetchone()
        keys = ("version", "state", "evidence_at")
    else:
        if not active_node(conn, source["node_id"]):
            raise ActionError("source-unavailable", 404)
        row = conn.execute(
            "SELECT observed_id, observed_at, status, credential_state, ttl_seconds FROM fleet_machine_telemetry WHERE node_id=?",
            (source["node_id"],),
        ).fetchone()
        keys = ("observed_id", "evidence_at", "state", "credential_state", "ttl_seconds")
    if row is None:
        raise ActionError("source-unavailable", 404)
    evidence = {key: (val[:512] if isinstance(val, str) else val) for key, val in zip(keys, row, strict=True)}
    condition: Any = row
    if kind == "telemetry":
        from . import fleet_hub_policy, fleet_policy

        policy_ttl = fleet_hub_policy.current_policy(conn)["document"]["routing"]["telemetry_ttl_seconds"]
        evidence["ttl_seconds"] = min(evidence["ttl_seconds"], policy_ttl or fleet_policy.DEFAULT_TELEMETRY_TTL_SECONDS)
        evidence["staleness"] = "fresh" if _fresh_telemetry(evidence) else "stale"
        condition = [evidence["state"], evidence["credential_state"], evidence["staleness"]]
    return {"source": source, "revision": digest(canonical([source, condition])), "evidence": evidence}


def _timestamp(value: Any) -> float:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return 0
        return stamp.timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError):
        return 0


def _fresh_telemetry(evidence: dict[str, Any]) -> bool:
    observed = _timestamp(evidence.get("evidence_at"))
    return 0 < observed <= time.time() + 5 and time.time() - observed < evidence["ttl_seconds"]


def recovered(snapshot: dict[str, Any], *, after: float) -> bool:
    """Only fresh source-specific records can clear a condition."""
    evidence = snapshot["evidence"]
    observed = _timestamp(evidence.get("evidence_at"))
    if not after < observed <= time.time() + 5:
        return False
    kind, state = snapshot["source"]["kind"], evidence["state"]
    if kind == "telemetry":
        return state == "available" and evidence["credential_state"] == "ok" and _fresh_telemetry(evidence)
    return kind == "work" and state == "completed"


def action_recovered(conn: sqlite3.Connection, action: dict[str, Any]) -> bool:
    """Only fresh evidence for the exact original subject can clear its condition."""
    source = action["request"]["source"]
    if source["kind"] != "run":
        current = resolve(conn, source)
        return recovered(current, after=action["created_at"])
    if not active_node(conn, source["node_id"]):
        return False
    current = resolve(conn, source)["evidence"]
    now = time.time()
    if current["state"] in {"run.completed", "external.completed"} and current["exit_status"] == 0:
        return all(action["created_at"] < _timestamp(current[key]) <= now + 5 for key in ("evidence_at", "received_at"))
    return False


def work_reference(item: Any) -> str:
    """Aggregate exclusions never acquire a work identity from their display label."""
    if not isinstance(item, dict) or not isinstance(item.get("work_id"), str) or type(item.get("version")) is not int:
        return ""
    try:
        return canonical(subject({"kind": "work", "work_id": item["work_id"]}))
    except ActionError:
        return ""
