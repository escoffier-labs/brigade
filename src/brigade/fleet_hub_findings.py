"""Private findings fingerprints and atomic, fenced report reservations.

The private sidecar key must be backed up with the hub database. It is never
returned. Only a reservation holder receives the domain-separated report HMAC.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

LEASE_SECONDS = 600
TRACKING = frozenset(
    {
        "gclid",
        "fbclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "ref",
        "source",
        "trk",
        "trackingid",
        "refid",
        "gh_src",
        "lever-source",
        "lever-origin",
    }
)


class FindingError(ValueError):
    def __init__(self, message: str, code: str = "finding_invalid", status: int = 400, report_ref: str | None = None):
        super().__init__(message)
        self.code, self.status, self.report_ref = code, status, report_ref


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS fleet_findings (
        finding_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL, first_seen REAL NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('open','reported','resolved')),
        report_ref TEXT, reservation_id TEXT, generation INTEGER NOT NULL DEFAULT 0,
        holder TEXT, expires_at REAL,
        CHECK(generation >= 0))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fleet_finding_grants (
        principal_id TEXT PRIMARY KEY, granted_at REAL NOT NULL, enrollment_epoch TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS fleet_findings_open ON fleet_findings(state,first_seen)")


# Accepted spellings that name the same kind. Only the canonical name is stored.
KIND_ALIASES = {"pr": "pull-request"}


def canonical_key(kind: str, key: str) -> str:
    if kind == "email":
        return key.strip().strip("<>").lower()
    if kind in ("pull-request", "pr"):
        match = re.fullmatch(r"([\w.-]+)/([\w.-]+)#([0-9]+)", key)
        if not match:
            raise FindingError("invalid pull request key")
        return f"{match[1].lower()}/{match[2].lower()}#{int(match[3])}"
    if kind == "job" and "://" in key:
        url = urlsplit(key)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            raise FindingError("invalid job URL")
        host = url.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        if url.port and url.port not in (80, 443):
            host += f":{url.port}"
        query = urlencode(
            sorted(
                (k, v)
                for k, v in parse_qsl(url.query)
                if not k.lower().startswith("utm_") and k.lower() not in TRACKING
            )
        )
        return urlunsplit(("https", host, url.path.rstrip("/"), query, ""))
    return key


def _key(conn: sqlite3.Connection) -> bytes:
    """Load or create a private key under the database's writer lock."""
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    if not db:
        raise FindingError("findings require a persistent database", "finding_unavailable", 503)
    path = Path(db + ".findings-key")
    if not path.exists() and conn.execute("SELECT 1 FROM fleet_findings LIMIT 1").fetchone():
        raise FindingError("findings key missing", "finding_unavailable", 503)
    # O_BINARY keeps Windows from translating newline bytes in the raw key.
    flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | flags, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(secrets.token_bytes(32))
            stream.flush()
            os.fsync(stream.fileno())
    fd = os.open(path, os.O_RDONLY | flags)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or (os.name == "posix" and info.st_mode & 0o077)
            or info.st_nlink != 1
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
        ):
            raise FindingError("unsafe findings key file", "finding_unavailable", 503)
        key = stream.read(33)
    if len(key) != 32:
        raise FindingError("invalid findings key file", "finding_unavailable", 503)
    return hmac.digest(key, b"brigade.findings.v1", "sha256")


def _text(raw: dict[str, Any], name: str, limit: int = 2048) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise FindingError(f"invalid {name}")
    return value.strip()


def owner_for(owners: dict[str, Any], kind: str) -> tuple[str | None, set[str]]:
    matches = [(name, owner) for name, owner in owners.items() if kind in owner.get("match", {}).get("findings", [])]
    # Overlapping charters cannot authorize two reporters. Fail closed until fixed.
    if len(matches) != 1:
        return None, set()
    name, owner = matches[0]
    match = owner.get("match", {})
    principals = {p if p.startswith("agent:") else f"agent:{p}" for p in match.get("principals", [])}
    return name, principals | set(match.get("nodes", []))


def handle(
    conn: sqlite3.Connection,
    raw: Any,
    *,
    caller: str,
    owners: dict[str, Any] | None,
    now: float | None = None,
    admin: bool = False,
    enforce_auth: bool = False,
    credential: str | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise FindingError("request must be an object")
    now = time.time() if now is None else now
    action = raw.get("action")
    if action not in ("observe", "reported", "resolve", "grant"):
        raise FindingError("unknown findings action")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if enforce_auth and not admin:
            from .fleet_hub_message_grants import classify_principal

            if credential is not None:
                from .fleet_hub import lookup_node_token
                from .fleet_hub_messages import lookup_principal_token

                node, revoked = lookup_node_token(conn, credential)
                authenticated = node if node and not revoked else lookup_principal_token(conn, credential)
                if authenticated != caller:
                    raise FindingError("unauthorized", "finding_unauthorized", 401)
            role = classify_principal(conn, caller)
            if role is None:
                raise FindingError("unauthorized", "finding_unauthorized", 401)
            if (
                role != "node"
                and conn.execute(
                    "SELECT 1 FROM fleet_finding_grants g JOIN fleet_message_principals p USING(principal_id) "
                    "WHERE g.principal_id=? AND g.enrollment_epoch=p.authorization_enrollment_epoch",
                    (caller,),
                ).fetchone()
                is None
            ):
                raise FindingError("findings grant required", "finding_forbidden", 403)
        if action == "grant":
            if not admin:
                raise FindingError("admin token required", "finding_forbidden", 403)
            principal = _text(raw, "principal_id", 128)
            if not isinstance(raw.get("enabled"), bool):
                raise FindingError("enabled must be boolean")
            from .fleet_hub_message_grants import classify_principal

            if classify_principal(conn, principal) is None:
                raise FindingError("principal is not active")
            if raw["enabled"]:
                epoch = conn.execute(
                    "SELECT authorization_enrollment_epoch FROM fleet_message_principals WHERE principal_id=?",
                    (principal,),
                ).fetchone()
                if epoch is None:
                    raise FindingError("findings grants target message principals")
                conn.execute("INSERT OR REPLACE INTO fleet_finding_grants VALUES (?,?,?)", (principal, now, epoch[0]))
            else:
                conn.execute("DELETE FROM fleet_finding_grants WHERE principal_id=?", (principal,))
            conn.commit()
            return {"principal_id": principal, "findings": raw["enabled"]}
        if owners is None:
            from .fleet_hub_policy import current_policy

            owners = current_policy(conn)["document"].get("owners", {})
        key = _key(conn)
        reservation = None
        if action == "observe":
            kind = _text(raw, "kind", 64)
            if not re.fullmatch(r"[a-z][a-z0-9-]*", kind):
                raise FindingError("invalid kind")
            # One external event must map to one fingerprint, owner and reservation.
            kind = KIND_ALIASES.get(kind, kind)
            canonical = canonical_key(kind, _text(raw, "key"))
            if not canonical:
                raise FindingError("empty canonical key")
            fingerprint = hmac.new(key, f"{kind}:{canonical}".encode("utf-8"), hashlib.sha256).hexdigest()
            conn.execute(
                "INSERT OR IGNORE INTO fleet_findings(finding_id,fingerprint,kind,first_seen,state) "
                "VALUES (?,?,?,?,'open')",
                (secrets.token_urlsafe(24), fingerprint, kind, now),
            )
            row = conn.execute("SELECT * FROM fleet_findings WHERE fingerprint=?", (fingerprint,)).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM fleet_findings WHERE finding_id=?", (_text(raw, "finding_id", 128),)
            ).fetchone()
        if row is None:
            raise FindingError("finding not found", "finding_not_found", 404)
        finding_id, _, kind, first_seen, state, report_ref, rid, generation, holder, expiry = row
        owner, eligible = owner_for(owners, kind)
        if action == "observe" and state == "open" and caller in eligible and (expiry is None or expiry <= now):
            rid, generation, holder, expiry = secrets.token_urlsafe(24), generation + 1, caller, now + LEASE_SECONDS
            conn.execute(
                "UPDATE fleet_findings SET reservation_id=?,generation=?,holder=?,expires_at=? WHERE finding_id=?",
                (rid, generation, holder, expiry, finding_id),
            )
            reservation = {
                "reservation_id": rid,
                "generation": generation,
                "expires_at": expiry,
                "report_key": hmac.new(key, f"report:{finding_id}".encode("utf-8"), hashlib.sha256).hexdigest(),
            }
        elif action == "reported":
            if report_ref is not None:
                raise FindingError("finding already reported", "finding_already_reported", 409, report_ref)
            supplied = _text(raw, "reservation_id", 128)
            if (
                not rid
                or not hmac.compare_digest(supplied.encode("utf-8"), rid.encode("utf-8"))
                or type(raw.get("generation")) is not int
                or raw["generation"] != generation
                or holder != caller
                or state != "open"
            ):
                raise FindingError("reservation expired or replaced", "finding_reservation_expired", 409)
            # The last holder can finish after expiry until a new reservation fences it.
            report_ref = _text(raw, "report_ref")
            state = "reported"
            conn.execute(
                "UPDATE fleet_findings SET report_ref=?,state='reported' WHERE finding_id=?", (report_ref, finding_id)
            )
        elif action == "resolve":
            if not admin and caller not in eligible and caller != holder:
                raise FindingError("finding owner required", "finding_forbidden", 403)
            state = "resolved"
            conn.execute("UPDATE fleet_findings SET state='resolved' WHERE finding_id=?", (finding_id,))
        conn.commit()
        return {
            "finding_id": finding_id,
            "first_seen": first_seen,
            "owner": owner,
            "state": state,
            "report_ref": report_ref,
            "reservation": reservation,
            "reservation_expires_at": expiry if state == "open" and expiry and expiry > now else None,
        }
    except BaseException:
        conn.rollback()
        raise
