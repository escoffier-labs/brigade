"""Shared Fleet model-roster protocol: canonical JSON, MAC, and retired families."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Iterable, Mapping

ROSTER_SCHEMA = "brigade.fleet_model_roster.v1"
ADMISSION_SCHEMA = "brigade.model_admission.v1"
ADMISSION_REQUEST_SCHEMA = "brigade.model_admission_request.v1"
PLAN_SCHEMA = "brigade.fleet_model_plan.v1"
MAC_PREFIX = b"brigade.fleet-model-roster.lkg.v1\0"
MAC_ALGORITHM = "hmac-sha256-node-bearer-v1"
LKG_TTL_SECONDS = 900
PLAN_MAX_TTL_SECONDS = 900
CLOCK_SKEW_SECONDS = 60
SHA256_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
FAMILY_SEPARATORS = ("-", "/", ":")
PROVIDER_SEPARATORS = ("/", ":")
PERMANENT_REASON = "permanently-retired"
PERMANENT_RETIRED_FAMILIES: tuple[tuple[str, str], ...] = (
    ("openai", "gpt-5.4"),
    ("openai", "gpt-5.5"),
)
DIGEST_KEYS = (
    "schema",
    "revision",
    "revision_updated_at",
    "seats",
    "consumer_defaults",
    "retired_models",
    "fleet_policy",
    "consumer_launch_bindings",
)
CACHE_ENVELOPE_KEYS = (
    "schema",
    "revision",
    "revision_updated_at",
    "issued_at",
    "expires_at",
    "audience_node_id",
    "document_sha256",
    "seats",
    "consumer_defaults",
    "retired_models",
)
OPTIONAL_CACHE_ENVELOPE_KEYS = ("fleet_policy", "consumer_launch_bindings")
CONSUMERS = frozenset({"brigade-run", "t3-fleet"})
BINDINGS_SCHEMA = "brigade.fleet_model_bindings.v1"
LAUNCH_BINDING_GROUPS: dict[str, frozenset[str]] = {
    "brigade": frozenset({"cli", "model"}),
    "t3_fleet": frozenset({"instance_id", "service_tier"}),
    "native": frozenset({"instance_id", "model"}),
}
MAX_LAUNCH_BINDING_PAIRS = 256
MAX_LAUNCH_BINDING_BYTES = 65536
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
LAUNCH_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:-]{0,255}$")
LAUNCH_TEXT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ADMISSION_PHASES = frozenset({"controller", "target", "brigade-run", "launch"})
PROOF_PHASES = frozenset({"target", "launch"})
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SEAT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
PROVIDER_ALIASES = {
    "codex": "openai",
    "openai-codex": "openai",
}


def canonical_json(value: Any) -> str:
    """Compact ASCII-safe JSON with sorted object keys."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest_body(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: payload[key] for key in DIGEST_KEYS if key in payload}


def cache_envelope(payload: Mapping[str, Any]) -> dict[str, Any]:
    body = {key: payload[key] for key in CACHE_ENVELOPE_KEYS if key in payload}
    for key in OPTIONAL_CACHE_ENVELOPE_KEYS:
        if key in payload:
            body[key] = payload[key]
    return body


def roster_digest(payload: Mapping[str, Any]) -> str:
    rendered = canonical_json(digest_body(payload))
    return "sha256:" + hashlib.sha256(rendered.encode("ascii")).hexdigest()


def roster_mac(raw_bearer: str, payload: Mapping[str, Any]) -> str:
    message = MAC_PREFIX + canonical_json(cache_envelope(payload)).encode("ascii")
    return hmac.new(raw_bearer.encode("utf-8"), message, hashlib.sha256).hexdigest()


def canonicalize_provider(provider: str) -> str:
    """Map provider aliases onto the permanent-retirement floor."""
    return PROVIDER_ALIASES.get(provider, provider)


def _model_prefixes(provider: str) -> tuple[str, ...]:
    """Allowed provider spellings that may prefix a model id for this provider."""
    canonical = canonicalize_provider(provider)
    prefixes = {provider, canonical}
    for alias, target in PROVIDER_ALIASES.items():
        if target == canonical or canonicalize_provider(alias) == canonical:
            prefixes.add(alias)
            prefixes.add(target)
    return tuple(sorted(prefixes, key=len, reverse=True))


def normalize_model(provider: str, model: str) -> str:
    """Strip a leading provider alias separated by ``/`` or ``:``.

    Hyphens stay in the model id so ``cursor-grok-4.6`` is not treated as a
    ``cursor`` alias of ``grok-4.6``. OpenAI-family aliases (``codex``,
    ``openai-codex``) are stripped so ``codex/gpt-5.4`` matches the permanent
    floor without treating ``gpt-5.40`` as ``gpt-5.4``.
    """
    raw = model.strip()
    for prefix in _model_prefixes(provider):
        for separator in PROVIDER_SEPARATORS:
            token = f"{prefix}{separator}"
            if raw.startswith(token):
                return raw[len(token) :]
    return raw


def family_matches(family: str, model: str) -> bool:
    """True for an exact family or a `-` `/` `:` suffix. `gpt-5.4` does not match `gpt-5.40`."""
    if model == family:
        return True
    return any(model.startswith(f"{family}{separator}") for separator in FAMILY_SEPARATORS)


def retired_reason(
    provider: str,
    model: str,
    rows: Iterable[Mapping[str, Any]] | None = None,
) -> str | None:
    """Return a bounded reason when provider/model matches a retired family."""
    canonical_provider = canonicalize_provider(provider)
    normalized = normalize_model(canonical_provider, normalize_model(provider, model))
    families: dict[tuple[str, str], str] = {
        (canonicalize_provider(provider), family): PERMANENT_REASON for provider, family in PERMANENT_RETIRED_FAMILIES
    }
    if rows is not None:
        for row in rows:
            provider_key = canonicalize_provider(str(row["provider"]))
            family_key = str(row["family"])
            if (provider_key, family_key) not in families:
                families[(provider_key, family_key)] = str(row.get("reason_code") or PERMANENT_REASON)
    for (retired_provider, family), reason in families.items():
        if retired_provider == canonical_provider and family_matches(
            normalize_model(retired_provider, family), normalized
        ):
            return reason
    return None


def validate_roster_rows(payload: Mapping[str, Any]) -> str | None:
    """Reject signed roster collections whose row types are not usable."""
    seats = payload.get("seats")
    if not isinstance(seats, list):
        return "malformed-roster"
    for item in seats:
        if not isinstance(item, dict):
            return "malformed-roster"
        if any(
            not isinstance(item.get(key), str) or not item[key] for key in ("seat", "provider", "model", "reasoning")
        ):
            return "malformed-roster"
        if type(item.get("enabled")) is not bool:
            return "malformed-roster"
        bindings = item.get("bindings")
        if not isinstance(bindings, dict) or set(bindings) != {"brigade", "t3_fleet"}:
            return "malformed-roster"
        brigade = bindings.get("brigade")
        if not isinstance(brigade, dict) or not isinstance(brigade.get("cli"), str):
            return "malformed-roster"
        brigade_keys = set(brigade)
        if brigade_keys == {"cli", "model"}:
            if not isinstance(brigade.get("model"), str) or not brigade["model"]:
                return "malformed-roster"
        elif brigade_keys != {"cli"}:
            return "malformed-roster"
        t3_fleet = bindings.get("t3_fleet")
        if not isinstance(t3_fleet, dict) or set(t3_fleet) != {"instance_id", "service_tier"}:
            return "malformed-roster"
        if not isinstance(t3_fleet.get("instance_id"), str):
            return "malformed-roster"
        if t3_fleet.get("service_tier") is not None and not isinstance(t3_fleet.get("service_tier"), str):
            return "malformed-roster"
    defaults = payload.get("consumer_defaults")
    if not isinstance(defaults, dict):
        return "malformed-roster"
    for key, value in defaults.items():
        if not isinstance(key, str) or (value is not None and type(value) is not str):
            return "malformed-roster"
    retired = payload.get("retired_models")
    if not isinstance(retired, list):
        return "malformed-roster"
    for row in retired:
        if not isinstance(row, dict):
            return "malformed-roster"
        if any(not isinstance(row.get(key), str) or not row[key] for key in ("provider", "family")):
            return "malformed-roster"
        if "reason_code" in row and (not isinstance(row["reason_code"], str) or not row["reason_code"]):
            return "malformed-roster"
        if "permanent" in row and type(row["permanent"]) is not bool:
            return "malformed-roster"
    if "fleet_policy" in payload:
        _, error = parse_fleet_policy_authority(payload.get("fleet_policy"))
        if error is not None:
            return "malformed-roster"
    if "consumer_launch_bindings" in payload:
        error = validate_consumer_launch_bindings(payload.get("consumer_launch_bindings"))
        if error is not None:
            return "malformed-roster"
    return None


def _launch_leaf(value: Any, *, identity: bool) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or _CONTROL_RE.search(value):
        raise ValueError("malformed-leaf")
    pattern = LAUNCH_IDENTITY_PATTERN if identity else LAUNCH_TEXT_PATTERN
    if pattern.fullmatch(value) is None:
        raise ValueError("malformed-leaf")
    return value


def empty_launch_groups() -> dict[str, dict[str, Any]]:
    return {name: {} for name in LAUNCH_BINDING_GROUPS}


def compact_launch_groups(groups: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Keep allowed optional keys with configured values. Empty groups stay present."""
    compacted = empty_launch_groups()
    if not isinstance(groups, Mapping):
        return compacted
    for group, allowed in LAUNCH_BINDING_GROUPS.items():
        body = groups.get(group)
        if not isinstance(body, Mapping):
            continue
        for key in sorted(allowed):
            value = body.get(key)
            if isinstance(value, str) and value:
                compacted[group][key] = value
    return compacted


def validate_consumer_launch_bindings(raw: Any) -> str | None:
    """Reject unsigned or malformed consumer launch-binding projections."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return "malformed-roster"
    if len(raw) > MAX_LAUNCH_BINDING_PAIRS:
        return "malformed-roster"
    pairs = 0
    for consumer, seats in raw.items():
        if not isinstance(consumer, str) or consumer not in CONSUMERS or _CONTROL_RE.search(consumer):
            return "malformed-roster"
        if not isinstance(seats, dict):
            return "malformed-roster"
        if len(seats) > MAX_LAUNCH_BINDING_PAIRS:
            return "malformed-roster"
        for seat, groups in seats.items():
            if not isinstance(seat, str) or SEAT_NAME_PATTERN.fullmatch(seat) is None or _CONTROL_RE.search(seat):
                return "malformed-roster"
            if not isinstance(groups, dict):
                return "malformed-roster"
            if set(groups) - set(LAUNCH_BINDING_GROUPS):
                return "malformed-roster"
            pairs += 1
            if pairs > MAX_LAUNCH_BINDING_PAIRS:
                return "malformed-roster"
            for group, allowed in LAUNCH_BINDING_GROUPS.items():
                body = groups.get(group, {})
                if body is None:
                    body = {}
                if not isinstance(body, dict):
                    return "malformed-roster"
                if set(body) - allowed:
                    return "malformed-roster"
                for key, value in body.items():
                    identity = key in {"model", "instance_id"}
                    try:
                        parsed = _launch_leaf(value, identity=identity)
                    except ValueError:
                        return "malformed-roster"
                    if key in body and value is not None and parsed is None and value != "":
                        return "malformed-roster"
    rendered = canonical_json(raw)
    if len(rendered.encode("ascii")) > MAX_LAUNCH_BINDING_BYTES:
        return "malformed-roster"
    return None


def adapter_plan_binding(
    consumer: str,
    groups: Mapping[str, Any] | None,
    *,
    canonical_model: str | None = None,
    adapter: str | None = None,
) -> dict[str, Any] | None:
    """Exact consumer launch mapping for plan.binding. Canonical model stays separate."""
    if canonical_model is not None and (not isinstance(canonical_model, str) or not canonical_model):
        return None
    compacted = compact_launch_groups(groups)
    if adapter is None and consumer not in ("brigade-run", "t3-fleet"):
        return None
    selected = adapter or ("t3-fleet" if consumer == "t3-fleet" else "brigade-run")
    if selected == "t3-fleet":
        native = compacted.get("native") or {}
        t3_fleet = compacted.get("t3_fleet") or {}
        instance_id = native.get("instance_id") or t3_fleet.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id:
            return None
        payload: dict[str, Any] = {
            "instance_id": instance_id,
            "service_tier": t3_fleet.get("service_tier") or None,
        }
        native_model = native.get("model")
        if isinstance(native_model, str) and native_model:
            payload["model"] = native_model
        return payload
    brigade = compacted.get("brigade") or {}
    instance_id = brigade.get("cli")
    if not isinstance(instance_id, str) or not instance_id:
        return None
    payload = {"instance_id": instance_id, "service_tier": None}
    launch_model = brigade.get("model")
    if isinstance(launch_model, str) and launch_model:
        payload["model"] = launch_model
    return payload


def parse_fleet_policy_authority(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Parse signed ``fleet_policy`` authority metadata.

    Missing metadata is ``(None, None)`` and means a legacy unactivated
    snapshot. Malformed metadata is an error, never unenrolled.
    """
    if raw is None:
        return None, None
    if not isinstance(raw, dict) or set(raw) != {"active", "version", "digest"}:
        return None, "malformed-authority"
    if type(raw.get("active")) is not bool:
        return None, "malformed-authority"
    version = raw.get("version")
    if type(version) is not int or version <= 0:
        return None, "malformed-authority"
    digest = raw.get("digest")
    if not isinstance(digest, str) or SHA256_DIGEST_PATTERN.fullmatch(digest) is None:
        return None, "malformed-authority"
    return {"active": raw["active"], "version": version, "digest": digest}, None


def binding_launch_models(
    seat: Mapping[str, Any],
    *,
    launch_groups: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Canonical seat.model plus trusted native/brigade launch-model leaves."""
    models: list[str] = []
    canonical = seat.get("model")
    if isinstance(canonical, str) and canonical:
        models.append(canonical)
    sources: list[Mapping[str, Any]] = []
    bindings = seat.get("bindings")
    if isinstance(bindings, Mapping):
        sources.append(bindings)
    if isinstance(launch_groups, Mapping):
        sources.append(launch_groups)
    for source in sources:
        for group in ("brigade", "native"):
            body = source.get(group)
            if not isinstance(body, Mapping):
                continue
            launch = body.get("model")
            if isinstance(launch, str) and launch and launch not in models:
                models.append(launch)
    return tuple(models)


def brigade_launch_model(seat: Mapping[str, Any]) -> str | None:
    """Exact brigade-run launch model when ``bindings.brigade.model`` is set."""
    bindings = seat.get("bindings")
    if not isinstance(bindings, Mapping):
        return None
    brigade = bindings.get("brigade")
    if not isinstance(brigade, Mapping):
        return None
    model = brigade.get("model")
    return model if isinstance(model, str) and model else None


def consumer_launch_groups(roster: Mapping[str, Any], consumer: str, seat: str) -> Mapping[str, Any] | None:
    """One seat's signed ``consumer_launch_bindings`` groups, or None when unprojected."""
    launch_map = roster.get("consumer_launch_bindings")
    if not isinstance(launch_map, Mapping):
        return None
    consumer_map = launch_map.get(consumer)
    if not isinstance(consumer_map, Mapping):
        return None
    groups = consumer_map.get(seat)
    return groups if isinstance(groups, Mapping) else None


def effective_brigade_launch_model(
    seat: Mapping[str, Any],
    *,
    launch_groups: Mapping[str, Any] | None = None,
) -> str | None:
    """Reviewed native launch id for brigade-run: consumer projection first, then the seat row.

    The value is always an explicit binding leaf. Nothing here derives a native
    id from the canonical slug by delimiter or alias rewriting.
    """
    if isinstance(launch_groups, Mapping):
        brigade = compact_launch_groups(launch_groups).get("brigade") or {}
        projected = brigade.get("model")
        if isinstance(projected, str) and projected:
            return projected
    return brigade_launch_model(seat)
