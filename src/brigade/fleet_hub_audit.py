"""Standalone D9 audit storage. Callers own authorization and transactions.

Call ``ensure_schema`` explicitly at startup, then ``append`` inside the same
transaction as the audited mutation. No helper opens a connection, explicitly
starts or finishes a transaction, or performs authentication. Refusal aggregation and rate
limits belong to the future invite caller, not this primitive.

Detail has exactly nine optional fields: principal_id, node_id, invite_id,
hub_id, key_id, statement_id, role, grants, count. Identifier values use ASCII
letters/digits and ._- (plus an optional agent: prefix for principals). They are
1..128 characters, with agent names restricted to 1..63 lowercase characters.
Actor, subject, principal_id, node_id and bare node grants accept canonical
public identities, including opaque node ids up to 128 characters. Agent
principals and grants retain the lowercase name grammar and 63-character limit.
Credential markers are refused in every identifier on write. Provider prefixes
must start the value or follow a separator (., _, -, or :), not a letter/digit.
Sensitive labels (secret, credential, token, password, bearer, binding,
keyrecord, content) are explicit markers only at the start, after a dot, or
after the optional agent: prefix, and followed by a dot, such as
``fixture.token.value`` or ``agent:token.value``. Plain words and substrings
in ordinary names are not credential markers. Nonidentity fields
(invite_id, hub_id, key_id, statement_id) also refuse runs of 32 or more
base64url characters, even inside longer identifiers. Public mesh ids such as
h_<22 base64url characters>, k1_<22 characters> and 22-character invite ids fit.
Callers must supply public identifiers: no syntax check can distinguish every
unmarked credential from a same-shaped public identity. Never pass credentials
as identifiers. The reserved literal ``unknown`` is refused.

Roles are agent or teammate. Grants are at most 64 identifiers or the exact
wildcards *, node:*, agent:*. Count is an integer in 0..1,000,000 (not bool).
Nested/free-text fields, arbitrary keys and recognizable credentials are refused
with value-free errors. Page cursors are SQLite nonnegative integers and limits
are 1..500, with defaults 0 and 100. Rows contain decoded detail dictionaries.
Reads validate stored structure and identifier bounds without reapplying the
current write-admission credential markers to previously admitted history.
SQLite triggers prevent row mutation, not access by a database administrator.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Literal, TypedDict, cast

Action = Literal[
    "invite.create",
    "invite.redeem",
    "invite.redeem_refused",
    "invite.revoke",
    "member.remove",
    "grants.change",
    "key.publish",
    "key.rotate",
    "key.revoke",
    "hub.identity",
    "hub.migration",
    "statement.upload",
]


class AuditDetail(TypedDict, total=False):
    """Only public identifiers, roles, grant patterns and bounded counts."""

    principal_id: str
    node_id: str
    invite_id: str
    hub_id: str
    key_id: str
    statement_id: str
    role: Literal["agent", "teammate"]
    grants: list[str]
    count: int


class AuditRow(TypedDict):
    """A persisted audit record, ordered and addressed by its integer id."""

    id: int
    at: str
    actor: str
    action: Action
    subject: str
    detail: AuditDetail


_ACTIONS = frozenset(
    {
        "invite.create",
        "invite.redeem",
        "invite.redeem_refused",
        "invite.revoke",
        "member.remove",
        "grants.change",
        "key.publish",
        "key.rotate",
        "key.revoke",
        "hub.identity",
        "hub.migration",
        "statement.upload",
    }
)
_ID_FIELDS = frozenset({"principal_id", "node_id", "invite_id", "hub_id", "key_id", "statement_id"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_AGENT = re.compile(r"agent:[a-z][a-z0-9._-]{0,62}")
_CREDENTIAL = re.compile(
    r"(?<![A-Za-z0-9])(?:bfi1|gh[pousr]_|github_pat_|glpat-|gldt-|sk[-_]|xox[baprs]-|xoxe[.-]|"
    r"npm_|hf_|AIza[A-Za-z0-9_-]{30,}|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|ya29\.|eyJ)|"
    r"(?:^|[.:])(?:secret|credential|token|password|bearer|binding|keyrecord|content)(?=\.)",
    re.IGNORECASE,
)
_OPAQUE = re.compile(r"[A-Za-z0-9_-]{32,}")


def _identifier(value: object, *, principal: bool = False, identity: bool = False, admission: bool = True) -> str:
    if (
        not isinstance(value, str)
        or value == "unknown"
        or len(value) > 128
        or (admission and _CREDENTIAL.search(value))
        or (not identity and _OPAQUE.search(value))
    ):
        raise ValueError("invalid audit identifier")
    pattern = _AGENT if principal and value.startswith("agent:") else _IDENTIFIER
    if pattern.fullmatch(value) is None:
        raise ValueError("invalid audit identifier")
    return value


def _action(value: object) -> Action:
    if not isinstance(value, str) or value not in _ACTIONS:
        raise ValueError("invalid audit action")
    return cast(Action, value)


def _detail(raw: object, *, admission: bool = True) -> AuditDetail:
    if not isinstance(raw, dict) or not raw.keys() <= _ID_FIELDS | {"role", "grants", "count"}:
        raise ValueError("invalid audit detail fields")
    result = dict(raw)
    for field, value in raw.items():
        if field in _ID_FIELDS:
            _identifier(
                value,
                principal=field == "principal_id",
                identity=field in ("principal_id", "node_id"),
                admission=admission,
            )
        elif field == "role":
            if not isinstance(value, str) or value not in ("agent", "teammate"):
                raise ValueError("invalid audit role")
        elif field == "count":
            if type(value) is not int or not 0 <= value <= 1_000_000:
                raise ValueError("invalid audit count")
        elif field == "grants":
            if not isinstance(value, list) or len(value) > 64:
                raise ValueError("invalid audit grants")
            for pattern in value:
                if not isinstance(pattern, str):
                    raise ValueError("invalid audit grant pattern")
                if pattern not in ("*", "node:*", "agent:*"):
                    _identifier(pattern, principal=True, identity=True, admission=admission)
            result[field] = list(value)
    return cast(AuditDetail, result)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create the table and guards without committing caller work.

    Individual execute calls intentionally avoid executescript's implicit commit.
    Existing transactions, including schema changes, remain caller-owned.
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS fleet_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
        subject TEXT NOT NULL, detail TEXT NOT NULL)""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS fleet_audit_log_no_update
        BEFORE UPDATE ON fleet_audit_log BEGIN
        SELECT RAISE(ABORT, 'audit log is append-only'); END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS fleet_audit_log_no_delete
        BEFORE DELETE ON fleet_audit_log BEGIN
        SELECT RAISE(ABORT, 'audit log is append-only'); END""")
    # REPLACE's implicit DELETE skips delete triggers unless recursive_triggers
    # is enabled. Refuse the collision before SQLite can remove the old row.
    conn.execute("""CREATE TRIGGER IF NOT EXISTS fleet_audit_log_no_replace
        BEFORE INSERT ON fleet_audit_log
        WHEN EXISTS (SELECT 1 FROM fleet_audit_log WHERE id=NEW.id) BEGIN
        SELECT RAISE(ABORT, 'audit log is append-only'); END""")


def append(conn: sqlite3.Connection, *, actor: str, action: Action, subject: str, detail: AuditDetail) -> int:
    """Validate and append in the caller's transaction; return the new row id.

    Invalid input raises ValueError without including submitted values or issuing
    a write. SQLite errors propagate. This never commits or rolls back, including
    on failure. An active transaction is required unless legacy transaction control
    with non-None isolation_level lets SQLite open an implicit transaction.
    """
    actor = _identifier(actor, principal=True, identity=True)
    subject = _identifier(subject, principal=True, identity=True)
    action = _action(action)
    validated = _detail(detail)
    legacy_control = getattr(sqlite3, "LEGACY_TRANSACTION_CONTROL", None)
    implicit_transaction = getattr(conn, "autocommit", None) == legacy_control and conn.isolation_level is not None
    if not conn.in_transaction and not implicit_transaction:
        raise ValueError("audit append requires a transaction")
    cursor = conn.execute(
        "INSERT INTO fleet_audit_log(at,actor,action,subject,detail) VALUES(?,?,?,?,?)",
        (
            datetime.now(timezone.utc).isoformat(),
            actor,
            action,
            subject,
            json.dumps(validated, sort_keys=True, separators=(",", ":")),
        ),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def page(conn: sqlite3.Connection, *, after_id: int = 0, limit: int = 100) -> list[AuditRow]:
    """Read up to limit records strictly after the cursor, in ascending id order.

    The future caller must authenticate before reading. No schema initialization
    or transaction management occurs here. Invalid bounds raise ValueError.
    Stored identities, action and detail retain structural validation,
    independent of later changes to credential markers used to admit new writes.
    """
    if type(after_id) is not int or not 0 <= after_id <= 2**63 - 1:
        raise ValueError("invalid audit cursor")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("invalid audit page limit")
    rows = conn.execute(
        "SELECT id,at,actor,action,subject,detail FROM fleet_audit_log WHERE id>? ORDER BY id ASC LIMIT ?",
        (after_id, limit),
    )
    return [
        AuditRow(
            id=row[0],
            at=row[1],
            actor=_identifier(row[2], principal=True, identity=True, admission=False),
            action=_action(row[3]),
            subject=_identifier(row[4], principal=True, identity=True, admission=False),
            detail=_detail(json.loads(row[5]), admission=False),
        )
        for row in rows
    ]
