"""Bounded, expiring snapshots for credential-scoped directory and claim pages."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Callable
from typing import Any

from . import fleet_hub
from .fleet_hub_messages import MessageError, MessageQuotaError

PAGE_DEFAULT = 200
PAGE_MAX = 500
# Reserve room below the 8 MiB client cap for both JSON layers, fences and RPC.
PAGE_MAX_BYTES = 7 * 1024 * 1024
SNAPSHOT_TTL_SECONDS = 600
SNAPSHOT_MAX_PER_CALLER = 8
SNAPSHOT_MAX_COUNT = 128
SNAPSHOT_MAX_BYTES = 64 * 1024 * 1024
SNAPSHOT_SINGLE_MAX_BYTES = 16 * 1024 * 1024
_SCHEMA = (
    (
        "table",
        "fleet_read_snapshots",
        "CREATE TABLE IF NOT EXISTS fleet_read_snapshots ("
        "snapshot_id TEXT PRIMARY KEY, caller TEXT NOT NULL, query_hash TEXT NOT NULL, "
        "expires_at REAL NOT NULL, payload TEXT NOT NULL, payload_bytes INTEGER NOT NULL)",
    ),
    (
        "index",
        "fleet_read_snapshots_expiry",
        "CREATE INDEX IF NOT EXISTS fleet_read_snapshots_expiry ON fleet_read_snapshots(expires_at)",
    ),
)


class SnapshotWriteRequired(Exception):
    """Restart a paged read under write admission before storing its snapshot."""


def missing_schema(conn: sqlite3.Connection) -> list[str]:
    return [
        ddl
        for kind, name, ddl in _SCHEMA
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type=? AND name=?", (kind, name)).fetchone() is None
    ]


def ensure_schema(conn: sqlite3.Connection) -> None:
    for ddl in missing_schema(conn):
        conn.execute(ddl)


def page_limit(raw: dict[str, Any]) -> int:
    limit = raw.get("limit", PAGE_DEFAULT)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= PAGE_MAX:
        raise MessageError(f"{raw['action']} limit must be an integer from 1 to {PAGE_MAX}")
    return limit


def query_hash(conn: sqlite3.Connection, raw: dict[str, Any], *, caller: str) -> str:
    # Bind to the current credential without putting its fingerprint in the cursor.
    statement = (
        "SELECT token_hash FROM fleet_message_principals WHERE principal_id=? AND revoked_at IS NULL"
        if caller.startswith("agent:")
        else "SELECT token_sha256 FROM nodes WHERE node_id=? AND revoked_at IS NULL"
    )
    credential = conn.execute(statement, (caller,)).fetchone()
    if credential is None:
        raise MessageError("paging caller is not active")
    query = [raw["action"], raw.get("all", False), raw.get("filter", "").casefold(), credential[0]]
    return hashlib.sha256(json.dumps(query).encode()).hexdigest()


def load(
    conn: sqlite3.Connection, raw: dict[str, Any], *, caller: str
) -> tuple[dict[str, list[dict[str, Any]]], list[int], str] | None:
    if "cursor" not in raw:
        return None
    cursor = raw["cursor"]
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 512 or not cursor.isascii():
            raise ValueError
        decoded = base64.b64decode(cursor, altchars=b"-_", validate=True)
        value = json.loads(decoded)
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not isinstance(value[0], str)
            or re.fullmatch(r"[0-9a-f]{48}", value[0]) is None
            or not isinstance(value[1], list)
            or len(value[1]) != (3 if raw["action"] == "who" else 1)
            or any(type(offset) is not int or not 0 <= offset <= 2**31 - 1 for offset in value[1])
        ):
            raise ValueError
    except (ValueError, UnicodeError, binascii.Error):
        raise MessageError("invalid paging cursor") from None
    snapshot_id, offsets = value
    row = conn.execute(
        "SELECT payload FROM fleet_read_snapshots WHERE snapshot_id=? AND caller=? AND query_hash=? AND expires_at>?",
        (snapshot_id, caller, query_hash(conn, raw, caller=caller), fleet_hub._now_epoch()),
    ).fetchone()
    if row is None:
        raise MessageError("paging cursor is invalid or expired")
    data = json.loads(row[0])
    if any(offset > len(rows) for offset, rows in zip(offsets, data.values(), strict=True)):
        raise MessageError("invalid paging cursor offset")
    return data, offsets, snapshot_id


def _save(conn: sqlite3.Connection, data: dict[str, Any], *, caller: str, binding: str) -> str:
    payload = json.dumps(data, separators=(",", ":"))
    size = len(payload.encode())
    if size > SNAPSHOT_SINGLE_MAX_BYTES:
        raise MessageError("paging snapshot exceeds capacity")
    now = fleet_hub._now_epoch()
    conn.execute("DELETE FROM fleet_read_snapshots WHERE expires_at<=?", (now,))
    count, total, owned = conn.execute(
        "SELECT COUNT(*),COALESCE(SUM(payload_bytes),0),COALESCE(SUM(caller=?),0) FROM fleet_read_snapshots", (caller,)
    ).fetchone()
    if count >= SNAPSHOT_MAX_COUNT or total + size > SNAPSHOT_MAX_BYTES or owned >= SNAPSHOT_MAX_PER_CALLER:
        raise MessageQuotaError(
            "paging snapshot capacity reached; retry after snapshots expire", code="paging_snapshot_capacity"
        )
    snapshot_id = secrets.token_hex(24)
    conn.execute(
        "INSERT INTO fleet_read_snapshots VALUES(?,?,?,?,?,?)",
        (snapshot_id, caller, binding, now + SNAPSHOT_TTL_SECONDS, payload, size),
    )
    return snapshot_id


def _wire_size(row: dict[str, Any]) -> int:
    # The outer MCP JSON escapes the indented inner JSON. Each nonempty text
    # may acquire an untrusted fence (under 1 KiB including its JSON wrapper).
    # Charge every string, even authenticated fields that are never fenced.
    text = json.dumps(row, indent=2)
    return len(json.dumps(text).encode()) + 512 + 1024 * sum(isinstance(v, str) and bool(v) for v in row.values())


def page(
    conn: sqlite3.Connection,
    raw: dict[str, Any],
    data: dict[str, list[dict[str, Any]]],
    *,
    caller: str,
    limit: int,
    visible: Callable[[str, dict[str, Any]], bool],
    offsets: list[int] | None = None,
    snapshot_id: str | None = None,
    snapshot_write: bool = False,
) -> dict[str, Any]:
    offsets = list(offsets) if offsets is not None else [0] * len(data)
    result: dict[str, Any] = {}
    totals: dict[str, int] = {}
    remaining = PAGE_MAX_BYTES - 8192
    oversized = 0
    more = False
    for index, (key, rows) in enumerate(data.items()):
        totals[key] = sum(visible(key, row) for row in rows)
        selected: list[dict[str, Any]] = []
        position = offsets[index]
        while position < len(rows):
            row = rows[position]
            if not visible(key, row):
                position += 1
                continue
            if key != "sessions" and len(selected) >= limit:
                break
            size = _wire_size(row)
            if size > PAGE_MAX_BYTES - 8192:
                oversized += 1
                position += 1
                continue
            if size > remaining:
                break
            selected.append(row)
            remaining -= size
            position += 1
        result[key] = selected
        offsets[index] = position
        more |= position < len(rows)
    if more and snapshot_id is None:
        if not snapshot_write:
            raise SnapshotWriteRequired
        snapshot_id = _save(conn, data, caller=caller, binding=query_hash(conn, raw, caller=caller))
    cursor = (
        base64.urlsafe_b64encode(json.dumps([snapshot_id, offsets], separators=(",", ":")).encode()).decode()
        if more
        else None
    )
    return {
        **result,
        "total": totals["claims"] if "claims" in data else totals,
        "truncated": more or oversized > 0,
        "oversized": oversized,
        "next_cursor": cursor,
    }
