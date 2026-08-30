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
MAC_PREFIX = b"brigade.fleet-model-roster.lkg.v1\0"
MAC_ALGORITHM = "hmac-sha256-node-bearer-v1"
LKG_TTL_SECONDS = 900
CLOCK_SKEW_SECONDS = 60
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
)
CACHE_ENVELOPE_KEYS = (
    "schema",
    "revision",
    "revision_updated_at",
    "issued_at",
    "expires_at",
    "audience_node_id",
    "roster_digest",
    "seats",
    "consumer_defaults",
    "retired_models",
)
CONSUMERS = frozenset({"brigade-run", "t3-fleet"})
ADMISSION_PHASES = frozenset({"controller", "target", "brigade-run"})
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
    return {key: payload[key] for key in CACHE_ENVELOPE_KEYS if key in payload}


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
    families: dict[tuple[str, str], str] = {family: PERMANENT_REASON for family in PERMANENT_RETIRED_FAMILIES}
    if rows is not None:
        for row in rows:
            families[(str(row["provider"]), str(row["family"]))] = str(row.get("reason_code") or PERMANENT_REASON)
    for (retired_provider, family), reason in families.items():
        retired_canonical = canonicalize_provider(retired_provider)
        permanent = (retired_provider, family) in PERMANENT_RETIRED_FAMILIES
        provider_matches = retired_canonical == canonical_provider if permanent else retired_provider == provider
        if provider_matches and family_matches(normalize_model(retired_canonical, family), normalized):
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
        if "bindings" in item and item["bindings"] is not None and not isinstance(item["bindings"], dict):
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
        if not isinstance(row.get("provider"), str) or not isinstance(row.get("family"), str):
            return "malformed-roster"
        if "permanent" in row and type(row["permanent"]) is not bool:
            return "malformed-roster"
    return None
