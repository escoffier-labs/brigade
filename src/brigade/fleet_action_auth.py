"""Explicit, separately scoped operator capabilities. No browser bearer fallback."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from typing import Any

SESSION_TTL = 900
BOOTSTRAP_TTL = 300
COOKIE = "brigade_operator"
BOOT_COOKIE = "brigade_action_bootstrap"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class ActionError(ValueError):
    def __init__(self, reason: str = "action-refused", status: int = 403):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def opaque(value: Any) -> str:
    if not isinstance(value, str) or ID.fullmatch(value) is None:
        raise ActionError("invalid-identity", 400)
    return value


def destination_reference(family: str, value: Any) -> str:
    """Validate returned native handles without widening unrelated identities."""
    if family != "t3":
        return opaque(value)
    if (
        not isinstance(value, str)
        or len(value) > 166
        or re.fullmatch(
            r"[a-z0-9][a-z0-9-]{0,63}-[a-z0-9][a-z0-9-]{0,63}-"
            r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
            value,
        )
        is None
    ):
        raise ActionError("invalid-t3-reference", 400)
    return value


def subject(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ActionError("invalid-subject", 400)
    fields = {"run": {"kind", "node_id", "run_id"}, "work": {"kind", "work_id"}, "telemetry": {"kind", "node_id"}}
    if not isinstance(value.get("kind"), str) or set(value) != fields.get(value["kind"]):
        raise ActionError("invalid-subject", 400)
    return {key: opaque(val) for key, val in value.items()}


def selector(value: Any) -> dict[str, str]:
    """Bounded grants permit future runs, while submission subjects stay concrete."""
    if isinstance(value, dict) and value.get("kind") == "run":
        if not {"kind", "node_id"} <= set(value) or set(value) - {"kind", "node_id", "run_id", "repo_identity"}:
            raise ActionError("invalid-selector", 400)
        clean = {key: opaque(val) for key, val in value.items() if key != "repo_identity"}
        if "repo_identity" in value:
            from .fleet_session_presence import validate_repo_identity

            if not isinstance(value["repo_identity"], str) or validate_repo_identity(value["repo_identity"]) is None:
                raise ActionError("invalid-repository", 400)
            clean["repo_identity"] = value["repo_identity"]
        return clean
    return subject(value)


def selector_matches(conn: sqlite3.Connection, scope: dict[str, Any], source: dict[str, Any]) -> bool:
    """Compare exact identities, resolving repository authority only from Hub events."""
    if scope.get("kind") != source.get("kind"):
        return False
    if source["kind"] in {"run", "telemetry"} and not active_node(conn, source["node_id"]):
        return False
    if any(source.get(key) != val for key, val in scope.items() if key != "repo_identity"):
        return False
    if "repo_identity" in scope:
        row = conn.execute(
            "SELECT repo_identity FROM events WHERE node_id=? AND run_id=? "
            "ORDER BY sequence DESC, received_at DESC, digest DESC LIMIT 1",
            (source.get("node_id"), source.get("run_id")),
        ).fetchone()
        return bool(row and row[0] == scope["repo_identity"])
    return True


def selector_covers(grant: dict[str, Any], proposed: dict[str, Any]) -> bool:
    """A catalog scope cannot widen the administrator's grant."""
    return all(proposed.get(key) == val for key, val in grant.items())


def ensure_schema(conn: sqlite3.Connection) -> None:
    for ddl in (
        "CREATE TABLE IF NOT EXISTS action_operator_grants (grant_id TEXT PRIMARY KEY, login TEXT UNIQUE NOT NULL, "
        "scopes TEXT NOT NULL, revoked_at REAL)",
        "CREATE TABLE IF NOT EXISTS action_broker_grants (node_id TEXT PRIMARY KEY, targets TEXT NOT NULL, "
        "subjects TEXT NOT NULL, revoked_at REAL)",
        "CREATE TABLE IF NOT EXISTS action_sessions (credential_digest TEXT PRIMARY KEY, csrf_digest TEXT NOT NULL, "
        "grant_id TEXT NOT NULL, login TEXT NOT NULL, expires_at REAL NOT NULL)",
    ):
        conn.execute(ddl)


def grant_operator(conn: sqlite3.Connection, login: str, scopes: Any) -> str:
    if (
        not isinstance(login, str)
        or not login
        or login != login.strip()
        or len(login) > 254
        or any(ord(c) < 33 for c in login)
    ):
        raise ActionError("invalid-login", 400)
    if not isinstance(scopes, list) or not 1 <= len(scopes) <= 100:
        raise ActionError("invalid-scopes", 400)
    clean = []
    for scope in scopes:
        if not isinstance(scope, dict) or set(scope) != {"target_id", "subject"}:
            raise ActionError("invalid-scopes", 400)
        clean.append({"target_id": opaque(scope["target_id"]), "subject": selector(scope["subject"])})
    grant_id = "og_" + secrets.token_hex(16)
    with conn:
        # Replacement rotates identity, so prior sessions and queued grants cannot revive.
        conn.execute("DELETE FROM action_operator_grants WHERE login=?", (login,))
        conn.execute("INSERT INTO action_operator_grants VALUES (?, ?, ?, NULL)", (grant_id, login, canonical(clean)))
    return grant_id


def revoke_operator(conn: sqlite3.Connection, grant_id: str) -> None:
    with conn:
        conn.execute("UPDATE action_operator_grants SET revoked_at=? WHERE grant_id=?", (time.time(), opaque(grant_id)))


def grant_broker(conn: sqlite3.Connection, node_id: str, target_ids: Any, subjects: Any) -> None:
    node_id = opaque(node_id)
    if not active_node(conn, node_id):
        raise ActionError("broker-not-enrolled")
    if (
        not isinstance(target_ids, list)
        or not 1 <= len(target_ids) <= 100
        or not isinstance(subjects, list)
        or not 1 <= len(subjects) <= 100
    ):
        raise ActionError("invalid-scopes", 400)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO action_broker_grants VALUES (?, ?, ?, NULL)",
            (node_id, canonical([opaque(t) for t in target_ids]), canonical([selector(s) for s in subjects])),
        )


def revoke_broker(conn: sqlite3.Connection, node_id: str) -> None:
    from .fleet_hub_actions import _event

    node_id = opaque(node_id)
    with conn:
        conn.execute("UPDATE action_broker_grants SET revoked_at=? WHERE node_id=?", (time.time(), node_id))
        conn.execute("UPDATE action_targets SET expires_at=0 WHERE node_id=?", (node_id,))
        rows = conn.execute(
            "SELECT action_id, generation FROM agent_actions WHERE node_id=? AND started_at IS NULL "
            "AND status IN ('queued', 'dispatching')",
            (node_id,),
        ).fetchall()
        for action_id, generation in rows:
            conn.execute("UPDATE agent_actions SET status='expired' WHERE action_id=?", (action_id,))
            _event(conn, action_id, generation, "expired", {"status": "expired", "reason": "broker-revoked"})


def active_node(conn: sqlite3.Connection, node_id: str) -> bool:
    return conn.execute("SELECT 1 FROM nodes WHERE node_id=? AND revoked_at IS NULL", (node_id,)).fetchone() is not None


def broker_allowed(conn: sqlite3.Connection, node_id: str, target_id: str, source: dict[str, Any]) -> bool:
    row = conn.execute(
        "SELECT targets, subjects FROM action_broker_grants WHERE node_id=? AND revoked_at IS NULL", (node_id,)
    ).fetchone()
    return bool(
        row
        and active_node(conn, node_id)
        and target_id in json.loads(row[0])
        and any(selector_matches(conn, s, source) for s in json.loads(row[1]))
    )


def operator_allowed(conn: sqlite3.Connection, grant_id: str, target_id: str, source: dict[str, Any]) -> bool:
    row = conn.execute(
        "SELECT scopes FROM action_operator_grants WHERE grant_id=? AND revoked_at IS NULL", (grant_id,)
    ).fetchone()
    return bool(
        row
        and any(
            s["target_id"] == target_id and selector_matches(conn, s["subject"], source) for s in json.loads(row[0])
        )
    )


def login_grant(conn: sqlite3.Connection, login: str) -> str:
    row = conn.execute(
        "SELECT grant_id FROM action_operator_grants WHERE login=? AND revoked_at IS NULL", (login,)
    ).fetchone()
    if row is None:
        raise ActionError("operator-not-allowlisted")
    return str(row[0])


def bootstrap(login: str, signing_key: str) -> tuple[str, str]:
    nonce = secrets.token_urlsafe(32)
    stamp = str(int(time.time()))
    signature = hmac.new(signing_key.encode(), canonical([login, nonce, stamp]).encode(), hashlib.sha256).hexdigest()
    return nonce, stamp + "." + signature


def activate(conn: sqlite3.Connection, login: str, nonce: str, proof: str, signing_key: str) -> tuple[str, str]:
    stamp, _, signature = proof.partition(".")
    expected = hmac.new(signing_key.encode(), canonical([login, nonce, stamp]).encode(), hashlib.sha256).hexdigest()
    if (
        not stamp.isdigit()
        or not 0 <= time.time() - int(stamp) < BOOTSTRAP_TTL
        or not hmac.compare_digest(signature, expected)
        or len(nonce) != 43
    ):
        raise ActionError("bootstrap-refused")
    grant_id = login_grant(conn, login)
    credential = secrets.token_urlsafe(32)
    csrf = csrf_for(credential, signing_key)
    with conn:
        conn.execute("DELETE FROM action_sessions WHERE expires_at<=?", (time.time(),))
        conn.execute(
            "INSERT INTO action_sessions VALUES (?, ?, ?, ?, ?)",
            (digest(credential), digest(csrf), grant_id, login, time.time() + SESSION_TTL),
        )
    return credential, csrf


def session(conn: sqlite3.Connection, credential: str, csrf: str | None = None) -> dict[str, Any]:
    row = conn.execute(
        "SELECT s.grant_id, s.login, s.csrf_digest FROM action_sessions s "
        "JOIN action_operator_grants g ON g.grant_id=s.grant_id "
        "WHERE s.credential_digest=? AND s.expires_at>? AND g.revoked_at IS NULL",
        (digest(credential), time.time()),
    ).fetchone()
    if row is None or (csrf is not None and not hmac.compare_digest(row[2], digest(csrf))):
        raise ActionError("operator-session-refused")
    return {"grant_id": row[0], "login": row[1]}


def csrf_for(credential: str, signing_key: str) -> str:
    """Deterministic secret per session, independently stored as a digest."""
    return hmac.new(signing_key.encode(), credential.encode(), hashlib.sha256).hexdigest()
