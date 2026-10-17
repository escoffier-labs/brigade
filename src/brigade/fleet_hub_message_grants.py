"""Transactional recipient grants for external message principals."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import fleet_hub_messages as messages

GRANT_LIMIT = 64
WILDCARDS = frozenset({"*", "node:*", "agent:*"})
AGENT_RECIPIENT_UNACKED_CAP = 200
# Mirrors fleet_hub.CLAIM_ID_PATTERN without importing the hub server on clients.
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS fleet_message_send_grants (
        principal_id TEXT NOT NULL, pattern TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(principal_id, pattern))""")


def normalize_patterns(raw: object) -> list[str]:
    if not isinstance(raw, list) or len(raw) > GRANT_LIMIT:
        raise messages.MessageError(f"may_send_to must be a list of at most {GRANT_LIMIT} patterns")
    for pattern in raw:
        if not isinstance(pattern, str):
            raise messages.MessageError("invalid recipient grant pattern")
        if pattern in WILDCARDS:
            continue
        if pattern.startswith("agent:"):
            valid = messages._PRINCIPAL_NAME_RE.fullmatch(pattern[6:]) is not None
        else:
            valid = pattern != "unknown" and _NODE_ID_RE.fullmatch(pattern) is not None
        if not valid:
            raise messages.MessageError("invalid recipient grant pattern")
    return sorted(set(raw))


def replace_grants(conn: sqlite3.Connection, principal_id: str, patterns: list[str]) -> None:
    conn.execute("DELETE FROM fleet_message_send_grants WHERE principal_id=?", (principal_id,))
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO fleet_message_send_grants(principal_id,pattern,created_at) VALUES(?,?,?)",
        [(principal_id, pattern, now) for pattern in patterns],
    )


def grants_for(conn: sqlite3.Connection, principal_id: str) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT pattern FROM fleet_message_send_grants WHERE principal_id=? ORDER BY pattern", (principal_id,)
        )
    ]


def require_granted(conn: sqlite3.Connection, sender: str, recipient: str) -> None:
    if not sender.startswith("agent:"):
        return
    allowed = {recipient, "*", "agent:*" if recipient.startswith("agent:") else "node:*"}
    if recipient == sender or not allowed.intersection(grants_for(conn, sender)):
        raise messages.MessageForbidden(
            "recipient is not permitted by send grants", code="message_recipient_not_permitted"
        )


def require_recipient_cap(conn: sqlite3.Connection, sender: str, recipient: str) -> None:
    if not sender.startswith("agent:"):
        return
    count = conn.execute(
        "SELECT COUNT(*) FROM fleet_messages WHERE sender=? AND recipient=? AND acked_at IS NULL",
        (sender, recipient),
    ).fetchone()[0]
    if count >= AGENT_RECIPIENT_UNACKED_CAP:
        raise messages.MessageQuotaError(
            "sender recipient message quota exceeded", code="message_sender_recipient_quota", recipient=recipient
        )


def set_grants(conn: sqlite3.Connection, raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or set(raw) != {"action", "name", "may_send_to"} or raw["action"] != "grants":
        raise messages.MessageError("principal grants requires action='grants', name, and may_send_to")
    name = messages._text(raw["name"], "name", 63)
    if name is None or messages._PRINCIPAL_NAME_RE.fullmatch(name) is None:
        raise messages.MessageError("invalid principal name")
    patterns = normalize_patterns(raw["may_send_to"])
    conn.execute("BEGIN IMMEDIATE")
    try:
        principal = messages.get_principal(conn, {"action": "get", "name": name})
        if principal is not None:
            replace_grants(conn, f"agent:{name}", patterns)
            principal["may_send_to"] = patterns
        conn.commit()
        return principal
    except BaseException:
        conn.rollback()
        raise
