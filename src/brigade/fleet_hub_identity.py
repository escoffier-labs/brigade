"""Opaque admin-supplied mesh statements and public Hub identity storage."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .fleet_hub_message_keys import decode_binary, timestamp
from .fleet_hub_messages import MessageConflict, MessageError

if TYPE_CHECKING:
    from .fleet_hub_sealed import StoragePolicy

# Same upper bound as the existing Hub JSON request body.
MAX_STATEMENT_BYTES = 8 * 1024 * 1024
KINDS = frozenset({"membership", "manifest", "revocation"})
SCHEMA = (
    ("table", "hub_meta", "CREATE TABLE IF NOT EXISTS hub_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"),
    (
        "table",
        "fleet_mesh_statements",
        "CREATE TABLE IF NOT EXISTS fleet_mesh_statements ("
        "kind TEXT PRIMARY KEY, seq INTEGER NOT NULL, statement TEXT NOT NULL, uploaded_at TEXT NOT NULL)",
    ),
)


def missing_schema(conn: sqlite3.Connection) -> list[str]:
    return [
        ddl
        for kind, name, ddl in SCHEMA
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type=? AND name=?", (kind, name)).fetchone() is None
    ]


def ensure_schema(conn: sqlite3.Connection) -> None:
    for ddl in missing_schema(conn):
        conn.execute(ddl)


def _blob(value: Any) -> str:
    try:
        text = json.dumps(value, allow_nan=False)
        if not isinstance(value, dict) or len(text.encode()) > MAX_STATEMENT_BYTES:
            raise ValueError
        return text
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise MessageError("mesh statement must be a bounded JSON object") from None


def identity(conn: sqlite3.Connection) -> dict[str, Any] | None:
    rows = dict(conn.execute("SELECT key,value FROM hub_meta WHERE key IN ('identity','migration')"))
    if "identity" not in rows:
        return None
    value = json.loads(rows["identity"])
    return {"identity": value, "migration": json.loads(rows["migration"])} if "migration" in rows else value


def validate_storage_policy(raw: Any, policy: StoragePolicy | None = None) -> None:
    """Check advertised policy without changing or verifying signed bytes.

    Older opaque statements may omit these fields. Supplied fields must be
    exact, bounded types and agree with the frozen runtime enforcement.
    """
    if raw is None:
        return
    value = raw.get("identity", raw)
    for field in ("require_sealed", "max_envelope_bytes"):
        if field not in value:
            continue
        supplied = value[field]
        valid = (
            type(supplied) is bool
            if field == "require_sealed"
            else type(supplied) is int and 1 <= supplied <= 96 * 1024
        )
        if not valid:
            raise MessageError(f"invalid identity {field}")
        enforced = (
            None
            if policy is None
            else (policy.require_sealed if field == "require_sealed" else min(policy.max_envelope_bytes, 96 * 1024))
        )
        if policy is not None and supplied != enforced:
            raise MessageError(
                f"identity {field} disagrees with runtime storage policy; "
                "use compatible runtime configuration or upload a new admin-signed identity"
            )


def _canonical(value: Any) -> bytes:
    _blob(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _validate_identity(value: Any) -> None:
    _blob(value)
    validate_storage_policy(value)
    public = decode_binary(value.get("hub_sig_pub"), 32)
    expected = (
        "h_"
        + base64.urlsafe_b64encode(hashlib.sha256(b"brigade-mesh/v1/hub-id\0" + public).digest()[:16])
        .rstrip(b"=")
        .decode()
    )
    if value.get("hub_id") != expected:
        raise MessageError("hub id does not match public key")
    if value.get("backend") not in ("full", "hub-lite"):
        raise MessageError("invalid hub backend")
    capabilities = value.get("capabilities")
    allowed = {"messages", "invites", "presence", "questions", "claims", "events", "runs"}
    if (
        not isinstance(capabilities, list)
        or any(not isinstance(c, str) or c not in allowed for c in capabilities)
        or capabilities != sorted(set(capabilities))
    ):
        raise MessageError("invalid hub capabilities")
    if type(value.get("seq")) is not int or not 0 <= value["seq"] <= 2**63 - 1:
        raise MessageError("invalid identity sequence")


def _mark(value: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    signer = hashlib.sha256(decode_binary(value["hub_sig_pub"], 32)).hexdigest()
    return signer, {"seq": value["seq"], "digest": hashlib.sha256(_canonical(value)).hexdigest()}


def _installed(conn: sqlite3.Connection) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Validate retained metadata, bootstrapping only the installed legacy signer."""
    try:
        rows = dict(
            conn.execute("SELECT key,value FROM hub_meta WHERE key IN ('identity','identity_marks','migration')")
        )
        if "identity" not in rows:
            if rows:
                raise ValueError
            return None, {}
        value = json.loads(rows["identity"])
        _validate_identity(value)
        signer, mark = _mark(value)
        if "identity_marks" not in rows:
            return value, {signer: mark}
        if len(rows["identity_marks"].encode()) > 256 * 1024:
            raise ValueError
        history = json.loads(rows["identity_marks"])
        if (
            not isinstance(history, dict)
            or set(history) != {"v", "signers"}
            or type(history["v"]) is not int
            or history["v"] != 1
        ):
            raise ValueError
        marks = history["signers"]
        if not isinstance(marks, dict) or not 1 <= len(marks) <= 1024:
            raise ValueError
        for key, item in marks.items():
            if not re.fullmatch(r"[0-9a-f]{64}", key) or not isinstance(item, dict) or set(item) != {"seq", "digest"}:
                raise ValueError
            if (
                type(item["seq"]) is not int
                or not 0 <= item["seq"] <= 2**63 - 1
                or not isinstance(item["digest"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["digest"])
            ):
                raise ValueError
        if marks.get(signer) != mark:
            raise ValueError
        return value, marks
    except (MessageError, ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        raise MessageError("installed Hub identity metadata is invalid", code="hub_identity_invalid") from None


def require_current_hub(conn: sqlite3.Connection, hub: str) -> bool:
    """Fence new writes and report configured state under the caller's writer lock."""
    value, _ = _installed(conn)
    if value is not None and value["hub_id"] != hub:
        raise MessageConflict("Hub identity changed", code="hub_identity_changed")
    return value is not None


def _origin_require(condition: Any, code: str) -> None:
    if not condition:
        raise ValueError(code)


def _origin(value: str) -> str:
    """Strict D4 HTTP(S) origin normalization, without network or acceptance."""
    code = "hub_migration_invalid"
    _origin_require(isinstance(value, str) and value.isascii() and len(value) <= 4096, code)
    _origin_require(not any(ord(c) <= 32 or ord(c) == 127 or c in "%\\?#@" for c in value), code)
    match = re.fullmatch(r"(https?)://([^/]+)(?:/)?", value, re.IGNORECASE | re.ASCII)
    _origin_require(match is not None, code)
    assert match is not None
    scheme, authority = match.groups()
    scheme = scheme.lower()
    port = None
    if authority.startswith("["):
        match = re.fullmatch(r"\[([^\[\]]+)\](?::([0-9]+))?", authority, re.ASCII)
        _origin_require(match is not None, code)
        assert match is not None
        host, port = match.groups()
        try:
            host = "[" + ipaddress.IPv6Address(host).compressed + "]"
        except ValueError:
            raise ValueError(code) from None
    else:
        match = re.fullmatch(r"([^:\[\]]+)(?::([0-9]+))?", authority, re.ASCII)
        _origin_require(match is not None, code)
        assert match is not None
        host, port = match.groups()
        _origin_require(len(host) <= 253, code)
        # WHATWG treats a final decimal/hex label, including bare 0x, as an
        # IPv4 candidate. Require strict dotted decimal for that entire host
        # so clients cannot disagree about whether it names an address or DNS.
        final_label = host.rsplit(".", 1)[-1]
        if re.fullmatch(r"[0-9]+|0[xX][0-9a-fA-F]*", final_label, re.ASCII):
            try:
                host = str(ipaddress.IPv4Address(host))
            except ValueError:
                raise ValueError(code) from None
        else:
            _origin_require(
                all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in host.split(".")),
                code,
            )
            host = host.lower()
    suffix = ""
    if port is not None:
        _origin_require(len(port) <= 5 and not port.startswith("0") and 1 <= int(port) <= 65535, code)
        if (scheme, port) not in {("https", "443"), ("http", "80")}:
            suffix = ":" + port
    origin = scheme + "://" + host + suffix
    _origin_require(len(origin) <= 2048, code)
    return origin


def _migration(raw: Any, value: dict[str, Any], old: dict[str, Any] | None = None) -> str:
    try:
        blob = _canonical(raw)
        fields = {
            "v",
            "old_hub_id",
            "new_hub_id",
            "new_hub_sig_pub",
            "new_identity_seq",
            "new_identity_sha256",
            "endpoint",
            "created_at",
            "seq",
            "sig",
        }
        if len(blob) > 16 * 1024 or set(raw) != fields or type(raw["v"]) is not int or raw["v"] != 1:
            raise ValueError
        for field in ("seq", "new_identity_seq"):
            if type(raw[field]) is not int or not 0 <= raw[field] <= 2**53 - 1:
                raise ValueError
        old_id = raw["old_hub_id"]
        if not isinstance(old_id, str) or not old_id.startswith("h_"):
            raise ValueError
        decode_binary(old_id[2:], 16)
        decode_binary(raw["new_hub_sig_pub"], 32)
        decode_binary(raw["new_identity_sha256"], 32)
        decode_binary(raw["sig"], 64)
        timestamp(raw["created_at"])
        if (
            old_id == value["hub_id"]
            or raw["new_hub_id"] != value["hub_id"]
            or raw["new_hub_sig_pub"] != value["hub_sig_pub"]
            or raw["new_identity_seq"] != value["seq"]
            or decode_binary(raw["new_identity_sha256"], 32) != hashlib.sha256(_canonical(value)).digest()
            or raw["endpoint"] != value["endpoint"]
            or _origin(raw["endpoint"]) != raw["endpoint"]
        ):
            raise ValueError
        if old is not None and old["hub_id"] != value["hub_id"] and old_id != old["hub_id"]:
            raise ValueError
        return blob.decode("ascii")
    except (ValueError, TypeError, KeyError, MessageError, RecursionError, UnicodeError):
        raise MessageError("Hub migration does not match identity", code="hub_migration_invalid") from None


def upload_identity(
    conn: sqlite3.Connection, raw: Any, *, storage_policy: StoragePolicy | None = None
) -> dict[str, Any]:
    _blob(raw)
    value = raw.get("identity", raw)
    _validate_identity(value)
    validate_storage_policy(value, storage_policy)
    supplied = "identity" in raw and "migration" in raw
    proof = _migration(raw["migration"], value) if supplied else None
    opened = not conn.in_transaction
    if opened:
        conn.execute("BEGIN IMMEDIATE")
    conn.execute("SAVEPOINT hub_identity_upload")
    try:
        # A savepoint alone does not upgrade a caller's deferred read transaction.
        conn.execute("UPDATE hub_meta SET value=value WHERE key='identity'")
        current, marks = _installed(conn)
        signer, mark = _mark(value)
        previous = marks.get(signer)
        if previous is not None:
            if mark["seq"] < previous["seq"]:
                raise MessageConflict("identity sequence decreased", code="signed_statement_rollback")
            if mark["seq"] == previous["seq"] and mark != previous:
                raise MessageConflict("identity sequence has different bytes", code="signed_statement_conflict")
        # A historical exact replay cannot reactivate an old signer.
        historical = previous == mark and current is not None and current["hub_id"] != value["hub_id"]
        if supplied:
            proof = _migration(raw["migration"], value, None if historical else current)
        if not historical:
            if signer not in marks and len(marks) >= 1024:
                raise MessageError("Hub identity history is full", code="hub_identity_history_full")
            if not supplied:
                stored = conn.execute("SELECT value FROM hub_meta WHERE key='migration'").fetchone()
                if stored:
                    try:
                        proof = _migration(json.loads(stored[0]), value)
                    except (ValueError, MessageError, RecursionError):
                        proof = None
            marks[signer] = mark
            conn.execute(
                "INSERT INTO hub_meta VALUES('identity',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_blob(value),),
            )
            conn.execute("DELETE FROM hub_meta WHERE key='migration'")
            if proof is not None:
                conn.execute("INSERT INTO hub_meta VALUES('migration',?)", (proof,))
        conn.execute(
            "INSERT INTO hub_meta VALUES('identity_marks',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps({"v": 1, "signers": marks}),),
        )
        result = identity(conn)
        assert result is not None
        conn.execute("RELEASE hub_identity_upload")
        if opened:
            conn.commit()
        return result
    except BaseException:
        if opened:
            conn.rollback()
        else:
            conn.execute("ROLLBACK TO hub_identity_upload")
            conn.execute("RELEASE hub_identity_upload")
        raise


def upload_statement(conn: sqlite3.Connection, raw: Any) -> dict[str, Any]:
    if (
        not isinstance(raw, dict)
        or set(raw) != {"kind", "statement"}
        or not isinstance(raw["kind"], str)
        or raw["kind"] not in KINDS
    ):
        raise MessageError("invalid mesh statement kind")
    blob = _blob(raw["statement"])
    seq = raw["statement"].get("seq")
    if type(seq) is not int or not 0 <= seq <= 2**63 - 1:
        raise MessageError("invalid statement sequence")
    with conn:
        changed = conn.execute(
            "INSERT INTO fleet_mesh_statements VALUES(?,?,?,?) ON CONFLICT(kind) DO UPDATE SET "
            "seq=excluded.seq,statement=excluded.statement,uploaded_at=excluded.uploaded_at WHERE excluded.seq>fleet_mesh_statements.seq",
            (raw["kind"], seq, blob, datetime.now(timezone.utc).isoformat()),
        )
        if changed.rowcount != 1:
            raise MessageConflict("statement sequence must increase", code="signed_statement_rollback")
        # Match the HTTP serializer, including escaping and the response wrapper.
        # The existing statement cap also equals the message client's response cap.
        response = manifests(conn, {"action": "manifests_get"})
        if len(json.dumps(response).encode("utf-8")) > MAX_STATEMENT_BYTES:
            raise MessageError("mesh statements exceed the response size limit")
    return {"kind": raw["kind"], "seq": seq}


def manifests(conn: sqlite3.Connection, raw: dict[str, Any]) -> dict[str, Any]:
    if set(raw) != {"action"}:
        raise MessageError("manifests_get takes no arguments")
    return {
        "statements": {
            kind: json.loads(blob)
            for kind, blob in conn.execute("SELECT kind,statement FROM fleet_mesh_statements ORDER BY kind")
        }
    }
