"""Fleet control-plane policy contract and resolver (``brigade.fleet_policy.v1``).

Slice 1 of the fleet control plane: the *pure* half. This module owns the
document shape (machines, seats, consumers, repositories, fleet defaults),
its digest, the four-layer preference resolver with a per-leaf source chain,
the hard repository-privacy constraint, inventory classification, and pin
preservation. It has no SQLite, no HTTP, and no Brigade runtime imports
beyond the shared canonical-JSON helper, so a caller can resolve a document
it already holds (a cached one, a previewed one, a rolled-back one) without
touching the Hub.

Persistence, revisions, and session receipts live in ``fleet_hub_policy``.

Precedence, low to high: fleet defaults, consumer defaults, repository
patches, explicit session overrides. A ``null`` at an override layer removes
that layer's override and restores inheritance rather than writing a null.
Hard constraints run *after* preference resolution: an unknown or private
repository can never enable training by override.

Nothing here enforces policy on a running session and nothing here migrates
the legacy ``model_policy`` roster; both are later slices.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Iterable, Mapping

from .fleet_model_inventory import DEFAULT_ALLOWED_SOURCES
from .fleet_model_roster import canonical_json

POLICY_SCHEMA = "brigade.fleet_policy.v1"
POLICY_SESSION_SCHEMA = "brigade.fleet_policy_session.v1"

MAX_RECORDS = 256
MAX_LIST_ITEMS = 64
MAX_TEXT = 256
MAX_DOCUMENT_BYTES = 262144
MAX_CONCURRENCY = 256
MAX_PRIORITY = 1000
MAX_TIMEOUT_SECONDS = 86400

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:-]{0,255}$")

PRIVACY_VALUES = ("public", "private", "unknown")
COST_CLASS_VALUES = ("paid", "subscription", "free", "unknown")
RELOAD_VALUES = ("none", "refreshable", "restart-required")
COVERAGE_VALUES = ("unverified", "verified", "unsupported")
OS_VALUES = ("linux", "windows", "macos", "unknown")
OPENCODE_MUSE_SPARK_1_3_IDS = frozenset(
    {
        "muse-spark-1.3-contributor-free",
        "opencode/muse-spark-1.3-contributor-free",
        "opencode-muse-spark-1.3-contributor-free",
    }
)

TOP_LEVEL_FIELDS = frozenset({"schema", "defaults", "machines", "seats", "consumers", "repositories", "routing"})
DEFAULT_TELEMETRY_TTL_SECONDS = 300
DEFAULT_RESERVATION_TTL_SECONDS = 300
DEFAULT_QUOTA_RESERVE_PERCENT = 4
MAX_PERCENT = 100
PIN_IDENTITY_FIELDS = ("provider", "model", "effort")
PIN_BINDING_MODEL_PATHS = (("brigade", "model"), ("native", "model"))

# Settings leaves. ``roles`` takes free-form role names; the other sections
# have a fixed key set, so a typo is a rejected document, not a silent no-op.
FREE_SECTIONS = ("roles",)
FIXED_SECTIONS: dict[str, dict[str, str]] = {
    "data": {"allow_training": "bool", "retention": "text", "allow_free": "bool"},
    "execution": {"machine": "name", "concurrency": "concurrency", "timeout_seconds": "timeout"},
}
SETTINGS_SECTIONS = frozenset(FREE_SECTIONS) | frozenset(FIXED_SECTIONS)

# The built-in floor, folded into the ``fleet-defaults`` layer so an empty
# policy still resolves to something safe (training off, one slot).
BASE_SETTINGS: dict[str, dict[str, Any]] = {
    "roles": {},
    "data": {"allow_training": False, "retention": None, "allow_free": False},
    "execution": {"machine": None, "concurrency": 1, "timeout_seconds": None},
}


class FleetPolicyError(ValueError):
    """A policy document, override, or inventory payload is not usable."""


def _reject_json_constant(name: str) -> None:
    raise FleetPolicyError(f"fleet policy JSON must not contain {name}")


def reject_json_tree(value: Any, where: str = "document") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise FleetPolicyError(f"fleet policy {where} keys must be strings")
            if any(ord(char) < 32 or ord(char) == 127 for char in key):
                raise FleetPolicyError(f"fleet policy {where} keys must not contain control characters")
            reject_json_tree(item, f"{where}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            reject_json_tree(item, f"{where}[{index}]")
        return
    if isinstance(value, str):
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise FleetPolicyError(f"fleet policy {where} must not contain control characters")
        return
    if value is None or type(value) is bool or type(value) is int:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise FleetPolicyError(f"fleet policy {where} must be a finite number")
        return
    raise FleetPolicyError(f"fleet policy {where} is not a JSON value")


def parse_json_object(raw: bytes | str, *, limit: int = MAX_DOCUMENT_BYTES) -> dict[str, Any]:
    """Decode one bounded JSON object. Rejects non-objects, NaN/Inf, and controls."""
    if isinstance(raw, str):
        encoded = raw.encode("utf-8")
        text = raw
    else:
        encoded = raw
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FleetPolicyError("fleet policy JSON is not valid UTF-8") from exc
    if len(encoded) > limit:
        raise FleetPolicyError(f"fleet policy JSON must be at most {limit} bytes")
    try:
        value = json.loads(text, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as exc:
        raise FleetPolicyError("fleet policy JSON is not valid JSON") from exc
    if not isinstance(value, dict):
        raise FleetPolicyError("fleet policy JSON must be an object")
    reject_json_tree(value)
    return value


def empty_routing() -> dict[str, Any]:
    """Generic routing options: disabled, no named hosts or accounts."""
    return {
        "enabled": False,
        "telemetry_ttl_seconds": DEFAULT_TELEMETRY_TTL_SECONDS,
        "reservation_ttl_seconds": DEFAULT_RESERVATION_TTL_SECONDS,
        "quota_reserve_percent": DEFAULT_QUOTA_RESERVE_PERCENT,
        "workload_requirements": {},
        "quota_pools": {},
        "inventory_collectors": {},
    }


def empty_document() -> dict[str, Any]:
    """The safe, generic starting policy: no machines, seats, or repositories."""
    return {
        "schema": POLICY_SCHEMA,
        "defaults": {},
        "machines": {},
        "seats": {},
        "consumers": {},
        "repositories": {},
        "routing": empty_routing(),
    }


def _mapping(raw: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise FleetPolicyError(f"fleet policy {where} must be a JSON object")
    return raw


def _reject_unknown(raw: Mapping[str, Any], allowed: Iterable[str], where: str) -> None:
    unknown = sorted(set(raw).difference(set(allowed)))
    if unknown:
        raise FleetPolicyError(f"unknown {where} field(s): {', '.join(unknown)}")


def _text(raw: Any, where: str, *, optional: bool = True) -> str | None:
    if raw is None:
        if optional:
            return None
        raise FleetPolicyError(f"fleet policy {where} is required")
    if not isinstance(raw, str):
        raise FleetPolicyError(f"fleet policy {where} must be a string")
    if len(raw) > MAX_TEXT:
        raise FleetPolicyError(f"fleet policy {where} must be at most {MAX_TEXT} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise FleetPolicyError(f"fleet policy {where} must not contain control characters")
    return raw


def _name(raw: Any, where: str, *, optional: bool = False) -> str | None:
    value = _text(raw, where, optional=optional)
    if value is None:
        return None
    if not NAME_PATTERN.match(value):
        raise FleetPolicyError(f"fleet policy {where} must match {NAME_PATTERN.pattern}")
    return value


def _required_name(raw: Any, where: str) -> str:
    value = _name(raw, where)
    assert value is not None
    return value


def _identity(raw: Any, where: str) -> str:
    value = _text(raw, where, optional=False)
    assert value is not None
    if not IDENTITY_PATTERN.match(value):
        raise FleetPolicyError(f"fleet policy {where} must match {IDENTITY_PATTERN.pattern}")
    return value


def _bool(raw: Any, where: str, *, default: bool) -> bool:
    if raw is None:
        return default
    if type(raw) is not bool:
        raise FleetPolicyError(f"fleet policy {where} must be a boolean")
    return raw


def _int(raw: Any, where: str, *, default: int | None, low: int, high: int) -> int | None:
    if raw is None:
        return default
    if type(raw) is not int:
        raise FleetPolicyError(f"fleet policy {where} must be an integer")
    if not low <= raw <= high:
        raise FleetPolicyError(f"fleet policy {where} must be an integer in {low}..{high}")
    return raw


def _name_list(raw: Any, where: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise FleetPolicyError(f"fleet policy {where} must be a list")
    if len(raw) > MAX_LIST_ITEMS:
        raise FleetPolicyError(f"fleet policy {where} must hold at most {MAX_LIST_ITEMS} items")
    return [_required_name(item, f"{where}[{index}]") for index, item in enumerate(raw)]


def _enum(raw: Any, where: str, allowed: tuple[str, ...], *, default: str) -> str:
    if raw is None:
        return default
    value = _text(raw, where, optional=False)
    if value not in allowed:
        raise FleetPolicyError(f"fleet policy {where} must be one of: {', '.join(allowed)}")
    return value


def _records(raw: Any, where: str) -> Mapping[str, Any]:
    records = _mapping(raw, where)
    if len(records) > MAX_RECORDS:
        raise FleetPolicyError(f"fleet policy '{where}' must hold at most {MAX_RECORDS} records")
    return records


def _settings_leaf(value: Any, kind: str, where: str) -> Any:
    if value is None:
        return None
    if kind == "bool":
        return _bool(value, where, default=False)
    if kind == "concurrency":
        return _int(value, where, default=None, low=0, high=MAX_CONCURRENCY)
    if kind == "timeout":
        return _int(value, where, default=None, low=1, high=MAX_TIMEOUT_SECONDS)
    if not isinstance(value, str):
        raise FleetPolicyError(f"fleet policy settings leaf {where} must be a string or null")
    if kind == "name":
        return _name(value, f"settings leaf {where}")
    return _text(value, f"settings leaf {where}")


def parse_settings(raw: Any, where: str) -> dict[str, dict[str, Any]]:
    """Validate one sparse settings tree (defaults, a patch, or overrides)."""
    tree = _mapping(raw, where)
    unknown = sorted(set(tree).difference(SETTINGS_SECTIONS))
    if unknown:
        raise FleetPolicyError(f"unknown settings section(s) in {where}: {', '.join(unknown)}")
    parsed: dict[str, dict[str, Any]] = {}
    for section in sorted(tree):
        body = _mapping(tree[section], f"{where}.{section}")
        if section in FIXED_SECTIONS:
            allowed = FIXED_SECTIONS[section]
            extra = sorted(set(body).difference(allowed))
            if extra:
                raise FleetPolicyError(f"unknown settings key(s) in {where}.{section}: {', '.join(extra)}")
            parsed[section] = {
                key: _settings_leaf(body[key], allowed[key], f"{where}.{section}.{key}") for key in sorted(body)
            }
            continue
        parsed[section] = {}
        for key in sorted(body):
            role = _required_name(key, f"{where}.{section} key '{key}'")
            parsed[section][role] = _settings_leaf(body[key], "name", f"{where}.{section}.{role}")
    return parsed


BINDING_GROUPS: dict[str, dict[str, str]] = {
    "brigade": {"cli": "text", "model": "identity"},
    "t3_fleet": {"instance_id": "identity", "service_tier": "name"},
    "native": {"instance_id": "identity", "model": "identity"},
}


def empty_bindings() -> dict[str, dict[str, Any]]:
    return {
        "brigade": {"cli": None, "model": None},
        "t3_fleet": {"instance_id": None, "service_tier": None},
        "native": {"instance_id": None, "model": None},
    }


def parse_bindings(raw: Any, where: str, *, sparse: bool = False) -> dict[str, dict[str, Any]]:
    """Exact consumer/harness bindings. Unknown keys are rejected, never aliased."""
    if raw is None:
        return {} if sparse else empty_bindings()
    body = _mapping(raw, where)
    _reject_unknown(body, BINDING_GROUPS, where)
    parsed: dict[str, dict[str, Any]] = {} if sparse else empty_bindings()
    for group in sorted(body):
        fields = BINDING_GROUPS[group]
        group_body = _mapping(body[group], f"{where}.{group}")
        _reject_unknown(group_body, fields, f"{where}.{group}")
        target = parsed.setdefault(group, {} if sparse else dict(empty_bindings()[group]))
        for key in sorted(group_body):
            value = group_body[key]
            kind = fields[key]
            leaf = f"{where}.{group}.{key}"
            if value is None:
                target[key] = None
            elif kind == "identity":
                target[key] = _identity(value, leaf)
            elif kind == "name":
                target[key] = _name(value, leaf, optional=True)
            else:
                target[key] = _text(value, leaf)
    return parsed


def _parse_seat_bindings(raw: Any, where: str) -> dict[str, dict[str, dict[str, Any]]]:
    if raw is None:
        return {}
    body = _mapping(raw, where)
    if len(body) > MAX_RECORDS:
        raise FleetPolicyError(f"fleet policy '{where}' must hold at most {MAX_RECORDS} records")
    return {
        _required_name(name, f"{where} seat"): parse_bindings(body[name], f"{where}.{name}", sparse=True)
        for name in sorted(body)
    }


def effective_seat_bindings(
    document: Mapping[str, Any],
    consumer: str | None,
    seat: str,
    *,
    _parsed: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Seat bindings with an optional per-consumer overlay. No fuzzy translation."""
    parsed = _parsed if _parsed is not None else parse_document(document)
    record = parsed["seats"].get(seat)
    if record is None:
        return empty_bindings()
    bindings = {group: dict(fields) for group, fields in record["bindings"].items()}
    consumer_record = parsed["consumers"].get(consumer) if consumer else None
    overlay = (consumer_record or {}).get("seat_bindings", {}).get(seat) or {}
    for group, fields in overlay.items():
        target = bindings.setdefault(group, {})
        for key, value in fields.items():
            if value is not None:
                target[key] = value
    return bindings


def matching_seats(
    document: Mapping[str, Any],
    consumer: str | None,
    *,
    provider: str,
    model: str,
    instance_id: str,
    reasoning: str | None = None,
) -> list[str]:
    """Seats whose configured bindings match the request exactly."""
    parsed = parse_document(document)
    names: list[str] = []
    for seat_name, seat in parsed["seats"].items():
        if seat["provider"] != provider:
            continue
        bindings = effective_seat_bindings(parsed, consumer, seat_name)
        if consumer == "brigade-run":
            launch_model = bindings.get("brigade", {}).get("model")
            if launch_model is not None:
                if launch_model != model:
                    continue
            elif seat["model"] != model:
                continue
            cli = bindings.get("brigade", {}).get("cli")
            if not isinstance(cli, str) or not cli or cli != instance_id:
                continue
        else:
            native_model = bindings.get("native", {}).get("model")
            if native_model is not None:
                if native_model != model:
                    continue
            elif seat["model"] != model:
                continue
            native_id = bindings.get("native", {}).get("instance_id")
            fleet_id = bindings.get("t3_fleet", {}).get("instance_id")
            if native_id is not None:
                if native_id != instance_id:
                    continue
            elif fleet_id is not None:
                if fleet_id != instance_id:
                    continue
            else:
                continue
        if reasoning is not None and seat["effort"] != reasoning:
            continue
        names.append(seat_name)
    return names


def _parse_machine(name: str, raw: Any) -> dict[str, Any]:
    where = f"machine '{name}'"
    body = _mapping(raw, where)
    _reject_unknown(
        body,
        (
            "os",
            "capabilities",
            "preferred_workloads",
            "discouraged_workloads",
            "prohibited_workloads",
            "priority",
            "concurrency",
            "enabled",
            "draining",
            "fallback",
            "allowed_overrides",
            "node_id",
            "notes",
        ),
        where,
    )
    return {
        "os": _enum(body.get("os"), f"{where} field 'os'", OS_VALUES, default="unknown"),
        "capabilities": _name_list(body.get("capabilities"), f"{where} field 'capabilities'"),
        "preferred_workloads": _name_list(body.get("preferred_workloads"), f"{where} field 'preferred_workloads'"),
        "discouraged_workloads": _name_list(
            body.get("discouraged_workloads"), f"{where} field 'discouraged_workloads'"
        ),
        "prohibited_workloads": _name_list(body.get("prohibited_workloads"), f"{where} field 'prohibited_workloads'"),
        "priority": _int(body.get("priority"), f"{where} field 'priority'", default=100, low=0, high=MAX_PRIORITY),
        "concurrency": _int(
            body.get("concurrency"), f"{where} field 'concurrency'", default=1, low=0, high=MAX_CONCURRENCY
        ),
        "enabled": _bool(body.get("enabled"), f"{where} field 'enabled'", default=True),
        "draining": _bool(body.get("draining"), f"{where} field 'draining'", default=False),
        "fallback": _name_list(body.get("fallback"), f"{where} field 'fallback'"),
        "allowed_overrides": _name_list(body.get("allowed_overrides"), f"{where} field 'allowed_overrides'"),
        "node_id": None if body.get("node_id") is None else _identity(body.get("node_id"), f"{where} field 'node_id'"),
        "notes": _text(body.get("notes"), f"{where} field 'notes'"),
    }


def _is_opencode_contributor_free(provider: str | None, model: str | None) -> bool:
    if not model:
        return False
    if provider == "opencode" and model in OPENCODE_MUSE_SPARK_1_3_IDS:
        return True
    if model in OPENCODE_MUSE_SPARK_1_3_IDS:
        return True
    if model.startswith("opencode/") and model.split("/", 1)[1] in OPENCODE_MUSE_SPARK_1_3_IDS:
        return True
    return False


def _parse_seat(name: str, raw: Any) -> dict[str, Any]:
    where = f"seat '{name}'"
    body = _mapping(raw, where)
    _reject_unknown(
        body,
        (
            "provider",
            "model",
            "effort",
            "eligible_machines",
            "concurrency",
            "timeout_seconds",
            "fallback",
            "cost_class",
            "training_allowed",
            "retention",
            "quota_pool",
            "enabled",
            "pinned",
            "bindings",
            "notes",
        ),
        where,
    )
    provider = _required_name(body.get("provider"), f"{where} field 'provider'")
    model = _identity(body.get("model"), f"{where} field 'model'")
    bindings = parse_bindings(body.get("bindings"), f"{where} field 'bindings'")
    native_model = bindings.get("native", {}).get("model")
    brigade_model = bindings.get("brigade", {}).get("model")

    is_contributor = (
        _is_opencode_contributor_free(provider, model)
        or _is_opencode_contributor_free(provider, native_model)
        or _is_opencode_contributor_free(provider, brigade_model)
    )
    raw_cost_class = body.get("cost_class")
    if is_contributor and raw_cost_class in ("paid", "subscription"):
        raise FleetPolicyError(
            f"fleet policy {where} contradicts contributor terms: cannot claim cost_class '{raw_cost_class}'"
        )
    if is_contributor and body.get("training_allowed") is False:
        raise FleetPolicyError(f"fleet policy {where} contradicts contributor terms: training_allowed cannot be false")

    cost_class_default = "free" if is_contributor else "unknown"
    training_default = True if is_contributor else False

    cost_class = _enum(
        body.get("cost_class"),
        f"{where} field 'cost_class'",
        COST_CLASS_VALUES,
        default=cost_class_default,
    )
    training_allowed = _bool(
        body.get("training_allowed"),
        f"{where} field 'training_allowed'",
        default=training_default,
    )
    if is_contributor:
        cost_class = "free"
        training_allowed = True

    return {
        "provider": provider,
        "model": model,
        "effort": _name(body.get("effort"), f"{where} field 'effort'", optional=True),
        "eligible_machines": _name_list(body.get("eligible_machines"), f"{where} field 'eligible_machines'"),
        "concurrency": _int(
            body.get("concurrency"), f"{where} field 'concurrency'", default=1, low=0, high=MAX_CONCURRENCY
        ),
        "timeout_seconds": _int(
            body.get("timeout_seconds"),
            f"{where} field 'timeout_seconds'",
            default=None,
            low=1,
            high=MAX_TIMEOUT_SECONDS,
        ),
        "fallback": _name_list(body.get("fallback"), f"{where} field 'fallback'"),
        "cost_class": cost_class,
        "training_allowed": training_allowed,
        "retention": _text(body.get("retention"), f"{where} field 'retention'"),
        "quota_pool": _name(body.get("quota_pool"), f"{where} field 'quota_pool'", optional=True),
        "enabled": _bool(body.get("enabled"), f"{where} field 'enabled'", default=True),
        "pinned": _bool(body.get("pinned"), f"{where} field 'pinned'", default=False),
        "bindings": bindings,
        "notes": _text(body.get("notes"), f"{where} field 'notes'"),
    }


def _parse_consumer(name: str, raw: Any) -> dict[str, Any]:
    where = f"consumer '{name}'"
    body = _mapping(raw, where)
    _reject_unknown(
        body,
        ("default_patches", "reload", "coverage", "adapter_version", "seat_bindings", "legacy_bootstrap", "notes"),
        where,
    )
    return {
        "default_patches": parse_settings(body.get("default_patches") or {}, f"{where} default_patches"),
        "reload": _enum(body.get("reload"), f"{where} field 'reload'", RELOAD_VALUES, default="none"),
        "coverage": _enum(body.get("coverage"), f"{where} field 'coverage'", COVERAGE_VALUES, default="unverified"),
        "adapter_version": _text(body.get("adapter_version"), f"{where} field 'adapter_version'"),
        "seat_bindings": _parse_seat_bindings(body.get("seat_bindings"), f"{where} seat_bindings"),
        "legacy_bootstrap": _bool(body.get("legacy_bootstrap"), f"{where} field 'legacy_bootstrap'", default=False),
        "notes": _text(body.get("notes"), f"{where} field 'notes'"),
    }


def _parse_repository(identity: str, raw: Any) -> dict[str, Any]:
    where = f"repository '{identity}'"
    body = _mapping(raw, where)
    _reject_unknown(body, ("privacy", "patches", "owner", "eligible_machines", "notes"), where)
    return {
        "privacy": _enum(body.get("privacy"), f"{where} field 'privacy'", PRIVACY_VALUES, default="unknown"),
        "patches": parse_settings(body.get("patches") or {}, f"{where} patches"),
        "owner": _text(body.get("owner"), f"{where} field 'owner'"),
        "eligible_machines": _name_list(body.get("eligible_machines"), f"{where} field 'eligible_machines'"),
        "notes": _text(body.get("notes"), f"{where} field 'notes'"),
    }


def _parse_workload_requirement(name: str, raw: Any) -> dict[str, Any]:
    where = f"routing.workload_requirements.{name}"
    body = _mapping(raw, where)
    _reject_unknown(body, ("os", "capabilities"), where)
    return {
        "os": _enum(body.get("os"), f"{where}.os", OS_VALUES, default="unknown"),
        "capabilities": _name_list(body.get("capabilities"), f"{where}.capabilities"),
    }


def _parse_quota_window(window_id: str, raw: Any, where: str) -> dict[str, Any]:
    body = _mapping(raw, where)
    _reject_unknown(body, ("unit", "limit"), where)
    unit = _text(body.get("unit"), f"{where}.unit", optional=False)
    if unit not in ("percent", "requests"):
        raise FleetPolicyError(f"fleet policy {where}.unit must be one of: percent, requests")
    limit = _int(body.get("limit"), f"{where}.limit", default=None, low=1, high=1_000_000_000)
    if limit is None:
        raise FleetPolicyError(f"fleet policy {where}.limit is required")
    return {"window_id": _required_name(window_id, f"{where} window_id"), "unit": unit, "limit": limit}


def _parse_quota_pool(name: str, raw: Any) -> dict[str, Any]:
    where = f"routing.quota_pools.{name}"
    body = _mapping(raw, where)
    _reject_unknown(
        body,
        ("account_id", "provider", "reserve_percent", "freshness_seconds", "windows", "collector_node"),
        where,
    )
    account_id = body.get("account_id")
    provider = body.get("provider")
    if account_id is None or provider is None:
        raise FleetPolicyError(f"fleet policy {where} requires 'account_id' and 'provider'")
    windows_raw = body.get("windows") or {}
    windows_body = _records(windows_raw, f"{where}.windows")
    return {
        "account_id": _identity(account_id, f"{where}.account_id"),
        "provider": _required_name(provider, f"{where}.provider"),
        "reserve_percent": _int(
            body.get("reserve_percent"),
            f"{where}.reserve_percent",
            default=None,
            low=0,
            high=MAX_PERCENT,
        ),
        "freshness_seconds": _int(
            body.get("freshness_seconds"),
            f"{where}.freshness_seconds",
            default=DEFAULT_TELEMETRY_TTL_SECONDS,
            low=1,
            high=MAX_TIMEOUT_SECONDS,
        ),
        "windows": {
            _required_name(window_id, f"{where} window"): _parse_quota_window(
                str(window_id), windows_body[window_id], f"{where}.windows.{window_id}"
            )
            for window_id in sorted(windows_body)
        },
        "collector_node": None
        if body.get("collector_node") is None
        else _identity(body.get("collector_node"), f"{where}.collector_node"),
    }


INVENTORY_COLLECTOR_FIELDS = ("node_id", "source", "provider", "harness", "account_id")


def _optional_identity(raw: Any, where: str) -> str:
    """Identity string, or empty when the inventory tuple uses the empty default."""
    if raw == "":
        return ""
    return _identity(raw, where)


def _parse_inventory_collector(name: str, raw: Any) -> dict[str, Any]:
    where = f"routing.inventory_collectors.{name}"
    body = _mapping(raw, where)
    _reject_unknown(body, INVENTORY_COLLECTOR_FIELDS, where)
    missing = [field for field in INVENTORY_COLLECTOR_FIELDS if field not in body]
    if missing:
        raise FleetPolicyError(f"fleet policy {where} requires {', '.join(missing)}")
    source = _identity(body.get("source"), f"{where}.source")
    if source not in DEFAULT_ALLOWED_SOURCES:
        allowed = ", ".join(sorted(DEFAULT_ALLOWED_SOURCES))
        raise FleetPolicyError(f"fleet policy {where}.source must be one of: {allowed}")
    return {
        "node_id": _identity(body.get("node_id"), f"{where}.node_id"),
        "source": source,
        "provider": _required_name(body.get("provider"), f"{where}.provider"),
        "harness": _optional_identity(body.get("harness"), f"{where}.harness"),
        "account_id": _optional_identity(body.get("account_id"), f"{where}.account_id"),
    }


def inventory_collector_bindings(document: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """Unambiguous provider -> harness/account bindings from configured collectors.

    A provider that appears under more than one harness or account is omitted so
    callers cannot pick a favorable coverage.
    """
    routing = document.get("routing") if isinstance(document, Mapping) else None
    collectors = routing.get("inventory_collectors") if isinstance(routing, Mapping) else None
    if not isinstance(collectors, Mapping):
        return {}
    by_provider: dict[str, set[tuple[str, str]]] = {}
    for record in collectors.values():
        if not isinstance(record, Mapping) or record.get("provider") is None:
            continue
        provider = str(record["provider"])
        by_provider.setdefault(provider, set()).add(
            (str(record.get("harness") or ""), str(record.get("account_id") or ""))
        )
    bindings: dict[str, dict[str, str]] = {}
    for provider, tuples in by_provider.items():
        if len(tuples) != 1:
            continue
        harness, account_id = next(iter(tuples))
        bindings[provider] = {"harness": harness, "account_id": account_id}
    return bindings


def parse_routing(raw: Any) -> dict[str, Any]:
    """Validate static routing options. Missing ``routing`` yields disabled defaults."""
    if raw is None:
        return empty_routing()
    body = _mapping(raw, "routing")
    _reject_unknown(
        body,
        (
            "enabled",
            "telemetry_ttl_seconds",
            "reservation_ttl_seconds",
            "quota_reserve_percent",
            "workload_requirements",
            "quota_pools",
            "inventory_collectors",
        ),
        "routing",
    )
    workloads = _records(body.get("workload_requirements") or {}, "routing.workload_requirements")
    pools = _records(body.get("quota_pools") or {}, "routing.quota_pools")
    collectors = _records(body.get("inventory_collectors") or {}, "routing.inventory_collectors")
    return {
        "enabled": _bool(body.get("enabled"), "routing.enabled", default=False),
        "telemetry_ttl_seconds": _int(
            body.get("telemetry_ttl_seconds"),
            "routing.telemetry_ttl_seconds",
            default=DEFAULT_TELEMETRY_TTL_SECONDS,
            low=1,
            high=MAX_TIMEOUT_SECONDS,
        ),
        "reservation_ttl_seconds": _int(
            body.get("reservation_ttl_seconds"),
            "routing.reservation_ttl_seconds",
            default=DEFAULT_RESERVATION_TTL_SECONDS,
            low=1,
            high=MAX_TIMEOUT_SECONDS,
        ),
        "quota_reserve_percent": _int(
            body.get("quota_reserve_percent"),
            "routing.quota_reserve_percent",
            default=DEFAULT_QUOTA_RESERVE_PERCENT,
            low=0,
            high=MAX_PERCENT,
        ),
        "workload_requirements": {
            _required_name(name, "routing workload"): _parse_workload_requirement(str(name), workloads[name])
            for name in sorted(workloads)
        },
        "quota_pools": {
            _required_name(name, "routing quota pool"): _parse_quota_pool(str(name), pools[name])
            for name in sorted(pools)
        },
        "inventory_collectors": {
            _required_name(name, "routing inventory collector"): _parse_inventory_collector(str(name), collectors[name])
            for name in sorted(collectors)
        },
    }


def parse_document(raw: Any) -> dict[str, Any]:
    """Validate and normalize a policy document. Idempotent on its own output."""
    body = _mapping(raw, "document")
    _reject_unknown(body, TOP_LEVEL_FIELDS, "document")
    if body.get("schema") != POLICY_SCHEMA:
        raise FleetPolicyError(f"fleet policy field 'schema' must be '{POLICY_SCHEMA}'")
    machines = _records(body.get("machines") or {}, "machines")
    seats = _records(body.get("seats") or {}, "seats")
    consumers = _records(body.get("consumers") or {}, "consumers")
    repositories = _records(body.get("repositories") or {}, "repositories")
    parsed_machines = {
        _required_name(name, "machine name"): _parse_machine(str(name), machines[name]) for name in sorted(machines)
    }
    seen_nodes: dict[str, str] = {}
    for machine_name, machine in parsed_machines.items():
        node_id = machine.get("node_id")
        if not node_id:
            continue
        previous = seen_nodes.get(str(node_id))
        if previous is not None:
            raise FleetPolicyError(
                f"fleet policy machine node_id {node_id!r} is mapped to both {previous!r} and {machine_name!r}"
            )
        seen_nodes[str(node_id)] = machine_name
    document = {
        "schema": POLICY_SCHEMA,
        "defaults": parse_settings(body.get("defaults") or {}, "defaults"),
        "machines": parsed_machines,
        "seats": {_required_name(name, "seat name"): _parse_seat(str(name), seats[name]) for name in sorted(seats)},
        "consumers": {
            _required_name(name, "consumer name"): _parse_consumer(str(name), consumers[name])
            for name in sorted(consumers)
        },
        "repositories": {
            _identity(identity, "repository identity"): _parse_repository(str(identity), repositories[identity])
            for identity in sorted(repositories)
        },
        "routing": parse_routing(body.get("routing")),
    }
    rendered = canonical_json(document)
    if len(rendered.encode("ascii")) > MAX_DOCUMENT_BYTES:
        raise FleetPolicyError(f"fleet policy document must be at most {MAX_DOCUMENT_BYTES} bytes")
    return document


def document_digest(document: Mapping[str, Any]) -> str:
    """``sha256:`` digest over the canonical form of a parsed document."""
    parsed = parse_document(document)
    return "sha256:" + hashlib.sha256(canonical_json(parsed).encode("ascii")).hexdigest()


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, Mapping):
        flat: dict[str, Any] = {}
        for key in sorted(value):
            child = f"{prefix}.{key}" if prefix else str(key)
            flat.update(_flatten(value[key], child))
        return flat
    return {prefix: value}


def diff_documents(current: Mapping[str, Any], proposed: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Leaf-level diff of two documents, keyed by dotted path."""
    before = _flatten(parse_document(current))
    after = _flatten(parse_document(proposed))
    return {
        "added": [{"path": path, "to": after[path]} for path in sorted(set(after) - set(before))],
        "removed": [{"path": path, "from": before[path]} for path in sorted(set(before) - set(after))],
        "changed": [
            {"path": path, "from": before[path], "to": after[path]}
            for path in sorted(set(before) & set(after))
            if before[path] != after[path]
        ],
    }


def _flatten_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for section in sorted(settings):
        body = settings[section]
        if not isinstance(body, Mapping):
            continue
        for key in sorted(body):
            flat[f"{section}.{key}"] = body[key]
    return flat


def _nest(flat: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    nested: dict[str, dict[str, Any]] = {}
    for path in sorted(flat):
        section, _, key = path.partition(".")
        nested.setdefault(section, {})[key] = flat[path]
    return nested


def _layers(
    document: Mapping[str, Any],
    consumer: str | None,
    repo_identity: str | None,
    overrides: Mapping[str, Any] | None,
) -> list[tuple[str, dict[str, Any]]]:
    fleet = _flatten_settings(BASE_SETTINGS)
    for path, value in _flatten_settings(document["defaults"]).items():
        if value is not None:
            fleet[path] = value
    consumer_record = document["consumers"].get(consumer) if consumer else None
    repo_record = document["repositories"].get(repo_identity) if repo_identity else None
    return [
        ("fleet-defaults", fleet),
        (
            f"consumer:{consumer}" if consumer else "consumer",
            _flatten_settings(consumer_record["default_patches"]) if consumer_record else {},
        ),
        (
            f"repo:{repo_identity}" if repo_identity else "repo",
            _flatten_settings(repo_record["patches"]) if repo_record else {},
        ),
        ("session", _flatten_settings(parse_settings(overrides or {}, "session overrides"))),
    ]


def resolve_policy(
    document: Mapping[str, Any],
    consumer: str | None,
    repo_identity: str | None,
    overrides: Mapping[str, Any] | None = None,
    override_reason: str | None = None,
    *,
    _parsed: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve effective settings plus a per-leaf source chain.

    Layers apply low to high: fleet defaults (including the built-in floor),
    consumer defaults, repository patches, explicit session overrides. A
    ``null`` at an override layer is *inherit*, not "set to null". Hard
    constraints are applied afterwards and reported separately from
    preferences, so a denied override stays visible instead of vanishing.
    """
    parsed = _parsed if _parsed is not None else parse_document(document)
    layers = _layers(parsed, consumer, repo_identity, overrides)
    paths = sorted({path for _, layer in layers for path in layer})

    effective_flat: dict[str, Any] = {}
    sources: dict[str, dict[str, Any]] = {}
    for path in paths:
        chain: list[dict[str, Any]] = []
        value: Any = None
        winning: str | None = None
        for name, layer in layers:
            if path not in layer:
                chain.append({"layer": name, "value": None, "action": "unset"})
                continue
            candidate = layer[path]
            if candidate is None:
                chain.append({"layer": name, "value": None, "action": "inherit"})
                continue
            chain.append({"layer": name, "value": candidate, "action": "set"})
            value = candidate
            winning = name
        sources[path] = {"value": value, "layer": winning, "chain": chain}
        if winning is not None:
            effective_flat[path] = value

    warnings: list[str] = []
    if consumer and consumer not in parsed["consumers"]:
        warnings.append("unregistered-consumer")
    repo_record = parsed["repositories"].get(repo_identity) if repo_identity else None
    if repo_identity and repo_record is None:
        warnings.append("unknown-repository")
    privacy = repo_record["privacy"] if repo_record else "unknown"

    constraints: list[dict[str, Any]] = []
    denied: list[dict[str, Any]] = []
    if privacy != "public" and effective_flat.get("data.allow_training") is True:
        denied.append(
            {
                "path": "data.allow_training",
                "requested": True,
                "layer": sources["data.allow_training"]["layer"],
                "reason": "unknown-repository" if repo_record is None else "private-repository",
            }
        )
        effective_flat["data.allow_training"] = False
        sources["data.allow_training"]["value"] = False
        sources["data.allow_training"]["layer"] = "constraint:repo-privacy-training"
        constraints.append(
            {
                "rule": "repo-privacy-training",
                "effect": "deny",
                "path": "data.allow_training",
                "repository": repo_identity,
                "privacy": privacy,
            }
        )

    return {
        "schema": POLICY_SCHEMA,
        "consumer": consumer,
        "repository": repo_identity,
        "repository_privacy": privacy,
        "effective": _nest(effective_flat),
        "sources": sources,
        "constraints": constraints,
        "denied_overrides": denied,
        "override_reason": _text(override_reason, "override reason"),
        "warnings": warnings,
    }


def effective_seat_terms(
    document: Mapping[str, Any],
    seat: str,
    consumer: str | None = None,
    *,
    _parsed: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Minimum training, cost, and warning constraints for a seat under an optional consumer overlay."""
    parsed = _parsed if _parsed is not None else parse_document(document)
    record = parsed["seats"].get(seat)
    if record is None:
        raise FleetPolicyError(f"unknown seat '{seat}'")

    potential_models: list[str | None] = [record["model"]]
    if consumer is not None:
        bindings = effective_seat_bindings(parsed, consumer, seat, _parsed=parsed)
        potential_models.append(bindings.get("native", {}).get("model"))
        potential_models.append(bindings.get("brigade", {}).get("model"))
    else:
        seat_bindings = record.get("bindings") or {}
        potential_models.append((seat_bindings.get("native") or {}).get("model"))
        potential_models.append((seat_bindings.get("brigade") or {}).get("model"))
        for c_name in parsed.get("consumers", {}):
            c_bindings = effective_seat_bindings(parsed, c_name, seat, _parsed=parsed)
            potential_models.append(c_bindings.get("native", {}).get("model"))
            potential_models.append(c_bindings.get("brigade", {}).get("model"))

    provider = record["provider"]
    is_contributor = any(_is_opencode_contributor_free(provider, m) for m in potential_models if m)

    if is_contributor:
        cost_class = "free"
        requires_training = True
        warnings = ["contributor-terms-enforced"]
    else:
        cost_class = record.get("cost_class", "unknown")
        requires_training = bool(record.get("training_allowed", False))
        warnings = []

    return {
        "seat": seat,
        "consumer": consumer,
        "provider": provider,
        "model": record["model"],
        "is_contributor": is_contributor,
        "cost_class": cost_class,
        "training_allowed": requires_training,
        "requires_training": requires_training,
        "requires_free": cost_class == "free",
        "warnings": warnings,
        "constraints": {
            "allow_training": requires_training,
            "allow_free": cost_class == "free",
        },
    }


def admissible_seat(
    document: Mapping[str, Any],
    seat: str,
    resolution: Mapping[str, Any],
    *,
    _parsed: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Whether ``seat`` may run under an already-resolved policy, and why not."""
    parsed = _parsed if _parsed is not None else parse_document(document)
    record = parsed["seats"].get(seat)
    if record is None:
        return {"seat": seat, "admissible": False, "reasons": ["unknown-seat"]}
    reasons: list[str] = []
    if not record["enabled"]:
        reasons.append("seat-disabled")

    consumer = resolution.get("consumer")
    terms = effective_seat_terms(parsed, seat, consumer, _parsed=parsed)
    requires_training = terms["requires_training"]
    cost_class = terms["cost_class"]

    effective = resolution.get("effective", {})
    if requires_training and not bool(effective.get("data", {}).get("allow_training", False)):
        reasons.append("training-not-permitted")
    if cost_class == "free" and not bool(effective.get("data", {}).get("allow_free", False)):
        reasons.append("free-not-permitted")
    machine = effective.get("execution", {}).get("machine")
    if machine and record["eligible_machines"] and machine not in record["eligible_machines"]:
        reasons.append("machine-not-eligible")
    return {"seat": seat, "admissible": not reasons, "reasons": reasons}


def validate_inventory(document: Mapping[str, Any], inventory: Mapping[str, Any]) -> dict[str, Any]:
    """Classify each seat's model against a provider inventory snapshot.

    ``inventory`` maps provider to ``{"state": "fresh"|..., "available": [...],
    "retired": [...], "blocked": [...]}``. An inventory that could not be read
    yields ``unavailable``, deliberately distinct from ``missing``: a failed
    provider login is not proof that a saved model is gone.
    """
    parsed = parse_document(document)
    rows: list[dict[str, Any]] = []
    for seat in sorted(parsed["seats"]):
        record = parsed["seats"][seat]
        row = {"seat": seat, "provider": record["provider"], "model": record["model"], "reason": None}
        report = inventory.get(record["provider"]) if isinstance(inventory, Mapping) else None
        if not isinstance(report, Mapping):
            rows.append({**row, "state": "unavailable", "reason": "provider-not-inventoried"})
            continue
        if report.get("state") != "fresh":
            reason = _text(report.get("reason"), "inventory reason") or "inventory-unavailable"
            rows.append({**row, "state": "unavailable", "reason": reason})
            continue
        model = record["model"]
        if model in list(report.get("blocked") or []):
            rows.append({**row, "state": "policy-blocked"})
        elif model in list(report.get("retired") or []):
            rows.append({**row, "state": "retired"})
        elif model in list(report.get("available") or []):
            rows.append({**row, "state": "available"})
        else:
            rows.append({**row, "state": "missing"})
    summary: dict[str, int] = {}
    for row in rows:
        summary[str(row["state"])] = summary.get(str(row["state"]), 0) + 1
    return {"seats": rows, "summary": summary}


def _binding_model(record: Mapping[str, Any], group: str) -> Any:
    bindings = record.get("bindings") if isinstance(record.get("bindings"), Mapping) else {}
    body = bindings.get(group) if isinstance(bindings, Mapping) else None
    if not isinstance(body, Mapping):
        return None
    return body.get("model")


def _identity_changes(record: Mapping[str, Any], candidate: Mapping[str, Any]) -> list[str]:
    changed = [field for field in PIN_IDENTITY_FIELDS if record[field] != candidate[field]]
    for group, key in PIN_BINDING_MODEL_PATHS:
        path = f"bindings.{group}.{key}"
        if _binding_model(record, group) != _binding_model(candidate, group):
            changed.append(path)
    return changed


def _identity_value(record: Mapping[str, Any], field: str) -> Any:
    if field.startswith("bindings."):
        _, group, key = field.split(".", 2)
        bindings = record.get("bindings") if isinstance(record.get("bindings"), Mapping) else {}
        body = bindings.get(group) if isinstance(bindings, Mapping) else None
        if not isinstance(body, Mapping):
            return None
        return body.get(key)
    return record.get(field)


def _copy_binding_models(source: Mapping[str, Any], target: dict[str, Any]) -> None:
    bindings = dict(target.get("bindings") or empty_bindings())
    source_bindings = source.get("bindings") if isinstance(source.get("bindings"), Mapping) else {}
    for group, key in PIN_BINDING_MODEL_PATHS:
        body = dict(bindings.get(group) or {})
        source_body = source_bindings.get(group) if isinstance(source_bindings, Mapping) else None
        body[key] = source_body.get(key) if isinstance(source_body, Mapping) else None
        bindings[group] = body
    target["bindings"] = bindings


def pin_violations(current: Mapping[str, Any], proposed: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Pinned seats may not change identity in the revision that unpins them.

    Changing a pinned seat's provider, model, effort, or model-bearing binding
    leaves takes two saves: an explicit unpin, then the change. Removing a
    pinned seat is also refused. Instance and machine moves stay allowed.
    """
    before = parse_document(current)
    after = parse_document(proposed)
    violations: list[dict[str, Any]] = []
    for seat in sorted(before["seats"]):
        record = before["seats"][seat]
        if not record["pinned"]:
            continue
        candidate = after["seats"].get(seat)
        if candidate is None:
            violations.append({"seat": seat, "reason": "pinned-seat-removed", "changed": []})
            continue
        changed = _identity_changes(record, candidate)
        if not changed:
            continue
        violations.append(
            {
                "seat": seat,
                "reason": "pinned-seat-change" if candidate["pinned"] else "unpin-and-change",
                "changed": changed,
                "from": {field: _identity_value(record, field) for field in changed},
                "to": {field: _identity_value(candidate, field) for field in changed},
            }
        )
    return violations


def preserve_pins(current: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    """Carry pinned seats from ``current`` into ``incoming`` unchanged.

    Bootstrap and import paths use this so seeding a policy from another
    source cannot quietly move a held seat onto a different model.
    """
    before = parse_document(current)
    after = parse_document(incoming)
    for seat, record in before["seats"].items():
        if not record["pinned"]:
            continue
        candidate = dict(after["seats"].get(seat) or record)
        for field in PIN_IDENTITY_FIELDS:
            candidate[field] = record[field]
        _copy_binding_models(record, candidate)
        candidate["pinned"] = True
        after["seats"][seat] = candidate
    after["seats"] = {name: after["seats"][name] for name in sorted(after["seats"])}
    return parse_document(after)
