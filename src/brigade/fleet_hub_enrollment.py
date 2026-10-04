"""Expiring dashboard capabilities, stored only as digests.

Schema creation belongs to hub startup. Redemption holds the SQLite writer
lock before checking expiry, consuming a code and inserting its session.
"""

import hashlib
import ipaddress
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .fleet_client import validate_opaque_label

CODE_TTL = 300
SESSION_TTL = 2592000
BODY_LIMIT = 16384
CREDENTIAL_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
SESSION_ID_RE = re.compile(r"ds_[0-9a-f]{32}\Z")
PUBLIC_COLUMNS = "session_id, label, scope, created_at, expires_at, revoked_at"


class EnrollmentError(ValueError):
    """A generic refusal that never carries a submitted capability."""

    def __init__(self) -> None:
        super().__init__("dashboard request refused")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dashboard_enrollment_codes ("
        "code_digest TEXT PRIMARY KEY, label TEXT, issued_at REAL NOT NULL, expires_at REAL NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dashboard_sessions ("
        "session_id TEXT PRIMARY KEY, credential_digest TEXT NOT NULL UNIQUE, label TEXT, "
        "scope TEXT NOT NULL CHECK(scope = 'read-only'), created_at REAL NOT NULL, "
        "expires_at REAL NOT NULL, revoked_at REAL)"
    )


def digest(credential: str) -> str:
    return hashlib.sha256(credential.encode("ascii")).hexdigest()


def valid_credential(value: Any) -> bool:
    return isinstance(value, str) and CREDENTIAL_RE.fullmatch(value) is not None


def valid_session_id(value: Any) -> bool:
    return isinstance(value, str) and SESSION_ID_RE.fullmatch(value) is not None


def label_value(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        raise EnrollmentError()
    try:
        return validate_opaque_label("label", value) if value.strip() else None
    except ValueError:
        raise EnrollmentError() from None


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def mint(conn: sqlite3.Connection, label: Any = None) -> dict[str, Any]:
    label = label_value(label)
    code = secrets.token_urlsafe(32)
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = time.time()
        conn.execute("DELETE FROM dashboard_enrollment_codes WHERE expires_at <= ?", (now,))
        conn.execute(
            "INSERT INTO dashboard_enrollment_codes VALUES (?, ?, ?, ?)",
            (digest(code), label, now, now + CODE_TTL),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {"code": code, "expires_at": iso(now + CODE_TTL)}


def available(conn: sqlite3.Connection, code: str) -> bool:
    return (
        valid_credential(code)
        and conn.execute(
            "SELECT 1 FROM dashboard_enrollment_codes WHERE code_digest = ? AND expires_at > ?",
            (digest(code), time.time()),
        ).fetchone()
        is not None
    )


def redeem(conn: sqlite3.Connection, code: str) -> str:
    if not valid_credential(code):
        raise EnrollmentError()
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = time.time()
        row = conn.execute(
            "SELECT label FROM dashboard_enrollment_codes WHERE code_digest = ? AND expires_at > ?",
            (digest(code), now),
        ).fetchone()
        if row is None:
            raise EnrollmentError()
        credential = secrets.token_urlsafe(32)
        session_id = "ds_" + secrets.token_hex(16)
        conn.execute(
            "INSERT INTO dashboard_sessions "
            "(session_id, credential_digest, label, scope, created_at, expires_at) "
            "VALUES (?, ?, ?, 'read-only', ?, ?)",
            (session_id, digest(credential), row[0], now, now + SESSION_TTL),
        )
        conn.execute("DELETE FROM dashboard_enrollment_codes WHERE code_digest = ?", (digest(code),))
        conn.commit()
        return credential
    except BaseException:
        conn.rollback()
        raise


def authorized(conn: sqlite3.Connection, credential: str) -> bool:
    return (
        valid_credential(credential)
        and conn.execute(
            "SELECT 1 FROM dashboard_sessions WHERE credential_digest = ? AND expires_at > ? AND revoked_at IS NULL",
            (digest(credential), time.time()),
        ).fetchone()
        is not None
    )


def list_sessions(conn: sqlite3.Connection, *, include_all: bool = False, after: str | None = None) -> dict[str, Any]:
    if after is not None and not valid_session_id(after):
        raise EnrollmentError()
    rows = conn.execute(
        f"SELECT {PUBLIC_COLUMNS} FROM dashboard_sessions WHERE session_id > ? "
        + ("" if include_all else "AND expires_at > ? AND revoked_at IS NULL ")
        + "ORDER BY session_id LIMIT 101",
        (after or "",) if include_all else (after or "", time.time()),
    ).fetchall()
    sessions = []
    for row in rows[:100]:
        session = dict(zip(PUBLIC_COLUMNS.split(", "), row, strict=True))
        for field in ("created_at", "expires_at", "revoked_at"):
            session[field] = iso(session[field]) if session[field] is not None else None
        sessions.append(session)
    return {"sessions": sessions, "next_after": sessions[-1]["session_id"] if len(rows) > 100 else None}


def revoke(conn: sqlite3.Connection, session_id: str) -> bool:
    if not valid_session_id(session_id):
        raise EnrollmentError()
    with conn:
        result = conn.execute(
            "UPDATE dashboard_sessions SET revoked_at = COALESCE(revoked_at, ?) WHERE session_id = ?",
            (time.time(), session_id),
        )
    return result.rowcount == 1


def strict_params(raw: str) -> dict[str, str]:
    if raw == "":
        return {}
    if len(raw) > BODY_LIMIT or re.search(r"%(?![0-9a-fA-F]{2})", raw):
        raise EnrollmentError()
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=4, errors="strict")
    except (ValueError, UnicodeError):
        raise EnrollmentError() from None
    result = dict(pairs)
    if len(result) != len(pairs):
        raise EnrollmentError()
    return result


def code_param(raw: str) -> str:
    params = strict_params(raw)
    if set(params) != {"code"} or not valid_credential(params["code"]):
        raise EnrollmentError()
    return params["code"]


def canonical_origin(scheme: str, authority: str) -> str:
    """Validate a request Host without trusting forwarding host/port headers."""
    if not authority or re.search(r"[\s\x00-\x1f\x7f-\x9f/?#@\\]", authority):
        raise EnrollmentError()
    try:
        parsed = urlsplit(f"{scheme}://{authority}")
        host, port = parsed.hostname, parsed.port
        if not host or (port is not None and not 0 < port <= 65535) or authority.endswith(":"):
            raise EnrollmentError()
        if ":" in host:
            ipaddress.IPv6Address(host)
            host = f"[{host.lower()}]"
        elif not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            raise EnrollmentError()
        suffix = f":{port}" if port is not None and port != (443 if scheme == "https" else 80) else ""
        return f"{scheme}://{host.lower()}{suffix}"
    except ValueError:
        raise EnrollmentError() from None


def serialized_origin(value: str) -> str:
    if re.search(r"[\s\x00-\x1f\x7f-\x9f\\]", value):
        raise EnrollmentError()
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.path
            or parsed.query
            or parsed.fragment
            or "?" in value
            or "#" in value
        ):
            raise EnrollmentError()
        return canonical_origin(parsed.scheme, parsed.netloc)
    except ValueError:
        raise EnrollmentError() from None


def cookie_credential(headers: list[str], name: str) -> str | None:
    if len(headers) != 1:
        return None
    found = []
    for pair in headers[0].split(";"):
        key, sep, value = pair.strip().partition("=")
        if not sep or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
            return None
        if key == name:
            found.append(value)
    return found[0] if len(found) == 1 and valid_credential(found[0]) else None
