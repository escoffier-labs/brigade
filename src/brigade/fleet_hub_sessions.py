"""Fleet Hub authority for interactive session presence (schema version 14)."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import fleet_hub
from .fleet_hub import FleetHubError
from .fleet_session_presence import (
    DEFAULT_TTL_SECONDS,
    MAX_DIRTY_JSON_BYTES,
    MAX_DIRTY_PATH_CHARS,
    MAX_DIRTY_PATHS,
    MAX_TTL_SECONDS,
    MIN_TTL_SECONDS,
    validate_repo_identity,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS interactive_sessions (
    node_id TEXT NOT NULL,
    harness TEXT NOT NULL,
    session_id TEXT NOT NULL,
    repo_identity TEXT NOT NULL,
    identity_scope TEXT NOT NULL,
    repo_label TEXT NOT NULL,
    checkout_path TEXT NOT NULL,
    branch TEXT,
    dirty_paths_json TEXT NOT NULL,
    dirty_truncated INTEGER NOT NULL,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    ended_at TEXT,
    ttl_seconds INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (node_id, harness, session_id, repo_identity)
);
"""

ACTIONS = frozenset({"upsert", "end"})
IDENTITY_SCOPES = frozenset({"fleet", "node"})
OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
LIST_ACTIVE_LIMIT = 500
LIST_HISTORY_LIMIT = 1000
_UPSERT_FIELDS = frozenset(
    {
        "action",
        "harness",
        "session_id",
        "repo_identity",
        "identity_scope",
        "repo_label",
        "checkout_path",
        "branch",
        "dirty_paths",
        "dirty_truncated",
        "ttl_seconds",
        "node_id",
    }
)
_END_FIELDS = frozenset({"action", "harness", "session_id", "repo_identity", "node_id"})
_SESSION_COLUMNS = (
    "node_id, harness, session_id, repo_identity, identity_scope, repo_label, "
    "checkout_path, branch, dirty_paths_json, dirty_truncated, state, started_at, "
    "heartbeat_at, ended_at, ttl_seconds, expires_at"
)


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the interactive_sessions table (v13 -> v14 additive migration)."""
    conn.execute(SCHEMA)


def handle_session(
    conn: sqlite3.Connection,
    raw: object,
    *,
    caller_node: str | None,
) -> tuple[int, dict[str, object]]:
    """Validate and apply one upsert or end. Owner comes from ``caller_node``."""
    request = _validate_session_request(raw, caller_node=caller_node)
    now = fleet_hub._now_epoch()
    now_iso = fleet_hub._epoch_to_iso(now)
    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    try:
        payload: dict[str, object] | None
        if request["action"] == "upsert":
            payload = _upsert_session(conn, request, now=now, now_iso=now_iso)
        else:
            payload = _end_session(conn, request, now_iso=now_iso)
        if opened:
            conn.commit()
    except BaseException:
        if opened:
            conn.rollback()
        raise
    return 200, {"session": payload}


def list_sessions(
    conn: sqlite3.Connection,
    *,
    include_all: bool = False,
    now_epoch: float | None = None,
) -> list[dict[str, object]]:
    """Active unexpired sessions, or history when ``include_all`` is set."""
    now = fleet_hub._now_epoch() if now_epoch is None else now_epoch
    limit = LIST_HISTORY_LIMIT if include_all else LIST_ACTIVE_LIMIT
    rows = conn.execute(
        f"SELECT {_SESSION_COLUMNS} FROM interactive_sessions "
        "WHERE (? = 1 OR (state = 'active' AND expires_at > ?)) "
        "ORDER BY heartbeat_at DESC, started_at DESC, node_id, harness, session_id "
        "LIMIT ?",
        (int(include_all), now, limit),
    ).fetchall()
    return [_session_payload(row) for row in rows]


def _validate_session_request(raw: object, *, caller_node: str | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise FleetHubError("session request must be a JSON object")
    action = raw.get("action")
    if action not in ACTIONS:
        raise FleetHubError("session field 'action' must be 'upsert' or 'end'")
    allowed = _UPSERT_FIELDS if action == "upsert" else _END_FIELDS
    unknown = set(raw) - allowed
    if unknown:
        raise FleetHubError(f"session request has unsupported field {sorted(unknown)[0]!r}")
    if caller_node is None:
        node_id = _required_text(raw.get("node_id"), "node_id", limit=128)
        if fleet_hub.CLAIM_ID_PATTERN.match(node_id) is None:
            raise FleetHubError("session field 'node_id' is not a valid node identity")
    else:
        if "node_id" in raw:
            raise FleetHubError("session field 'node_id' is not allowed on node writes")
        node_id = caller_node
        if fleet_hub.CLAIM_ID_PATTERN.match(node_id) is None:
            raise FleetHubError("session field 'node_id' is not a valid node identity")
    request: dict[str, Any] = {
        "action": action,
        "node_id": node_id,
        "harness": _opaque_id(raw.get("harness"), "harness"),
        "session_id": _opaque_id(raw.get("session_id"), "session_id"),
        "repo_identity": _repo_identity(raw.get("repo_identity")),
    }
    if action == "end":
        return request
    request["identity_scope"] = _identity_scope(raw.get("identity_scope"))
    request["repo_label"] = _required_text(raw.get("repo_label"), "repo_label", limit=256)
    request["checkout_path"] = _required_text(raw.get("checkout_path"), "checkout_path", limit=1024)
    request["branch"] = _optional_text(raw.get("branch"), "branch", limit=256)
    request["dirty_paths"] = _dirty_paths(raw.get("dirty_paths"))
    truncated = raw.get("dirty_truncated")
    if not isinstance(truncated, bool):
        raise FleetHubError("session field 'dirty_truncated' must be a boolean")
    request["dirty_truncated"] = truncated
    ttl = raw.get("ttl_seconds", DEFAULT_TTL_SECONDS)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < MIN_TTL_SECONDS or ttl > MAX_TTL_SECONDS:
        raise FleetHubError(
            f"session field 'ttl_seconds' must be an integer from {MIN_TTL_SECONDS} to {MAX_TTL_SECONDS}"
        )
    request["ttl_seconds"] = ttl
    return request


def _required_text(raw: object, field: str, *, limit: int) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise FleetHubError(f"session field {field!r} must be a non-empty string")
    value = raw.strip()
    fleet_hub._reject_controls(value, field, kind="session")
    if len(value) > limit:
        raise FleetHubError(f"session field {field!r} must be at most {limit} characters")
    return value


def _optional_text(raw: object, field: str, *, limit: int) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise FleetHubError(f"session field {field!r} must be a string")
    value = raw.strip()
    if not value:
        return None
    fleet_hub._reject_controls(value, field, kind="session")
    if len(value) > limit:
        raise FleetHubError(f"session field {field!r} must be at most {limit} characters")
    return value


def _opaque_id(raw: object, field: str) -> str:
    value = _required_text(raw, field, limit=128)
    if OPAQUE_ID_RE.match(value) is None:
        raise FleetHubError(f"session field {field!r} must be an opaque identity")
    return value


def _identity_scope(raw: object) -> str:
    value = _required_text(raw, "identity_scope", limit=16)
    if value not in IDENTITY_SCOPES:
        raise FleetHubError("session field 'identity_scope' must be 'fleet' or 'node'")
    return value


def _repo_identity(raw: object) -> str:
    value = _required_text(raw, "repo_identity", limit=512)
    if validate_repo_identity(value) is None:
        raise FleetHubError("session field 'repo_identity' must be a credential-free identity")
    return value


def _dirty_paths(raw: object) -> list[str]:
    if not isinstance(raw, list):
        raise FleetHubError("session field 'dirty_paths' must be an array of strings")
    if len(raw) > MAX_DIRTY_PATHS:
        raise FleetHubError(f"session field 'dirty_paths' must have at most {MAX_DIRTY_PATHS} entries")
    paths: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item:
            raise FleetHubError("session field 'dirty_paths' must be an array of strings")
        fleet_hub._reject_controls(item, "dirty_paths", kind="session")
        normalized = item.replace("\\", "/")
        candidate = Path(normalized)
        if candidate.is_absolute() or ".." in candidate.parts or normalized.startswith("/"):
            raise FleetHubError("session field 'dirty_paths' must be repository-relative Git paths")
        if len(normalized) > MAX_DIRTY_PATH_CHARS:
            raise FleetHubError(
                f"session field 'dirty_paths' entries must be at most {MAX_DIRTY_PATH_CHARS} characters"
            )
        paths.append(normalized)
    encoded = json.dumps(paths, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_DIRTY_JSON_BYTES:
        raise FleetHubError("session field 'dirty_paths' exceeds the encoded size limit")
    return paths


def _upsert_session(
    conn: sqlite3.Connection,
    request: dict[str, Any],
    *,
    now: float,
    now_iso: str,
) -> dict[str, object]:
    dirty_json = json.dumps(request["dirty_paths"], ensure_ascii=False)
    conn.execute(
        "INSERT INTO interactive_sessions ("
        "node_id, harness, session_id, repo_identity, identity_scope, repo_label, "
        "checkout_path, branch, dirty_paths_json, dirty_truncated, state, started_at, "
        "heartbeat_at, ended_at, ttl_seconds, expires_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL, ?, ?) "
        "ON CONFLICT(node_id, harness, session_id, repo_identity) DO UPDATE SET "
        "identity_scope = excluded.identity_scope, "
        "repo_label = excluded.repo_label, "
        "checkout_path = excluded.checkout_path, "
        "branch = excluded.branch, "
        "dirty_paths_json = excluded.dirty_paths_json, "
        "dirty_truncated = excluded.dirty_truncated, "
        "state = 'active', "
        "started_at = CASE WHEN interactive_sessions.state = 'active' "
        "AND interactive_sessions.expires_at > ? THEN interactive_sessions.started_at "
        "ELSE excluded.started_at END, "
        "heartbeat_at = excluded.heartbeat_at, "
        "ended_at = NULL, "
        "ttl_seconds = excluded.ttl_seconds, "
        "expires_at = excluded.expires_at",
        (
            request["node_id"],
            request["harness"],
            request["session_id"],
            request["repo_identity"],
            request["identity_scope"],
            request["repo_label"],
            request["checkout_path"],
            request["branch"],
            dirty_json,
            int(request["dirty_truncated"]),
            now_iso,
            now_iso,
            request["ttl_seconds"],
            now + request["ttl_seconds"],
            now,
        ),
    )
    payload = _fetch_session(
        conn,
        request["node_id"],
        request["harness"],
        request["session_id"],
        request["repo_identity"],
    )
    if payload is None:
        raise FleetHubError("session row was not stored")
    return payload


def _end_session(conn: sqlite3.Connection, request: dict[str, Any], *, now_iso: str) -> dict[str, object] | None:
    conn.execute(
        "UPDATE interactive_sessions SET state = 'ended', "
        "ended_at = COALESCE(ended_at, ?), heartbeat_at = ? "
        "WHERE node_id = ? AND harness = ? AND session_id = ? AND repo_identity = ?",
        (
            now_iso,
            now_iso,
            request["node_id"],
            request["harness"],
            request["session_id"],
            request["repo_identity"],
        ),
    )
    return _fetch_session(
        conn,
        request["node_id"],
        request["harness"],
        request["session_id"],
        request["repo_identity"],
        required=False,
    )


def _fetch_session(
    conn: sqlite3.Connection,
    node_id: str,
    harness: str,
    session_id: str,
    repo_identity: str,
    *,
    required: bool = True,
) -> dict[str, object] | None:
    row = conn.execute(
        f"SELECT {_SESSION_COLUMNS} FROM interactive_sessions "
        "WHERE node_id = ? AND harness = ? AND session_id = ? AND repo_identity = ?",
        (node_id, harness, session_id, repo_identity),
    ).fetchone()
    if row is None:
        if required:
            raise FleetHubError("session row was not stored")
        return None
    return _session_payload(row)


def _session_payload(row: tuple[Any, ...]) -> dict[str, object]:
    try:
        dirty_paths = json.loads(row[8])
    except json.JSONDecodeError:
        dirty_paths = []
    if not isinstance(dirty_paths, list):
        dirty_paths = []
    return {
        "node_id": row[0],
        "harness": row[1],
        "session_id": row[2],
        "repo_identity": row[3],
        "identity_scope": row[4],
        "repo_label": row[5],
        "checkout_path": row[6],
        "branch": row[7],
        "dirty_paths": dirty_paths,
        "dirty_truncated": bool(row[9]),
        "state": row[10],
        "started_at": row[11],
        "heartbeat_at": row[12],
        "ended_at": row[13],
        "ttl_seconds": row[14],
        "expires_at": row[15],
    }
