"""Bounded Fleet Model Inventory component for the Brigade control plane.

This module provides server-owned persistent model inventory storage,
source provenance tracking, coverage-scoped freshness evaluation,
safe probe helpers, and exact identity classification. Zero runtime
dependencies (standard library only).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit

from brigade import proc

PAYLOAD_SCHEMA = "brigade.fleet_model_inventory.v1"
SNAPSHOT_SCHEMA = "brigade.fleet_model_inventory.snapshot.v1"

DEFAULT_ALLOWED_SOURCES = frozenset(
    {
        "cli:agy",
        "cli:opencode",
        "cli:cursor-agent",
        "cli:grok",
        "manual:browser",
    }
)

DEFAULT_EXECUTABLE_ALLOWLIST = frozenset(
    {
        "agy",
        "opencode",
        "cursor-agent",
        "grok",
    }
)

ALLOWED_PROBE_COMMANDS = frozenset(
    {
        ("agy", "models"),
        ("opencode", "models"),
        ("cursor-agent", "models"),
        ("grok", "models"),
    }
)

UNSUPPORTED_HARNESSES = frozenset(
    {
        "claudecode",
        "codex",
        "jules",
        "grokbot",
    }
)

MAX_PAYLOAD_BYTES = 256 * 1024  # 256 KiB
MAX_STDOUT_BYTES = 64 * 1024  # 64 KiB accepted inventory output after proc's 1 MiB capture cap
MAX_MODELS_COUNT = 1000
MAX_STRING_LENGTH = 1024
MAX_JSON_DEPTH = 8
MAX_JSON_NODES = 8192
DEFAULT_PROBE_TIMEOUT = 15.0
DEFAULT_MAX_CLOCK_SKEW_SECONDS = 300.0  # 5 minutes
DEFAULT_MAX_TTL_SECONDS = 30 * 86400.0  # 30 days

ALLOWED_PAYLOAD_KEYS = frozenset(
    {
        "schema",
        "source",
        "provider",
        "harness",
        "account_id",
        "scope",
        "status",
        "captured_at",
        "expires_at",
        "models",
        "error_reason",
        "evidence_type",
        "operator",
    }
)

ALLOWED_MODEL_KEYS = frozenset(
    {
        "model_id",
        "native_id",
        "state",
        "display_name",
        "description",
    }
)

KNOWN_ERROR_CODES = frozenset(
    {
        "probe-failed",
        "probe-timed-out",
        "executable-not-found",
        "permission-denied",
        "command-not-allowed",
        "output-too-large",
        "unsupported-output-shape",
        "unsupported-harness-no-verified-live-model-list",
        "authentication-revoked",
        "authentication-required",
        "rate-limit-exceeded",
        "quota-exceeded",
        "network-unavailable",
        "same-instant-conflict",
        "inventory-expired",
        "divergent-read",
        "provider-error",
        "future-timestamp-beyond-skew",
        "invalid-timeout",
        "ambiguous-provider-coverage",
        "coverage-not-found",
        "provider-not-inventoried",
        "partial-inventory-cannot-prove-missing",
    }
)

KNOWN_AGY_PROGRESS_PREAMBLE_RE = re.compile(
    r"^(?:\[info\]\s+|loading\s+models|fetching\s+available\s+models|listing\s+models|retrieving\s+models|progress:?\s*).*",
    re.IGNORECASE,
)

ScopeKind = Literal["complete", "partial"]
StatusKind = Literal["ok", "error"]
InventoryState = Literal[
    "fresh",
    "stale",
    "unavailable",
    "missing",
    "retired",
    "blocked",
    "policy-blocked",
    "unknown",
    "available",
]


class FleetModelInventoryError(Exception):
    """Base exception for fleet model inventory operations."""


class InventoryValidationError(FleetModelInventoryError):
    """Raised when submitted payload fails schema, size, or safety bounds."""


class InventoryCollisionError(FleetModelInventoryError):
    """Raised when an observation id collides with different measurement data."""


class InventorySourceRejectedError(FleetModelInventoryError):
    """Raised when trusted_source does not match payload source or is not allowed."""


@dataclass(frozen=True)
class ModelRecord:
    model_id: str
    native_id: str
    state: str = "available"  # "available" | "retired" | "blocked"
    display_name: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "model_id": self.model_id,
            "native_id": self.native_id,
            "state": self.state,
            "display_name": self.display_name,
            "description": self.description,
        }


@dataclass(frozen=True)
class ProbeResult:
    returncode: int
    stdout: str
    error: str | None = None


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS fleet_model_inventory_observations (
        observation_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        provider TEXT NOT NULL,
        harness TEXT NOT NULL DEFAULT '',
        account_id TEXT NOT NULL DEFAULT '',
        scope TEXT NOT NULL,
        status TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        models_json TEXT NOT NULL,
        error_reason TEXT,
        evidence_type TEXT NOT NULL DEFAULT 'cli_probe',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_fmi_obs_coverage ON fleet_model_inventory_observations (
        provider, harness, account_id, captured_at
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fleet_model_inventory_projections (
        provider TEXT NOT NULL,
        harness TEXT NOT NULL DEFAULT '',
        account_id TEXT NOT NULL DEFAULT '',
        observation_id TEXT NOT NULL,
        source TEXT NOT NULL,
        scope TEXT NOT NULL,
        status TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        models_json TEXT NOT NULL,
        error_reason TEXT,
        evidence_type TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (provider, harness, account_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fleet_model_inventory_aliases (
        provider TEXT NOT NULL,
        canonical_model TEXT NOT NULL,
        native_model TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (provider, canonical_model)
    )
    """,
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create additive tables and indexes without committing outer transactions."""
    for statement in _SCHEMA_STATEMENTS:
        conn.execute(statement)


def _check_no_credential_urls(text: str, field_name: str) -> None:
    if "://" in text:
        parts = text.split()
        for part in parts:
            if "://" in part:
                parsed = urlsplit(part)
                if parsed.username or parsed.password or ("@" in parsed.netloc):
                    raise InventoryValidationError("credential-bearing URL detected")


def _validate_safe_string(value: Any, name: str, *, max_length: int = MAX_STRING_LENGTH) -> str:
    if not isinstance(value, str):
        raise InventoryValidationError(f"{name} must be a string")
    if len(value) > max_length:
        raise InventoryValidationError(f"{name} exceeds maximum length")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
        raise InventoryValidationError(f"control character in {name}")
    _check_no_credential_urls(value, name)
    return value


def _validate_strict_json(obj: Any, _label: str = "payload", *, depth: int = 0, nodes: list[int] | None = None) -> None:
    """Enforce bounded strict JSON tree with generic errors that omit map keys and values."""
    counter = nodes if nodes is not None else [0]
    counter[0] += 1
    if counter[0] > MAX_JSON_NODES:
        raise InventoryValidationError("json tree exceeds maximum node count")
    if depth > MAX_JSON_DEPTH:
        raise InventoryValidationError("json tree exceeds maximum depth")
    if obj is None or isinstance(obj, bool) or isinstance(obj, int):
        return
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise InventoryValidationError("nonfinite float in json tree")
        return
    if isinstance(obj, str):
        if len(obj) > MAX_STRING_LENGTH:
            raise InventoryValidationError("string exceeds maximum length in json tree")
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", obj):
            raise InventoryValidationError("control character in json tree")
        _check_no_credential_urls(obj, "json")
        return
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise InventoryValidationError("non-string dictionary key in json tree")
            if len(k) > MAX_STRING_LENGTH:
                raise InventoryValidationError("dictionary key exceeds maximum length in json tree")
            if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", k):
                raise InventoryValidationError("control character in json tree")
            _validate_strict_json(v, _label, depth=depth + 1, nodes=counter)
        return
    if type(obj) is list:
        for item in obj:
            _validate_strict_json(item, _label, depth=depth + 1, nodes=counter)
        return
    raise InventoryValidationError("unsupported object type in json tree")


def _parse_iso_timestamp(ts: Any, field_name: str) -> datetime:
    """Parse ISO 8601 string, rejecting naive timestamps and never echoing raw inputs on error."""
    if not isinstance(ts, str) or not ts.strip():
        raise InventoryValidationError(f"{field_name} must be a non-empty ISO 8601 string")
    clean = ts.strip()
    has_tz = clean.endswith("Z") or bool(re.search(r"[+-]\d{2}(?::?\d{2})?$", clean))
    if not has_tz:
        raise InventoryValidationError(
            f"{field_name} must include explicit timezone offset (naive timestamps rejected)"
        )
    if clean.endswith("Z"):
        clean = clean[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(clean)
    except ValueError:
        raise InventoryValidationError(f"{field_name} is malformed ISO 8601 timestamp") from None
    if dt.tzinfo is None:
        raise InventoryValidationError(
            f"{field_name} must include explicit timezone offset (naive timestamps rejected)"
        )
    return dt.astimezone(timezone.utc)


def _format_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_now(now: str | datetime | float | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, bool):
        raise InventoryValidationError("now must be a valid timestamp")
    if isinstance(now, (int, float)):
        if math.isnan(now) or math.isinf(now):
            raise InventoryValidationError("nonfinite timestamp")
        return datetime.fromtimestamp(now, tz=timezone.utc)
    if isinstance(now, datetime):
        if now.tzinfo is None:
            raise InventoryValidationError("now must include explicit timezone offset (naive datetime rejected)")
        return now.astimezone(timezone.utc)
    return _parse_iso_timestamp(now, "now")


def _sanitize_error_reason(reason: Any) -> str:
    """Restrict error reasons strictly to known closed codes; never leak raw text or secrets."""
    if not isinstance(reason, str) or not reason.strip():
        return "probe-failed"
    clean = reason.strip()
    if clean in KNOWN_ERROR_CODES:
        return clean
    if re.fullmatch(r"probe-exited-\d+", clean):
        return clean
    return "provider-error"


def _parse_coverage_timestamp(ts: Any) -> datetime | None:
    """Parse a coverage timestamp, returning None for missing/malformed/naive values."""
    try:
        return _parse_iso_timestamp(ts, "timestamp")
    except InventoryValidationError:
        return None


def _evaluate_coverage_freshness(
    cov: Mapping[str, Any],
    now_dt: datetime,
) -> tuple[InventoryState, str | None]:
    """Evaluate coverage freshness at `now_dt`. Missing or invalid timestamps do not admit."""
    cov_status = cov.get("status")
    cov_scope = cov.get("scope", "complete")
    error_reason = cov.get("error_reason")
    if not isinstance(error_reason, str) or not error_reason.strip():
        error_reason = cov.get("reason")

    if cov_status == "error":
        reason = error_reason if isinstance(error_reason, str) and error_reason.strip() else "probe-failed"
        return "unavailable", _sanitize_error_reason(reason)
    if cov_status == "conflict":
        return "unknown", "same-instant-conflict"

    cap_dt = _parse_coverage_timestamp(cov.get("captured_at"))
    exp_dt = _parse_coverage_timestamp(cov.get("expires_at"))
    if cap_dt is None or exp_dt is None:
        return "unavailable", "inventory-unavailable"
    if (cap_dt - now_dt).total_seconds() > DEFAULT_MAX_CLOCK_SKEW_SECONDS:
        return "unavailable", "future-timestamp-beyond-skew"
    if now_dt >= exp_dt:
        return "stale", "inventory-expired"
    if cov_scope == "partial":
        return "fresh", "partial-inventory"
    return "fresh", None


def _provider_aliases(
    aliases: Mapping[str, Any] | None,
    provider: str,
    *,
    harness: str | None = None,
    allow_flat: bool = False,
) -> dict[str, str]:
    """Resolve explicit operator aliases for one provider.

    Multi-provider projections must pass allow_flat=False so a flat
    {canonical: native} map cannot bless another provider. classify_exact_identity
    may pass allow_flat=True only because that call already selected one provider.
    Stored aliases are keyed (provider, canonical) only and never select a
    different account or harness coverage.
    """
    if not aliases:
        return {}
    raw = aliases.get(provider) if provider in aliases else None
    if isinstance(raw, Mapping):
        if harness:
            nested = raw.get(harness)
            if isinstance(nested, Mapping):
                return {k: v for k, v in nested.items() if isinstance(k, str) and isinstance(v, str)}
        return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
    if allow_flat and raw is None and not any(isinstance(v, Mapping) for v in aliases.values()):
        return {k: v for k, v in aliases.items() if isinstance(k, str) and isinstance(v, str)}
    return {}


def _apply_canonical_aliases(names: list[str], aliases: Mapping[str, str]) -> list[str]:
    """Add canonical names whose native identity is already present. No string guessing."""
    present = set(names)
    out = list(names)
    for canonical, native in aliases.items():
        if native in present and canonical not in present:
            out.append(canonical)
            present.add(canonical)
    return out


def set_alias(
    conn: sqlite3.Connection,
    provider: str,
    canonical_model: str,
    native_model: str,
    *,
    now: str | datetime | float | None = None,
) -> None:
    """Store an explicit operator alias mapping canonical seat model to native model."""
    _validate_safe_string(provider, "provider")
    _validate_safe_string(canonical_model, "canonical_model")
    _validate_safe_string(native_model, "native_model")
    created_at = _format_iso(_normalize_now(now))
    conn.execute("SAVEPOINT fmi_set_alias")
    try:
        conn.execute(
            """
            INSERT INTO fleet_model_inventory_aliases (provider, canonical_model, native_model, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(provider, canonical_model) DO UPDATE SET
                native_model = excluded.native_model,
                created_at = excluded.created_at
            """,
            (provider.strip(), canonical_model.strip(), native_model.strip(), created_at),
        )
        conn.execute("RELEASE SAVEPOINT fmi_set_alias")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT fmi_set_alias")
        conn.execute("RELEASE SAVEPOINT fmi_set_alias")
        raise


def get_aliases(conn: sqlite3.Connection, provider: str | None = None) -> dict[str, str]:
    """Retrieve explicit operator aliases for a provider or all providers."""
    if provider is not None:
        rows = conn.execute(
            "SELECT canonical_model, native_model FROM fleet_model_inventory_aliases WHERE provider = ?",
            (provider,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT canonical_model, native_model FROM fleet_model_inventory_aliases").fetchall()
    return {row[0]: row[1] for row in rows}


def ingest(
    conn: sqlite3.Connection,
    payload: Mapping[str, Any],
    *,
    trusted_source: str,
    allowed_sources: Collection[str] | None = None,
    now: str | datetime | float | None = None,
    max_clock_skew: float = DEFAULT_MAX_CLOCK_SKEW_SECONDS,
    max_ttl_seconds: float = DEFAULT_MAX_TTL_SECONDS,
) -> dict[str, Any]:
    """Ingest a sanitized model inventory observation payload with strict validation.

    Enforces:
    - Bounded payload size and strict JSON tree validation.
    - Allowed fieldset and refusal of unknown keys.
    - Trusted source provenance verification and evidence type honesty.
    - Timestamp validation: rejects naive timestamps, bounds future clock skew, checks TTL.
    - Model list: exact list type, bounds count, refuses duplicate model IDs.
    - Deterministic observation ID and complete collision detection comparing stored fields.
    - Same-instant conflict stickiness under replay until strictly newer observation.
    - Atomic rollback on injected write failures.
    """
    if not isinstance(payload, Mapping):
        raise InventoryValidationError("payload must be a JSON object mapping")

    # Strict JSON tree validation
    _validate_strict_json(payload)

    # Reject unknown keys
    unknown_keys = set(payload.keys()) - ALLOWED_PAYLOAD_KEYS
    if unknown_keys:
        raise InventoryValidationError("unknown keys in payload")

    raw_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    if len(raw_bytes) > MAX_PAYLOAD_BYTES:
        raise InventoryValidationError(f"payload size {len(raw_bytes)} bytes exceeds limit {MAX_PAYLOAD_BYTES}")

    allowed = allowed_sources if allowed_sources is not None else DEFAULT_ALLOWED_SOURCES
    if trusted_source not in allowed:
        raise InventorySourceRejectedError("trusted_source is not in allowed sources")

    claimed_source = payload.get("source")
    if claimed_source != trusted_source:
        raise InventorySourceRejectedError("payload source does not match caller trusted_source")

    schema = payload.get("schema")
    if schema != PAYLOAD_SCHEMA:
        raise InventoryValidationError("invalid payload schema")

    provider = _validate_safe_string(payload.get("provider"), "provider").strip()
    if not provider:
        raise InventoryValidationError("provider must be a non-empty string")

    harness = payload.get("harness") or ""
    if not isinstance(harness, str):
        raise InventoryValidationError("harness must be a string")
    harness = _validate_safe_string(harness, "harness").strip()

    account_id = payload.get("account_id") or ""
    if not isinstance(account_id, str):
        raise InventoryValidationError("account_id must be a string")
    account_id = _validate_safe_string(account_id, "account_id").strip()

    scope = payload.get("scope")
    if scope not in ("complete", "partial"):
        raise InventoryValidationError("scope must be either 'complete' or 'partial'")

    status = payload.get("status")
    if status not in ("ok", "error"):
        raise InventoryValidationError("status must be either 'ok' or 'error'")

    now_dt = _normalize_now(now)
    captured_at_dt = _parse_iso_timestamp(payload.get("captured_at"), "captured_at")
    expires_at_dt = _parse_iso_timestamp(payload.get("expires_at"), "expires_at")

    # Clock skew and TTL bounds
    if (captured_at_dt - now_dt).total_seconds() > max_clock_skew:
        raise InventoryValidationError("captured_at cannot be in the future beyond allowed clock skew")

    if expires_at_dt < captured_at_dt:
        raise InventoryValidationError("expires_at must be greater than or equal to captured_at")

    if (expires_at_dt - captured_at_dt).total_seconds() > max_ttl_seconds:
        raise InventoryValidationError("expires_at exceeds maximum allowed TTL")

    captured_at_iso = _format_iso(captured_at_dt)
    expires_at_iso = _format_iso(expires_at_dt)

    # Source provenance is trusted caller config + exact kind, not payload
    if trusted_source == "manual:browser":
        expected_evidence_type = "manual_browser"
    elif trusted_source == "cli:opencode":
        expected_evidence_type = "catalog"
    else:
        expected_evidence_type = "cli_probe"

    claimed_evidence_type = payload.get("evidence_type")
    if claimed_evidence_type is not None:
        if not isinstance(claimed_evidence_type, str):
            raise InventoryValidationError("evidence_type must be a string")
        if claimed_evidence_type != expected_evidence_type:
            raise InventoryValidationError("evidence_type is not permitted for source")
    evidence_type = expected_evidence_type

    canonical_models: list[dict[str, str]] = []
    error_reason: str | None = None

    if status == "ok":
        models_raw = payload.get("models")
        if type(models_raw) is not list:
            raise InventoryValidationError("models must be a list for status 'ok'")
        if len(models_raw) > MAX_MODELS_COUNT:
            raise InventoryValidationError(f"models count exceeds limit {MAX_MODELS_COUNT}")

        seen_model_ids: set[str] = set()
        for idx, item in enumerate(models_raw):
            if isinstance(item, str):
                model_str = _validate_safe_string(item, f"models[{idx}]").strip()
                if not model_str:
                    continue
                if model_str in seen_model_ids:
                    raise InventoryValidationError("duplicate model id in models list")
                seen_model_ids.add(model_str)
                canonical_models.append(
                    {
                        "model_id": model_str,
                        "native_id": model_str,
                        "state": "available",
                        "display_name": model_str,
                        "description": "",
                    }
                )
            elif isinstance(item, Mapping):
                # Check for unknown keys in model object
                model_unknown_keys = set(item.keys()) - ALLOWED_MODEL_KEYS
                if model_unknown_keys:
                    raise InventoryValidationError(f"unknown keys in models[{idx}] record")

                mid = _validate_safe_string(item.get("model_id"), f"models[{idx}].model_id").strip()
                if not mid:
                    raise InventoryValidationError(f"models[{idx}].model_id cannot be empty")
                if mid in seen_model_ids:
                    raise InventoryValidationError("duplicate model id in models list")
                seen_model_ids.add(mid)

                nid = _validate_safe_string(item.get("native_id") or mid, f"models[{idx}].native_id").strip()
                mstate = item.get("state") or "available"
                if mstate not in ("available", "retired", "blocked"):
                    raise InventoryValidationError(f"invalid state at models[{idx}].state")
                dname = _validate_safe_string(item.get("display_name") or mid, f"models[{idx}].display_name").strip()
                desc = _validate_safe_string(item.get("description") or "", f"models[{idx}].description").strip()
                canonical_models.append(
                    {
                        "model_id": mid,
                        "native_id": nid,
                        "state": mstate,
                        "display_name": dname,
                        "description": desc,
                    }
                )
            else:
                raise InventoryValidationError(f"models[{idx}] must be a string or mapping")
        canonical_models.sort(key=lambda m: (m["model_id"], m["native_id"]))
    else:
        error_reason = _sanitize_error_reason(payload.get("error_reason"))

    measurement = {
        "account_id": account_id,
        "captured_at": captured_at_iso,
        "error_reason": error_reason,
        "evidence_type": evidence_type,
        "expires_at": expires_at_iso,
        "harness": harness,
        "models": canonical_models,
        "provider": provider,
        "schema": PAYLOAD_SCHEMA,
        "scope": scope,
        "source": trusted_source,
        "status": status,
    }

    measurement_canonical_bytes = json.dumps(measurement, sort_keys=True, separators=(",", ":")).encode("utf-8")
    observation_id = hashlib.sha256(measurement_canonical_bytes).hexdigest()
    models_json = json.dumps(canonical_models, sort_keys=True)
    created_at_iso = _format_iso(datetime.now(timezone.utc))

    conn.execute("SAVEPOINT fmi_ingest")
    try:
        existing_obs = conn.execute(
            """
            SELECT observation_id, source, provider, harness, account_id,
                   scope, status, captured_at, expires_at, models_json,
                   error_reason, evidence_type
            FROM fleet_model_inventory_observations
            WHERE observation_id = ?
            """,
            (observation_id,),
        ).fetchone()

        if existing_obs is not None:
            # ObservationID must compare ALL stored measurement fields on collision
            if (
                existing_obs[1] != trusted_source
                or existing_obs[2] != provider
                or existing_obs[3] != harness
                or existing_obs[4] != account_id
                or existing_obs[5] != scope
                or existing_obs[6] != status
                or existing_obs[7] != captured_at_iso
                or existing_obs[8] != expires_at_iso
                or existing_obs[9] != models_json
                or existing_obs[10] != error_reason
                or existing_obs[11] != evidence_type
            ):
                raise InventoryCollisionError(f"observation hash collision detected on id {observation_id}")
        else:
            conn.execute(
                """
                INSERT INTO fleet_model_inventory_observations (
                    observation_id, source, provider, harness, account_id,
                    scope, status, captured_at, expires_at, models_json,
                    error_reason, evidence_type, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_id,
                    trusted_source,
                    provider,
                    harness,
                    account_id,
                    scope,
                    status,
                    captured_at_iso,
                    expires_at_iso,
                    models_json,
                    error_reason,
                    evidence_type,
                    created_at_iso,
                ),
            )

        cur_proj = conn.execute(
            """
            SELECT observation_id, captured_at, status, models_json, error_reason, scope
            FROM fleet_model_inventory_projections
            WHERE provider = ? AND harness = ? AND account_id = ?
            """,
            (provider, harness, account_id),
        ).fetchone()

        projection_action = "none"

        if cur_proj is None:
            conn.execute(
                """
                INSERT INTO fleet_model_inventory_projections (
                    provider, harness, account_id, observation_id, source,
                    scope, status, captured_at, expires_at, models_json,
                    error_reason, evidence_type, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provider,
                    harness,
                    account_id,
                    observation_id,
                    trusted_source,
                    scope,
                    status,
                    captured_at_iso,
                    expires_at_iso,
                    models_json,
                    error_reason,
                    evidence_type,
                    created_at_iso,
                ),
            )
            projection_action = "created"
        else:
            cur_obs_id, cur_cap_str, cur_status, cur_models_json, cur_error, _ = cur_proj
            cur_cap_dt = _parse_iso_timestamp(cur_cap_str, "current projection captured_at")

            if cur_status == "conflict":
                # Same-instant conflict remains sticky under replay of either input until strictly new observation
                if captured_at_dt <= cur_cap_dt:
                    projection_action = "conflict_marked_unknown"
                else:
                    conn.execute(
                        """
                        UPDATE fleet_model_inventory_projections
                        SET observation_id = ?,
                            source = ?,
                            scope = ?,
                            status = ?,
                            captured_at = ?,
                            expires_at = ?,
                            models_json = ?,
                            error_reason = ?,
                            evidence_type = ?,
                            updated_at = ?
                        WHERE provider = ? AND harness = ? AND account_id = ?
                        """,
                        (
                            observation_id,
                            trusted_source,
                            scope,
                            status,
                            captured_at_iso,
                            expires_at_iso,
                            models_json,
                            error_reason,
                            evidence_type,
                            created_at_iso,
                            provider,
                            harness,
                            account_id,
                        ),
                    )
                    projection_action = "superseded"
            elif captured_at_dt < cur_cap_dt:
                projection_action = "ignored_older_snapshot"
            elif captured_at_dt == cur_cap_dt:
                if observation_id == cur_obs_id:
                    projection_action = "idempotent"
                else:
                    conn.execute(
                        """
                        UPDATE fleet_model_inventory_projections
                        SET status = 'conflict',
                            error_reason = 'same-instant-conflict',
                            models_json = '[]',
                            updated_at = ?
                        WHERE provider = ? AND harness = ? AND account_id = ?
                        """,
                        (created_at_iso, provider, harness, account_id),
                    )
                    projection_action = "conflict_marked_unknown"
            else:
                conn.execute(
                    """
                    UPDATE fleet_model_inventory_projections
                    SET observation_id = ?,
                        source = ?,
                        scope = ?,
                        status = ?,
                        captured_at = ?,
                        expires_at = ?,
                        models_json = ?,
                        error_reason = ?,
                        evidence_type = ?,
                        updated_at = ?
                    WHERE provider = ? AND harness = ? AND account_id = ?
                    """,
                    (
                        observation_id,
                        trusted_source,
                        scope,
                        status,
                        captured_at_iso,
                        expires_at_iso,
                        models_json,
                        error_reason,
                        evidence_type,
                        created_at_iso,
                        provider,
                        harness,
                        account_id,
                    ),
                )
                projection_action = "superseded"
        conn.execute("RELEASE SAVEPOINT fmi_ingest")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT fmi_ingest")
        conn.execute("RELEASE SAVEPOINT fmi_ingest")
        raise

    return {
        "observation_id": observation_id,
        "provider": provider,
        "harness": harness,
        "account_id": account_id,
        "status": status,
        "projection_action": projection_action,
    }


def snapshot(
    conn: sqlite3.Connection,
    *,
    now: str | datetime | float | None = None,
    provider: str | None = None,
    harness: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Return a projection snapshot at time `now`.

    Computes exact fresh/stale/unavailable/unknown states per coverage.
    Expiry boundary: now >= expires_at is evaluated as stale.
    Without explicit scope, only a single unambiguous coverage may be projected per provider.
    Multiple coverages cannot autoselect favorable data.
    """
    now_dt = _normalize_now(now)
    now_iso = _format_iso(now_dt)

    query = """
    SELECT provider, harness, account_id, observation_id, source,
           scope, status, captured_at, expires_at, models_json,
           error_reason, evidence_type, updated_at
    FROM fleet_model_inventory_projections
    WHERE 1=1
    """
    params: list[Any] = []
    if provider is not None:
        query += " AND provider = ?"
        params.append(provider.strip())
    if harness is not None:
        query += " AND harness = ?"
        params.append(harness.strip())
    if account_id is not None:
        query += " AND account_id = ?"
        params.append(account_id.strip())

    query += " ORDER BY provider ASC, harness ASC, account_id ASC"

    rows = conn.execute(query, params).fetchall()

    coverages: list[dict[str, Any]] = []
    provider_coverages: dict[str, list[dict[str, Any]]] = {}

    for row in rows:
        (
            r_prov,
            r_harn,
            r_acct,
            r_obs_id,
            r_src,
            r_scope,
            r_status,
            r_cap,
            r_exp,
            r_models_json,
            r_err,
            r_ev_type,
            _,
        ) = row

        models_list: list[dict[str, Any]] = json.loads(r_models_json)

        available_ids: list[str] = []
        retired_ids: list[str] = []
        blocked_ids: list[str] = []

        for m in models_list:
            mid = m["model_id"]
            mstate = m.get("state", "available")
            if mstate == "available":
                available_ids.append(mid)
            elif mstate == "retired":
                retired_ids.append(mid)
            elif mstate == "blocked":
                blocked_ids.append(mid)

        state, reason = _evaluate_coverage_freshness(
            {
                "status": r_status,
                "scope": r_scope,
                "captured_at": r_cap,
                "expires_at": r_exp,
                "error_reason": r_err,
            },
            now_dt,
        )

        if state in ("unavailable", "unknown", "stale"):
            available_ids = []
            retired_ids = []
            blocked_ids = []

        cov_record = {
            "provider": r_prov,
            "harness": r_harn,
            "account_id": r_acct,
            "observation_id": r_obs_id,
            "source": r_src,
            "scope": r_scope,
            "status": r_status,
            "state": state,
            "reason": reason,
            "captured_at": r_cap,
            "expires_at": r_exp,
            "evidence_type": r_ev_type,
            "available": available_ids,
            "retired": retired_ids,
            "blocked": blocked_ids,
            "models": models_list,
        }
        coverages.append(cov_record)
        provider_coverages.setdefault(r_prov, []).append(cov_record)

    providers: dict[str, dict[str, Any]] = {}
    for prov, cov_list in provider_coverages.items():
        if len(cov_list) == 1:
            providers[prov] = cov_list[0]
        else:
            # Ambiguous: multiple coverages exist and cannot autoselect favorable data
            providers[prov] = {
                "provider": prov,
                "harness": "",
                "account_id": "",
                "state": "unavailable",
                "reason": "ambiguous-provider-coverage",
                "scope": "complete",
                "status": "error",
                "available": [],
                "retired": [],
                "blocked": [],
                "models": [],
            }

    return {
        "schema": SNAPSHOT_SCHEMA,
        "generated_at": now_iso,
        "coverages": coverages,
        "providers": providers,
    }


def _make_classification_result(
    model: str,
    effective_model: str,
    provider: str,
    state: InventoryState,
    *,
    reason: str | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    res: dict[str, Any] = {
        "seat_model": model,
        "effective_model": effective_model,
        "provider": provider,
        "state": state,
    }
    if reason is not None:
        res["reason"] = reason
    if scope is not None:
        res["scope"] = scope
    return res


def classify_exact_identity(
    source: sqlite3.Connection | Mapping[str, Any],
    *,
    provider: str,
    model: str,
    now: str | datetime | float | None = None,
    aliases: Mapping[str, Any] | None = None,
    harness: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Classify a requested seat model against verified inventory.

    Supports explicit operator alias mappings scoped by provider.
    Never guesses or alters strings. Models are marked missing only when a
    healthy, fresh, complete inventory snapshot is present.
    Enforces captured/generated/expiry timestamps against actual now.

    A flat {canonical: native} alias map is accepted here only because this
    call already selected one provider. Multi-provider projections must use
    explicit provider-scoped aliases; see to_fleet_policy_inventory.
    """
    provider = provider.strip()
    model = model.strip()
    now_dt = _normalize_now(now)

    prov_aliases: dict[str, str] = {}
    if aliases:
        prov_aliases = _provider_aliases(aliases, provider, harness=harness, allow_flat=True)
    elif isinstance(source, sqlite3.Connection):
        prov_aliases = get_aliases(source, provider)

    effective_model = prov_aliases.get(model, model)

    cov: Mapping[str, Any] | None = None

    if isinstance(source, sqlite3.Connection):
        snap = snapshot(source, now=now_dt, provider=provider, harness=harness, account_id=account_id)
        cov = snap.get("providers", {}).get(provider)
        if not isinstance(cov, Mapping):
            reason = (
                "coverage-not-found" if (harness is not None or account_id is not None) else "provider-not-inventoried"
            )
            return _make_classification_result(model, effective_model, provider, "unavailable", reason=reason)
    elif isinstance(source, Mapping):
        _validate_strict_json(source, "snapshot")
        gen_at = source.get("generated_at")
        if gen_at is not None:
            gen_dt = _parse_iso_timestamp(gen_at, "generated_at")
            if (gen_dt - now_dt).total_seconds() > DEFAULT_MAX_CLOCK_SKEW_SECONDS:
                return _make_classification_result(
                    model, effective_model, provider, "unavailable", reason="future-timestamp-beyond-skew"
                )

        coverages = source.get("coverages", [])
        matching_coverages = [
            c
            for c in coverages
            if isinstance(c, Mapping)
            and c.get("provider") == provider
            and (harness is None or c.get("harness") == harness)
            and (account_id is None or c.get("account_id") == account_id)
        ]

        if len(matching_coverages) == 1:
            cov = matching_coverages[0]
        elif len(matching_coverages) > 1:
            return _make_classification_result(
                model, effective_model, provider, "unavailable", reason="ambiguous-provider-coverage"
            )
        else:
            if harness is not None or account_id is not None:
                return _make_classification_result(
                    model, effective_model, provider, "unavailable", reason="coverage-not-found"
                )
            cov = source.get("providers", {}).get(provider)
            if not isinstance(cov, Mapping):
                return _make_classification_result(
                    model, effective_model, provider, "unavailable", reason="provider-not-inventoried"
                )
    else:
        raise InventoryValidationError("source must be a sqlite3.Connection or snapshot mapping")

    cov_scope = cov.get("scope", "complete")
    cov_state, cov_reason = _evaluate_coverage_freshness(cov, now_dt)

    if cov_state == "unavailable":
        return _make_classification_result(
            model, effective_model, provider, "unavailable", reason=cov_reason or "probe-failed"
        )
    if cov_state == "unknown":
        return _make_classification_result(model, effective_model, provider, "unavailable", reason="inventory-conflict")
    if cov_state == "stale":
        return _make_classification_result(model, effective_model, provider, "unavailable", reason="inventory-expired")

    available_set = set(cov.get("available") or [])
    retired_set = set(cov.get("retired") or [])
    blocked_set = set(cov.get("blocked") or [])

    if cov_scope == "partial":
        if effective_model in blocked_set:
            return _make_classification_result(model, effective_model, provider, "policy-blocked", scope="partial")
        if effective_model in retired_set:
            return _make_classification_result(model, effective_model, provider, "retired", scope="partial")
        if effective_model in available_set:
            return _make_classification_result(model, effective_model, provider, "available", scope="partial")
        return _make_classification_result(
            model, effective_model, provider, "unavailable", reason="uninventoried-in-partial-snapshot", scope="partial"
        )

    if effective_model in blocked_set:
        return _make_classification_result(model, effective_model, provider, "policy-blocked")
    if effective_model in retired_set:
        return _make_classification_result(model, effective_model, provider, "retired")
    if effective_model in available_set:
        return _make_classification_result(model, effective_model, provider, "available")

    return _make_classification_result(model, effective_model, provider, "missing")


def to_fleet_policy_inventory(
    conn_or_snapshot: sqlite3.Connection | Mapping[str, Any],
    *,
    now: str | datetime | float | None = None,
    aliases: Mapping[str, Any] | None = None,
    bindings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Expose current inventory projection in the shape consumed by fleet_policy.validate_inventory.

    Accepts explicit provider->(harness, account_id) bindings or marks ambiguous multiple coverages unavailable.
    Reevaluates captured_at/expires_at at `now` even when a mapping snapshot still says state=fresh.
    Aliases are explicit and provider-scoped. A flat {canonical: native} map is not applied across
    providers; classify_exact_identity may accept that flat form only because the provider is already
    selected. Stored aliases are keyed (provider, canonical) and never select another account or harness.
    Never claims 'fresh' when coverage is stale, partial, ambiguous, or unavailable.
    """
    now_dt = _normalize_now(now)
    snap: Mapping[str, Any]
    if isinstance(conn_or_snapshot, sqlite3.Connection):
        snap = snapshot(conn_or_snapshot, now=now_dt)
    else:
        snap = conn_or_snapshot

    generated_block: str | None = None
    gen_at = snap.get("generated_at") if isinstance(snap, Mapping) else None
    if gen_at is not None:
        gen_dt = _parse_coverage_timestamp(gen_at)
        if gen_dt is None:
            generated_block = "inventory-unavailable"
        elif (gen_dt - now_dt).total_seconds() > DEFAULT_MAX_CLOCK_SKEW_SECONDS:
            generated_block = "future-timestamp-beyond-skew"

    coverages = snap.get("coverages", []) if isinstance(snap, Mapping) else []
    by_provider: dict[str, list[dict[str, Any]]] = {}
    for c in coverages:
        if isinstance(c, Mapping) and c.get("provider"):
            by_provider.setdefault(str(c["provider"]), []).append(dict(c))

    result: dict[str, Any] = {}

    def _unavailable(reason: str) -> dict[str, Any]:
        return {
            "state": "unavailable",
            "reason": reason,
            "available": [],
            "retired": [],
            "blocked": [],
        }

    for provider, cov_list in by_provider.items():
        if generated_block is not None:
            result[provider] = _unavailable(generated_block)
            continue

        cov: dict[str, Any] | None = None
        if bindings and provider in bindings:
            binding = bindings[provider]
            b_harn: str | None = None
            b_acct: str | None = None
            if isinstance(binding, Mapping):
                b_harn = binding.get("harness")
                b_acct = binding.get("account_id")
            elif isinstance(binding, (tuple, list)):
                b_harn = binding[0] if len(binding) > 0 else None
                b_acct = binding[1] if len(binding) > 1 else None
            elif isinstance(binding, str):
                b_harn = binding

            matching = [
                c
                for c in cov_list
                if (b_harn is None or c.get("harness") == b_harn) and (b_acct is None or c.get("account_id") == b_acct)
            ]
            if len(matching) == 1:
                cov = matching[0]
            elif len(matching) > 1:
                result[provider] = _unavailable("ambiguous-provider-coverage")
                continue
            else:
                result[provider] = _unavailable("coverage-not-found")
                continue
        else:
            if len(cov_list) == 1:
                cov = cov_list[0]
            else:
                result[provider] = _unavailable("ambiguous-provider-coverage")
                continue

        state, reason = _evaluate_coverage_freshness(cov, now_dt)
        scope = cov.get("scope", "complete")
        available = list(cov.get("available") or [])
        retired = list(cov.get("retired") or [])
        blocked = list(cov.get("blocked") or [])

        prov_aliases: dict[str, str] = {}
        if aliases:
            prov_aliases = _provider_aliases(aliases, provider, harness=cov.get("harness"), allow_flat=False)
        elif isinstance(conn_or_snapshot, sqlite3.Connection):
            prov_aliases = get_aliases(conn_or_snapshot, provider)

        available = _apply_canonical_aliases(available, prov_aliases)
        retired = _apply_canonical_aliases(retired, prov_aliases)
        blocked = _apply_canonical_aliases(blocked, prov_aliases)

        if state == "fresh" and scope == "complete":
            result[provider] = {
                "state": "fresh",
                "available": available,
                "retired": retired,
                "blocked": blocked,
            }
        elif state == "stale":
            result[provider] = _unavailable("inventory-expired")
        elif scope == "partial":
            result[provider] = {
                "state": "unavailable",
                "reason": "partial-inventory-cannot-prove-missing",
                "available": available,
                "retired": retired,
                "blocked": blocked,
            }
        else:
            result[provider] = _unavailable(reason or "inventory-unavailable")

    return result


def parse_agy_models(output: str) -> list[ModelRecord]:
    """Parse output from `agy models` (tab-separated ID and display text).

    Strips known progress preambles if present.
    If output is empty or contains unknown/malformed lines, raises InventoryValidationError.
    """
    if not isinstance(output, str):
        raise InventoryValidationError("output must be a string")
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise InventoryValidationError("unsupported output: empty output from agy models")

    records: list[ModelRecord] = []
    seen_model_ids: set[str] = set()

    for idx, line in enumerate(lines):
        if KNOWN_AGY_PROGRESS_PREAMBLE_RE.match(line):
            continue
        if "\t" not in line:
            raise InventoryValidationError(f"unsupported output: line {idx} is not tab-separated")
        parts = line.split("\t")
        model_id = parts[0].strip()
        display_name = parts[1].strip() if len(parts) > 1 else model_id
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]*", model_id):
            raise InventoryValidationError("unsupported output: invalid model id format")
        if model_id in seen_model_ids:
            raise InventoryValidationError("duplicate model id in agy models output")
        seen_model_ids.add(model_id)
        records.append(
            ModelRecord(
                model_id=model_id,
                native_id=model_id,
                state="available",
                display_name=display_name,
            )
        )

    if not records:
        raise InventoryValidationError("unsupported output: no model records parsed from agy models")

    return records


def parse_opencode_models(output: str) -> list[ModelRecord]:
    """Parse output from `opencode models` (linewise exact provider/model IDs).

    Retains full provider/model ID in native_id for native launch.
    """
    if not isinstance(output, str):
        raise InventoryValidationError("output must be a string")
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise InventoryValidationError("unsupported output: empty output from opencode models")

    records: list[ModelRecord] = []
    seen_model_ids: set[str] = set()

    for idx, line in enumerate(lines):
        match = re.fullmatch(r"([a-zA-Z0-9_-]+)/([a-zA-Z0-9._:-]+)", line)
        if match is None:
            raise InventoryValidationError(
                f"unsupported output: line {idx} is not a valid provider/model: invalid shape"
            )
        full_id = line
        if full_id in seen_model_ids:
            raise InventoryValidationError("duplicate model id in opencode models output")
        seen_model_ids.add(full_id)
        records.append(
            ModelRecord(
                model_id=full_id,
                native_id=full_id,
                state="available",
                display_name=full_id,
            )
        )
    return records


def parse_cursor_agent_models(output: str) -> list[ModelRecord]:
    """Parse output from `cursor-agent models`."""
    if not isinstance(output, str):
        raise InventoryValidationError("output must be a string")
    lines = output.splitlines()

    header_found = False
    tip_found = False
    records: list[ModelRecord] = []
    seen_model_ids: set[str] = set()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "Available models":
            header_found = True
            continue
        if not header_found:
            continue
        if stripped.startswith("Tip: use --model <id>"):
            tip_found = True
            break
        match = re.match(r"^([a-zA-Z0-9][a-zA-Z0-9._:/\[\],=-]*)\s+-\s+(.+)$", stripped)
        if match is None:
            raise InventoryValidationError("unsupported output shape for cursor-agent models")
        mid = match.group(1).strip()
        desc = match.group(2).strip()
        if mid in seen_model_ids:
            raise InventoryValidationError("duplicate model id in cursor-agent models output")
        seen_model_ids.add(mid)
        records.append(
            ModelRecord(
                model_id=mid,
                native_id=mid,
                state="available",
                display_name=desc,
                description=desc,
            )
        )

    if not header_found or not tip_found or not records:
        raise InventoryValidationError("unsupported output shape for cursor-agent models")

    return records


def parse_grok_models(output: str) -> list[ModelRecord]:
    """Parse output from `grok models`."""
    if not isinstance(output, str):
        raise InventoryValidationError("output must be a string")
    lines = output.splitlines()
    header_index: int | None = None
    for index, line in enumerate(lines):
        if line.strip() == "Available models:":
            header_index = index
            break
    if header_index is None:
        raise InventoryValidationError("unsupported output: missing 'Available models:' header in grok models")

    records: list[ModelRecord] = []
    seen_model_ids: set[str] = set()

    for line in lines[header_index + 1 :]:
        if not line.strip():
            continue
        match = re.match(r"^\s*\*\s+([^\s(]+)(?:\s+\(([^)]+)\))?", line)
        if match is None:
            raise InventoryValidationError("unsupported output shape in grok models")
        mid = match.group(1).strip()
        extra = match.group(2).strip() if match.group(2) else ""
        dname = f"{mid} ({extra})" if extra else mid
        if mid in seen_model_ids:
            raise InventoryValidationError("duplicate model id in grok models output")
        seen_model_ids.add(mid)
        records.append(
            ModelRecord(
                model_id=mid,
                native_id=mid,
                state="available",
                display_name=dname,
            )
        )

    if not records:
        raise InventoryValidationError("unsupported output: no models found in grok models output")

    return records


ProbeRunner = Callable[[list[str], float], tuple[int, str, str]]


def run_probe(
    argv: Sequence[str],
    *,
    runner: ProbeRunner | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    allowed_commands: Collection[tuple[str, ...]] | None = None,
    executable_allowlist: Collection[str] | None = None,
) -> ProbeResult:
    """Run a CLI inventory probe with strict execution bounds.

    Enforces exact read-only argv commands and timeout bounds. Live execution
    uses brigade.proc.run(..., supervise_group=True). proc retains at most 1 MiB;
    inventory then rejects any accepted stdout above 64 KiB and treats
    output_limit_exceeded, stream_limit_exceeded, incomplete_process_group, and
    decode_failed as unavailable. Stderr and stdout failure text are never copied
    into probe errors.
    """
    if not isinstance(argv, (list, tuple)) or isinstance(argv, (str, bytes)):
        return ProbeResult(returncode=127, stdout="", error="command-not-allowed")
    if not argv:
        return ProbeResult(returncode=127, stdout="", error="empty-command")
    for arg in argv:
        if not isinstance(arg, str) or not arg:
            return ProbeResult(returncode=127, stdout="", error="command-not-allowed")
        if re.search(r"[\x00-\x1f\x7f]", arg):
            return ProbeResult(returncode=126, stdout="", error="command-not-allowed")

    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or math.isnan(timeout)
        or math.isinf(timeout)
        or timeout <= 0
    ):
        return ProbeResult(returncode=1, stdout="", error="invalid-timeout")
    bounded_timeout = min(max(float(timeout), 1.0), 60.0)

    cmd_tuple = tuple(argv)
    valid_commands = allowed_commands if allowed_commands is not None else ALLOWED_PROBE_COMMANDS
    if cmd_tuple not in valid_commands:
        return ProbeResult(returncode=126, stdout="", error="command-not-allowed")

    if runner is not None:
        try:
            code, stdout, _ = runner(list(argv), bounded_timeout)
            if not isinstance(stdout, str):
                return ProbeResult(returncode=1, stdout="", error="probe-failed")
            if len(stdout.encode("utf-8")) > MAX_STDOUT_BYTES:
                return ProbeResult(returncode=1, stdout="", error="output-too-large")
            if code != 0:
                return ProbeResult(returncode=code, stdout="", error=f"probe-exited-{code}")
            return ProbeResult(returncode=0, stdout=stdout, error=None)
        except Exception:
            return ProbeResult(returncode=1, stdout="", error="probe-failed")

    try:
        result = proc.run(list(argv), timeout=bounded_timeout, supervise_group=True)
    except Exception:
        return ProbeResult(returncode=1, stdout="", error="probe-failed")
    return _probe_result_from_proc(result)


def _probe_result_from_proc(result: proc.Result) -> ProbeResult:
    """Map proc.Result to ProbeResult. Incomplete or oversized output is unavailable.

    proc.run captures up to 1 MiB and reaps the process group. Inventory then
    accepts at most 64 KiB of stdout. Failure flags and stderr are never copied
    into the probe error string.
    """
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    try:
        accepted_bytes = len(stdout.encode("utf-8"))
    except Exception:
        return ProbeResult(returncode=1, stdout="", error="probe-failed")
    oversized = (
        result.output_limit_exceeded
        or result.stream_limit_exceeded
        or result.stdout_bytes > MAX_STDOUT_BYTES
        or accepted_bytes > MAX_STDOUT_BYTES
    )
    if oversized:
        return ProbeResult(returncode=1, stdout="", error="output-too-large")
    if result.incomplete_process_group or result.decode_failed:
        return ProbeResult(returncode=1, stdout="", error="probe-failed")
    if result.code == 124:
        return ProbeResult(returncode=124, stdout="", error="probe-timed-out")
    if result.code == 127:
        return ProbeResult(returncode=127, stdout="", error="executable-not-found")
    if result.code == 126:
        return ProbeResult(returncode=126, stdout="", error="permission-denied")
    if result.code != 0:
        return ProbeResult(returncode=result.code, stdout="", error=f"probe-exited-{result.code}")
    return ProbeResult(returncode=0, stdout=stdout, error=None)


def probe_cli_inventory(
    harness: str,
    *,
    provider: str | None = None,
    runner: ProbeRunner | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    allowed_commands: Collection[tuple[str, ...]] | None = None,
    executable_allowlist: Collection[str] | None = None,
    now: str | datetime | float | None = None,
    ttl_seconds: float = 3600.0,
) -> dict[str, Any]:
    """Execute a live CLI probe for supported harnesses and return an ingestible payload.

    Separates harness from provider through explicit configuration.
    Honest evidence type labeling: catalog for OpenCode, cli_probe for execution CLIs.
    """
    now_dt = _normalize_now(now)
    now_iso = _format_iso(now_dt)
    exp_iso = _format_iso(datetime.fromtimestamp(now_dt.timestamp() + ttl_seconds, tz=timezone.utc))

    clean_harness = harness.strip().lower()
    if clean_harness in UNSUPPORTED_HARNESSES:
        return {
            "schema": PAYLOAD_SCHEMA,
            "source": f"cli:{clean_harness}",
            "provider": clean_harness,
            "harness": clean_harness,
            "account_id": "",
            "scope": "complete",
            "status": "error",
            "captured_at": now_iso,
            "expires_at": exp_iso,
            "error_reason": "unsupported-harness-no-verified-live-model-list",
            "models": [],
        }

    if clean_harness == "agy":
        cmd = ["agy", "models"]
        eff_provider = provider.strip() if provider else "google"
        parser = parse_agy_models
        source = "cli:agy"
        evidence_type = "cli_probe"
    elif clean_harness in ("opencode", "opencode-go"):
        cmd = ["opencode", "models"]
        eff_provider = provider.strip() if provider else "opencode"
        parser = parse_opencode_models
        source = "cli:opencode"
        evidence_type = "catalog"
    elif clean_harness in ("cursor", "cursor-agent"):
        cmd = ["cursor-agent", "models"]
        eff_provider = provider.strip() if provider else "cursor"
        parser = parse_cursor_agent_models
        source = "cli:cursor-agent"
        evidence_type = "cli_probe"
    elif clean_harness == "grok":
        cmd = ["grok", "models"]
        eff_provider = provider.strip() if provider else "xai"
        parser = parse_grok_models
        source = "cli:grok"
        evidence_type = "cli_probe"
    else:
        return {
            "schema": PAYLOAD_SCHEMA,
            "source": f"cli:{clean_harness}",
            "provider": clean_harness,
            "harness": clean_harness,
            "account_id": "",
            "scope": "complete",
            "status": "error",
            "captured_at": now_iso,
            "expires_at": exp_iso,
            "error_reason": "unsupported-harness-no-verified-live-model-list",
            "models": [],
        }

    probe = run_probe(cmd, runner=runner, timeout=timeout, allowed_commands=allowed_commands)
    if probe.returncode != 0 or probe.error is not None:
        return {
            "schema": PAYLOAD_SCHEMA,
            "source": source,
            "provider": eff_provider,
            "harness": clean_harness,
            "account_id": "",
            "scope": "complete",
            "status": "error",
            "captured_at": now_iso,
            "expires_at": exp_iso,
            "evidence_type": evidence_type,
            "error_reason": _sanitize_error_reason(probe.error),
            "models": [],
        }

    try:
        models = parser(probe.stdout)
    except InventoryValidationError:
        return {
            "schema": PAYLOAD_SCHEMA,
            "source": source,
            "provider": eff_provider,
            "harness": clean_harness,
            "account_id": "",
            "scope": "complete",
            "status": "error",
            "captured_at": now_iso,
            "expires_at": exp_iso,
            "evidence_type": evidence_type,
            "error_reason": "unsupported-output-shape",
            "models": [],
        }

    return {
        "schema": PAYLOAD_SCHEMA,
        "source": source,
        "provider": eff_provider,
        "harness": clean_harness,
        "account_id": "",
        "scope": "complete",
        "status": "ok",
        "captured_at": now_iso,
        "expires_at": exp_iso,
        "evidence_type": evidence_type,
        "models": [m.to_dict() for m in models],
    }


def build_manual_browser_payload(
    provider: str,
    models: Sequence[str | Mapping[str, Any]],
    *,
    captured_at: str,
    expires_at: str,
    operator: str,
    harness: str = "",
    account_id: str = "",
    scope: ScopeKind = "complete",
) -> dict[str, Any]:
    """Create a validated, expiring manual browser observation payload."""
    _validate_safe_string(provider, "provider")
    _validate_safe_string(operator, "operator")
    _validate_safe_string(harness, "harness")
    _validate_safe_string(account_id, "account_id")
    _parse_iso_timestamp(captured_at, "captured_at")
    _parse_iso_timestamp(expires_at, "expires_at")

    payload = {
        "schema": PAYLOAD_SCHEMA,
        "source": "manual:browser",
        "provider": provider.strip(),
        "harness": harness.strip(),
        "account_id": account_id.strip(),
        "scope": scope,
        "status": "ok",
        "captured_at": captured_at,
        "expires_at": expires_at,
        "evidence_type": "manual_browser",
        "models": list(models),
        "operator": operator.strip(),
    }
    _validate_strict_json(payload)
    return payload
