"""Transactional action ledger. Starting is an irreversible dispatch fence."""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from typing import Any

from . import fleet_action_auth as auth, fleet_action_sources as sources, fleet_action_targets as targets
from .fleet_action_auth import ActionError, canonical, digest, opaque, subject

SCHEMA = "brigade.fleet_action.v1"
CLAIM_TTL = 60
T3_TTL = 240
T3_PREACCEPT_REJECTIONS = frozenset({"invalid_config", "invalid_request", "policy_rejected", "target_busy"})
BLOCKING_TTL = 3840
ACTION_TTL = 900
STATES = {
    "queued",
    "dispatching",
    "accepted",
    "running",
    "needs-input",
    "finished",
    "failed",
    "unknown",
    "canceled",
    "expired",
}
TERMINAL = {"finished", "failed", "canceled", "expired"}


def ensure_schema(conn: sqlite3.Connection) -> None:
    auth.ensure_schema(conn)
    targets.ensure_schema(conn)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_actions (action_id TEXT PRIMARY KEY, grant_id TEXT NOT NULL, "
        "client_id TEXT NOT NULL, request_digest TEXT NOT NULL, document TEXT NOT NULL, "
        "node_id TEXT NOT NULL, target_id TEXT NOT NULL, status TEXT NOT NULL, generation INTEGER NOT NULL, "
        "claim_until REAL, started_at REAL, created_at REAL NOT NULL, expires_at REAL NOT NULL, "
        "destination_ref TEXT, delivery TEXT NOT NULL DEFAULT 'none', verification TEXT NOT NULL DEFAULT 'pending', "
        "UNIQUE(grant_id, client_id))"
    )
    if "destination_thread" not in {row[1] for row in conn.execute("PRAGMA table_info(agent_actions)")}:
        conn.execute("ALTER TABLE agent_actions ADD COLUMN destination_thread TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS agent_actions_queue ON agent_actions(node_id, status, created_at)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS action_events (action_id TEXT NOT NULL, generation INTEGER NOT NULL, "
        "event_id TEXT NOT NULL, document TEXT NOT NULL, created_at REAL NOT NULL, "
        "PRIMARY KEY(action_id, generation, event_id))"
    )


def _event(conn: sqlite3.Connection, action_id: str, generation: int, event_id: str, document: Any) -> None:
    data = canonical(document)
    row = conn.execute(
        "SELECT document FROM action_events WHERE action_id=? AND generation=? AND event_id=?",
        (action_id, generation, event_id),
    ).fetchone()
    if row is not None and row[0] != data:
        raise ActionError("event-conflict", 409)
    conn.execute(
        "INSERT OR IGNORE INTO action_events VALUES (?, ?, ?, ?, ?)",
        (action_id, generation, event_id, data, time.time()),
    )


def _load(conn: sqlite3.Connection, action_id: str) -> dict[str, Any]:
    cur = conn.execute("SELECT * FROM agent_actions WHERE action_id=?", (opaque(action_id),))
    row = cur.fetchone()
    if row is None:
        raise ActionError("action-not-found", 404)
    result = dict(zip([col[0] for col in cur.description], row, strict=True))
    result["request"] = json.loads(result.pop("document"))
    return result


def show(conn: sqlite3.Connection, action_id: str) -> dict[str, Any]:
    action = _load(conn, action_id)
    # A live projection, never based on broker claims or destination output.
    try:
        if sources.action_recovered(conn, action):
            action["verification"] = "condition-cleared"
    except ActionError:
        pass
    action["schema"] = SCHEMA
    action["events"] = [
        {"generation": row[0], "event_id": row[1], "receipt": json.loads(row[2]), "at": row[3]}
        for row in reversed(
            conn.execute(
                "SELECT generation, event_id, document, created_at FROM action_events "
                "WHERE action_id=? ORDER BY created_at DESC, event_id DESC, generation DESC LIMIT 100",
                (action_id,),
            ).fetchall()
        )
    ]
    return action


def _authorized(conn: sqlite3.Connection, action: dict[str, Any]) -> bool:
    request = action["request"]
    if not auth.operator_allowed(conn, action["grant_id"], action["target_id"], request["source"]):
        return False
    if not auth.broker_allowed(conn, action["node_id"], action["target_id"], request["source"]):
        return False
    try:
        target = targets.selected(conn, action["target_id"], request["source"], request["model_id"])
    except ActionError:
        return False
    return (
        target["node_id"] == action["node_id"]
        and targets.native_identity(target) == targets.native_identity(request["target"])
        and target["binding_digest"] == request["target"]["binding_digest"]
    )


def submit(conn: sqlite3.Connection, grant_id: str, raw: Any) -> dict[str, Any]:
    fields = {"source", "observed_revision", "target_id", "model_id", "intent", "prompt", "client_id"}
    if not isinstance(raw, dict) or set(raw) != fields or raw["intent"] != "repair":
        raise ActionError("invalid-submission", 400)
    request = dict(raw)
    request["source"] = subject(raw["source"])
    opaque(request["target_id"])
    opaque(request["client_id"])
    if not isinstance(request["model_id"], str) or not isinstance(request["observed_revision"], str):
        raise ActionError("invalid-submission", 400)
    prompt = request["prompt"]
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > 12000 or "\x00" in prompt:
        raise ActionError("invalid-prompt", 400)
    request_digest = digest(canonical(request))
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not auth.operator_allowed(conn, grant_id, request["target_id"], request["source"]):
            raise ActionError("scope-refused")
        prior = conn.execute(
            "SELECT action_id, request_digest FROM agent_actions WHERE grant_id=? AND client_id=?",
            (grant_id, request["client_id"]),
        ).fetchone()
        if prior:
            if prior[1] != request_digest:
                raise ActionError("client-id-conflict", 409)
            conn.commit()
            return show(conn, prior[0])
        snapshot = sources.resolve(conn, request["source"])
        if request["observed_revision"] != snapshot["revision"]:
            raise ActionError("source-changed", 409)
        target = targets.selected(conn, request["target_id"], request["source"], request["model_id"])
        request.update(evidence=snapshot["evidence"], target=target)
        action_id, now = "aa_" + secrets.token_hex(16), time.time()
        conn.execute(
            "INSERT INTO agent_actions (action_id, grant_id, client_id, request_digest, document, node_id, "
            "target_id, status, generation, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)",
            (
                action_id,
                grant_id,
                request["client_id"],
                request_digest,
                canonical(request),
                target["node_id"],
                request["target_id"],
                now,
                now + ACTION_TTL,
            ),
        )
        _event(conn, action_id, 0, "queued", {"status": "queued"})
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return show(conn, action_id)


def claim(conn: sqlite3.Connection, node_id: str) -> dict[str, Any] | None:
    conn.execute("BEGIN IMMEDIATE")
    result = None
    try:
        # Lost after start is never eligible again. Known handles can be reconciled by execute.
        lost = conn.execute(
            "SELECT action_id, generation FROM agent_actions WHERE node_id=? AND started_at IS NOT NULL "
            "AND status='dispatching' AND claim_until<=?",
            (node_id, time.time()),
        ).fetchall()
        for action_id, generation in lost:
            conn.execute("UPDATE agent_actions SET status='unknown' WHERE action_id=?", (action_id,))
            _event(conn, action_id, generation, "unknown", {"status": "unknown", "reason": "started-lease-expired"})
        rows = conn.execute(
            "SELECT action_id FROM agent_actions WHERE node_id=? AND started_at IS NULL "
            "AND (status='queued' OR (status='dispatching' AND claim_until<=?)) ORDER BY created_at LIMIT 100",
            (node_id, time.time()),
        ).fetchall()
        for row in rows:
            action = _load(conn, row[0])
            if action["expires_at"] <= time.time() or not _authorized(conn, action):
                conn.execute("UPDATE agent_actions SET status='expired' WHERE action_id=?", (row[0],))
                _event(conn, row[0], action["generation"], "expired", {"status": "expired"})
                continue
            generation = action["generation"] + 1
            conn.execute(
                "UPDATE agent_actions SET status='dispatching', generation=?, claim_until=? WHERE action_id=?",
                (generation, time.time() + CLAIM_TTL, row[0]),
            )
            _event(conn, row[0], generation, "claimed", {"status": "dispatching"})
            result = row[0]
            break
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return show(conn, result) if result else None


def _fenced(conn: sqlite3.Connection, node_id: str, action_id: str, generation: int) -> dict[str, Any]:
    action = _load(conn, action_id)
    if (
        type(generation) is not int
        or generation != action["generation"]
        or node_id != action["node_id"]
        or generation < 1
    ):
        raise ActionError("fence-refused", 409)
    return action


def start(conn: sqlite3.Connection, node_id: str, action_id: str, generation: int) -> dict[str, Any]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        action = _fenced(conn, node_id, action_id, generation)
        if action["started_at"] is not None:
            raise ActionError("already-started", 409)
        if action["status"] != "dispatching" or action["claim_until"] <= time.time():
            raise ActionError("claim-expired", 409)
        if action["expires_at"] <= time.time() or not _authorized(conn, action):
            conn.execute("UPDATE agent_actions SET status='expired' WHERE action_id=?", (action_id,))
            _event(conn, action_id, generation, "expired", {"status": "expired"})
            conn.commit()
            raise ActionError("scope-revoked")
        # Recheck source immediately before the external side-effect fence.
        if sources.resolve(conn, action["request"]["source"])["revision"] != action["request"]["observed_revision"]:
            conn.execute("UPDATE agent_actions SET status='expired' WHERE action_id=?", (action_id,))
            _event(conn, action_id, generation, "expired", {"status": "expired", "reason": "source-changed"})
            conn.commit()
            raise ActionError("source-changed", 409)
        conn.execute(
            "UPDATE agent_actions SET started_at=?, claim_until=? WHERE action_id=?",
            (
                time.time(),
                time.time()
                + {"openclaw": BLOCKING_TTL, "t3": T3_TTL}.get(action["request"]["target"]["family"], CLAIM_TTL),
                action_id,
            ),
        )
        _event(conn, action_id, generation, "started", {"status": "dispatching", "possible_external_acceptance": True})
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return show(conn, action_id)


def report(conn: sqlite3.Connection, node_id: str, action_id: str, generation: int, raw: Any) -> dict[str, Any]:
    allowed = {"event_id", "status", "delivery", "destination_ref", "destination_thread", "rejection_code"}
    if not isinstance(raw, dict) or set(raw) - allowed or not {"event_id", "status"} <= set(raw):
        raise ActionError("invalid-receipt", 400)
    opaque(raw["event_id"])
    if raw["status"] not in STATES - {"queued", "dispatching", "expired", "canceled"}:
        raise ActionError("invalid-status", 400)
    if raw.get("delivery", "none") not in {"none", "pending", "delivered", "acknowledged"}:
        raise ActionError("invalid-delivery", 400)
    reference = raw.get("destination_ref")
    thread = raw.get("destination_thread")
    if thread is not None:
        opaque(thread)
    conn.execute("BEGIN IMMEDIATE")
    try:
        action = _fenced(conn, node_id, action_id, generation)
        if not auth.broker_allowed(conn, node_id, action["target_id"], action["request"]["source"]):
            raise ActionError("broker-scope-refused")
        if reference is not None:
            auth.destination_reference(action["request"]["target"]["family"], reference)
        prior = conn.execute(
            "SELECT document FROM action_events WHERE action_id=? AND generation=? AND event_id=?",
            (action_id, generation, raw["event_id"]),
        ).fetchone()
        if prior:
            if prior[0] != canonical(raw):
                raise ActionError("event-conflict", 409)
            conn.commit()
            return show(conn, action_id)
        if action["started_at"] is None or action["status"] in TERMINAL:
            raise ActionError("report-refused", 409)
        if (action["destination_ref"] and reference not in {None, action["destination_ref"]}) or (
            action["destination_thread"] and thread not in {None, action["destination_thread"]}
        ):
            raise ActionError("destination-conflict", 409)
        capability = action["request"]["target"]["capability"]
        status = raw["status"]
        preaccept = (
            action["request"]["target"]["family"] == "t3"
            and action["status"] == "dispatching"
            and not action["destination_ref"]
            and reference is None
            and status == "failed"
            and isinstance(raw.get("rejection_code"), str)
            and raw["rejection_code"] in T3_PREACCEPT_REJECTIONS
            and not conn.execute(
                "SELECT 1 FROM action_events WHERE action_id=? AND event_id LIKE 'result_%' LIMIT 1",
                (action_id,),
            ).fetchone()
        )
        if "rejection_code" in raw and not preaccept:
            raise ActionError("invalid-rejection", 400)
        if capability == "inbox":
            if status not in {"accepted", "unknown", "failed"}:
                raise ActionError("inbox-is-not-execution", 400)
            delivery = raw.get("delivery", action["delivery"])
            order = {"none": 0, "pending": 1, "delivered": 2, "acknowledged": 3}
            if order[delivery] < order[action["delivery"]]:
                raise ActionError("transition-refused", 409)
            if delivery in {"delivered", "acknowledged"} and not (reference or action["destination_ref"]):
                raise ActionError("destination-reference-required", 400)
        else:
            if raw.get("delivery", "none") != "none":
                raise ActionError("invalid-delivery", 400)
            if status != "unknown" and not preaccept and not (reference or action["destination_ref"]):
                raise ActionError("destination-reference-required", 400)
            transitions = {
                "dispatching": {"accepted", "running", "needs-input", "finished", "failed", "unknown"},
                "accepted": {"accepted", "running", "needs-input", "finished", "failed", "unknown"},
                "running": {"running", "needs-input", "finished", "failed", "unknown"},
                "needs-input": {"needs-input", "running", "finished", "failed", "unknown"},
                "unknown": {"unknown", "accepted", "running", "needs-input", "finished", "failed"},
            }
            prior_running = status == "accepted" and any(
                json.loads(row[0]).get("status") in {"running", "needs-input"}
                for row in conn.execute(
                    "SELECT document FROM action_events WHERE action_id=? AND generation=?", (action_id, generation)
                )
            )
            if prior_running or status not in transitions.get(action["status"], set()):
                raise ActionError("transition-refused", 409)
        _event(conn, action_id, generation, raw["event_id"], raw)
        conn.execute(
            "UPDATE agent_actions SET status=?, delivery=?, destination_ref=COALESCE(destination_ref, ?), "
            "destination_thread=COALESCE(destination_thread, ?) WHERE action_id=?",
            (raw["status"], raw.get("delivery", action["delivery"]), reference, thread, action_id),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return show(conn, action_id)
