"""Durable, recipient-scoped Fleet Hub agent messages."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

SUBJECT_LIMIT = 200
BODY_LIMIT = 64 * 1024
REF_LIMIT = 256
PRIORITIES = frozenset({"low", "normal", "high", "urgent"})
UNACKED_QUOTA = 1000
RETENTION_DAYS = 90

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    reference TEXT,
    priority TEXT NOT NULL,
    reply_to INTEGER REFERENCES fleet_messages(id),
    created_at TEXT NOT NULL,
    acked_at TEXT,
    acked_by TEXT
);
CREATE INDEX IF NOT EXISTS fleet_messages_inbox
ON fleet_messages (recipient, id);
"""


class MessageError(ValueError):
    """A bounded queue request was invalid."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def _text(value: Any, name: str, limit: int, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()) or len(value.encode("utf-8")) > limit:
        raise MessageError(f"{name} must be a non-empty UTF-8 string of at most {limit} bytes")
    return value


def _row(row: tuple[Any, ...]) -> dict[str, Any]:
    keys = (
        "id",
        "sender",
        "recipient",
        "subject",
        "body",
        "ref",
        "priority",
        "reply_to",
        "created_at",
        "acked_at",
        "acked_by",
    )
    return dict(zip(keys, row, strict=True))


def _fetch(conn: sqlite3.Connection, message_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id,sender,recipient,subject,body,reference,priority,reply_to,created_at,acked_at,acked_by "
        "FROM fleet_messages WHERE id=?",
        (message_id,),
    ).fetchone()
    return _row(row) if row else None


def _active_principal(conn: sqlite3.Connection, principal: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM nodes WHERE node_id=? AND revoked_at IS NULL", (principal,)).fetchone() is not None
    )


def send(conn: sqlite3.Connection, raw: dict[str, Any], *, sender: str) -> dict[str, Any]:
    recipient = _text(raw.get("to"), "to", 256)
    subject = _text(raw.get("subject"), "subject", SUBJECT_LIMIT)
    body = _text(raw.get("body"), "body", BODY_LIMIT)
    reference = _text(raw.get("ref"), "ref", REF_LIMIT, required=False)
    priority = raw.get("priority", "normal")
    if priority not in PRIORITIES:
        raise MessageError("priority must be low, normal, high, or urgent")
    assert recipient is not None and subject is not None and body is not None
    if not _active_principal(conn, recipient):
        raise MessageError("recipient is not an active hub principal")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    conn.execute("DELETE FROM fleet_messages WHERE created_at < ?", (cutoff,))
    count = conn.execute(
        "SELECT COUNT(*) FROM fleet_messages WHERE recipient=? AND acked_at IS NULL", (recipient,)
    ).fetchone()[0]
    if count >= UNACKED_QUOTA:
        raise MessageError("recipient message quota exceeded")
    created_at = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        "INSERT INTO fleet_messages(sender,recipient,subject,body,reference,priority,created_at) VALUES(?,?,?,?,?,?,?)",
        (sender, recipient, subject, body, reference, priority, created_at),
    )
    conn.commit()
    message_id = cursor.lastrowid
    if message_id is None:  # pragma: no cover - SQLite INSERT always supplies it
        raise sqlite3.DatabaseError("message insert did not return an id")
    return _fetch(conn, message_id) or {}


def list_messages(conn: sqlite3.Connection, raw: dict[str, Any], *, recipient: str) -> dict[str, Any]:
    after = raw.get("after", 0)
    if not isinstance(after, int) or isinstance(after, bool) or after < 0:
        raise MessageError("after must be a non-negative integer cursor")
    unacked = raw.get("unacked", False)
    if not isinstance(unacked, bool):
        raise MessageError("unacked must be a boolean")
    rows = conn.execute(
        "SELECT id,sender,recipient,subject,body,reference,priority,reply_to,created_at,acked_at,acked_by "
        "FROM fleet_messages WHERE recipient=? AND id>? "
        + ("AND acked_at IS NULL " if unacked else "")
        + "ORDER BY id LIMIT 100",
        (recipient, after),
    ).fetchall()
    messages = [_row(row) for row in rows]
    return {"messages": messages, "next_cursor": messages[-1]["id"] if messages else after}


def ack(conn: sqlite3.Connection, message_id: Any, *, recipient: str) -> dict[str, Any] | None:
    if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id < 1:
        raise MessageError("id must be a positive integer")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE fleet_messages SET acked_at=COALESCE(acked_at,?), acked_by=COALESCE(acked_by,?) "
        "WHERE id=? AND recipient=?",
        (now, recipient, message_id, recipient),
    )
    conn.commit()
    return (
        _fetch(conn, message_id)
        if conn.execute("SELECT 1 FROM fleet_messages WHERE id=? AND recipient=?", (message_id, recipient)).fetchone()
        else None
    )


def reply(conn: sqlite3.Connection, raw: dict[str, Any], *, sender: str) -> dict[str, Any] | None:
    message_id = raw.get("id")
    original = _fetch(conn, message_id) if isinstance(message_id, int) and not isinstance(message_id, bool) else None
    if original is None or original["recipient"] != sender:
        return None
    subject = raw.get("subject") or f"Re: {original['subject']}"
    message = send(
        conn,
        {
            "to": original["sender"],
            "subject": subject,
            "body": raw.get("body"),
            "ref": raw.get("ref") or original["ref"],
            "priority": raw.get("priority", "normal"),
        },
        sender=sender,
    )
    conn.execute("UPDATE fleet_messages SET reply_to=? WHERE id=?", (message_id, message["id"]))
    conn.commit()
    return _fetch(conn, message["id"])


def handle(conn: sqlite3.Connection, raw: Any, *, caller: str | None) -> tuple[int, dict[str, Any]]:
    if caller is None:
        return 403, {"error": "a node principal is required"}
    if not isinstance(raw, dict):
        raise MessageError("message request must be an object")
    action = raw.get("action")
    if action == "send":
        return 201, {"message": send(conn, raw, sender=caller)}
    if action == "list":
        return 200, list_messages(conn, raw, recipient=caller)
    if action == "ack":
        message = ack(conn, raw.get("id"), recipient=caller)
        return (200, {"message": message}) if message else (404, {"error": "message not found"})
    if action == "reply":
        message = reply(conn, raw, sender=caller)
        return (201, {"message": message}) if message else (404, {"error": "message not found"})
    raise MessageError("action must be send, list, ack, or reply")
