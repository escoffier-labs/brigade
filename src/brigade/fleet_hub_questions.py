"""Online, credential-generation-bound questions and their durable terminal audit."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from . import fleet_hub_messages as messages

QUESTION_LIMIT = 4096
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_QUESTION_ID = re.compile(r"[0-9a-f]{32}")
_TABLE = """CREATE TABLE IF NOT EXISTS fleet_questions (
    question_id TEXT PRIMARY KEY,
    caller TEXT NOT NULL,
    generation TEXT NOT NULL,
    request_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    operator TEXT NOT NULL,
    question TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    answer TEXT,
    answer_id INTEGER,
    source TEXT,
    answered_by TEXT,
    answered_at TEXT,
    UNIQUE(caller,generation,request_id)
)"""
_INDEX = "CREATE INDEX IF NOT EXISTS fleet_questions_deadline ON fleet_questions(status,deadline)"
# Copy the accepted answer in the same transaction as message insertion. Audit
# records have no message FK, so ordinary pruning cannot erase accepted answers.
# Both stamps are hub-generated UTC ISO strings. Direct comparison preserves
# microseconds that SQLite date conversion would round to milliseconds.
_TRIGGER = """CREATE TRIGGER IF NOT EXISTS fleet_questions_reply AFTER INSERT ON fleet_messages
BEGIN
    UPDATE fleet_questions SET status='answered', answer=NEW.body, answer_id=NEW.id,
        source='operator_reply', answered_by=NEW.sender, answered_at=NEW.received_at
    WHERE status='pending' AND message_id=NEW.reply_to AND operator=NEW.sender
        AND caller=NEW.recipient AND NEW.received_at<=deadline
        AND (EXISTS (SELECT 1 FROM nodes WHERE node_id=caller AND token_sha256=generation AND revoked_at IS NULL)
             OR EXISTS (SELECT 1 FROM fleet_message_principals
                WHERE principal_id=caller AND token_hash=generation AND revoked_at IS NULL));
END"""
_OBJECTS = (("fleet_questions", _TABLE), ("fleet_questions_deadline", _INDEX), ("fleet_questions_reply", _TRIGGER))


def missing_schema(conn: sqlite3.Connection) -> list[str]:
    """Read-only startup fast path, with additive table/index/trigger repair."""
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'fleet_questions%'")}
    return [ddl for name, ddl in _OBJECTS if name not in names]


def ensure_schema(conn: sqlite3.Connection) -> None:
    for ddl in missing_schema(conn):
        conn.execute(ddl)


def _generation(conn: sqlite3.Connection, caller: str) -> str:
    row = conn.execute(
        "SELECT token_sha256 FROM nodes WHERE node_id=? AND revoked_at IS NULL "
        "UNION ALL SELECT token_hash FROM fleet_message_principals WHERE principal_id=? AND revoked_at IS NULL",
        (caller, caller),
    ).fetchone()
    if row is None:
        raise messages.MessageAuthError("sender is not an active hub principal")
    return str(row[0])


def validate_create(raw: dict[str, Any]) -> tuple[str, int, str]:
    if set(raw) - {"action", "question", "timeout_secs", "request_id"}:
        raise messages.MessageError("unsupported question fields")
    question = messages._text(raw.get("question"), "question", QUESTION_LIMIT)
    timeout = raw.get("timeout_secs", 60)
    request_id = raw.get("request_id")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 120:
        raise messages.MessageError("timeout_secs must be an integer from 1 to 120")
    if not isinstance(request_id, str) or _REQUEST_ID.fullmatch(request_id) is None:
        raise messages.MessageError("request_id must be 1 to 64 ASCII identifier characters")
    assert question is not None
    return question, timeout, request_id


def _expire(conn: sqlite3.Connection, now: str) -> None:
    conn.execute(
        "UPDATE fleet_questions SET status='timeout' WHERE status='pending' AND deadline<=?",
        (now,),
    )
    cutoff = (datetime.fromisoformat(now) - timedelta(days=messages.RETENTION_DAYS)).isoformat()
    conn.execute("DELETE FROM fleet_questions WHERE created_at<?", (cutoff,))


def _result(row: tuple[Any, ...], *, created: bool = False) -> dict[str, Any]:
    keys = (
        "question_id",
        "caller",
        "request_id",
        "operator",
        "message_id",
        "deadline",
        "status",
        "answer",
        "answer_id",
        "source",
        "answered_by",
        "answered_at",
    )
    result = dict(zip(keys, row, strict=True))
    result["message_id"] = str(result["message_id"])
    if result["answer_id"] is not None:
        result["answer_id"] = str(result["answer_id"])
    return {**result, "created": created}


_COLUMNS = (
    "question_id,caller,request_id,operator,message_id,deadline,status,answer,answer_id,source,answered_by,answered_at"
)


def _fetch(conn: sqlite3.Connection, question_id: str) -> tuple[Any, ...]:
    row = conn.execute(f"SELECT {_COLUMNS} FROM fleet_questions WHERE question_id=?", (question_id,)).fetchone()
    assert row is not None
    return row


def _create(conn: sqlite3.Connection, raw: dict[str, Any], caller: str, generation: str, now: str) -> dict[str, Any]:
    question, timeout, request_id = validate_create(raw)
    digest = hashlib.sha256(json.dumps([question, timeout], ensure_ascii=False).encode("utf-8")).hexdigest()
    existing = conn.execute(
        "SELECT question_id,payload_hash FROM fleet_questions WHERE caller=? AND generation=? AND request_id=?",
        (caller, generation, request_id),
    ).fetchone()
    if existing:
        if existing[1] != digest:
            raise messages.MessageConflict(
                "request_id was used for a different question", code="question_request_conflict"
            )
        return _result(_fetch(conn, existing[0]))
    operator = os.environ.get("BRIGADE_FLEET_OPERATOR_RECIPIENT", "")
    if not operator or operator == caller or not messages._active_principal(conn, operator):
        raise messages.MessageError("hub operator recipient must be another active principal")
    question_id = secrets.token_hex(16)
    deadline = (datetime.fromisoformat(now) + timedelta(seconds=timeout)).isoformat()
    message = messages.send(
        conn,
        {
            "to": operator,
            "subject": "Operator question",
            "body": question,
            "kind": "question",
            "client_id": secrets.token_hex(16),
        },
        sender=caller,
    )
    conn.execute(
        "INSERT INTO fleet_questions(question_id,caller,generation,request_id,payload_hash,operator,question,message_id,created_at,deadline) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (question_id, caller, generation, request_id, digest, operator, question, message["id"], now, deadline),
    )
    return _result(_fetch(conn, question_id), created=True)


def handle(
    conn: sqlite3.Connection, raw: dict[str, Any], *, caller: str, authenticated_generation: str | None
) -> tuple[int, dict[str, Any]]:
    def operation() -> dict[str, Any]:
        generation = _generation(conn, caller)
        if authenticated_generation is None or not secrets.compare_digest(generation, authenticated_generation):
            raise messages.MessageAuthError("question credential generation changed")
        now = datetime.now(timezone.utc).isoformat()
        action = raw["action"]
        if action == "question_create":
            validate_create(raw)
        else:
            allowed = {"action", "question_id", "answer"} if action == "question_desktop" else {"action", "question_id"}
            if set(raw) - allowed:
                raise messages.MessageError("unsupported question fields")
            if not isinstance(raw.get("question_id"), str) or _QUESTION_ID.fullmatch(raw["question_id"]) is None:
                raise messages.MessageError("question_id is invalid")
            if action == "question_desktop":
                messages._text(raw.get("answer"), "answer", messages.BODY_LIMIT)
        _expire(conn, now)
        if action == "question_create":
            return _create(conn, raw, caller, generation, now)
        question_id = raw["question_id"]
        owner = conn.execute(
            "SELECT caller,generation FROM fleet_questions WHERE question_id=?", (question_id,)
        ).fetchone()
        if owner != (caller, generation):
            raise messages.MessageForbidden("question is not owned by this credential", code="question_not_owned")
        if action == "question_desktop":
            conn.execute(
                "UPDATE fleet_questions SET status='answered',answer=?,source='caller_reported_desktop',answered_by=?,answered_at=? WHERE question_id=? AND status='pending' AND deadline>?",
                (raw["answer"], caller, now, question_id, now),
            )
            messages._queue_notification(caller)
        return _result(_fetch(conn, question_id))

    result = messages._as_active_principal(conn, caller, operation)
    return (201 if result["created"] else 200), result
