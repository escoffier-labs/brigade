"""Opaque admin-supplied mesh statements and public Hub identity storage."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .fleet_hub_message_keys import decode_binary
from .fleet_hub_messages import MessageConflict, MessageError

# Same upper bound as the existing Hub JSON request body.
MAX_STATEMENT_BYTES = 8 * 1024 * 1024
KINDS = frozenset({"membership", "manifest", "revocation"})
SCHEMA = (
    ("table", "hub_meta", "CREATE TABLE IF NOT EXISTS hub_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"),
    (
        "table",
        "fleet_mesh_statements",
        "CREATE TABLE IF NOT EXISTS fleet_mesh_statements ("
        "kind TEXT PRIMARY KEY, seq INTEGER NOT NULL, statement TEXT NOT NULL, uploaded_at TEXT NOT NULL)",
    ),
)


def missing_schema(conn: sqlite3.Connection) -> list[str]:
    return [
        ddl
        for kind, name, ddl in SCHEMA
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type=? AND name=?", (kind, name)).fetchone() is None
    ]


def ensure_schema(conn: sqlite3.Connection) -> None:
    for ddl in missing_schema(conn):
        conn.execute(ddl)


def _blob(value: Any) -> str:
    try:
        text = json.dumps(value, allow_nan=False)
        if not isinstance(value, dict) or len(text.encode()) > MAX_STATEMENT_BYTES:
            raise ValueError
        return text
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise MessageError("mesh statement must be a bounded JSON object") from None


def identity(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT value FROM hub_meta WHERE key='identity'").fetchone()
    if row is None:
        return None
    value = json.loads(row[0])
    migration = conn.execute("SELECT value FROM hub_meta WHERE key='migration'").fetchone()
    return {"identity": value, "migration": json.loads(migration[0])} if migration else value


def upload_identity(conn: sqlite3.Connection, raw: Any) -> dict[str, Any]:
    _blob(raw)
    value = raw.get("identity", raw)
    blob = _blob(value)
    public = decode_binary(value.get("hub_sig_pub"), 32)
    expected = (
        "h_"
        + base64.urlsafe_b64encode(hashlib.sha256(b"brigade-mesh/v1/hub-id\0" + public).digest()[:16])
        .rstrip(b"=")
        .decode()
    )
    if value.get("hub_id") != expected:
        raise MessageError("hub id does not match public key")
    if value.get("backend") not in ("full", "hub-lite"):
        raise MessageError("invalid hub backend")
    capabilities = value.get("capabilities")
    allowed = {"messages", "invites", "presence", "questions", "claims", "events", "runs"}
    if (
        not isinstance(capabilities, list)
        or any(not isinstance(c, str) or c not in allowed for c in capabilities)
        or capabilities != sorted(set(capabilities))
    ):
        raise MessageError("invalid hub capabilities")
    if type(value.get("seq")) is not int or not 0 <= value["seq"] <= 2**63 - 1:
        raise MessageError("invalid identity sequence")
    migration = _blob(raw["migration"]) if "identity" in raw and "migration" in raw else None
    with conn:
        conn.execute(
            "INSERT INTO hub_meta VALUES('identity',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (blob,)
        )
        if migration is not None:
            conn.execute(
                "INSERT INTO hub_meta VALUES('migration',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (migration,),
            )
    result = identity(conn)
    assert result is not None
    return result


def upload_statement(conn: sqlite3.Connection, raw: Any) -> dict[str, Any]:
    if (
        not isinstance(raw, dict)
        or set(raw) != {"kind", "statement"}
        or not isinstance(raw["kind"], str)
        or raw["kind"] not in KINDS
    ):
        raise MessageError("invalid mesh statement kind")
    blob = _blob(raw["statement"])
    seq = raw["statement"].get("seq")
    if type(seq) is not int or not 0 <= seq <= 2**63 - 1:
        raise MessageError("invalid statement sequence")
    with conn:
        changed = conn.execute(
            "INSERT INTO fleet_mesh_statements VALUES(?,?,?,?) ON CONFLICT(kind) DO UPDATE SET "
            "seq=excluded.seq,statement=excluded.statement,uploaded_at=excluded.uploaded_at WHERE excluded.seq>fleet_mesh_statements.seq",
            (raw["kind"], seq, blob, datetime.now(timezone.utc).isoformat()),
        )
        if changed.rowcount != 1:
            raise MessageConflict("statement sequence must increase", code="signed_statement_rollback")
        # Match the HTTP serializer, including escaping and the response wrapper.
        # The existing statement cap also equals the message client's response cap.
        response = manifests(conn, {"action": "manifests_get"})
        if len(json.dumps(response).encode("utf-8")) > MAX_STATEMENT_BYTES:
            raise MessageError("mesh statements exceed the response size limit")
    return {"kind": raw["kind"], "seq": seq}


def manifests(conn: sqlite3.Connection, raw: dict[str, Any]) -> dict[str, Any]:
    if set(raw) != {"action"}:
        raise MessageError("manifests_get takes no arguments")
    return {
        "statements": {
            kind: json.loads(blob)
            for kind, blob in conn.execute("SELECT kind,statement FROM fleet_mesh_statements ORDER BY kind")
        }
    }
