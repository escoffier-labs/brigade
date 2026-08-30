"""Fleet Hub persistence for the versioned model roster (schema v15)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from . import fleet_hub, fleet_model_roster
from .fleet_hub import FleetHubConflict, FleetHubError, FleetHubForbidden

_ROSTER_COLUMNS = (
    "reasoning TEXT NOT NULL DEFAULT 'none'",
    "brigade_cli TEXT NOT NULL DEFAULT ''",
    "t3_instance_id TEXT NOT NULL DEFAULT ''",
    "t3_service_tier TEXT NOT NULL DEFAULT ''",
)
_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_roster_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
"""
_DEFAULTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_consumer_defaults (
    consumer TEXT NOT NULL PRIMARY KEY,
    seat TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
_RETIRED_SCHEMA = """
CREATE TABLE IF NOT EXISTS retired_models (
    provider TEXT NOT NULL,
    family TEXT NOT NULL,
    match_kind TEXT NOT NULL,
    permanent INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (provider, family)
);
"""
_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_admission_audit (
    node_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    consumer TEXT NOT NULL,
    source TEXT NOT NULL,
    roster_revision INTEGER NOT NULL,
    roster_digest TEXT NOT NULL,
    seat TEXT,
    provider TEXT,
    model TEXT,
    reasoning TEXT,
    consumer_binding TEXT,
    request_digest TEXT NOT NULL,
    decision TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (node_id, request_id, phase)
);
"""
_SET_FIELDS = frozenset(
    {
        "action",
        "expected_revision",
        "provider",
        "model",
        "seat",
        "enabled",
        "limit",
        "notes",
        "reasoning",
        "brigade_cli",
        "t3_instance_id",
        "t3_service_tier",
    }
)
_DEFAULT_FIELDS = frozenset({"action", "expected_revision", "consumer", "seat"})
_RETIRE_FIELDS = frozenset(
    {
        "action",
        "expected_revision",
        "provider",
        "family",
        "permanent",
        "reason_code",
        "match_kind",
    }
)
_ADMIT_FIELDS = frozenset(
    {
        "action",
        "schema",
        "consumer",
        "seat",
        "request_id",
        "phase",
        "expect_revision",
        "expect_digest",
    }
)
ROSTER_MUTATIONS = frozenset({"set", "set-default", "retire"})


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def utc_now() -> datetime:
    """Response-time clock. Tests may monkeypatch this."""
    return datetime.now(timezone.utc)


def _utc_now() -> str:
    return fleet_hub._utc_now()


def _as_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_z(value: datetime | str) -> str:
    return _as_utc(value).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _optional_binding(raw: Any, field: str) -> str:
    if raw is None:
        return ""
    return fleet_hub._model_policy_name(raw, field) if raw != "" else ""


def _expected_revision(raw: Any) -> int:
    value = raw.get("expected_revision") if isinstance(raw, dict) else None
    if type(value) is not int:
        raise FleetHubError("model policy field 'expected_revision' must be an integer")
    return value


def ensure_schema(conn: sqlite3.Connection) -> None:
    """v14 -> v15: versioned roster tables and exact seat bindings."""
    names = _columns(conn, "model_policy")
    if names:
        for column in _ROSTER_COLUMNS:
            name = column.split()[0]
            if name not in names:
                conn.execute(f"ALTER TABLE model_policy ADD COLUMN {column}")
    conn.execute(_META_SCHEMA)
    conn.execute(_DEFAULTS_SCHEMA)
    conn.execute(_RETIRED_SCHEMA)
    conn.execute(_AUDIT_SCHEMA)
    now = _utc_now()
    if conn.execute("SELECT 1 FROM model_roster_meta WHERE singleton=1").fetchone() is None:
        conn.execute(
            "INSERT INTO model_roster_meta (singleton, revision, updated_at, updated_by) VALUES (1, 1, ?, ?)",
            (now, "schema-v15"),
        )
    for provider, family in fleet_model_roster.PERMANENT_RETIRED_FAMILIES:
        conn.execute(
            "INSERT OR IGNORE INTO retired_models "
            "(provider, family, match_kind, permanent, reason_code, created_at) "
            "VALUES (?, ?, 'family-prefix', 1, ?, ?)",
            (provider, family, fleet_model_roster.PERMANENT_REASON, now),
        )
    for consumer in sorted(fleet_model_roster.CONSUMERS):
        conn.execute(
            "INSERT OR IGNORE INTO model_consumer_defaults (consumer, seat, updated_at) VALUES (?, '', ?)",
            (consumer, now),
        )


def _revision(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT revision FROM model_roster_meta WHERE singleton=1").fetchone()
    if row is None:
        raise FleetHubError("model roster revision metadata is missing")
    return int(row[0])


def _retired_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT provider, family, match_kind, permanent, reason_code FROM retired_models ORDER BY provider, family"
    ).fetchall()
    return [
        {
            "provider": row[0],
            "family": row[1],
            "match_kind": row[2],
            "permanent": bool(row[3]),
            "reason_code": row[4],
        }
        for row in rows
    ]


def _consumer_defaults(conn: sqlite3.Connection) -> dict[str, str | None]:
    rows = conn.execute("SELECT consumer, seat FROM model_consumer_defaults ORDER BY consumer").fetchall()
    return {str(row[0]): (str(row[1]) if row[1] else None) for row in rows}


def _seats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT seat, provider, model, reasoning, enabled, limit_count, "
        "brigade_cli, t3_instance_id, t3_service_tier FROM model_policy ORDER BY seat"
    ).fetchall()
    return [
        {
            "seat": row[0],
            "enabled": bool(row[4]),
            "provider": row[1],
            "model": row[2],
            "reasoning": row[3],
            "limit": row[5],
            "bindings": {
                "brigade": {"cli": row[6] or ""},
                "t3_fleet": {"instance_id": row[7] or "", "service_tier": row[8] or None},
            },
        }
        for row in rows
    ]


def project_roster(
    conn: sqlite3.Connection,
    *,
    audience_node_id: str | None = None,
    raw_node_bearer: str | None = None,
) -> dict[str, Any]:
    """Versioned roster plus the legacy ``models`` list. Admin reads omit MAC."""
    meta = conn.execute("SELECT revision, updated_at FROM model_roster_meta WHERE singleton=1").fetchone()
    if meta is None:
        raise FleetHubError("model roster revision metadata is missing")
    issued = utc_now()
    payload: dict[str, Any] = {
        "schema": fleet_model_roster.ROSTER_SCHEMA,
        "revision": int(meta[0]),
        "revision_updated_at": _iso_z(str(meta[1])),
        "issued_at": _iso_z(issued),
        "expires_at": _iso_z(issued + timedelta(seconds=fleet_model_roster.LKG_TTL_SECONDS)),
        "seats": _seats(conn),
        "consumer_defaults": _consumer_defaults(conn),
        "retired_models": _retired_rows(conn),
    }
    if audience_node_id:
        payload["audience_node_id"] = audience_node_id
    payload["document_sha256"] = fleet_model_roster.roster_digest(payload)
    payload["models"] = fleet_hub.list_model_policy(conn)
    if audience_node_id and raw_node_bearer:
        payload["mac"] = {
            "algorithm": fleet_model_roster.MAC_ALGORITHM,
            "value": fleet_model_roster.roster_mac(raw_node_bearer, payload),
        }
    return payload


def _mutate(
    conn: sqlite3.Connection,
    raw: Any,
    writer: Callable[[sqlite3.Connection], dict[str, Any]],
    *,
    updated_by: str = "admin",
) -> tuple[int, dict[str, Any]]:
    expected = _expected_revision(raw)
    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    try:
        current = _revision(conn)
        if current != expected:
            if opened:
                conn.rollback()
            return 409, {"error": "roster_revision_conflict"}
        payload = writer(conn)
        if payload.get("error"):
            if opened:
                conn.rollback()
            return 409, payload
        now = _utc_now()
        revision = current + 1
        conn.execute(
            "UPDATE model_roster_meta SET revision=?, updated_at=?, updated_by=? WHERE singleton=1",
            (revision, now, updated_by),
        )
        if opened:
            conn.commit()
        payload["revision"] = revision
        return 200, payload
    except BaseException:
        if opened:
            conn.rollback()
        raise


def _validate_set(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise FleetHubError("model policy request must be a JSON object")
    unknown = set(raw).difference(_SET_FIELDS)
    if unknown:
        raise FleetHubError(f"unknown model policy field(s): {', '.join(sorted(unknown))}")
    enabled = raw.get("enabled")
    if type(enabled) is not bool:
        raise FleetHubError("model policy field 'enabled' must be a boolean")
    limit = raw.get("limit")
    if limit is not None and (type(limit) is not int or not 0 <= limit <= 64):
        raise FleetHubError("model policy field 'limit' must be an integer in 0..64")
    if "reasoning" not in raw or raw.get("reasoning") is None:
        raise FleetHubError("model policy field 'reasoning' is required")
    reasoning = raw.get("reasoning")
    return {
        "seat": fleet_hub._model_policy_name(raw.get("seat"), "seat"),
        "provider": fleet_hub._cloud_provider(raw.get("provider")),
        "model": fleet_hub._model_policy_name(raw.get("model"), "model"),
        "enabled": enabled,
        "limit": limit,
        "notes": fleet_hub._safe_cloud_text(raw.get("notes"), "notes"),
        "reasoning": fleet_hub._model_policy_name(reasoning, "reasoning"),
        "brigade_cli": _optional_binding(raw.get("brigade_cli"), "brigade_cli"),
        "t3_instance_id": _optional_binding(raw.get("t3_instance_id"), "t3_instance_id"),
        "t3_service_tier": _optional_binding(raw.get("t3_service_tier"), "t3_service_tier"),
    }


def _retired_conflict(conn: sqlite3.Connection, provider: str, model: str) -> dict[str, Any] | None:
    if fleet_model_roster.retired_reason(provider, model, _retired_rows(conn)):
        return {"error": "retired-model"}
    return None


def _write_set(conn: sqlite3.Connection, request: dict[str, Any]) -> dict[str, Any]:
    denied = _retired_conflict(conn, str(request["provider"]), str(request["model"]))
    if denied is not None:
        return denied
    conn.execute(
        "INSERT INTO model_policy "
        "(seat, provider, model, reasoning, enabled, limit_count, brigade_cli, t3_instance_id, "
        "t3_service_tier, notes, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(seat) DO UPDATE SET provider=excluded.provider, model=excluded.model, "
        "reasoning=excluded.reasoning, enabled=excluded.enabled, limit_count=excluded.limit_count, "
        "brigade_cli=excluded.brigade_cli, t3_instance_id=excluded.t3_instance_id, "
        "t3_service_tier=excluded.t3_service_tier, notes=excluded.notes, updated_at=excluded.updated_at",
        (
            request["seat"],
            request["provider"],
            request["model"],
            request["reasoning"],
            int(request["enabled"]),
            request["limit"],
            request["brigade_cli"],
            request["t3_instance_id"],
            request["t3_service_tier"],
            request["notes"],
            _utc_now(),
        ),
    )
    return {
        "updated": True,
        "policy": {
            "seat": request["seat"],
            "provider": request["provider"],
            "model": request["model"],
            "enabled": request["enabled"],
            "limit": request["limit"],
            "notes": request["notes"],
            "reasoning": request["reasoning"],
            "brigade_cli": request["brigade_cli"],
            "t3_instance_id": request["t3_instance_id"],
            "t3_service_tier": request["t3_service_tier"] or None,
        },
    }


def _write_default(conn: sqlite3.Connection, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise FleetHubError("model policy request must be a JSON object")
    unknown = set(raw).difference(_DEFAULT_FIELDS)
    if unknown:
        raise FleetHubError(f"unknown model policy field(s): {', '.join(sorted(unknown))}")
    consumer = fleet_hub._model_policy_name(raw.get("consumer"), "consumer")
    if consumer not in fleet_model_roster.CONSUMERS:
        raise FleetHubError("model policy field 'consumer' must be brigade-run or t3-fleet")
    seat = fleet_hub._model_policy_name(raw.get("seat"), "seat")
    row = conn.execute("SELECT provider, model FROM model_policy WHERE seat=?", (seat,)).fetchone()
    if row is None:
        raise FleetHubError(f"model policy seat {seat!r} is not defined")
    denied = _retired_conflict(conn, str(row[0]), str(row[1]))
    if denied is not None:
        return denied
    conn.execute(
        "INSERT INTO model_consumer_defaults (consumer, seat, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(consumer) DO UPDATE SET seat=excluded.seat, updated_at=excluded.updated_at",
        (consumer, seat, _utc_now()),
    )
    return {"updated": True, "consumer": consumer, "seat": seat}


def _write_retire(conn: sqlite3.Connection, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise FleetHubError("model policy request must be a JSON object")
    unknown = set(raw).difference(_RETIRE_FIELDS)
    if unknown:
        raise FleetHubError(f"unknown model policy field(s): {', '.join(sorted(unknown))}")
    provider = fleet_hub._cloud_provider(raw.get("provider"))
    family = fleet_hub._model_policy_name(raw.get("family"), "family")
    existing = conn.execute(
        "SELECT permanent, match_kind, family FROM retired_models WHERE provider=? AND family=?",
        (provider, family),
    ).fetchone()
    seeded = (provider, family) in fleet_model_roster.PERMANENT_RETIRED_FAMILIES
    if (existing is not None and int(existing[0]) == 1) or seeded:
        return {"error": "permanent-retirement-immutable"}
    permanent = raw.get("permanent", False)
    if type(permanent) is not bool:
        raise FleetHubError("model policy field 'permanent' must be a boolean")
    match_kind = raw.get("match_kind", "family-prefix")
    if match_kind != "family-prefix":
        raise FleetHubError("model policy field 'match_kind' must be family-prefix")
    reason = raw.get("reason_code") or "operator-retired"
    reason_code = fleet_hub._model_policy_name(reason, "reason_code")
    conn.execute(
        "INSERT INTO retired_models (provider, family, match_kind, permanent, reason_code, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(provider, family) DO UPDATE SET match_kind=excluded.match_kind, "
        "permanent=excluded.permanent, reason_code=excluded.reason_code",
        (provider, family, match_kind, int(permanent), reason_code, _utc_now()),
    )
    return {
        "updated": True,
        "retired": {
            "provider": provider,
            "family": family,
            "match_kind": match_kind,
            "permanent": permanent,
            "reason_code": reason_code,
        },
    }


def set_model_policy(conn: sqlite3.Connection, raw: Any) -> dict[str, Any]:
    """Upsert one admin-controlled seat and increment the roster revision once."""
    request = _validate_set(raw)
    status, payload = _mutate(conn, raw, lambda item: _write_set(item, request))
    if status != 200:
        raise FleetHubConflict(str(payload.get("error") or "roster_revision_conflict"))
    return payload["policy"]


def _binding_for(consumer: str, seat: Mapping[str, Any]) -> dict[str, Any] | None:
    bindings = seat.get("bindings")
    if not isinstance(bindings, Mapping):
        return None
    if consumer == "t3-fleet":
        t3_fleet = bindings.get("t3_fleet")
        if not isinstance(t3_fleet, Mapping):
            return None
        instance_id = str(t3_fleet.get("instance_id") or "")
        if not instance_id:
            return None
        return {"instance_id": instance_id, "service_tier": t3_fleet.get("service_tier") or None}
    brigade = bindings.get("brigade")
    if not isinstance(brigade, Mapping):
        return None
    instance_id = str(brigade.get("cli") or "")
    if not instance_id:
        return None
    return {"instance_id": instance_id, "service_tier": None}


def _request_digest(raw: dict[str, Any]) -> str:
    body = {
        "consumer": raw.get("consumer"),
        "expect_digest": raw.get("expect_digest"),
        "expect_revision": raw.get("expect_revision"),
        "phase": raw.get("phase"),
        "seat": raw.get("seat"),
    }
    return hashlib.sha256(fleet_model_roster.canonical_json(body).encode("ascii")).hexdigest()


def _binding_value(raw: Any) -> dict[str, Any] | None:
    if raw is None or raw == "":
        return None
    return json.loads(str(raw))


def _admission_payload(row: tuple[Any, ...]) -> dict[str, Any]:
    decision = str(row[13])
    payload: dict[str, Any] = {
        "schema": fleet_model_roster.ADMISSION_SCHEMA,
        "state": "authoritative" if decision == "admitted" else "denied",
        "source": row[4],
        "roster_revision": row[5],
        "roster_digest": row[6],
        "seat": row[7],
        "provider": row[8],
        "model": row[9],
        "reasoning": row[10],
        "binding": _binding_value(row[11]),
        "expires_at": row[14],
    }
    if decision != "admitted":
        return {"error": decision, **payload}
    return payload


def _record_admission(
    conn: sqlite3.Connection,
    *,
    caller_node: str,
    request_id: str,
    phase: str,
    consumer: str,
    roster: Mapping[str, Any],
    request_digest: str,
    decision: str,
    seat: str | None,
    provider: str | None,
    model: str | None,
    reasoning: str | None,
    binding: dict[str, Any] | None,
) -> dict[str, Any]:
    now = _utc_now()
    binding_json = (
        json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=True) if binding is not None else None
    )
    conn.execute(
        "INSERT INTO model_admission_audit ("
        "node_id, request_id, phase, consumer, source, roster_revision, roster_digest, seat, "
        "provider, model, reasoning, consumer_binding, request_digest, decision, expires_at, created_at"
        ") VALUES (?, ?, ?, ?, 'hub', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            caller_node,
            request_id,
            phase,
            consumer,
            int(roster["revision"]),
            str(roster["document_sha256"]),
            seat,
            provider,
            model,
            reasoning,
            binding_json,
            request_digest,
            decision,
            str(roster["expires_at"]),
            now,
        ),
    )
    row = (
        caller_node,
        request_id,
        phase,
        consumer,
        "hub",
        int(roster["revision"]),
        str(roster["document_sha256"]),
        seat,
        provider,
        model,
        reasoning,
        binding_json,
        request_digest,
        decision,
        str(roster["expires_at"]),
        now,
    )
    return _admission_payload(row)


def _admit(conn: sqlite3.Connection, raw: Any, *, caller_node: str) -> tuple[int, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise FleetHubError("model admission request must be a JSON object")
    unknown = set(raw).difference(_ADMIT_FIELDS)
    if unknown:
        raise FleetHubError(f"unknown model admission field(s): {', '.join(sorted(unknown))}")
    if raw.get("schema") != fleet_model_roster.ADMISSION_REQUEST_SCHEMA:
        raise FleetHubError("model admission field 'schema' is unsupported")
    consumer = fleet_hub._model_policy_name(raw.get("consumer"), "consumer")
    if consumer not in fleet_model_roster.CONSUMERS:
        raise FleetHubError("model admission field 'consumer' must be brigade-run or t3-fleet")
    phase = raw.get("phase")
    if phase not in fleet_model_roster.ADMISSION_PHASES:
        raise FleetHubError("model admission field 'phase' is unsupported")
    request_id_raw = raw.get("request_id")
    if not isinstance(request_id_raw, str) or not fleet_hub.CLAIM_ID_PATTERN.match(request_id_raw):
        raise FleetHubError("model admission field 'request_id' is invalid")
    request_id = request_id_raw
    seat_raw = raw.get("seat")
    if seat_raw is not None and seat_raw != "":
        requested_seat = fleet_hub._model_policy_name(seat_raw, "seat")
    else:
        requested_seat = None
    expect_revision = raw.get("expect_revision")
    if expect_revision is not None and type(expect_revision) is not int:
        raise FleetHubError("model admission field 'expect_revision' must be an integer")
    expect_digest = raw.get("expect_digest")
    if expect_digest is not None and (not isinstance(expect_digest, str) or not expect_digest.startswith("sha256:")):
        raise FleetHubError("model admission field 'expect_digest' is invalid")
    request_digest = _request_digest(raw)
    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    try:
        existing = conn.execute(
            "SELECT node_id, request_id, phase, consumer, source, roster_revision, roster_digest, "
            "seat, provider, model, reasoning, consumer_binding, request_digest, decision, expires_at, created_at "
            "FROM model_admission_audit WHERE node_id=? AND request_id=? AND phase=?",
            (caller_node, request_id, phase),
        ).fetchone()
        if existing is not None:
            if str(existing[12]) != request_digest:
                if opened:
                    conn.rollback()
                return 409, {"error": "admission-conflict"}
            payload = _admission_payload(existing)
            if opened:
                conn.commit()
            return (200, payload) if str(existing[13]) == "admitted" else (409, payload)
        roster = project_roster(conn, audience_node_id=caller_node)
        if expect_revision is not None and int(roster["revision"]) != expect_revision:
            payload = _record_admission(
                conn,
                caller_node=caller_node,
                request_id=request_id,
                phase=str(phase),
                consumer=consumer,
                roster=roster,
                request_digest=request_digest,
                decision="roster_revision_conflict",
                seat=None,
                provider=None,
                model=None,
                reasoning=None,
                binding=None,
            )
            if opened:
                conn.commit()
            return 409, payload
        if expect_digest is not None and expect_digest != roster["document_sha256"]:
            payload = _record_admission(
                conn,
                caller_node=caller_node,
                request_id=request_id,
                phase=str(phase),
                consumer=consumer,
                roster=roster,
                request_digest=request_digest,
                decision="roster_digest_conflict",
                seat=None,
                provider=None,
                model=None,
                reasoning=None,
                binding=None,
            )
            if opened:
                conn.commit()
            return 409, payload
        seat_name = requested_seat or _consumer_defaults(conn).get(consumer)
        seat: dict[str, Any] | None = None
        binding: dict[str, Any] | None = None
        decision = "admitted"
        if not seat_name:
            decision = "default-missing"
        else:
            row = conn.execute(
                "SELECT seat, provider, model, reasoning, enabled, brigade_cli, t3_instance_id, t3_service_tier "
                "FROM model_policy WHERE seat=?",
                (seat_name,),
            ).fetchone()
            if row is None:
                decision = "seat-missing"
                seat = {"seat": seat_name}
            else:
                seat = {
                    "seat": row[0],
                    "provider": row[1],
                    "model": row[2],
                    "reasoning": row[3],
                    "enabled": bool(row[4]),
                    "bindings": {
                        "brigade": {"cli": row[5] or ""},
                        "t3_fleet": {"instance_id": row[6] or "", "service_tier": row[7] or None},
                    },
                }
                if not seat["enabled"]:
                    decision = "seat-disabled"
                elif fleet_model_roster.retired_reason(str(seat["provider"]), str(seat["model"]), _retired_rows(conn)):
                    decision = "retired-model"
                else:
                    binding = _binding_for(consumer, seat)
                    if binding is None:
                        decision = "binding-missing"
        payload = _record_admission(
            conn,
            caller_node=caller_node,
            request_id=request_id,
            phase=str(phase),
            consumer=consumer,
            roster=roster,
            request_digest=request_digest,
            decision=decision,
            seat=None if seat is None else str(seat["seat"]),
            provider=None if seat is None else seat.get("provider"),
            model=None if seat is None else seat.get("model"),
            reasoning=None if seat is None else seat.get("reasoning"),
            binding=binding,
        )
        if opened:
            conn.commit()
        return (200, payload) if decision == "admitted" else (409, payload)
    except BaseException:
        if opened:
            conn.rollback()
        raise


def _handle_lease(conn: sqlite3.Connection, raw: Any, *, caller_node: str | None) -> tuple[int, dict[str, Any]]:
    if caller_node is None:
        raise FleetHubForbidden("a node token is required to acquire or release a model lease")
    request = fleet_hub._validate_model_lease_request(raw)
    if caller_node is not None and request["node_id"] != caller_node:
        raise FleetHubForbidden("model lease node_id does not match the caller's node token")
    now = fleet_hub._now_epoch()
    if request["action"] == "release":
        cursor = conn.execute(
            "UPDATE model_leases SET released_at=? WHERE lease_id=? AND owner_node=? AND holder_token=? AND released_at IS NULL",
            (fleet_hub._epoch_to_iso(now), request["lease_id"], request["node_id"], request["holder"]),
        )
        conn.commit()
        return (
            (200, {"released": True})
            if cursor.rowcount == 1
            else (409, {"released": False, "error": "model lease is missing or fenced"})
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE model_leases SET released_at=? WHERE released_at IS NULL AND expires_at <= ?",
            (fleet_hub._epoch_to_iso(now), now),
        )
        policy = conn.execute(
            "SELECT provider, model, enabled, limit_count FROM model_policy WHERE seat=?", (request["seat"],)
        ).fetchone()
        if (
            policy is None
            or str(policy[0]) != request["provider"]
            or str(policy[1]) != request["model"]
            or not bool(policy[2])
        ):
            conn.commit()
            return 409, {"acquired": False, "error": "model policy denied lease"}
        limit = policy[3]
        used = conn.execute(
            "SELECT COUNT(*) FROM model_leases WHERE seat=? AND released_at IS NULL", (request["seat"],)
        ).fetchone()[0]
        if limit is not None and int(used) >= int(limit):
            conn.commit()
            return 409, {"acquired": False, "error": "model policy capacity is exhausted"}
        conn.execute(
            "INSERT INTO model_leases (lease_id, seat, owner_node, holder_token, acquired_at, expires_at, released_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                request["lease_id"],
                request["seat"],
                request["node_id"],
                request["holder"],
                fleet_hub._epoch_to_iso(now),
                now + request["ttl_seconds"],
            ),
        )
        conn.commit()
        return 200, {
            "acquired": True,
            "lease": {
                "lease_id": request["lease_id"],
                "seat": request["seat"],
                "expires_at": fleet_hub._epoch_to_iso(now + request["ttl_seconds"]),
            },
        }
    except BaseException:
        conn.rollback()
        raise


def handle_model_policy(
    conn: sqlite3.Connection, raw: Any, *, caller_node: str | None = None
) -> tuple[int, dict[str, Any]]:
    """Admin roster mutations, node admission, or a capacity lease."""
    action = raw.get("action") if isinstance(raw, dict) else None
    if action in ROSTER_MUTATIONS:
        if caller_node is not None:
            raise FleetHubForbidden("the admin token is required to mutate model policy")
        if action == "set":
            request = _validate_set(raw)
            return _mutate(conn, raw, lambda item: _write_set(item, request))
        if action == "set-default":
            return _mutate(conn, raw, lambda item: _write_default(item, raw))
        return _mutate(conn, raw, lambda item: _write_retire(item, raw))
    if action == "admit":
        if caller_node is None:
            raise FleetHubForbidden("a node token is required to admit a model")
        return _admit(conn, raw, caller_node=caller_node)
    return _handle_lease(conn, raw, caller_node=caller_node)
