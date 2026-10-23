"""Public mesh key records. Format validation only, without signature verification."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .fleet_hub_messages import MessageConflict, MessageError, MessageForbidden

SCHEMA = (
    (
        "table",
        "fleet_message_keys",
        "CREATE TABLE IF NOT EXISTS fleet_message_keys ("
        "principal_id TEXT NOT NULL, kid TEXT PRIMARY KEY, record TEXT NOT NULL, state TEXT NOT NULL, "
        "created_at TEXT NOT NULL, retired_at TEXT, revoked_at TEXT, compromised_from TEXT)",
    ),
    (
        "index",
        "fleet_message_keys_active",
        "CREATE UNIQUE INDEX IF NOT EXISTS fleet_message_keys_active "
        "ON fleet_message_keys(principal_id) WHERE state='active'",
    ),
    (
        "index",
        "fleet_message_keys_principal",
        "CREATE INDEX IF NOT EXISTS fleet_message_keys_principal ON fleet_message_keys(principal_id)",
    ),
)
COLUMNS = "principal_id,kid,record,state,created_at,retired_at,revoked_at,compromised_from"


def missing_schema(conn: sqlite3.Connection) -> list[str]:
    return [
        ddl
        for kind, name, ddl in SCHEMA
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type=? AND name=?", (kind, name)).fetchone() is None
    ]


def ensure_schema(conn: sqlite3.Connection) -> None:
    for ddl in missing_schema(conn):
        conn.execute(ddl)


def decode_binary(value: Any, size: int) -> bytes:
    if not isinstance(value, str) or len(value) != (size * 8 + 5) // 6 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise MessageError("invalid mesh binary format")
    raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if len(raw) != size or base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != value:
        raise MessageError("invalid mesh binary format")
    return raw


def timestamp(value: Any) -> datetime:
    if (
        not isinstance(value, str)
        or len(value) > 32
        or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{3}(?:[0-9]{3})?)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])",
            value,
        )
    ):
        raise MessageError("invalid mesh timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise MessageError("invalid mesh timestamp") from None


def validate_record(record: Any, *, caller: str) -> dict[str, Any]:
    required = {"v", "principal", "hub", "kid", "sig_pub", "enc_pub", "suites", "created_at", "self_sig"}
    if not isinstance(record, dict) or not required <= record.keys() <= required | {
        "transport",
        "prev_kid",
        "prev_sig",
    }:
        raise MessageError("invalid mesh key record")
    if record["principal"] != caller:
        raise MessageForbidden("key principal must match caller", code="principal_key_mismatch")
    if type(record["v"]) is not int or record["v"] != 1 or record["suites"] != ["bm1"]:
        raise MessageError("unsupported mesh key record")
    hub, kid = record["hub"], record["kid"]
    if not isinstance(hub, str) or not hub.startswith("h_"):
        raise MessageError("invalid mesh hub id")
    decode_binary(hub[2:], 16)
    sig, enc = decode_binary(record["sig_pub"], 32), decode_binary(record["enc_pub"], 1216)
    expected = (
        "k1_"
        + base64.urlsafe_b64encode(hashlib.sha256(b"brigade-mesh/v1/kid\0" + sig + enc).digest()[:16])
        .rstrip(b"=")
        .decode()
    )
    if kid != expected:
        raise MessageError("mesh key id does not match public keys")
    timestamp(record["created_at"])
    decode_binary(record["self_sig"], 64)
    if "prev_kid" in record:
        previous = record["prev_kid"]
        if not isinstance(previous, str) or not previous.startswith("k1_"):
            raise MessageError("invalid previous key id")
        decode_binary(previous[3:], 16)
    if "prev_sig" in record:
        decode_binary(record["prev_sig"], 64)
    if "transport" in record:
        transport = record["transport"]
        if (
            not isinstance(transport, dict)
            or set(transport) != {"iroh"}
            or not isinstance(transport["iroh"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", transport["iroh"])
        ):
            raise MessageError("invalid mesh transport")
    return record


def _entry(row: tuple[Any, ...]) -> dict[str, Any]:
    return {**dict(zip(COLUMNS.split(","), row, strict=True)), "record": json.loads(row[2])}


def publish(conn: sqlite3.Connection, raw: dict[str, Any], *, caller: str) -> dict[str, Any]:
    if set(raw) != {"action", "record"}:
        raise MessageError("keys_publish requires a record")
    value = validate_record(raw["record"], caller=caller)
    existing = conn.execute("SELECT state FROM fleet_message_keys WHERE kid=?", (value["kid"],)).fetchone()
    if existing:
        code = "principal_key_revoked" if existing[0] == "revoked" else "principal_key_exists"
        raise MessageConflict("key id was already published", code=code)
    active = conn.execute(
        "SELECT kid,created_at FROM fleet_message_keys WHERE principal_id=? AND state='active'", (caller,)
    ).fetchone()
    if active:
        if value.get("prev_kid") != active[0] or "prev_sig" not in value:
            raise MessageConflict("replacement requires the current previous key", code="principal_key_exists")
        if timestamp(value["created_at"]) <= timestamp(active[1]):
            raise MessageError("replacement key creation time must increase")
        conn.execute(
            "UPDATE fleet_message_keys SET state='retired',retired_at=? WHERE kid=?", (value["created_at"], active[0])
        )
    conn.execute(
        "INSERT INTO fleet_message_keys(principal_id,kid,record,state,created_at) VALUES(?,?,?,'active',?)",
        (caller, value["kid"], json.dumps(value), value["created_at"]),
    )
    return {
        "key": _entry(conn.execute(f"SELECT {COLUMNS} FROM fleet_message_keys WHERE kid=?", (value["kid"],)).fetchone())
    }


def get(conn: sqlite3.Connection, raw: dict[str, Any], *, caller: str) -> dict[str, Any] | None:
    from . import fleet_hub_message_grants as grants

    if set(raw) != {"action", "kid"} or not isinstance(raw["kid"], str) or not raw["kid"].startswith("k1_"):
        raise MessageError("keys_get requires a key id")
    decode_binary(raw["kid"][3:], 16)
    row = conn.execute(f"SELECT {COLUMNS} FROM fleet_message_keys WHERE kid=?", (raw["kid"],)).fetchone()
    patterns = frozenset(grants.grants_for(conn, caller)) if caller.startswith("agent:") else frozenset()
    if row is None or not grants.permits(patterns, caller, row[0]):
        return None
    return {"key": _entry(row)}


def decorate(conn: sqlite3.Connection, kind: str, row: dict[str, Any]) -> dict[str, Any]:
    if kind not in ("nodes", "principals"):
        return row
    principal = row["principal_id"] if kind == "principals" else row["node_id"]
    key = conn.execute(
        f"SELECT {COLUMNS} FROM fleet_message_keys WHERE principal_id=? AND state='active'", (principal,)
    ).fetchone()
    return {**row, "keys": [_entry(key)]} if key else row


def revoke(conn: sqlite3.Connection, principal: str, *, compromised_from: str = "key-creation") -> None:
    """Called only within the credential revocation transaction. Earlier wins."""
    now = datetime.now(timezone.utc).isoformat()
    for kid, created, previous in conn.execute(
        "SELECT kid,created_at,compromised_from FROM fleet_message_keys WHERE principal_id=?", (principal,)
    ).fetchall():
        cutoff = (
            created if compromised_from == "key-creation" else now if compromised_from == "now" else compromised_from
        )
        if previous is not None and timestamp(previous) <= timestamp(cutoff):
            cutoff = previous
        timestamp(cutoff)
        conn.execute(
            "UPDATE fleet_message_keys SET state='revoked',revoked_at=COALESCE(revoked_at,?),compromised_from=? WHERE kid=?",
            (now, cutoff, kid),
        )
