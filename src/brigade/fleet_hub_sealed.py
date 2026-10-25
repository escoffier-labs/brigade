"""Opaque sealed-message storage policy. No cryptographic operations on the Hub."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .fleet_hub_messages import MessageConflict, MessageError, MessageForbidden, MessageQuotaError
from .fleet_hub_message_keys import decode_binary, timestamp


_RH = frozenset(
    {
        "v",
        "suite",
        "hub",
        "from",
        "from_kid",
        "to",
        "kind",
        "priority",
        "client_id",
        "created_at",
        "deadline",
        "in_reply_to",
        "pad",
        "rcpt",
    }
)
_REQUIRED = _RH - {"deadline", "in_reply_to"}
_DIGESTS = {"ct_sha256", "wraps_sha256", "rc"}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _require(valid: object) -> None:
    if not valid:
        raise MessageError("invalid sealed envelope", code="envelope_malformed")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        _require(key not in value)
        value[key] = item
    return value


def _principal(value: Any) -> None:
    _require(
        isinstance(value, str)
        and re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9._-]{0,127}|agent:[a-z][a-z0-9._-]{0,62})", value)
    )


def _hub(value: Any) -> None:
    _require(isinstance(value, str))
    if value != "mesh":
        _require(value.startswith("h_"))
        decode_binary(value[2:], 16)


def _kid(value: Any) -> None:
    _require(isinstance(value, str) and value.startswith("k1_"))
    decode_binary(value[3:], 16)


def _parse(encoded: bytes) -> dict[str, Any]:
    # No crypto-core import: the Hub checks only the bm1 wire format. Bound
    # nesting and reject duplicate keys before building an untrusted JSON tree.
    _require(encoded.isascii() and b"\x00" not in encoded)
    depth, quoted, escaped = 0, False, False
    for byte in encoded:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            _require(depth <= 4)
        elif byte in (93, 125):
            depth -= 1
            _require(depth >= 0)
    _require(depth == 0 and not quoted)
    value = json.loads(encoded.decode("ascii"), object_pairs_hook=_unique)
    _require(isinstance(value, dict) and _REQUIRED <= value.keys() <= _RH | _DIGESTS | {"sig", "ct", "recipients"})
    _require(type(value["v"]) is int and value["v"] == 1 and value["suite"] == "bm1")
    _require(_DIGESTS | {"sig", "ct", "recipients"} <= value.keys())
    _hub(value["hub"])
    _principal(value["from"])
    _principal(value["to"])
    _kid(value["from_kid"])
    _require(isinstance(value["kind"], str) and value["kind"] in {"message", "question", "completion"})
    _require(isinstance(value["priority"], str) and value["priority"] in {"low", "normal", "high", "urgent"})
    _require(isinstance(value["client_id"], str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value["client_id"]))
    timestamp(value["created_at"])
    _require((value["kind"] == "question") == ("deadline" in value))
    if "deadline" in value:
        timestamp(value["deadline"])
    if "in_reply_to" in value:
        link = value["in_reply_to"]
        _require(isinstance(link, dict) and set(link) == {"hub", "from", "client_id"})
        _hub(link["hub"])
        _principal(link["from"])
        _require(isinstance(link["client_id"], str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", link["client_id"]))
    pad = value["pad"]
    _require(type(pad) is int and 1024 <= pad <= 65 * 1024 and pad % 1024 == 0)
    rcpt = value["rcpt"]
    _require(isinstance(rcpt, list) and 1 <= len(rcpt) <= 2)
    for kid in rcpt:
        _kid(kid)
    _require(rcpt == sorted(set(rcpt)) and value["from_kid"] in rcpt)
    for name in _DIGESTS:
        decode_binary(value[name], 32)
    decode_binary(value["sig"], 64)
    decode_binary(value["ct"], pad + 16)
    recipients = value["recipients"]
    _require(isinstance(recipients, list) and len(recipients) == len(rcpt))
    for kid, entry in zip(rcpt, recipients, strict=True):
        _require(isinstance(entry, dict) and set(entry) == {"kid", "wrap"} and entry["kid"] == kid)
        decode_binary(entry["wrap"], 1168)
    _require(encoded == canonical(value))
    return value


@dataclass(frozen=True)
class StoragePolicy:
    require_sealed: bool = False
    max_envelope_bytes: int = 96 * 1024
    sender_storage_bytes: int = 1024**3
    fleet_storage_bytes: int | None = None
    retention_days: int = 90

    def __post_init__(self) -> None:
        if type(self.retention_days) is not int or not 1 <= self.retention_days <= 3650:
            raise ValueError("message routing retention_days must be an integer from 1 to 3650")
        for value in (self.max_envelope_bytes, self.sender_storage_bytes, self.fleet_storage_bytes):
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError("sealed storage limits must be positive integers")


def policy_from_environment(environ: Mapping[str, str]) -> StoragePolicy:
    """Read startup-only storage policy; never rewrite operator-signed identity."""
    require_sealed = environ.get("BRIGADE_FLEET_MESSAGES_REQUIRE_SEALED", "")
    if require_sealed not in {"", "0", "1"}:
        raise ValueError("BRIGADE_FLEET_MESSAGES_REQUIRE_SEALED must be unset, empty, 0, or 1")
    values: dict[str, Any] = {"require_sealed": require_sealed == "1"}
    for field in ("max_envelope_bytes", "sender_storage_bytes", "fleet_storage_bytes", "retention_days"):
        name = "BRIGADE_FLEET_MESSAGES_" + field.upper()
        if name in environ:
            try:
                text = environ[name]
                if re.fullmatch(r"[1-9][0-9]*", text) is None:
                    raise ValueError
                values[field] = int(text)
            except ValueError:
                raise ValueError(f"{name} must be a positive integer") from None
    if "retention_days" in values and values["retention_days"] > 3650:
        raise ValueError("BRIGADE_FLEET_MESSAGES_RETENTION_DAYS must be an integer from 1 to 3650")
    return StoragePolicy(**values)


def prepare(raw: dict[str, Any], *, sender: str, reply_to: int | None, policy: StoragePolicy) -> dict[str, Any]:
    allowed = {"action", "envelope", "id"} if raw.get("action") == "reply" else {"action", "envelope", "reply_to"}
    if set(raw) - allowed:
        raise MessageError("an envelope cannot accompany plaintext or routing fields")
    text = raw["envelope"]
    if not isinstance(text, str):
        raise MessageError("envelope must be canonical ASCII JSON text", code="envelope_malformed")
    try:
        encoded = text.encode("ascii")
    except UnicodeError:
        raise MessageError("envelope must be canonical ASCII JSON text", code="envelope_malformed") from None
    if len(encoded) > min(policy.max_envelope_bytes, 96 * 1024):
        raise MessageError("sealed envelope exceeds storage limit", code="message_too_large")
    try:
        value = _parse(encoded)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise MessageError("invalid sealed envelope", code="envelope_malformed") from None
    if "ct" not in value:
        raise MessageError("a new send requires complete sealed content", code="envelope_malformed")
    if value["from"] != sender:
        raise MessageForbidden("envelope sender must match caller", code="message_sender_mismatch")
    if (reply_to is not None) != ("in_reply_to" in value):
        raise MessageError("reply_to and signed in_reply_to must both be present or absent")
    return value


def require_current_keys(conn: sqlite3.Connection, value: dict[str, Any]) -> None:
    def active(principal: str) -> str | None:
        row = conn.execute(
            "SELECT kid FROM fleet_message_keys WHERE principal_id=? AND state='active'", (principal,)
        ).fetchone()
        return str(row[0]) if row else None

    sender_kid = active(value["from"])
    if sender_kid is None or value["from_kid"] != sender_kid:
        raise MessageConflict("sender key is no longer active", code="sender_key_stale")
    recipient_kid = active(value["to"])
    expected = sorted({sender_kid, recipient_kid}) if recipient_kid is not None else []
    if (value["from"] != value["to"] and sender_kid == recipient_kid) or value["rcpt"] != expected:
        raise MessageConflict("recipient key is no longer active", code="recipient_key_stale")


def admit(conn: sqlite3.Connection, sender: str, size: int, policy: StoragePolicy) -> None:
    # Caller holds BEGIN IMMEDIATE from duplicate lookup through insertion.
    used = conn.execute(
        "SELECT COALESCE(SUM(envelope_bytes),0) FROM fleet_messages WHERE sender=?", (sender,)
    ).fetchone()[0]
    if used + size > policy.sender_storage_bytes:
        raise MessageQuotaError("sender sealed storage quota exceeded", code="sender_storage_quota")
    if policy.fleet_storage_bytes is not None:
        total = conn.execute("SELECT COALESCE(SUM(envelope_bytes),0) FROM fleet_messages").fetchone()[0]
        if total + size > policy.fleet_storage_bytes:
            raise MessageQuotaError("hub sealed storage is full", code="hub_storage_full")


PURGE_BATCH_SIZE = 32


def purge_content(conn: sqlite3.Connection) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows = conn.execute(
        "SELECT id,envelope FROM fleet_messages WHERE content_state='sealed' AND acked_at<=? "
        "ORDER BY acked_at,id LIMIT ?",
        (cutoff, PURGE_BATCH_SIZE),
    ).fetchall()
    for message_id, text in rows:
        value = json.loads(text)
        value.pop("ct")
        value.pop("recipients")
        tombstone = canonical(value).decode("ascii")
        conn.execute(
            "UPDATE fleet_messages SET envelope=?,envelope_bytes=?,content_state='content_expired' WHERE id=?",
            (tombstone, len(tombstone), message_id),
        )

    return len(rows)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("ascii")).hexdigest()
