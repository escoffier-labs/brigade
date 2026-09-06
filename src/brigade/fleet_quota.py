"""Fleet quota observations: parser, immutable store, and measured admission.

The collector lives in a separate repository. This module only parses the
``brigade.fleet_quota_observations.v1`` contract, stores observations as an
append-only stream with its own version, and admits a configured pool when
every overlapping window is known, fresh, and has remaining capacity above
the configured reserve. It does not probe providers and never increments
the control-plane policy revision.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping

from . import fleet_hub, fleet_policy
from .fleet_model_roster import canonical_json
from .fleet_policy import FleetPolicyError

QUOTA_SCHEMA = "brigade.fleet_quota_observations.v1"
QUOTA_SOURCES = ("crossusage-probe", "operator", "grokbot-export")
QUOTA_STATUSES = ("current", "unknown", "stale", "error")
WINDOW_UNIT = "percent"
WINDOW_UNITS = ("percent", "requests")
MAX_OBSERVATIONS = 256
MAX_WINDOWS = 32
MAX_RAW_BYTES = 65536
MAX_TEXT = fleet_policy.MAX_TEXT
MAX_REASON = 512

_OBSERVATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_quota_observations (
    observation_id TEXT NOT NULL PRIMARY KEY,
    account_id TEXT NOT NULL,
    pool_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    source TEXT NOT NULL,
    source_ref TEXT,
    collected_at TEXT NOT NULL,
    provider_time TEXT,
    expires_at TEXT,
    status TEXT NOT NULL,
    digest TEXT NOT NULL,
    document TEXT NOT NULL,
    raw TEXT NOT NULL,
    overrides_observation_id TEXT,
    reason TEXT,
    created_at TEXT NOT NULL
);
"""
_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_quota_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class FleetQuotaError(ValueError):
    """A quota observation payload is not usable."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _reject_json_constant(name: str) -> None:
    raise FleetQuotaError(f"fleet quota JSON must not contain {name}")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Additive observation store. Does not touch fleet_policy_meta."""
    conn.execute(_OBSERVATIONS_SCHEMA)
    conn.execute(_META_SCHEMA)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS fleet_quota_observations_pool ON fleet_quota_observations (pool_id, account_id)"
    )
    if conn.execute("SELECT 1 FROM fleet_quota_meta WHERE singleton=1").fetchone() is None:
        conn.execute(
            "INSERT INTO fleet_quota_meta (singleton, version, updated_at) VALUES (1, 0, ?)",
            (fleet_hub._utc_now(),),
        )


def quota_version(conn: sqlite3.Connection) -> int:
    ensure_schema(conn)
    row = conn.execute("SELECT version FROM fleet_quota_meta WHERE singleton=1").fetchone()
    return 0 if row is None else int(row[0])


def _mapping(raw: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise FleetQuotaError(f"fleet quota {where} must be a JSON object")
    return raw


def _text(raw: Any, where: str, *, optional: bool = False, limit: int = MAX_TEXT) -> str | None:
    if raw is None:
        if optional:
            return None
        raise FleetQuotaError(f"fleet quota {where} is required")
    if not isinstance(raw, str):
        raise FleetQuotaError(f"fleet quota {where} must be a string")
    if not raw and not optional:
        raise FleetQuotaError(f"fleet quota {where} is required")
    if len(raw) > limit:
        raise FleetQuotaError(f"fleet quota {where} must be at most {limit} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise FleetQuotaError(f"fleet quota {where} must not contain control characters")
    return raw


def _name(raw: Any, where: str) -> str:
    value = _text(raw, where)
    assert value is not None
    if not fleet_policy.NAME_PATTERN.match(value):
        raise FleetQuotaError(f"fleet quota {where} must match {fleet_policy.NAME_PATTERN.pattern}")
    return value


def _identity(raw: Any, where: str) -> str:
    value = _text(raw, where)
    assert value is not None
    if not fleet_policy.IDENTITY_PATTERN.match(value):
        raise FleetQuotaError(f"fleet quota {where} must match {fleet_policy.IDENTITY_PATTERN.pattern}")
    return value


def _enum(raw: Any, where: str, allowed: tuple[str, ...]) -> str:
    value = _text(raw, where)
    if value not in allowed:
        raise FleetQuotaError(f"fleet quota {where} must be one of: {', '.join(allowed)}")
    return value


def _timestamp(raw: Any, where: str, *, optional: bool = False) -> str | None:
    value = _text(raw, where, optional=optional)
    if value is None:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FleetQuotaError(f"fleet quota {where} must be an ISO-8601 timestamp") from exc
    if stamp.tzinfo is None:
        raise FleetQuotaError(f"fleet quota {where} must be timezone-aware")
    return stamp.astimezone(timezone.utc).isoformat()


def _parse_stamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


def _number(raw: Any, where: str) -> float:
    if type(raw) is bool or not isinstance(raw, (int, float)):
        raise FleetQuotaError(f"fleet quota {where} must be a finite number")
    value = float(raw)
    if not math.isfinite(value):
        raise FleetQuotaError(f"fleet quota {where} must be a finite number")
    return value


def _int(raw: Any, where: str, *, optional: bool = False) -> int | None:
    if raw is None:
        if optional:
            return None
        raise FleetQuotaError(f"fleet quota {where} is required")
    if type(raw) is not int:
        raise FleetQuotaError(f"fleet quota {where} must be an integer")
    if raw < 0:
        raise FleetQuotaError(f"fleet quota {where} must be a non-negative integer")
    return raw


def _parse_window(raw: Any, where: str) -> dict[str, Any]:
    body = _mapping(raw, where)
    unknown = sorted(
        set(body).difference({"window_id", "label", "used", "limit", "unit", "resets_at", "period_duration_ms"})
    )
    if unknown:
        raise FleetQuotaError(f"unknown fleet quota {where} field(s): {', '.join(unknown)}")
    used = _number(body.get("used"), f"{where}.used")
    limit = _number(body.get("limit"), f"{where}.limit")
    unit = _text(body.get("unit"), f"{where}.unit")
    if unit not in WINDOW_UNITS:
        raise FleetQuotaError(f"fleet quota {where}.unit must be one of: {', '.join(WINDOW_UNITS)}")
    return {
        "window_id": _name(body.get("window_id"), f"{where}.window_id"),
        "label": _text(body.get("label"), f"{where}.label"),
        "used": used,
        "limit": limit,
        "unit": unit,
        "resets_at": _timestamp(body.get("resets_at"), f"{where}.resets_at", optional=True),
        "period_duration_ms": _int(body.get("period_duration_ms"), f"{where}.period_duration_ms", optional=True),
    }


def _parse_observation(raw: Any, where: str) -> dict[str, Any]:
    body = _mapping(raw, where)
    allowed = {
        "observation_id",
        "account_id",
        "pool_id",
        "provider",
        "source",
        "source_ref",
        "collected_at",
        "provider_time",
        "expires_at",
        "status",
        "windows",
        "reason",
        "overrides_observation_id",
    }
    unknown = sorted(set(body).difference(allowed))
    if unknown:
        raise FleetQuotaError(f"unknown fleet quota {where} field(s): {', '.join(unknown)}")
    source = _enum(body.get("source"), f"{where}.source", QUOTA_SOURCES)
    status = _enum(body.get("status"), f"{where}.status", QUOTA_STATUSES)
    reason = _text(body.get("reason"), f"{where}.reason", optional=True, limit=MAX_REASON)
    overrides = _text(body.get("overrides_observation_id"), f"{where}.overrides_observation_id", optional=True)
    expires_at = _timestamp(body.get("expires_at"), f"{where}.expires_at", optional=True)
    windows_raw = body.get("windows")
    if not isinstance(windows_raw, list):
        raise FleetQuotaError(f"fleet quota {where}.windows must be a list")
    if len(windows_raw) > MAX_WINDOWS:
        raise FleetQuotaError(f"fleet quota {where}.windows must hold at most {MAX_WINDOWS} items")
    windows = [_parse_window(item, f"{where}.windows[{index}]") for index, item in enumerate(windows_raw)]
    if source == "operator":
        if not reason:
            raise FleetQuotaError(f"fleet quota {where}.reason is required for an operator correction")
        if not overrides:
            raise FleetQuotaError(
                f"fleet quota {where}.overrides_observation_id is required for an operator correction"
            )
        if not expires_at:
            raise FleetQuotaError(f"fleet quota {where}.expires_at is required for an operator correction")
    return {
        "observation_id": _identity(body.get("observation_id"), f"{where}.observation_id"),
        "account_id": _identity(body.get("account_id"), f"{where}.account_id"),
        "pool_id": _name(body.get("pool_id"), f"{where}.pool_id"),
        "provider": _name(body.get("provider"), f"{where}.provider"),
        "source": source,
        "source_ref": _text(body.get("source_ref"), f"{where}.source_ref", optional=True),
        "collected_at": _timestamp(body.get("collected_at"), f"{where}.collected_at"),
        "provider_time": _timestamp(body.get("provider_time"), f"{where}.provider_time", optional=True),
        "expires_at": expires_at,
        "status": status,
        "windows": windows,
        "reason": reason,
        "overrides_observation_id": overrides,
    }


def parse_observations(raw: Any) -> dict[str, Any]:
    """Validate one bounded quota observation envelope."""
    try:
        body: Mapping[str, Any]
        if isinstance(raw, (bytes, str)):
            body = fleet_policy.parse_json_object(raw, limit=MAX_RAW_BYTES)
        else:
            body = _mapping(raw, "document")
            fleet_policy.reject_json_tree(body, "quota document")
    except FleetPolicyError as exc:
        raise FleetQuotaError(str(exc).replace("fleet policy", "fleet quota")) from exc
    unknown = sorted(set(body).difference({"schema", "collected_at", "observations"}))
    if unknown:
        raise FleetQuotaError(f"unknown fleet quota document field(s): {', '.join(unknown)}")
    if body.get("schema") != QUOTA_SCHEMA:
        raise FleetQuotaError(f"fleet quota field 'schema' must be '{QUOTA_SCHEMA}'")
    observations_raw = body.get("observations")
    if not isinstance(observations_raw, list):
        raise FleetQuotaError("fleet quota field 'observations' must be a list")
    if len(observations_raw) > MAX_OBSERVATIONS:
        raise FleetQuotaError(f"fleet quota field 'observations' must hold at most {MAX_OBSERVATIONS} items")
    observations = [_parse_observation(item, f"observations[{index}]") for index, item in enumerate(observations_raw)]
    return {
        "schema": QUOTA_SCHEMA,
        "collected_at": _timestamp(body.get("collected_at"), "collected_at"),
        "observations": observations,
    }


def _sanitize_raw(observation: Mapping[str, Any]) -> dict[str, Any]:
    rendered = canonical_json(dict(observation))
    if len(rendered.encode("ascii")) > MAX_RAW_BYTES:
        raise FleetQuotaError("fleet quota observation exceeded the size limit")
    return json.loads(rendered)


def _digest(observation: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(observation).encode("ascii")).hexdigest()


def ingest_observations(conn: sqlite3.Connection, raw: Any) -> dict[str, Any]:
    """Store observations immutably. Exact duplicates are idempotent; conflicts raise."""
    parsed = parse_observations(raw)
    ensure_schema(conn)
    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    accepted = 0
    duplicate = 0
    try:
        now = fleet_hub._utc_now()
        for observation in parsed["observations"]:
            digest = _digest(observation)
            raw_payload = _sanitize_raw(observation)
            existing = conn.execute(
                "SELECT digest FROM fleet_quota_observations WHERE observation_id=?",
                (observation["observation_id"],),
            ).fetchone()
            if existing is not None:
                if existing[0] != digest:
                    raise FleetQuotaError(
                        f"fleet quota observation {observation['observation_id']!r} conflicts with stored content"
                    )
                duplicate += 1
                continue
            conn.execute(
                "INSERT INTO fleet_quota_observations (observation_id, account_id, pool_id, provider, source, "
                "source_ref, collected_at, provider_time, expires_at, status, digest, document, raw, "
                "overrides_observation_id, reason, created_at) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    observation["observation_id"],
                    observation["account_id"],
                    observation["pool_id"],
                    observation["provider"],
                    observation["source"],
                    observation["source_ref"],
                    observation["collected_at"],
                    observation["provider_time"],
                    observation["expires_at"],
                    observation["status"],
                    digest,
                    canonical_json(observation),
                    canonical_json(raw_payload),
                    observation["overrides_observation_id"],
                    observation["reason"],
                    now,
                ),
            )
            accepted += 1
        if accepted:
            conn.execute(
                "UPDATE fleet_quota_meta SET version=version+?, updated_at=? WHERE singleton=1",
                (accepted, now),
            )
        version = quota_version(conn)
        if opened:
            conn.commit()
        return {"accepted": accepted, "duplicate": duplicate, "version": version}
    except BaseException:
        if opened:
            conn.rollback()
        raise


def list_observations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT observation_id, document, raw FROM fleet_quota_observations ORDER BY created_at, observation_id"
    ).fetchall()
    result: list[dict[str, Any]] = []
    for observation_id, document, raw in rows:
        payload = json.loads(document)
        payload["raw"] = json.loads(raw)
        payload["observation_id"] = observation_id
        result.append(payload)
    return result


def _load_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT document FROM fleet_quota_observations").fetchall()
    return [json.loads(row[0]) for row in rows]


def _is_fresh(observation: Mapping[str, Any], *, now: datetime, freshness_seconds: int) -> bool:
    """Sensor freshness uses collected_at when provider_time is unknown.

    Operator corrections skip sensor TTL and are bounded only by expires_at.
    Observation expiry is a hard stop for every source.
    """
    expires = _parse_stamp(observation.get("expires_at"))
    if expires is not None and expires <= now:
        return False
    if observation.get("source") == "operator":
        return expires is not None and expires > now
    collected = _parse_stamp(observation.get("collected_at"))
    if collected is None:
        return False
    age = (now - collected).total_seconds()
    if age < -60:
        return False
    if age > freshness_seconds:
        return False
    provider_time = _parse_stamp(observation.get("provider_time"))
    if provider_time is None:
        return True
    provider_age = (now - provider_time).total_seconds()
    if provider_age < -60:
        return False
    return provider_age <= freshness_seconds


def _active_override(observation: Mapping[str, Any], *, now: datetime) -> bool:
    if observation.get("source") != "operator":
        return False
    expires = _parse_stamp(observation.get("expires_at"))
    return expires is not None and expires > now


def _window_reset(window: Mapping[str, Any], *, now: datetime) -> bool:
    resets = _parse_stamp(window.get("resets_at") if isinstance(window.get("resets_at"), str) else None)
    return resets is not None and resets <= now


def _remaining_percent(window: Mapping[str, Any]) -> float | None:
    used = window.get("used")
    limit = window.get("limit")
    if not isinstance(used, (int, float)) or not isinstance(limit, (int, float)):
        return None
    if type(used) is bool or type(limit) is bool:
        return None
    if not math.isfinite(float(used)) or not math.isfinite(float(limit)) or float(limit) <= 0:
        return None
    return (float(limit) - float(used)) / float(limit) * 100.0


def _observation_instant(observation: Mapping[str, Any]) -> datetime | None:
    return _parse_stamp(observation.get("collected_at"))


def _select_window_measurement(
    entries: list[dict[str, Any]],
    *,
    now: datetime,
    freshness_seconds: int,
) -> dict[str, Any] | None:
    """Pick the latest measurement, then assess it. Operator corrections win until expiry."""
    if not entries:
        return None
    corrections = [
        entry
        for entry in entries
        if _active_override(entry["observation"], now=now) and not _window_reset(entry["window"], now=now)
    ]
    if corrections:
        instants = {_observation_instant(entry["observation"]) for entry in corrections}
        values = {
            (entry["window"]["used"], entry["window"]["limit"], entry["window"].get("unit")) for entry in corrections
        }
        if len(instants) == 1 and len(values) > 1:
            return {"status": "unknown", "reason": "conflicting-current", "window": corrections[0]["window"]}
        latest_instant = max((instant for instant in instants if instant is not None), default=None)
        chosen = [entry for entry in corrections if _observation_instant(entry["observation"]) == latest_instant]
        chosen_values = {
            (entry["window"]["used"], entry["window"]["limit"], entry["window"].get("unit")) for entry in chosen
        }
        if latest_instant is None or len(chosen_values) > 1:
            return {"status": "unknown", "reason": "conflicting-current", "window": corrections[0]["window"]}
        return {
            "status": "current",
            "reason": None,
            "window": chosen[0]["window"],
            "observation": chosen[0]["observation"],
        }

    def _sort_key(entry: dict[str, Any]) -> tuple[datetime, str]:
        instant = _observation_instant(entry["observation"]) or datetime.min.replace(tzinfo=timezone.utc)
        return (instant, str(entry["observation"].get("observation_id") or ""))

    latest_instant = None
    latest_entries: list[dict[str, Any]] = []
    for entry in sorted(entries, key=_sort_key, reverse=True):
        instant = _observation_instant(entry["observation"])
        if latest_instant is None:
            latest_instant = instant
            latest_entries.append(entry)
            continue
        if instant == latest_instant:
            latest_entries.append(entry)
            continue
        break
    if not latest_entries:
        return None
    values = {
        (entry["window"]["used"], entry["window"]["limit"], entry["window"].get("unit")) for entry in latest_entries
    }
    if len(values) > 1:
        return {"status": "unknown", "reason": "conflicting-current", "window": latest_entries[0]["window"]}
    observation = latest_entries[0]["observation"]
    window = latest_entries[0]["window"]
    if observation.get("status") != "current":
        return {"status": "unknown", "reason": str(observation.get("status") or "unknown"), "window": window}
    if not _is_fresh(observation, now=now, freshness_seconds=freshness_seconds):
        return {"status": "unknown", "reason": "stale", "window": window}
    if _window_reset(window, now=now):
        return {"status": "unknown", "reason": "reset", "window": window}
    return {"status": "current", "reason": None, "window": window, "observation": observation}


def admit_pool(
    conn: sqlite3.Connection,
    pool_id: str,
    *,
    routing: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Admit only when every required window is known, fresh, and above reserve.

    Remaining is never summed across windows. Latest measurement per
    account/pool/window is selected first; a newer error does not keep an
    older current reading. Missing, stale, reset, error, mismatch, or
    conflicting current readings are ``quota-unknown``.
    """
    ensure_schema(conn)
    stamp = now or _now()
    pools = dict((routing or {}).get("quota_pools") or {})
    config = pools.get(pool_id)
    empty = {"pool_id": pool_id, "admitted": False, "reasons": ["quota-unknown"], "windows": []}
    if config is None:
        return empty
    reserve = config.get("reserve_percent")
    if reserve is None:
        reserve = int((routing or {}).get("quota_reserve_percent") or fleet_policy.DEFAULT_QUOTA_RESERVE_PERCENT)
    freshness = int(config.get("freshness_seconds") or fleet_policy.DEFAULT_TELEMETRY_TTL_SECONDS)
    account_id = config["account_id"]
    provider = config.get("provider")
    configured_windows = dict(config.get("windows") or {})
    rows = [
        row
        for row in _load_rows(conn)
        if row.get("pool_id") == pool_id
        and row.get("account_id") == account_id
        and (provider is None or row.get("provider") == provider)
    ]
    if not rows:
        return empty

    by_window: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("source") == "operator" and not _active_override(row, now=stamp):
            continue
        for window in row.get("windows") or []:
            window_id = window.get("window_id")
            if not isinstance(window_id, str):
                continue
            configured = configured_windows.get(window_id)
            if configured and configured.get("unit") and window.get("unit") != configured.get("unit"):
                by_window.setdefault(window_id, []).append({"observation": row, "window": window, "mismatch": True})
                continue
            if (
                configured
                and configured.get("limit") is not None
                and float(window.get("limit") or 0) != float(configured["limit"])
            ):
                by_window.setdefault(window_id, []).append({"observation": row, "window": window, "mismatch": True})
                continue
            by_window.setdefault(window_id, []).append({"observation": row, "window": window})

    required = list(configured_windows) if configured_windows else sorted(by_window)
    if not required:
        return {**empty, "reasons": ["quota-unknown"]}

    reports: list[dict[str, Any]] = []
    reasons: list[str] = []
    admitted = True
    for window_id in required:
        entries = [entry for entry in by_window.get(window_id, []) if not entry.get("mismatch")]
        selected = _select_window_measurement(entries, now=stamp, freshness_seconds=freshness)
        if selected is None or selected["status"] != "current":
            admitted = False
            reasons.append("quota-unknown")
            reports.append(
                {
                    "window_id": window_id,
                    "status": "unknown",
                    "reason": None if selected is None else selected.get("reason") or "missing",
                }
            )
            continue
        window = selected["window"]
        remaining_percent = _remaining_percent(window)
        remaining = float(window["limit"]) - float(window["used"])
        status = "current"
        reason = None
        if remaining_percent is None:
            admitted = False
            reasons.append("quota-unknown")
            reports.append({"window_id": window_id, "status": "unknown", "reason": "non-finite"})
            continue
        if remaining_percent <= float(reserve):
            admitted = False
            reasons.append("quota-exhausted")
            status = "exhausted"
            reason = "reserve"
        reports.append(
            {
                "window_id": window_id,
                "used": window["used"],
                "limit": window["limit"],
                "remaining": remaining,
                "status": status,
                "reason": reason,
            }
        )

    unique_reasons = []
    for reason in reasons:
        if reason not in unique_reasons:
            unique_reasons.append(reason)
    if "quota-unknown" in unique_reasons:
        unique_reasons = ["quota-unknown"]
        admitted = False
    return {
        "pool_id": pool_id,
        "admitted": admitted and not unique_reasons,
        "reasons": unique_reasons,
        "windows": reports,
    }
