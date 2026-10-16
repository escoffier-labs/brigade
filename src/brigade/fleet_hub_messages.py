"""Durable, recipient-scoped Fleet Hub agent messages."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, TypeGuard

SUBJECT_LIMIT = 200
BODY_LIMIT = 64 * 1024
REF_LIMIT = 256
PRIORITIES = frozenset({"low", "normal", "high", "urgent"})
UNACKED_QUOTA = 1000
SENDER_DAILY_QUOTA = 1000
SENDER_DAILY_WINDOW = timedelta(days=1)
RETENTION_DAYS = 90
MAX_LIST_RESPONSE_BYTES = 7 * 1024 * 1024
MAX_FUTURE_SKEW = timedelta(minutes=5)
MAX_SQLITE_INTEGER = (1 << 63) - 1
_PRINCIPAL_NAME_RE = re.compile(r"^[a-z][a-z0-9._-]{0,62}$")
_SENDER_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_THREAD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,127}$")
KINDS = frozenset({"message", "question", "completion"})
_ENROLLMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_PRINCIPAL_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")

_PRINCIPAL_SCHEMA = """CREATE TABLE IF NOT EXISTS fleet_message_principals (
    principal_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    enrollment_id TEXT
)"""


class MessageError(ValueError):
    """A bounded queue request was invalid."""


class MessageForbidden(MessageError):
    """A principal lacks permission for the requested recipient."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class MessageFutureTimestamp(MessageError):
    """A queued message timestamp that may become valid after clock correction."""


class MessageConflict(MessageError):
    """An idempotency key was reused for a different message payload."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class MessageQuotaError(MessageError):
    """A temporary per-principal message quota is full."""

    def __init__(self, message: str, *, code: str, recipient: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.recipient = recipient


class MessageRecipientInactive(MessageError):
    """A known recipient is temporarily unavailable because it was revoked."""

    def __init__(self, message: str, *, recipient: str) -> None:
        super().__init__(message)
        self.recipient = recipient


class MessageAuthError(MessageError):
    """A principal was revoked while its request was being processed."""


def _payload_digest(
    recipient: str,
    subject: str,
    body: str,
    reference: str | None,
    priority: str,
    reply_to: int | None = None,
    *,
    extras: dict[str, str] | None = None,
) -> str:
    fields: list[Any] = [recipient, subject, body, reference, priority, reply_to]
    if extras:
        fields.append(extras)
    value = json.dumps(fields, ensure_ascii=False)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _metadata(raw: dict[str, Any], *, sender: str | None = None) -> dict[str, str]:
    extras: dict[str, str] = {}
    for field, pattern in (("sender_label", _SENDER_LABEL_RE), ("thread", _THREAD_RE)):
        value = raw.get(field)
        if value is None:
            continue
        if field == "sender_label" and sender is not None and sender.startswith("agent:"):
            raise MessageError("sender_label is only available to node principals")
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            raise MessageError(f"{field} is invalid")
        extras[field] = value
    kind = raw.get("kind")
    if kind is not None:
        if not isinstance(kind, str) or kind not in KINDS:
            raise MessageError("kind must be message, question, or completion")
        if kind != "message":
            extras["kind"] = kind
    return extras


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS fleet_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            reference TEXT,
            priority TEXT NOT NULL,
            client_id TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            reply_to INTEGER REFERENCES fleet_messages(id),
            created_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            acked_at TEXT,
            acked_by TEXT
        )"""
    )
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(fleet_messages)")}
    if "client_id" not in columns:
        conn.execute("ALTER TABLE fleet_messages ADD COLUMN client_id TEXT")
    if "payload_hash" not in columns:
        conn.execute("ALTER TABLE fleet_messages ADD COLUMN payload_hash TEXT")
    if "received_at" not in columns:
        conn.execute("ALTER TABLE fleet_messages ADD COLUMN received_at TEXT")
    for column in ("sender_label", "thread", "kind"):
        if column not in columns:
            conn.execute(f"ALTER TABLE fleet_messages ADD COLUMN {column} TEXT")
    conn.execute("UPDATE fleet_messages SET client_id='legacy-' || id WHERE client_id IS NULL")
    conn.execute("UPDATE fleet_messages SET received_at=created_at WHERE received_at IS NULL")
    rows = conn.execute(
        "SELECT id,recipient,subject,body,reference,priority,reply_to FROM fleet_messages WHERE payload_hash IS NULL"
    ).fetchmany(1000)
    while rows:
        for message_id, recipient, subject, body, reference, priority, reply_to in rows:
            digest = _payload_digest(str(recipient), str(subject), str(body), reference, str(priority), reply_to)
            conn.execute("UPDATE fleet_messages SET payload_hash=? WHERE id=?", (digest, message_id))
        rows = conn.execute(
            "SELECT id,recipient,subject,body,reference,priority,reply_to FROM fleet_messages WHERE payload_hash IS NULL"
        ).fetchmany(1000)
    conn.execute(_PRINCIPAL_SCHEMA)
    principal_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(fleet_message_principals)")}
    if "enrollment_id" not in principal_columns:
        conn.execute("ALTER TABLE fleet_message_principals ADD COLUMN enrollment_id TEXT")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS fleet_message_sends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL, client_id TEXT NOT NULL, received_at TEXT NOT NULL
        )"""
    )
    send_cutoff = (datetime.now(timezone.utc) - SENDER_DAILY_WINDOW).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO fleet_message_sends(sender,client_id,received_at) "
        "SELECT m.sender,m.client_id,m.received_at FROM fleet_messages AS m "
        "WHERE m.received_at IS NOT NULL AND m.received_at>=? AND NOT EXISTS ("
        "SELECT 1 FROM fleet_message_sends AS s WHERE s.sender=m.sender AND s.client_id=m.client_id)",
        (send_cutoff,),
    )
    conn.execute("CREATE INDEX IF NOT EXISTS fleet_messages_sender ON fleet_messages (sender, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS fleet_messages_inbox ON fleet_messages (recipient, id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS fleet_messages_unacked_recipient "
        "ON fleet_messages (recipient) WHERE acked_at IS NULL"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS fleet_messages_created_at ON fleet_messages (created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS fleet_messages_reply_to ON fleet_messages (reply_to)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS fleet_message_sends_sender_received ON fleet_message_sends (sender, received_at)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS fleet_message_sends_received ON fleet_message_sends (received_at)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS fleet_messages_sender_client ON fleet_messages (sender, client_id)")
    from . import fleet_hub_message_grants

    fleet_hub_message_grants.ensure_schema(conn)
    _prune_expired(conn)


def _expiry_cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()


def _sqlite_integer(value: Any, *, allow_zero: bool = False) -> TypeGuard[int]:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and (value >= 0 if allow_zero else value >= 1)
        and value <= MAX_SQLITE_INTEGER
    )


def _prune_expired(conn: sqlite3.Connection) -> None:
    was_in_transaction = conn.in_transaction
    cutoff = _expiry_cutoff()
    conn.execute(
        "UPDATE fleet_messages SET reply_to=NULL WHERE reply_to IN "
        "(SELECT id FROM fleet_messages WHERE created_at < ?)",
        (cutoff,),
    )
    conn.execute("DELETE FROM fleet_messages WHERE created_at < ?", (cutoff,))
    conn.execute(
        "DELETE FROM fleet_message_sends WHERE received_at < ?",
        ((datetime.now(timezone.utc) - SENDER_DAILY_WINDOW).isoformat(),),
    )
    if not was_in_transaction:
        conn.commit()


def unread_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Unread message totals keyed by enrolled machine identity for the fleet board."""
    _prune_expired(conn)
    counts = {
        str(recipient): int(count)
        for recipient, count in conn.execute(
            "SELECT message.recipient,COUNT(*) FROM fleet_messages AS message "
            "JOIN nodes ON nodes.node_id=message.recipient "
            "WHERE message.acked_at IS NULL AND nodes.revoked_at IS NULL GROUP BY message.recipient"
        )
    }
    conn.commit()
    return counts


def _text(value: Any, name: str, limit: int, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise MessageError(f"{name} must be a non-empty UTF-8 string of at most {limit} bytes")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise MessageError(f"{name} must contain valid UTF-8 text") from exc
    if size > limit:
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
        "client_id",
        "reply_to",
        "created_at",
        "acked_at",
        "acked_by",
        "sender_label",
        "thread",
        "kind",
    )
    result = dict(zip(keys, row, strict=True))
    result["kind"] = result["kind"] or "message"
    return result


def _fetch(conn: sqlite3.Connection, message_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id,sender,recipient,subject,body,reference,priority,client_id,reply_to,created_at,acked_at,acked_by,sender_label,thread,kind "
        "FROM fleet_messages WHERE id=?",
        (message_id,),
    ).fetchone()
    return _row(row) if row else None


def _active_principal(conn: sqlite3.Connection, principal: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM nodes WHERE node_id=? AND revoked_at IS NULL", (principal,)).fetchone() is not None
        or conn.execute(
            "SELECT 1 FROM fleet_message_principals WHERE principal_id=? AND revoked_at IS NULL", (principal,)
        ).fetchone()
        is not None
    )


def _as_active_principal(conn: sqlite3.Connection, principal: str, operation: Any) -> Any:
    opened = not conn.in_transaction
    if opened:
        conn.execute("BEGIN IMMEDIATE")
    try:
        if not _active_principal(conn, principal):
            raise MessageAuthError("sender is not an active hub principal")
        result = operation()
        if opened:
            conn.commit()
        return result
    except BaseException:
        if opened:
            conn.rollback()
        raise


def _enqueued_at(value: Any) -> str:
    now = datetime.now(timezone.utc)
    if value is None:
        return now.isoformat()
    text = _text(value, "enqueued_at", 64)
    assert text is not None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MessageError("enqueued_at must be an ISO-8601 timestamp") from exc
    if stamp.tzinfo is None:
        raise MessageError("enqueued_at must include a UTC offset")
    try:
        stamp = stamp.astimezone(timezone.utc)
    except OverflowError as exc:
        raise MessageError("enqueued_at is outside the supported timestamp range") from exc
    if stamp < now - timedelta(days=RETENTION_DAYS):
        raise MessageError("message expired before delivery")
    if stamp > now + MAX_FUTURE_SKEW:
        raise MessageFutureTimestamp("enqueued_at is too far in the future")
    return stamp.isoformat()


def add_principal(conn: sqlite3.Connection, raw: object) -> dict[str, Any]:
    from . import fleet_hub_message_grants as grants

    allowed = {"name", "label", "request_id", "token", "expected_enrollment_id", "may_send_to"}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise MessageError("principal enrollment requires only name and optional label")
    patterns = grants.normalize_patterns(raw.get("may_send_to", []))
    name = _text(raw.get("name"), "name", 63)
    label = _text(raw.get("label", name), "label", 128)
    assert name is not None and label is not None
    if _PRINCIPAL_NAME_RE.fullmatch(name) is None:
        raise MessageError(
            "name must start with a lowercase letter and contain only lowercase letters, digits, dots, underscores, or hyphens"
        )
    principal_id = f"agent:{name}"
    request_id = raw.get("request_id")
    secret = raw.get("token")
    expected_enrollment_id = raw.get("expected_enrollment_id")
    if expected_enrollment_id is not None and (
        not isinstance(expected_enrollment_id, str) or _ENROLLMENT_ID_RE.fullmatch(expected_enrollment_id) is None
    ):
        raise MessageError("expected_enrollment_id is invalid")
    if request_id is None and secret is None:
        request_id = secrets.token_urlsafe(24)
        secret = secrets.token_urlsafe(32)
    if (request_id is None) != (secret is None):
        raise MessageError("principal enrollment request_id and token must be provided together")
    if (
        not isinstance(request_id, str)
        or _ENROLLMENT_ID_RE.fullmatch(request_id) is None
        or not isinstance(secret, str)
        or _PRINCIPAL_TOKEN_RE.fullmatch(secret) is None
    ):
        raise MessageError("principal enrollment request_id or token is invalid")
    assert isinstance(secret, str)
    token_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT revoked_at,enrollment_id,token_hash,label FROM fleet_message_principals WHERE principal_id=?",
            (principal_id,),
        ).fetchone()
        created_at = datetime.now(timezone.utc).isoformat()
        if existing is None:
            if expected_enrollment_id is not None:
                raise MessageConflict("principal enrollment generation changed", code="principal_enrollment_changed")
            conn.execute(
                "INSERT INTO fleet_message_principals(principal_id,name,label,token_hash,created_at,enrollment_id) "
                "VALUES(?,?,?,?,?,?)",
                (principal_id, name, label, token_hash, created_at, request_id),
            )
        elif existing[0] is not None:
            if request_id is not None and existing[1] == request_id:
                raise MessageConflict(
                    "revoked principal enrollment cannot be replayed", code="principal_enrollment_revoked"
                )
            if "expected_enrollment_id" not in raw or expected_enrollment_id != existing[1]:
                raise MessageConflict("principal enrollment generation changed", code="principal_enrollment_changed")
            conn.execute(
                "UPDATE fleet_message_principals SET label=?,token_hash=?,created_at=?,revoked_at=NULL,enrollment_id=? "
                "WHERE principal_id=? AND enrollment_id IS ? AND revoked_at IS NOT NULL",
                (label, token_hash, created_at, request_id, principal_id, expected_enrollment_id),
            )
        elif request_id is not None and existing[1] == request_id:
            if existing[2] != token_hash or existing[3] != label:
                raise MessageConflict("request_id was already used for a different principal enrollment")
            if grants.grants_for(conn, principal_id) != patterns:
                raise MessageConflict(
                    "principal send grants changed since enrollment. Inspect principals show or revoke before re-enrolling",
                    code="principal_enrollment_grants_changed",
                )
        else:
            raise MessageError("message principal already exists")
        grants.replace_grants(conn, principal_id, patterns)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {
        "principal_id": principal_id,
        "name": name,
        "label": label,
        "token": secret,
        "enrollment_id": request_id,
        "may_send_to": patterns,
    }


def lookup_principal_token(conn: sqlite3.Connection, presented: str) -> str | None:
    digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    row = conn.execute(
        "SELECT principal_id FROM fleet_message_principals WHERE token_hash=? AND revoked_at IS NULL", (digest,)
    ).fetchone()
    return str(row[0]) if row else None


def get_principal(conn: sqlite3.Connection, raw: object) -> dict[str, Any] | None:
    from . import fleet_hub_message_grants as grants

    if not isinstance(raw, dict) or set(raw) != {"action", "name"} or raw.get("action") != "get":
        raise MessageError("principal lookup requires action='get' and name")
    name = _text(raw.get("name"), "name", 63)
    assert name is not None
    row = conn.execute(
        "SELECT principal_id,name,enrollment_id,revoked_at FROM fleet_message_principals WHERE principal_id=?",
        (f"agent:{name}",),
    ).fetchone()
    return (
        {
            "principal_id": str(row[0]),
            "name": str(row[1]),
            "enrollment_id": row[2],
            "revoked": row[3] is not None,
            "may_send_to": grants.grants_for(conn, str(row[0])),
        }
        if row
        else None
    )


def revoke_principal(conn: sqlite3.Connection, raw: object) -> dict[str, str | None] | None:
    if (
        not isinstance(raw, dict)
        or set(raw) != {"action", "name", "expected_enrollment_id"}
        or raw.get("action") != "revoke"
    ):
        raise MessageError("principal revocation requires action='revoke', name, and expected enrollment generation")
    name = _text(raw.get("name"), "name", 63)
    assert name is not None
    expected_enrollment_id = raw.get("expected_enrollment_id")
    if expected_enrollment_id is not None and (
        not isinstance(expected_enrollment_id, str) or _ENROLLMENT_ID_RE.fullmatch(expected_enrollment_id) is None
    ):
        raise MessageError("expected_enrollment_id is invalid")
    principal_id = f"agent:{name}"
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT enrollment_id,revoked_at FROM fleet_message_principals WHERE principal_id=?", (principal_id,)
        ).fetchone()
        if existing is None:
            conn.commit()
            return None
        if existing[0] != expected_enrollment_id:
            raise MessageConflict("principal enrollment generation changed", code="principal_enrollment_changed")
        conn.execute(
            "UPDATE fleet_message_principals SET revoked_at=? "
            "WHERE principal_id=? AND enrollment_id IS ? AND revoked_at IS NULL",
            (datetime.now(timezone.utc).isoformat(), principal_id, expected_enrollment_id),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {"principal_id": principal_id, "name": name, "enrollment_id": expected_enrollment_id}


def handle_principal_admin(conn: sqlite3.Connection, raw: object) -> tuple[int, dict[str, Any]]:
    if isinstance(raw, dict) and raw.get("action") == "grants":
        from .fleet_hub_message_grants import set_grants

        principal = set_grants(conn, raw)
        return (200, {"principal": principal}) if principal else (404, {"error": "principal not found"})
    if isinstance(raw, dict) and raw.get("action") == "get":
        principal = get_principal(conn, raw)
        return (200, {"principal": principal}) if principal else (404, {"error": "principal not found"})
    if isinstance(raw, dict) and raw.get("action") == "revoke":
        principal = revoke_principal(conn, raw)
        return (200, {"principal": principal}) if principal else (404, {"error": "principal not found"})
    if isinstance(raw, dict) and raw.get("action") not in (None, "add"):
        raise MessageError("principal action must be add, get, grants, or revoke")
    if isinstance(raw, dict) and "action" in raw:
        raw = {key: value for key, value in raw.items() if key != "action"}
    return 201, {"principal": add_principal(conn, raw)}


def send(
    conn: sqlite3.Connection,
    raw: dict[str, Any],
    *,
    sender: str,
    reply_to: int | None = None,
) -> dict[str, Any]:
    _prune_expired(conn)
    recipient = _text(raw.get("to"), "to", 256)
    subject = _text(raw.get("subject"), "subject", SUBJECT_LIMIT)
    body = _text(raw.get("body"), "body", BODY_LIMIT)
    reference = _text(raw.get("ref"), "ref", REF_LIMIT, required=False)
    client_id = _text(raw.get("client_id"), "client_id", 64, required=False) or secrets.token_hex(16)
    enqueued_at = _enqueued_at(raw.get("enqueued_at"))
    priority = raw.get("priority", "normal")
    if not isinstance(priority, str) or priority not in PRIORITIES:
        raise MessageError("priority must be low, normal, high, or urgent")
    assert recipient is not None and subject is not None and body is not None
    extras = _metadata(raw, sender=sender)
    message_hash = _payload_digest(recipient, subject, body, reference, priority, reply_to, extras=extras)
    opened = not conn.in_transaction
    if opened:
        conn.execute("BEGIN IMMEDIATE")
    try:
        now = datetime.now(timezone.utc)
        if not _active_principal(conn, sender):
            raise MessageAuthError("sender is not an active hub principal")
        existing = conn.execute(
            "SELECT id,payload_hash FROM fleet_messages WHERE sender=? AND client_id=?", (sender, client_id)
        ).fetchone()
        if existing is not None:
            if existing[1] is not None and existing[1] != message_hash:
                raise MessageConflict("client_id was already used for a different message")
            if opened:
                conn.commit()
            return _fetch(conn, int(existing[0])) or {}
        from .fleet_hub_message_grants import require_granted, require_recipient_cap

        require_granted(conn, sender, recipient)
        if not _active_principal(conn, recipient):
            revoked = conn.execute(
                "SELECT 1 FROM nodes WHERE node_id=? AND revoked_at IS NOT NULL "
                "UNION ALL SELECT 1 FROM fleet_message_principals WHERE principal_id=? AND revoked_at IS NOT NULL "
                "LIMIT 1",
                (recipient, recipient),
            ).fetchone()
            if revoked is not None:
                raise MessageRecipientInactive("recipient is temporarily inactive", recipient=recipient)
            raise MessageError("recipient is not an active hub principal")
        require_recipient_cap(conn, sender, recipient)
        count = conn.execute(
            "SELECT COUNT(*) FROM fleet_messages WHERE recipient=? AND acked_at IS NULL", (recipient,)
        ).fetchone()[0]
        if count >= UNACKED_QUOTA:
            raise MessageQuotaError(
                "recipient message quota exceeded", code="message_recipient_quota", recipient=recipient
            )
        sender_cutoff = (now - SENDER_DAILY_WINDOW).isoformat()
        sender_count = conn.execute(
            "SELECT COUNT(*) FROM fleet_message_sends WHERE sender=? AND received_at>=?", (sender, sender_cutoff)
        ).fetchone()[0]
        if sender_count >= SENDER_DAILY_QUOTA:
            raise MessageQuotaError("sender message quota exceeded", code="message_sender_quota")
        created_at = enqueued_at
        cursor = conn.execute(
            "INSERT INTO fleet_messages(sender,recipient,subject,body,reference,priority,client_id,payload_hash,reply_to,created_at,received_at,sender_label,thread,kind) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sender,
                recipient,
                subject,
                body,
                reference,
                priority,
                client_id,
                message_hash,
                reply_to,
                created_at,
                now.isoformat(),
                extras.get("sender_label"),
                extras.get("thread"),
                extras.get("kind"),
            ),
        )
        message_id = cursor.lastrowid
        if message_id is None:  # pragma: no cover - SQLite INSERT always supplies it
            raise sqlite3.DatabaseError("message insert did not return an id")
        conn.execute(
            "INSERT INTO fleet_message_sends(sender,client_id,received_at) VALUES(?,?,?)",
            (sender, client_id, now.isoformat()),
        )
        if opened:
            conn.commit()
        return _fetch(conn, message_id) or {}
    except BaseException:
        if opened:
            conn.rollback()
        raise


def list_messages(conn: sqlite3.Connection, raw: dict[str, Any], *, recipient: str) -> dict[str, Any]:
    opened = not conn.in_transaction
    _prune_expired(conn)
    after = raw.get("after", 0)
    if not _sqlite_integer(after, allow_zero=True):
        raise MessageError("after must be a non-negative integer within SQLite's range")
    unacked = raw.get("unacked", False)
    if not isinstance(unacked, bool):
        raise MessageError("unacked must be a boolean")
    rows = conn.execute(
        "SELECT id,sender,recipient,subject,body,reference,priority,client_id,reply_to,created_at,acked_at,acked_by,sender_label,thread,kind "
        "FROM fleet_messages WHERE recipient=? AND id>? "
        + ("AND acked_at IS NULL " if unacked else "")
        + "ORDER BY id LIMIT 100",
        (recipient, after),
    ).fetchall()
    if opened:
        conn.commit()
    return _message_page(rows, after=after)


def _message_page(rows: list[tuple[Any, ...]], *, after: int) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    serialized_messages_size = 0
    for row in rows:
        candidate = _row(row)
        serialized_candidate = json.dumps(candidate).encode("utf-8")
        response_overhead = len(json.dumps({"messages": [], "next_cursor": candidate["id"]}).encode("utf-8")) - 2
        separator_size = 2 if messages else 0
        if (
            response_overhead + serialized_messages_size + separator_size + len(serialized_candidate)
            > MAX_LIST_RESPONSE_BYTES
        ):
            break
        messages.append(candidate)
        serialized_messages_size += separator_size + len(serialized_candidate)
    return {"messages": messages, "next_cursor": messages[-1]["id"] if messages else after}


def sent(conn: sqlite3.Connection, raw: dict[str, Any], *, sender: str) -> dict[str, Any]:
    _prune_expired(conn)
    if set(raw) - {"action", "after", "ids"} or ("after" in raw and "ids" in raw):
        raise MessageError("sent accepts either after or ids")
    after = raw.get("after", 0)
    if not _sqlite_integer(after, allow_zero=True):
        raise MessageError("after must be a non-negative integer within SQLite's range")
    params: list[Any] = [sender]
    if "ids" in raw:
        ids = raw["ids"]
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or not all(_sqlite_integer(i) for i in ids):
            raise MessageError("ids must contain 1 to 100 positive integers within SQLite's range")
        condition = "id IN (" + ",".join("?" for _ in ids) + ")"
        params.extend(ids)
    else:
        condition = "id>?"
        params.append(after)
    rows = conn.execute(
        "SELECT id,sender,recipient,subject,body,reference,priority,client_id,reply_to,created_at,acked_at,acked_by,"
        "sender_label,thread,kind FROM fleet_messages WHERE sender=? AND " + condition + " ORDER BY id LIMIT 100",
        params,
    ).fetchall()
    return _message_page(rows, after=after)


def ack(conn: sqlite3.Connection, message_id: Any, *, recipient: str) -> dict[str, Any] | None:
    opened = not conn.in_transaction
    _prune_expired(conn)
    if not _sqlite_integer(message_id):
        raise MessageError("id must be a positive integer within SQLite's range")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE fleet_messages SET acked_at=COALESCE(acked_at,?), acked_by=COALESCE(acked_by,?) "
        "WHERE id=? AND recipient=?",
        (now, recipient, message_id, recipient),
    )
    if opened:
        conn.commit()
    return (
        _fetch(conn, message_id)
        if conn.execute("SELECT 1 FROM fleet_messages WHERE id=? AND recipient=?", (message_id, recipient)).fetchone()
        else None
    )


def reply(conn: sqlite3.Connection, raw: dict[str, Any], *, sender: str) -> dict[str, Any] | None:
    _prune_expired(conn)
    message_id = raw.get("id")
    if not _sqlite_integer(message_id):
        raise MessageError("id must be a positive integer within SQLite's range")
    original = _fetch(conn, message_id)
    if original is None or original["recipient"] != sender:
        client_id = _text(raw.get("client_id"), "client_id", 64, required=False)
        if not client_id:
            return None
        accepted = conn.execute(
            "SELECT recipient,subject,reference,priority,thread FROM fleet_messages "
            "WHERE sender=? AND client_id=? AND reply_to IS NULL",
            (sender, client_id),
        ).fetchone()
        if accepted is None:
            return None
        subject = raw.get("subject")
        if subject is None:
            subject = str(accepted[1])
        reference = raw.get("ref")
        if reference is None:
            reference = accepted[2]
        message = send(
            conn,
            {
                "to": accepted[0],
                "subject": subject,
                "body": raw.get("body"),
                "ref": reference,
                "priority": raw.get("priority", accepted[3]),
                "client_id": client_id,
                "enqueued_at": raw.get("enqueued_at"),
                "thread": raw.get("thread", accepted[4]),
                **{field: raw[field] for field in ("sender_label", "kind") if field in raw},
            },
            sender=sender,
            reply_to=message_id,
        )
        return _fetch(conn, message["id"])
    subject = raw.get("subject")
    if subject is None:
        prefixed = f"Re: {original['subject']}"
        subject = prefixed if len(prefixed.encode("utf-8")) <= SUBJECT_LIMIT else original["subject"]
    else:
        subject = _text(subject, "subject", SUBJECT_LIMIT)
    reference = raw.get("ref")
    if reference is None:
        reference = original["ref"]
    else:
        reference = _text(reference, "ref", REF_LIMIT, required=False)
    message = send(
        conn,
        {
            "to": original["sender"],
            "subject": subject,
            "body": raw.get("body"),
            "ref": reference,
            "priority": raw.get("priority", "normal"),
            "client_id": raw.get("client_id"),
            "enqueued_at": raw.get("enqueued_at"),
            "thread": raw.get("thread", original["thread"]),
            "sender_label": raw.get("sender_label"),
            "kind": raw.get("kind"),
        },
        sender=sender,
        reply_to=message_id,
    )
    return _fetch(conn, message["id"])


def handle(conn: sqlite3.Connection, raw: Any, *, caller: str | None) -> tuple[int, dict[str, Any]]:
    if caller is None:
        return 403, {"error": "a node principal is required"}
    if not isinstance(raw, dict):
        raise MessageError("message request must be an object")
    action = raw.get("action")
    if action == "send":
        return 201, {"message": send(conn, raw, sender=caller)}
    if action == "sent":
        return 200, _as_active_principal(conn, caller, lambda: sent(conn, raw, sender=caller))
    if action == "list":
        return 200, _as_active_principal(conn, caller, lambda: list_messages(conn, raw, recipient=caller))
    if action == "ack":
        message = _as_active_principal(conn, caller, lambda: ack(conn, raw.get("id"), recipient=caller))
        return (200, {"message": message}) if message else (404, {"error": "message not found"})
    if action == "reply":
        message = _as_active_principal(conn, caller, lambda: reply(conn, raw, sender=caller))
        return (201, {"message": message}) if message else (404, {"error": "message not found"})
    raise MessageError("action must be send, list, sent, ack, or reply")
