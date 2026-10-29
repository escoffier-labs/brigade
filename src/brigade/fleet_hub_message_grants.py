"""Transactional recipient grants for external message principals."""

from __future__ import annotations

import re
import secrets
import sqlite3
from collections.abc import Iterable
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
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(fleet_message_principals)")}
    if "grant_epoch" not in columns:
        conn.execute("ALTER TABLE fleet_message_principals ADD COLUMN grant_epoch TEXT")
    for principal, enrollment_epoch, enrollment_id, token_hash in conn.execute(
        "SELECT principal_id,authorization_enrollment_epoch,enrollment_id,token_hash "
        "FROM fleet_message_principals WHERE grant_epoch IS NULL"
    ).fetchall():
        conn.execute(
            "UPDATE fleet_message_principals SET grant_epoch=? WHERE principal_id=?",
            (new_authorization_epoch(excluded=(enrollment_epoch, enrollment_id, token_hash)), principal),
        )


def new_authorization_epoch(*, excluded: tuple[str, ...] = ()) -> str:
    """Choose independent public metadata, with bounded collision handling."""
    for _ in range(4):
        value = secrets.token_urlsafe(24)
        if value not in excluded:
            return value
    raise messages.MessageForbidden("message authorization unavailable", code="message_authorization_unavailable")


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
    previous = conn.execute(
        "SELECT grant_epoch,authorization_enrollment_epoch,enrollment_id,token_hash "
        "FROM fleet_message_principals WHERE principal_id=?",
        (principal_id,),
    ).fetchone()
    epoch = new_authorization_epoch(excluded=tuple(previous) if previous else ())
    conn.execute("DELETE FROM fleet_message_send_grants WHERE principal_id=?", (principal_id,))
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO fleet_message_send_grants(principal_id,pattern,created_at) VALUES(?,?,?)",
        [(principal_id, pattern, now) for pattern in patterns],
    )
    conn.execute("UPDATE fleet_message_principals SET grant_epoch=? WHERE principal_id=?", (epoch, principal_id))


def grants_for(conn: sqlite3.Connection, principal_id: str) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT pattern FROM fleet_message_send_grants WHERE principal_id=? ORDER BY pattern", (principal_id,)
        )
    ]


def classify_principal(conn: sqlite3.Connection, principal_id: str) -> str | None:
    """Classify only active enrolled identities; unsupported shapes fail closed."""
    if principal_id.startswith("agent:"):
        if messages._PRINCIPAL_NAME_RE.fullmatch(principal_id[6:]) is None:
            return None
        row = conn.execute(
            "SELECT role FROM fleet_message_principals WHERE principal_id=? AND revoked_at IS NULL",
            (principal_id,),
        ).fetchone()
        return str(row[0]) if row and row[0] in ("agent", "teammate") else None
    if principal_id == "unknown" or _NODE_ID_RE.fullmatch(principal_id) is None:
        return None
    return (
        "node"
        if conn.execute("SELECT 1 FROM nodes WHERE node_id=? AND revoked_at IS NULL", (principal_id,)).fetchone()
        else None
    )


def permits(conn: sqlite3.Connection, patterns: Iterable[str], sender: str, recipient: str) -> bool:
    """One-way read visibility. Reciprocity belongs only to message insertion."""
    role = classify_principal(conn, sender)
    if role is None:
        return False
    if role == "node":
        return True
    # Public key history remains readable after recipient revocation. Directory
    # readers separately filter active rows; only the caller confers privilege.
    allowed = {recipient, "*", "agent:*" if recipient.startswith("agent:") else "node:*"}
    return recipient != sender and bool(allowed.intersection(patterns))


def authorization_identity(
    conn: sqlite3.Connection, *, caller: str, peers: list[str], authenticated_generation: str | None
) -> dict[str, Any]:
    """Read mutual authorization inside the caller's active-principal snapshot."""
    from .fleet_hub_questions import _generation

    if not caller.startswith("agent:"):
        raise messages.MessageForbidden(
            "identity peers requires an agent principal", code="message_identity_peers_unsupported"
        )
    if authenticated_generation is None or not secrets.compare_digest(
        _generation(conn, caller), authenticated_generation
    ):
        raise messages.MessageAuthError("sender is not an active hub principal")

    def epochs(principal: str) -> tuple[str, str] | None:
        row = conn.execute(
            "SELECT authorization_enrollment_epoch,grant_epoch FROM fleet_message_principals "
            "WHERE principal_id=? AND revoked_at IS NULL",
            (principal,),
        ).fetchone()
        if row is None or any(
            not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value) is None for value in row
        ):
            return None
        return row[0], row[1]

    caller_epochs = epochs(caller)
    if caller_epochs is None:
        raise messages.MessageAuthError("sender is not an active hub principal")
    caller_grants = grants_for(conn, caller)
    records = []
    for peer in peers:
        peer_epochs = epochs(peer)
        authorized = (
            peer_epochs is not None
            and permits(conn, caller_grants, caller, peer)
            and permits(conn, grants_for(conn, peer), peer, caller)
        )
        records.append(
            {
                "principal": peer,
                "authorized": authorized,
                "enrollment_epoch": peer_epochs[0] if authorized and peer_epochs is not None else None,
                "grant_epoch": peer_epochs[1] if authorized and peer_epochs is not None else None,
            }
        )
    return {
        "schema": "brigade.message_authorization.v1",
        "principal": caller,
        "enrollment_epoch": caller_epochs[0],
        "grant_epoch": caller_epochs[1],
        "peers": records,
    }


def require_granted(conn: sqlite3.Connection, sender: str, recipient: str) -> None:
    sender_role = classify_principal(conn, sender)
    recipient_role = classify_principal(conn, recipient)
    if sender_role is None:
        raise messages.MessageAuthError("sender is not an active hub principal")
    symmetric = "teammate" in (sender_role, recipient_role)

    def named(source: str, target: str) -> bool:
        target_role = classify_principal(conn, target)
        allowed = {target, "*", "node:*" if target_role == "node" else "agent:*"}
        return source != target and bool(allowed.intersection(grants_for(conn, source)))

    if (symmetric and (not named(sender, recipient) or not named(recipient, sender))) or (
        not symmetric and sender_role != "node" and not permits(conn, grants_for(conn, sender), sender, recipient)
    ):
        raise messages.MessageForbidden(
            "recipient is not permitted by send grants", code="message_recipient_not_permitted"
        )


def require_recipient_cap(conn: sqlite3.Connection, sender: str, recipient: str) -> None:
    if classify_principal(conn, sender) == "node":
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
    if (
        not isinstance(raw, dict)
        or raw.get("action") != "grants"
        or set(raw) not in ({"action", "name", "may_send_to"}, {"action", "node_id", "may_send_to"})
    ):
        raise messages.MessageError("principal grants requires action='grants', one target, and may_send_to")
    node = "node_id" in raw
    target = raw.get("node_id" if node else "name")
    if (
        not isinstance(target, str)
        or (node and (target == "unknown" or _NODE_ID_RE.fullmatch(target) is None))
        or (not node and messages._PRINCIPAL_NAME_RE.fullmatch(target) is None)
    ):
        raise messages.MessageError("invalid principal grant target")
    principal_id = target if node else f"agent:{target}"
    patterns = normalize_patterns(raw["may_send_to"])
    conn.execute("BEGIN IMMEDIATE")
    try:
        role = classify_principal(conn, principal_id)
        if node and role is None:
            raise messages.MessageError("principal grant target is not active")
        principal: dict[str, Any] | None = (
            {"principal_id": principal_id, "node_id": target}
            if node
            else messages.get_principal(conn, {"action": "get", "name": target})
        )
        if principal is None:
            conn.commit()
            return None
        replace_grants(conn, principal_id, patterns)
        from . import fleet_hub_audit

        fleet_hub_audit.append(
            conn,
            actor="admin",
            action="grants.change",
            subject=principal_id,
            detail={"grants": patterns},
            known_public_identifiers=frozenset({principal_id})
            | frozenset(pattern for pattern in patterns if pattern not in WILDCARDS),
        )
        assert principal is not None
        principal["may_send_to"] = patterns
        conn.commit()
        return principal
    except BaseException:
        conn.rollback()
        raise
