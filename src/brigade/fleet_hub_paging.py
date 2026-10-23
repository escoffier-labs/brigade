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
from dataclasses import dataclass
from typing import Any

from . import fleet_hub
from .fleet_hub_messages import MessageError, MessageQuotaError

PAGE_DEFAULT = 200
PAGE_MAX = 500
# Reserve room below the 8 MiB client cap for both JSON layers, fences and RPC.
PAGE_MAX_BYTES = 7 * 1024 * 1024
SNAPSHOT_TTL_SECONDS = 600
SNAPSHOT_IDLE_SECONDS = 60
SNAPSHOT_MAX_PER_CALLER = 8
SNAPSHOT_MAX_PER_CALLER_BYTES = 16 * 1024 * 1024
SNAPSHOT_MAX_COUNT = 128
SNAPSHOT_MAX_BYTES = 64 * 1024 * 1024
SNAPSHOT_SINGLE_MAX_BYTES = 16 * 1024 * 1024
# Retain the payload column as a format discriminator: pre-release databases
# can contain whole-document snapshots, whose cursors must never use row seeks.
_SCHEMA: tuple[tuple[str, str, str], ...] = (
    (
        "table",
        "fleet_read_snapshots",
        "CREATE TABLE IF NOT EXISTS fleet_read_snapshots ("
        "snapshot_id TEXT PRIMARY KEY, caller TEXT NOT NULL, query_hash TEXT NOT NULL, "
        "expires_at REAL NOT NULL, payload TEXT NOT NULL, payload_bytes INTEGER NOT NULL, "
        "last_access REAL NOT NULL)",
    ),
    (
        "index",
        "fleet_read_snapshots_expiry",
        "CREATE INDEX IF NOT EXISTS fleet_read_snapshots_expiry ON fleet_read_snapshots(expires_at)",
    ),
)

# Visibility changes belong to authorization writes, never to continuation reads.
# Cache each row's current visibility and maintain totals with deltas, so even a
# narrowed grant can seek past a large forbidden prefix without reading it.
_VISIBLE_SQL = """(
    (caller NOT GLOB 'agent:*' OR (recipient != caller AND EXISTS (
        SELECT 1 FROM fleet_message_send_grants g WHERE g.principal_id=caller
        AND g.pattern IN (recipient, '*', CASE WHEN recipient GLOB 'agent:*' THEN 'agent:*' ELSE 'node:*' END)
    ))) AND (
        kind='claims' OR
        (kind='principals' AND EXISTS (SELECT 1 FROM fleet_message_principals p
            WHERE p.principal_id=recipient AND p.revoked_at IS NULL)) OR
        (kind IN ('nodes','sessions') AND EXISTS (SELECT 1 FROM nodes n
            WHERE n.node_id=recipient AND n.revoked_at IS NULL))
    )
)"""
_SCHEMA += (
    (
        "table",
        "fleet_read_snapshot_groups",
        "CREATE TABLE IF NOT EXISTS fleet_read_snapshot_groups ("
        "snapshot_id TEXT NOT NULL, kind TEXT NOT NULL, row_count INTEGER NOT NULL, total INTEGER NOT NULL, "
        "PRIMARY KEY(snapshot_id,kind)) WITHOUT ROWID",
    ),
    (
        "table",
        "fleet_read_snapshot_rows",
        "CREATE TABLE IF NOT EXISTS fleet_read_snapshot_rows ("
        "snapshot_id TEXT NOT NULL, kind TEXT NOT NULL, ordinal INTEGER NOT NULL, caller TEXT NOT NULL, "
        "recipient TEXT NOT NULL, payload TEXT NOT NULL, wire_bytes INTEGER NOT NULL, visible INTEGER NOT NULL, "
        "PRIMARY KEY(snapshot_id,kind,ordinal)) WITHOUT ROWID",
    ),
    (
        "index",
        "fleet_read_snapshot_visible",
        "CREATE INDEX IF NOT EXISTS fleet_read_snapshot_visible "
        "ON fleet_read_snapshot_rows(snapshot_id,kind,visible,ordinal)",
    ),
    (
        "index",
        "fleet_read_snapshot_recipient",
        "CREATE INDEX IF NOT EXISTS fleet_read_snapshot_recipient ON fleet_read_snapshot_rows(recipient)",
    ),
    (
        "index",
        "fleet_read_snapshot_caller",
        "CREATE INDEX IF NOT EXISTS fleet_read_snapshot_caller ON fleet_read_snapshot_rows(caller)",
    ),
    (
        "trigger",
        "fleet_read_snapshot_delete",
        "CREATE TRIGGER IF NOT EXISTS fleet_read_snapshot_delete "
        "AFTER DELETE ON fleet_read_snapshots BEGIN "
        "DELETE FROM fleet_read_snapshot_rows WHERE snapshot_id=OLD.snapshot_id; "
        "DELETE FROM fleet_read_snapshot_groups WHERE snapshot_id=OLD.snapshot_id; END",
    ),
    (
        "trigger",
        "fleet_read_snapshot_total",
        "CREATE TRIGGER IF NOT EXISTS fleet_read_snapshot_total "
        "AFTER UPDATE OF visible ON fleet_read_snapshot_rows WHEN OLD.visible != NEW.visible BEGIN "
        "UPDATE fleet_read_snapshot_groups SET total=total+NEW.visible-OLD.visible "
        "WHERE snapshot_id=NEW.snapshot_id AND kind=NEW.kind; END",
    ),
)
for _table, _identity, _scope in (
    ("nodes", "node_id", "kind IN ('nodes','sessions')"),
    ("fleet_message_principals", "principal_id", "kind='principals'"),
    ("fleet_message_send_grants", "principal_id", "1"),
):
    for _event, _refs in (("INSERT", ("NEW",)), ("DELETE", ("OLD",)), ("UPDATE", ("OLD", "NEW"))):
        _name = f"fleet_read_visibility_{_table}_{_event.lower()}"
        _column = "caller" if _table == "fleet_message_send_grants" else "recipient"
        _matches = " OR ".join(f"{_column}={ref}.{_identity}" for ref in _refs)
        _event_sql = (
            "UPDATE OF pattern,principal_id" if _table == "fleet_message_send_grants" else "UPDATE OF revoked_at"
        )
        _SCHEMA += (
            (
                "trigger",
                _name,
                f"CREATE TRIGGER IF NOT EXISTS {_name} AFTER "
                f"{_event_sql if _event == 'UPDATE' else _event} ON {_table} BEGIN "
                f"UPDATE fleet_read_snapshot_rows SET visible={_VISIBLE_SQL} "
                f"WHERE ({_matches}) AND {_scope}; END",
            ),
        )


@dataclass(frozen=True)
class Snapshot:
    """Constant-size collection metadata; row payloads stay in SQLite."""

    groups: list[tuple[str, int, int]]


class SnapshotWriteRequired(Exception):
    """Restart a paged read under write admission before storing its snapshot."""


def missing_schema(conn: sqlite3.Connection) -> list[str]:
    missing = [
        ddl
        for kind, name, ddl in _SCHEMA
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type=? AND name=?", (kind, name)).fetchone() is None
    ]
    columns = {row[1] for row in conn.execute("PRAGMA table_info(fleet_read_snapshots)")}
    if columns and "last_access" not in columns:
        missing.extend(
            [
                "ALTER TABLE fleet_read_snapshots ADD COLUMN last_access REAL NOT NULL DEFAULT 0",
                f"UPDATE fleet_read_snapshots SET last_access=expires_at-{SNAPSHOT_TTL_SECONDS}",
            ]
        )
    return missing


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


def load(conn: sqlite3.Connection, raw: dict[str, Any], *, caller: str) -> tuple[Snapshot, list[int], str] | None:
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
            or any(type(offset) is not int or not 0 <= offset <= 2**31 - 1 for offset in value[1])
        ):
            raise ValueError
    except (ValueError, UnicodeError, binascii.Error):
        raise MessageError("invalid paging cursor") from None
    snapshot_id, offsets = value
    row = conn.execute(
        "SELECT 1 FROM fleet_read_snapshots WHERE snapshot_id=? AND caller=? AND query_hash=? "
        "AND expires_at>? AND payload='rows-v1'",
        (snapshot_id, caller, query_hash(conn, raw, caller=caller), fleet_hub._now_epoch()),
    ).fetchone()
    if row is None:
        raise MessageError(
            "paging cursor is invalid or expired; restart from the first page", code="paging_cursor_expired"
        )
    keys = ("nodes", "principals", "sessions") if raw["action"] == "who" else ("claims",)
    if len(offsets) != len(keys):
        # The binding hash includes the action, so only a well-formed cursor for this
        # query reaches here with the wrong shape.
        raise MessageError("invalid paging cursor")
    groups = []
    for key, offset in zip(keys, offsets, strict=True):
        group = conn.execute(
            "SELECT row_count,total FROM fleet_read_snapshot_groups WHERE snapshot_id=? AND kind=?", (snapshot_id, key)
        ).fetchone()
        if group is None:
            raise MessageError(
                "paging cursor is invalid or expired; restart from the first page", code="paging_cursor_expired"
            )
        if offset > group[0]:
            raise MessageError("invalid paging cursor offset")
        groups.append((key, group[0], group[1]))
    return Snapshot(groups), offsets, snapshot_id


def _save(
    conn: sqlite3.Connection,
    data: dict[str, list[dict[str, Any]]],
    *,
    caller: str,
    binding: str,
    visible: Callable[[str, dict[str, Any]], bool],
) -> str:
    payload = json.dumps(data, separators=(",", ":"))
    size = len(payload.encode())
    if size > SNAPSHOT_SINGLE_MAX_BYTES:
        raise MessageError("paging snapshot exceeds capacity")
    now = fleet_hub._now_epoch()
    conn.execute("DELETE FROM fleet_read_snapshots WHERE expires_at<=?", (now,))
    count, total, owned, owned_bytes = conn.execute(
        "SELECT COUNT(*),COALESCE(SUM(payload_bytes),0),COALESCE(SUM(caller=?),0),"
        "COALESCE(SUM(CASE WHEN caller=? THEN payload_bytes ELSE 0 END),0) FROM fleet_read_snapshots",
        (caller, caller),
    ).fetchone()
    if SNAPSHOT_MAX_PER_CALLER < 1 or size > SNAPSHOT_MAX_PER_CALLER_BYTES:
        raise MessageQuotaError("paging snapshot exceeds caller capacity", code="paging_snapshot_capacity")
    # A full caller budget must not turn its next first-page read into a 429.
    # Reclaim only that caller's least recently used walk, including its indexed child rows.
    while owned >= SNAPSHOT_MAX_PER_CALLER or owned_bytes + size > SNAPSHOT_MAX_PER_CALLER_BYTES:
        oldest, oldest_bytes = conn.execute(
            "SELECT snapshot_id,payload_bytes FROM fleet_read_snapshots WHERE caller=? ORDER BY last_access,rowid LIMIT 1",
            (caller,),
        ).fetchone()
        conn.execute("DELETE FROM fleet_read_snapshots WHERE snapshot_id=?", (oldest,))
        count -= 1
        total -= oldest_bytes
        owned -= 1
        owned_bytes -= oldest_bytes
    while count >= SNAPSHOT_MAX_COUNT or total + size > SNAPSHOT_MAX_BYTES:
        idle = conn.execute(
            "SELECT snapshot_id,payload_bytes FROM fleet_read_snapshots "
            "WHERE last_access<? ORDER BY last_access,rowid LIMIT 1",
            (now - SNAPSHOT_IDLE_SECONDS,),
        ).fetchone()
        if idle is None:
            raise MessageQuotaError(
                "paging snapshot capacity reached; retry after idle walks can be reclaimed",
                code="paging_snapshot_capacity",
            )
        conn.execute("DELETE FROM fleet_read_snapshots WHERE snapshot_id=?", (idle[0],))
        count -= 1
        total -= idle[1]
    snapshot_id = secrets.token_hex(24)
    conn.execute(
        "INSERT INTO fleet_read_snapshots VALUES(?,?,?,?,?,?,?)",
        (snapshot_id, caller, binding, now + SNAPSHOT_TTL_SECONDS, "rows-v1", size, now),
    )
    for key, rows in data.items():
        stored = [
            (
                snapshot_id,
                key,
                ordinal,
                caller,
                str(
                    row["owner_node"]
                    if key == "claims"
                    else row["principal_id"]
                    if key == "principals"
                    else row["node_id"]
                ),
                json.dumps(row, separators=(",", ":")),
                _wire_size(row),
                int(visible(key, row)),
            )
            for ordinal, row in enumerate(rows)
        ]
        conn.execute(
            "INSERT INTO fleet_read_snapshot_groups VALUES(?,?,?,?)",
            (snapshot_id, key, len(rows), sum(row[-1] for row in stored)),
        )
        conn.executemany("INSERT INTO fleet_read_snapshot_rows VALUES(?,?,?,?,?,?,?,?)", stored)
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
    snapshot_write: bool = False,
) -> dict[str, Any]:
    offsets = [0] * len(data)
    snapshot_id = None
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
    opted_in = "limit" in raw or "cursor" in raw
    if more and opted_in:
        if not snapshot_write:
            raise SnapshotWriteRequired
        snapshot_id = _save(conn, data, caller=caller, binding=query_hash(conn, raw, caller=caller), visible=visible)
    cursor = (
        base64.urlsafe_b64encode(json.dumps([snapshot_id, offsets], separators=(",", ":")).encode()).decode()
        if more and snapshot_id is not None
        else None
    )
    return {
        **result,
        "total": totals["claims"] if "claims" in data else totals,
        "truncated": more or oversized > 0,
        "oversized": oversized,
        "next_cursor": cursor,
    }


def snapshot_page(
    conn: sqlite3.Connection,
    snapshot: Snapshot,
    offsets: list[int],
    snapshot_id: str,
    *,
    limit: int,
    snapshot_write: bool,
) -> dict[str, Any]:
    # Every continuation refreshes access time or releases a completed walk.
    if not snapshot_write:
        raise SnapshotWriteRequired
    result: dict[str, Any] = {}
    totals = {}
    remaining = PAGE_MAX_BYTES - 8192
    oversized = 0
    more = False
    offsets = list(offsets)
    for index, (key, length, total) in enumerate(snapshot.groups):
        totals[key] = total
        selected: list[dict[str, Any]] = []
        position = offsets[index]
        # Require the visibility/ordinal seek even when statistics predate a
        # grant change. The primary key could otherwise scan a forbidden prefix.
        # SQLite streams only the page and one lookahead, without OFFSET.
        rows = conn.execute(
            "SELECT ordinal,wire_bytes FROM fleet_read_snapshot_rows "
            "INDEXED BY fleet_read_snapshot_visible "
            "WHERE snapshot_id=? AND kind=? AND visible=1 AND ordinal>=? ORDER BY ordinal",
            (snapshot_id, key, position),
        )
        try:
            for ordinal, size in rows:
                position = ordinal
                if key != "sessions" and len(selected) >= limit:
                    more = True
                    break
                if size > PAGE_MAX_BYTES - 8192:
                    oversized += 1
                    position += 1
                    continue
                if size > remaining:
                    more = True
                    break
                # Lookahead and oversized rows need only cached metadata.
                payload = conn.execute(
                    "SELECT payload FROM fleet_read_snapshot_rows WHERE snapshot_id=? AND kind=? AND ordinal=?",
                    (snapshot_id, key, ordinal),
                ).fetchone()[0]
                selected.append(json.loads(payload))
                remaining -= size
                position += 1
            else:
                position = length
        finally:
            rows.close()
        result[key] = selected
        offsets[index] = position
    if not more:
        conn.execute("DELETE FROM fleet_read_snapshots WHERE snapshot_id=?", (snapshot_id,))
    else:
        conn.execute(
            "UPDATE fleet_read_snapshots SET last_access=? WHERE snapshot_id=?", (fleet_hub._now_epoch(), snapshot_id)
        )
    cursor = (
        base64.urlsafe_b64encode(json.dumps([snapshot_id, offsets], separators=(",", ":")).encode()).decode()
        if more
        else None
    )
    return {
        **result,
        "total": totals.get("claims", totals),
        "truncated": more or oversized > 0,
        "oversized": oversized,
        "next_cursor": cursor,
    }
