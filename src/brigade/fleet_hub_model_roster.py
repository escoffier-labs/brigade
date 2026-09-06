"""Fleet Hub persistence for the versioned model roster (schema v15)."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from . import fleet_hub, fleet_model_roster
from .fleet_hub import FleetHubConflict, FleetHubError, FleetHubForbidden

_ROSTER_COLUMNS = (
    "reasoning TEXT NOT NULL DEFAULT 'none'",
    "brigade_cli TEXT NOT NULL DEFAULT ''",
    "brigade_model TEXT NOT NULL DEFAULT ''",
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
        "brigade_model",
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
        "policy_session_id",
        "policy_version",
        "policy_digest",
        "decision_id",
        "repo_identity",
        "delegation_id",
        "policy_context_hash",
    }
)
_LEASE_LINKAGE_COLUMNS = (
    "provider TEXT",
    "model TEXT",
    "launch_model TEXT",
    "account_id TEXT",
    "pool TEXT",
    "decision_id TEXT",
    "session_id TEXT",
    "consumer TEXT",
    "context_hash TEXT",
    "request_id TEXT",
    "policy_digest TEXT",
    "policy_version INTEGER",
)
ROSTER_MUTATIONS = frozenset({"set", "set-default", "retire"})
_LAUNCH_PROOF_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_launch_proofs (
    node_id TEXT NOT NULL,
    consumer TEXT NOT NULL,
    policy_session_id TEXT NOT NULL,
    proof_key TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (node_id, consumer, policy_session_id, proof_key)
);
"""
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


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


def _optional_launch_identity(raw: Any, field: str) -> str:
    """Bounded native launch id (``provider/model`` is legal), or '' to clear it.

    A launch identity is handed to the adapter verbatim, so it is rejected
    rather than sanitized: control characters and surrounding whitespace are
    refused instead of being stripped into a different id than the operator
    reviewed.
    """
    if raw is None or raw == "":
        return ""
    if not isinstance(raw, str):
        raise FleetHubError(f"model policy field {field!r} must be a string")
    if _CONTROL_RE.search(raw):
        raise FleetHubError(f"model policy field {field!r} is not a valid launch identity")
    value = fleet_hub._model_identity_name(raw, field)
    if value != raw:
        raise FleetHubError(f"model policy field {field!r} is not a valid launch identity")
    return value


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
    conn.execute(_LAUNCH_PROOF_SCHEMA)
    lease_columns = _columns(conn, "model_leases")
    if lease_columns:
        for column in _LEASE_LINKAGE_COLUMNS:
            name = column.split()[0]
            if name not in lease_columns:
                conn.execute(f"ALTER TABLE model_leases ADD COLUMN {column}")
    now = _utc_now()
    if conn.execute("SELECT 1 FROM model_roster_meta WHERE singleton=1").fetchone() is None:
        conn.execute(
            "INSERT INTO model_roster_meta (singleton, revision, updated_at, updated_by) VALUES (1, 1, ?, ?)",
            (now, "schema-v15"),
        )
    from . import fleet_policy_migration

    if fleet_policy_migration.is_activated(conn):
        return
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


def raw_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT revision FROM model_roster_meta WHERE singleton=1").fetchone()
    if row is None:
        raise FleetHubError("model roster revision metadata is missing")
    return int(row[0])


def _revision(conn: sqlite3.Connection) -> int:
    return raw_revision(conn)


def raw_retired_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
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


def _retired_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return raw_retired_rows(conn)


def raw_consumer_defaults(conn: sqlite3.Connection) -> dict[str, str | None]:
    rows = conn.execute("SELECT consumer, seat FROM model_consumer_defaults ORDER BY consumer").fetchall()
    return {str(row[0]): (str(row[1]) if row[1] else None) for row in rows}


def _authority_active(conn: sqlite3.Connection) -> bool:
    from . import fleet_policy_migration

    return fleet_policy_migration.is_activated(conn)


def _consumer_defaults(conn: sqlite3.Connection) -> dict[str, str | None]:
    if _authority_active(conn):
        from . import fleet_policy_migration

        return fleet_policy_migration.projected_consumer_defaults(conn)
    return raw_consumer_defaults(conn)


def raw_seats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT seat, provider, model, reasoning, enabled, limit_count, "
        "brigade_cli, t3_instance_id, t3_service_tier, brigade_model FROM model_policy ORDER BY seat"
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
                "brigade": _brigade_binding(row[6], row[9]),
                "t3_fleet": {"instance_id": row[7] or "", "service_tier": row[8] or None},
            },
        }
        for row in rows
    ]


def _brigade_binding(cli: Any, launch_model: Any) -> dict[str, Any]:
    """Brigade launch group for a roster row. ``model`` appears only when bound."""
    binding: dict[str, Any] = {"cli": cli or ""}
    if isinstance(launch_model, str) and launch_model:
        binding["model"] = launch_model
    return binding


def _seats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if _authority_active(conn):
        from . import fleet_policy_migration

        return fleet_policy_migration.projected_seats(conn)
    return raw_seats(conn)


def _owned_error() -> dict[str, Any]:
    from . import fleet_policy_migration

    return {"error": fleet_policy_migration.AUTHORITY_OWNED}


def _require_legacy_writable(conn: sqlite3.Connection) -> None:
    from . import fleet_policy_migration

    fleet_policy_migration.refuse_legacy_write(conn)


def project_roster(
    conn: sqlite3.Connection,
    *,
    audience_node_id: str | None = None,
    raw_node_bearer: str | None = None,
) -> dict[str, Any]:
    """Versioned roster plus the legacy ``models`` list. Admin reads omit MAC."""
    authority_meta = None
    if _authority_active(conn):
        from . import fleet_policy_migration

        revision, updated_at = fleet_policy_migration.projected_roster_meta(conn)
        seats = fleet_policy_migration.projected_seats(conn)
        defaults = fleet_policy_migration.projected_consumer_defaults(conn)
        models = fleet_policy_migration.projected_models(conn, seats)
        authority_meta = fleet_policy_migration.projected_authority_metadata(conn)
    else:
        meta = conn.execute("SELECT revision, updated_at FROM model_roster_meta WHERE singleton=1").fetchone()
        if meta is None:
            raise FleetHubError("model roster revision metadata is missing")
        revision, updated_at = int(meta[0]), str(meta[1])
        seats = raw_seats(conn)
        defaults = raw_consumer_defaults(conn)
        models = fleet_hub.list_model_policy(conn)
    issued = utc_now()
    payload: dict[str, Any] = {
        "schema": fleet_model_roster.ROSTER_SCHEMA,
        "revision": revision,
        "revision_updated_at": _iso_z(updated_at),
        "issued_at": _iso_z(issued),
        "expires_at": _iso_z(issued + timedelta(seconds=fleet_model_roster.LKG_TTL_SECONDS)),
        "seats": seats,
        "consumer_defaults": defaults,
        "retired_models": raw_retired_rows(conn),
    }
    if authority_meta is not None:
        payload["fleet_policy"] = authority_meta
        launch_bindings = project_consumer_launch_bindings(conn)
        if launch_bindings:
            payload["consumer_launch_bindings"] = launch_bindings
    if audience_node_id:
        payload["audience_node_id"] = audience_node_id
    payload["document_sha256"] = fleet_model_roster.roster_digest(payload)
    payload["models"] = models
    if audience_node_id and raw_node_bearer:
        payload["mac"] = {
            "algorithm": fleet_model_roster.MAC_ALGORITHM,
            "value": fleet_model_roster.roster_mac(raw_node_bearer, payload),
        }
    return payload


def project_consumer_launch_bindings(conn: sqlite3.Connection) -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
    """Configured consumer -> seat -> effective binding groups. Never Cartesian expansion."""
    from . import fleet_hub_policy, fleet_policy

    current = fleet_hub_policy.current_policy(conn)
    document = current["document"]
    parsed = fleet_policy.parse_document(document)
    projected: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    pairs = 0
    for consumer in parsed["consumers"]:
        if consumer not in fleet_model_roster.CONSUMERS:
            continue
        seats_map: dict[str, dict[str, dict[str, Any]]] = {}
        for seat_name in parsed["seats"]:
            groups = fleet_policy.effective_seat_bindings(parsed, consumer, seat_name, _parsed=parsed)
            compacted = fleet_model_roster.compact_launch_groups(groups)
            if not any(compacted[group] for group in fleet_model_roster.LAUNCH_BINDING_GROUPS):
                continue
            seats_map[seat_name] = compacted
            pairs += 1
            if pairs > fleet_model_roster.MAX_LAUNCH_BINDING_PAIRS:
                raise FleetHubError("consumer launch bindings exceeded the configured cardinality bound")
        if seats_map:
            projected[consumer] = seats_map
    rendered = fleet_model_roster.canonical_json(projected)
    if len(rendered.encode("ascii")) > fleet_model_roster.MAX_LAUNCH_BINDING_BYTES:
        raise FleetHubError("consumer launch bindings exceeded the signed size bound")
    return projected


def _mutate(
    conn: sqlite3.Connection,
    raw: Any,
    writer: Callable[[sqlite3.Connection], dict[str, Any]],
    *,
    updated_by: str = "admin",
) -> tuple[int, dict[str, Any]]:
    if _authority_active(conn):
        return 409, _owned_error()
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
        "brigade_model": _optional_launch_identity(raw.get("brigade_model"), "brigade_model"),
        "t3_instance_id": _optional_binding(raw.get("t3_instance_id"), "t3_instance_id"),
        "t3_service_tier": _optional_binding(raw.get("t3_service_tier"), "t3_service_tier"),
    }


def _retired_conflict(conn: sqlite3.Connection, provider: str, model: str) -> dict[str, Any] | None:
    if fleet_model_roster.retired_reason(provider, model, _retired_rows(conn)):
        return {"error": "retired-model"}
    return None


def _write_set(conn: sqlite3.Connection, request: dict[str, Any]) -> dict[str, Any]:
    _require_legacy_writable(conn)
    denied = _retired_conflict(conn, str(request["provider"]), str(request["model"]))
    if denied is not None:
        return denied
    conn.execute(
        "INSERT INTO model_policy "
        "(seat, provider, model, reasoning, enabled, limit_count, brigade_cli, brigade_model, t3_instance_id, "
        "t3_service_tier, notes, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(seat) DO UPDATE SET provider=excluded.provider, model=excluded.model, "
        "reasoning=excluded.reasoning, enabled=excluded.enabled, limit_count=excluded.limit_count, "
        "brigade_cli=excluded.brigade_cli, brigade_model=excluded.brigade_model, "
        "t3_instance_id=excluded.t3_instance_id, "
        "t3_service_tier=excluded.t3_service_tier, notes=excluded.notes, updated_at=excluded.updated_at",
        (
            request["seat"],
            request["provider"],
            request["model"],
            request["reasoning"],
            int(request["enabled"]),
            request["limit"],
            request["brigade_cli"],
            request["brigade_model"],
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
            "brigade_model": request["brigade_model"] or None,
            "t3_instance_id": request["t3_instance_id"],
            "t3_service_tier": request["t3_service_tier"] or None,
        },
    }


def _write_default(conn: sqlite3.Connection, raw: Any) -> dict[str, Any]:
    _require_legacy_writable(conn)
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
    _require_legacy_writable(conn)
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


def _binding_for(
    consumer: str,
    seat: Mapping[str, Any],
    *,
    launch_groups: Mapping[str, Any] | None = None,
    adapter: str | None = None,
) -> dict[str, Any] | None:
    groups = launch_groups
    if groups is None:
        groups = seat.get("bindings") if isinstance(seat.get("bindings"), Mapping) else None
    planned = fleet_model_roster.adapter_plan_binding(
        consumer,
        groups,
        canonical_model=str(seat.get("model") or "") or None,
        adapter=adapter,
    )
    if planned is not None:
        return planned
    bindings = seat.get("bindings")
    if not isinstance(bindings, Mapping):
        return None
    if consumer == "t3-fleet" or adapter == "t3-fleet":
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
    payload = {"instance_id": instance_id, "service_tier": None}
    launch_model = brigade.get("model")
    if isinstance(launch_model, str) and launch_model:
        payload["model"] = launch_model
    return payload


def _request_digest(raw: dict[str, Any]) -> str:
    body = {
        "consumer": raw.get("consumer"),
        "decision_id": raw.get("decision_id"),
        "delegation_id": raw.get("delegation_id"),
        "expect_digest": raw.get("expect_digest"),
        "expect_revision": raw.get("expect_revision"),
        "phase": raw.get("phase"),
        "policy_digest": raw.get("policy_digest"),
        "policy_session_id": raw.get("policy_session_id"),
        "policy_version": raw.get("policy_version"),
        "policy_context_hash": raw.get("policy_context_hash"),
        "repo_identity": raw.get("repo_identity"),
        "seat": raw.get("seat"),
    }
    return hashlib.sha256(fleet_model_roster.canonical_json(body).encode("ascii")).hexdigest()


def _reject_control(value: object, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or _CONTROL_RE.search(value) or len(value) > 256:
        raise FleetHubError(f"model admission field {field!r} is invalid")
    return value


def _optional_sha256(value: object, field: str) -> str | None:
    text = _reject_control(value, field)
    if text is None:
        return None
    if fleet_model_roster.SHA256_DIGEST_PATTERN.fullmatch(text) is None:
        raise FleetHubError(f"model admission field {field!r} is invalid")
    return text


def _proof_key(*, consumer: str, policy_session_id: str, decision_id: str | None) -> str:
    if consumer == "t3-fleet":
        if not decision_id:
            raise FleetHubError("t3-fleet launch proof requires decision_id")
        return decision_id
    return policy_session_id


def _selected_identities(selected: Mapping[str, Any]) -> dict[str, Any]:
    launch = selected.get("launch_model")
    if not isinstance(launch, str) or not launch:
        launch = selected.get("native_model") if isinstance(selected.get("native_model"), str) else None
    binding = selected.get("binding") if isinstance(selected.get("binding"), Mapping) else {}
    if not isinstance(launch, str) or not launch:
        launch = binding.get("model") if isinstance(binding, Mapping) else None
    canonical = selected.get("model")
    return {
        "seat": selected.get("seat"),
        "provider": selected.get("provider"),
        "model": canonical,
        "launch_model": launch if isinstance(launch, str) and launch else None,
        "native_model": launch if isinstance(launch, str) and launch else None,
        "instance_id": selected.get("instance_id"),
    }


def _lookup_pending(
    conn: sqlite3.Connection,
    *,
    caller_node: str,
    consumer: str,
    session_id: str,
    repo_identity: str | None,
) -> dict[str, Any] | None:
    from . import fleet_hub_policy

    if repo_identity is not None:
        return fleet_hub_policy.find_pending_policy(
            conn,
            session_id=session_id,
            consumer=consumer,
            repo_identity=repo_identity,
            node_id=caller_node,
        )
    rows = conn.execute(
        "SELECT repo_identity FROM fleet_policy_pending WHERE session_id=? AND consumer=? AND owner_node=?",
        (session_id, consumer, caller_node),
    ).fetchall()
    if len(rows) != 1:
        return None
    return fleet_hub_policy.find_pending_policy(
        conn,
        session_id=session_id,
        consumer=consumer,
        repo_identity=rows[0][0],
        node_id=caller_node,
    )


def _revalidate_reservation(
    conn: sqlite3.Connection,
    *,
    caller_node: str,
    consumer: str,
    session_id: str,
    decision_id: str,
    pending: Mapping[str, Any],
    selected: Mapping[str, Any],
) -> str | None:
    from . import fleet_hub_routing, fleet_hub_policy, fleet_policy

    try:
        origin = fleet_hub_routing.decision_origin_context(conn, decision_id, caller_node)
    except FleetHubForbidden:
        return "decision-not-owned"
    except FleetHubError:
        return "decision-unknown"
    origin_consumer = origin.get("consumer")
    if origin_consumer not in {None, consumer} and origin_consumer != consumer:
        return "decision-consumer-mismatch"
    if origin.get("session_id") not in {None, session_id} and origin.get("session_id") != session_id:
        return "decision-session-mismatch"
    origin_repo = origin.get("repo_identity") or ""
    pending_repo = pending.get("repo_identity") or ""
    if origin_repo and origin_repo != pending_repo:
        return "decision-repo-mismatch"
    origin_seat = origin.get("seat")
    if origin_seat and selected.get("seat") and origin_seat != selected.get("seat"):
        return "decision-seat-mismatch"
    workload = origin.get("workload")
    if not isinstance(workload, str) or not workload:
        return "decision-workload-missing"
    try:
        fleet_hub_routing.route(
            conn,
            {
                "decision_id": decision_id,
                "session_id": origin.get("session_id") or session_id,
                "consumer": origin.get("consumer") or consumer,
                "repo_identity": origin.get("repo_identity") or pending.get("repo_identity"),
                "workload": workload,
            },
            caller_node,
        )
    except FleetHubForbidden:
        return "decision-not-owned"
    except FleetHubConflict as exc:
        text = str(exc)
        if "reservation-unknown" in text or "expired" in text:
            return "reservation-expired"
        if "policy-mismatch" in text:
            return "policy-stale"
        return "reservation-invalid"
    except FleetHubError as exc:
        text = str(exc)
        if "does not exist" in text:
            return "decision-unknown"
        if "not live" in text or "released" in text:
            return "reservation-released"
        if "expired" in text:
            return "reservation-expired"
        return "reservation-invalid"
    current = fleet_hub_policy.current_policy(conn)
    document = current["document"]
    resolved = fleet_policy.resolve_policy(document, consumer, pending.get("repo_identity"))
    seat_name = selected.get("seat")
    if isinstance(seat_name, str) and seat_name:
        terms = fleet_policy.effective_seat_terms(document, seat_name, consumer)
        admission = fleet_policy.admissible_seat(document, seat_name, resolved)
        if terms.get("requires_training") and "training-not-permitted" in admission["reasons"]:
            return "training-disallowed"
        if "training-not-permitted" in admission["reasons"]:
            return "training-disallowed"
        if not admission["admissible"]:
            return "policy-blocked"
    return None


def _policy_proof_error(
    conn: sqlite3.Connection,
    raw: Mapping[str, Any],
    *,
    caller_node: str,
    consumer: str,
    phase: str,
    requested_seat: str | None,
    seat: Mapping[str, Any] | None,
) -> str | None:
    from . import fleet_hub_policy, fleet_hub_routing, fleet_policy_delegation

    session_id = _reject_control(raw.get("policy_session_id"), "policy_session_id")
    policy_digest = _optional_sha256(raw.get("policy_digest"), "policy_digest")
    policy_version = raw.get("policy_version")
    decision_id = _reject_control(raw.get("decision_id"), "decision_id")
    repo_identity = _reject_control(raw.get("repo_identity"), "repo_identity")
    policy_context_hash = _optional_sha256(raw.get("policy_context_hash"), "policy_context_hash")
    if session_id is None or policy_digest is None or type(policy_version) is not int or policy_version <= 0:
        return "policy-proof-missing"
    if repo_identity is None:
        return "policy-proof-missing"
    pending = _lookup_pending(
        conn,
        caller_node=caller_node,
        consumer=consumer,
        session_id=session_id,
        repo_identity=repo_identity,
    )
    if pending is None:
        return "policy-proof-missing"
    if pending.get("owner_node") != caller_node:
        return "policy-proof-node-mismatch"
    if pending.get("consumer") != consumer:
        return "policy-proof-consumer-mismatch"
    if pending.get("source") != "prepare":
        return "policy-proof-not-prepared"
    if pending.get("revision") != policy_version or pending.get("digest") != policy_digest:
        return "policy-proof-mismatch"
    if (pending.get("repo_identity") or "") != repo_identity:
        return "policy-proof-repo-mismatch"
    pending_hash = pending.get("context_hash")
    if not isinstance(pending_hash, str) or fleet_model_roster.SHA256_DIGEST_PATTERN.fullmatch(pending_hash) is None:
        return "policy-proof-not-prepared"
    if phase == "launch":
        if policy_context_hash is None:
            return "policy-proof-missing"
        if policy_context_hash != pending_hash:
            return "policy-proof-mismatch"
    selected_raw = pending.get("selected")
    if not isinstance(selected_raw, Mapping) or not selected_raw.get("seat"):
        return "policy-proof-not-prepared"
    selected = _selected_identities(selected_raw)
    if requested_seat and selected.get("seat") != requested_seat:
        return "policy-proof-seat-mismatch"
    adapter = None
    if raw.get("delegation_id"):
        try:
            delegation = fleet_policy_delegation.validate_delegation(
                conn,
                str(raw.get("delegation_id")),
                caller_node,
                phase="launch" if phase == "launch" else "prepare",
            )
        except FleetHubForbidden:
            return "decision-not-owned"
        except FleetHubConflict:
            return "policy-stale"
        except FleetHubError:
            return "delegation-unavailable"
        if delegation.get("adapter") != "t3-fleet" or delegation.get("consumer") != consumer:
            return "delegation-unavailable"
        adapter = "t3-fleet"
        if not decision_id:
            decision_id = str(delegation.get("decision_id") or "") or None
        if decision_id != delegation.get("decision_id"):
            return "decision-unknown"
        if (delegation.get("repo_identity") or "") != repo_identity:
            return "policy-proof-repo-mismatch"
        if selected.get("seat") and delegation.get("seat") and selected.get("seat") != delegation.get("seat"):
            return "policy-proof-seat-mismatch"
    launch_groups = None
    if seat is not None:
        seat_name = str(seat.get("seat") or selected.get("seat") or "")
        try:
            bindings_map = project_consumer_launch_bindings(conn)
        except FleetHubError:
            bindings_map = {}
        consumer_map = bindings_map.get(consumer) if isinstance(bindings_map, Mapping) else None
        if isinstance(consumer_map, Mapping) and seat_name:
            launch_groups = consumer_map.get(seat_name)
        if selected.get("provider") != seat.get("provider"):
            return "policy-proof-identity-mismatch"
        allowed = set(fleet_model_roster.binding_launch_models(seat))
        if isinstance(launch_groups, Mapping):
            for group in launch_groups.values():
                if isinstance(group, Mapping):
                    model_leaf = group.get("model")
                    if isinstance(model_leaf, str) and model_leaf:
                        allowed.add(model_leaf)
        canonical = selected.get("model")
        native = selected.get("launch_model")
        if canonical not in allowed:
            return "policy-proof-identity-mismatch"
        if native and native != canonical:
            native_group = (launch_groups or {}).get("native") if isinstance(launch_groups, Mapping) else None
            brigade_group = (launch_groups or {}).get("brigade") if isinstance(launch_groups, Mapping) else None
            explicit = None
            if isinstance(native_group, Mapping):
                explicit = native_group.get("model")
            if explicit is None and isinstance(brigade_group, Mapping):
                explicit = brigade_group.get("model")
            if explicit != native:
                return "policy-proof-identity-mismatch"
        binding = _binding_for(consumer, seat, launch_groups=launch_groups, adapter=adapter)
        instance_id = binding.get("instance_id") if isinstance(binding, dict) else None
        if selected.get("instance_id") and instance_id and selected.get("instance_id") != instance_id:
            return "policy-proof-identity-mismatch"
    current = fleet_hub_policy.current_policy(conn)
    if current["revision"] != policy_version or current["digest"] != policy_digest:
        return "policy-stale"
    if phase == "launch":
        loaded = fleet_hub_policy.find_loaded_session(
            conn,
            session_id=session_id,
            consumer=consumer,
            repo_identity=pending.get("repo_identity"),
            node_id=caller_node,
        )
        if loaded is None:
            return "policy-ack-missing"
        loaded_hash = loaded.get("context_hash")
        if not isinstance(loaded_hash, str) or loaded_hash != pending_hash or loaded_hash != policy_context_hash:
            return "policy-proof-mismatch"
        if loaded.get("owner_node") != caller_node:
            return "policy-proof-node-mismatch"
        if loaded.get("revision") != policy_version or loaded.get("digest") != policy_digest:
            return "policy-stale"
        if loaded.get("state") != "current":
            return "policy-stale"
        if loaded.get("refresh_state") in {"requested", "failed"}:
            return "policy-stale"
        loaded_selected = loaded.get("selected") if isinstance(loaded.get("selected"), Mapping) else None
        if loaded_selected is None:
            return "policy-ack-missing"
        loaded_ids = _selected_identities(loaded_selected)
        if loaded_ids.get("seat") != selected.get("seat") or loaded_ids.get("model") != selected.get("model"):
            return "policy-proof-identity-mismatch"
        if loaded_ids.get("launch_model") != selected.get("launch_model"):
            return "policy-proof-identity-mismatch"
        pending_snapshot = loaded.get("pending_snapshot")
        if isinstance(pending_snapshot, Mapping):
            current_pending_hash = pending_snapshot.get("context_hash")
            if current_pending_hash != loaded_hash:
                return "policy-proof-mismatch"
    require_reservation = consumer == "t3-fleet" or adapter == "t3-fleet"
    if require_reservation:
        if not decision_id:
            return "decision-proof-missing"
        reservation_error = _revalidate_reservation(
            conn,
            caller_node=caller_node,
            consumer=consumer,
            session_id=session_id,
            decision_id=decision_id,
            pending=pending,
            selected=selected,
        )
        if reservation_error is not None:
            return reservation_error
    elif decision_id:
        try:
            fleet_hub_routing.decision_origin_context(conn, decision_id, caller_node)
        except Exception:
            return "decision-unknown"
    return None


def _consume_launch_proof(
    conn: sqlite3.Connection,
    *,
    caller_node: str,
    consumer: str,
    request_id: str,
    request_digest: str,
    policy_session_id: str,
    decision_id: str | None,
) -> str | None:
    proof_key = _proof_key(consumer=consumer, policy_session_id=policy_session_id, decision_id=decision_id)
    existing = conn.execute(
        "SELECT request_id, request_digest FROM model_launch_proofs "
        "WHERE node_id=? AND consumer=? AND policy_session_id=? AND proof_key=?",
        (caller_node, consumer, policy_session_id, proof_key),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO model_launch_proofs ("
            "node_id, consumer, policy_session_id, proof_key, request_id, request_digest, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (caller_node, consumer, policy_session_id, proof_key, request_id, request_digest, _utc_now()),
        )
        return None
    if str(existing[0]) != request_id or str(existing[1]) != request_digest:
        return "launch-proof-replay"
    return None


def _lease_proof_error(
    conn: sqlite3.Connection,
    request: Mapping[str, Any],
    *,
    caller_node: str,
    policy: Mapping[str, Any],
) -> str | None:
    session_id = request.get("policy_session_id")
    digest = request.get("policy_digest")
    version = request.get("policy_version")
    request_id = request.get("request_id")
    if not session_id or not digest or type(version) is not int or not request_id:
        return "policy-proof-missing"
    consumer = request.get("consumer") or "brigade-run"
    fake_raw = {
        "policy_session_id": session_id,
        "policy_digest": digest,
        "policy_version": version,
        "decision_id": request.get("decision_id"),
        "repo_identity": request.get("repo_identity"),
        "delegation_id": request.get("delegation_id"),
        "policy_context_hash": request.get("policy_context_hash") or request.get("context_hash"),
    }
    proof_error = _policy_proof_error(
        conn,
        fake_raw,
        caller_node=caller_node,
        consumer=str(consumer),
        phase="launch",
        requested_seat=str(request.get("seat") or ""),
        seat=policy,
    )
    if proof_error is not None:
        return proof_error
    proof_key = _proof_key(
        consumer=str(consumer),
        policy_session_id=str(session_id),
        decision_id=request.get("decision_id") if isinstance(request.get("decision_id"), str) else None,
    )
    existing = conn.execute(
        "SELECT request_id, request_digest FROM model_launch_proofs "
        "WHERE node_id=? AND consumer=? AND policy_session_id=? AND proof_key=?",
        (caller_node, consumer, session_id, proof_key),
    ).fetchone()
    if existing is None:
        return "policy-proof-missing"
    if str(existing[0]) != str(request_id):
        return "launch-proof-replay"
    return None


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
    if raw.get("policy_version") is not None and type(raw.get("policy_version")) is not int:
        raise FleetHubError("model admission field 'policy_version' must be an integer")
    _reject_control(raw.get("policy_session_id"), "policy_session_id")
    _reject_control(raw.get("decision_id"), "decision_id")
    _reject_control(raw.get("repo_identity"), "repo_identity")
    _reject_control(raw.get("delegation_id"), "delegation_id")
    if raw.get("policy_digest"):
        _optional_sha256(raw.get("policy_digest"), "policy_digest")
    if raw.get("policy_context_hash"):
        _optional_sha256(raw.get("policy_context_hash"), "policy_context_hash")
    request_digest = _request_digest(raw)
    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    try:
        activated = _authority_active(conn)
        existing = conn.execute(
            "SELECT node_id, request_id, phase, consumer, source, roster_revision, roster_digest, "
            "seat, provider, model, reasoning, consumer_binding, request_digest, decision, expires_at, created_at "
            "FROM model_admission_audit WHERE node_id=? AND request_id=? AND phase=?",
            (caller_node, request_id, phase),
        ).fetchone()
        if existing is not None and str(existing[12]) != request_digest:
            if opened:
                conn.rollback()
            return 409, {"error": "admission-conflict"}
        if existing is not None and not (
            activated and phase in fleet_model_roster.PROOF_PHASES and str(existing[13]) == "admitted"
        ):
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
            found = next((item for item in _seats(conn) if item["seat"] == seat_name), None)
            if found is None:
                decision = "seat-missing"
                seat = {"seat": seat_name}
            else:
                seat = found
                retired = None
                for identity in fleet_model_roster.binding_launch_models(seat):
                    retired = fleet_model_roster.retired_reason(str(seat["provider"]), identity, _retired_rows(conn))
                    if retired is not None:
                        break
                if not seat["enabled"]:
                    decision = "seat-disabled"
                elif retired is not None:
                    decision = "retired-model"
                elif not isinstance(seat["reasoning"], str) or not seat["reasoning"].strip():
                    decision = "binding-missing"
                else:
                    launch_groups = None
                    roster_bindings = roster.get("consumer_launch_bindings")
                    if isinstance(roster_bindings, Mapping):
                        consumer_map = roster_bindings.get(consumer)
                        if isinstance(consumer_map, Mapping):
                            launch_groups = consumer_map.get(seat["seat"])
                    binding = _binding_for(consumer, seat, launch_groups=launch_groups)
                    if binding is None:
                        decision = "binding-missing"
        if activated and phase in fleet_model_roster.PROOF_PHASES and decision == "admitted":
            proof_error = _policy_proof_error(
                conn,
                raw,
                caller_node=caller_node,
                consumer=consumer,
                phase=str(phase),
                requested_seat=seat_name,
                seat=seat,
            )
            if proof_error is not None:
                decision = proof_error
                binding = None
            elif phase == "launch":
                session_id = str(raw.get("policy_session_id") or "")
                consume_error = _consume_launch_proof(
                    conn,
                    caller_node=caller_node,
                    consumer=consumer,
                    request_id=request_id,
                    request_digest=request_digest,
                    policy_session_id=session_id,
                    decision_id=_reject_control(raw.get("decision_id"), "decision_id"),
                )
                if consume_error is not None:
                    decision = consume_error
                    binding = None
        if existing is not None:
            if decision != "admitted" and str(existing[13]) == "admitted":
                payload = {"error": decision, "schema": fleet_model_roster.ADMISSION_SCHEMA, "state": "denied"}
                if opened:
                    conn.commit()
                return 409, payload
            payload = _admission_payload(existing)
            if opened:
                conn.commit()
            return (200, payload) if str(existing[13]) == "admitted" else (409, payload)
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
        policy = next((item for item in _seats(conn) if item["seat"] == request["seat"]), None)
        canonical_model = str(policy["model"]) if policy is not None else None
        allowed_models = set(fleet_model_roster.binding_launch_models(policy)) if policy is not None else set()
        launch_model = request.get("launch_model")
        if (
            policy is None
            or str(policy["provider"]) != request["provider"]
            or request["model"] != canonical_model
            or not bool(policy["enabled"])
        ):
            conn.commit()
            return 409, {"acquired": False, "error": "model policy denied lease"}
        if launch_model and launch_model not in allowed_models and launch_model != canonical_model:
            conn.commit()
            return 409, {"acquired": False, "error": "model policy denied lease"}
        if _authority_active(conn):
            retired = None
            for identity in allowed_models:
                retired = fleet_model_roster.retired_reason(str(policy["provider"]), identity, _retired_rows(conn))
                if retired is not None:
                    break
            if retired is not None:
                conn.commit()
                return 409, {"acquired": False, "error": "model policy denied lease"}
            proof_error = _lease_proof_error(conn, request, caller_node=caller_node, policy=policy)
            if proof_error is not None:
                conn.commit()
                return 409, {"acquired": False, "error": proof_error}
            existing_bound = conn.execute(
                "SELECT lease_id FROM model_leases WHERE request_id=? AND released_at IS NULL",
                (request.get("request_id"),),
            ).fetchone()
            if existing_bound is not None:
                if str(existing_bound[0]) == request["lease_id"]:
                    conn.commit()
                    return 200, {
                        "acquired": True,
                        "lease": {
                            "lease_id": request["lease_id"],
                            "seat": request["seat"],
                            "expires_at": fleet_hub._epoch_to_iso(now + request["ttl_seconds"]),
                        },
                    }
                conn.commit()
                return 409, {"acquired": False, "error": "launch-proof-replay"}
            from . import fleet_hub_capacity, fleet_hub_policy

            document = fleet_hub_policy.current_policy(conn)["document"]
            now_dt = datetime.now(timezone.utc)
            exclude = None
            if request.get("decision_id") and request.get("policy_session_id"):
                candidate = {
                    "decision_id": str(request["decision_id"]),
                    "session_id": str(request["policy_session_id"]),
                    "node_id": caller_node,
                }
                try:
                    usage = fleet_hub_capacity.capacity_usage(
                        conn,
                        document=document,
                        seat=request["seat"],
                        now=now_dt,
                        exclude_execution=candidate,
                    )
                    exclude = candidate
                except FleetHubError:
                    usage = fleet_hub_capacity.capacity_usage(conn, document=document, seat=request["seat"], now=now_dt)
            else:
                usage = fleet_hub_capacity.capacity_usage(conn, document=document, seat=request["seat"], now=now_dt)
            _ = exclude
            seat_record = (document.get("seats") or {}).get(request["seat"]) or {}
            limit = seat_record.get("concurrency", policy.get("limit"))
            used = usage["used"]["seat"]
        else:
            limit = policy.get("limit")
            used = conn.execute(
                "SELECT COUNT(*) FROM model_leases WHERE seat=? AND released_at IS NULL", (request["seat"],)
            ).fetchone()[0]
        if limit is not None and int(used) >= int(limit):
            conn.commit()
            return 409, {"acquired": False, "error": "model policy capacity is exhausted"}
        seat_record = {}
        if _authority_active(conn):
            from . import fleet_hub_policy as _policy

            seat_record = (_policy.current_policy(conn)["document"].get("seats") or {}).get(request["seat"]) or {}
        conn.execute(
            "INSERT INTO model_leases ("
            "lease_id, seat, owner_node, holder_token, acquired_at, expires_at, released_at, "
            "provider, model, launch_model, account_id, pool, decision_id, session_id, "
            "consumer, context_hash, request_id, policy_digest, policy_version"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request["lease_id"],
                request["seat"],
                request["node_id"],
                request["holder"],
                fleet_hub._epoch_to_iso(now),
                now + request["ttl_seconds"],
                request["provider"],
                request["model"],
                request.get("launch_model"),
                None,
                seat_record.get("quota_pool"),
                request.get("decision_id"),
                request.get("policy_session_id"),
                request.get("consumer") or "brigade-run",
                request.get("policy_context_hash") or request.get("context_hash"),
                request.get("request_id"),
                request.get("policy_digest"),
                request.get("policy_version"),
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
