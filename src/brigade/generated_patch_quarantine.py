"""GeneratedPatchQuarantine (#507): model edits promote only via independent verify.

Model-generated patches are untrusted proposals. The ledger treats them as
quarantined until an independent verifier receipt records repository-test
outcomes together with generation metadata (candidate count and model/version).

Per Lajko et al. (ACM APR 2024, doi:10.1145/3643788.3648021), model confidence,
lexical/textual similarity to known fixes, and repeated sampling are explicitly
non-promoting signals: they may be recorded for audit but never move a skill or
card toward promotion.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

QUARANTINE_SCHEMA = "brigade.generated_patch_quarantine.v1"
QUARANTINE_SCHEMA_VERSION = 1

# Outcome ``source`` values that must never earn a non-zero signal_value.
NON_PROMOTING_OUTCOME_SOURCES = frozenset(
    {
        "model_confidence",
        "confidence",
        "lexical_similarity",
        "textual_similarity",
        "repeated_sampling",
    }
)

# Keys that may appear on a quarantine envelope for audit only. Presence does
# not satisfy eligibility and cannot substitute for repository tests or an
# independent verifier receipt.
NON_PROMOTING_AUDIT_KEYS = frozenset(
    {
        "model_confidence",
        "confidence",
        "lexical_similarity",
        "textual_similarity",
        "repeated_sampling",
        "sampling_rounds",
        "sample_count",
    }
)

_ENV_CANDIDATE_COUNT = "BRIGADE_GENERATED_PATCH_CANDIDATE_COUNT"
_ENV_MODEL = "BRIGADE_GENERATED_PATCH_MODEL"
_ENV_MODEL_VERSION = "BRIGADE_GENERATED_PATCH_MODEL_VERSION"
_ENV_CONTEXT_MODEL = "BRIGADE_CONTEXT_MODEL"


def is_non_promoting_outcome_source(source: str) -> bool:
    """Return True when ``source`` is an explicitly non-promoting outcome source."""
    return source in NON_PROMOTING_OUTCOME_SOURCES


def build_quarantine(
    *,
    candidate_count: int,
    model: str,
    model_version: str,
    audit: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a quarantine envelope for a generated-patch subject binding."""
    payload: dict[str, Any] = {
        "schema": QUARANTINE_SCHEMA,
        "schema_version": QUARANTINE_SCHEMA_VERSION,
        "status": "quarantined",
        "candidate_count": int(candidate_count),
        "model": str(model),
        "model_version": str(model_version),
    }
    if audit:
        for key, value in audit.items():
            if key in NON_PROMOTING_AUDIT_KEYS and key not in payload:
                payload[key] = value
    return payload


def validate_quarantine(payload: object) -> str | None:
    """Return a stable ineligibility reason when quarantine metadata is incomplete."""
    if not isinstance(payload, dict):
        return "generated_patch_quarantine_incomplete"
    if payload.get("schema") not in (None, QUARANTINE_SCHEMA):
        return "generated_patch_quarantine_incomplete"
    schema_version = payload.get("schema_version")
    if schema_version is not None and schema_version != QUARANTINE_SCHEMA_VERSION:
        return "generated_patch_quarantine_incomplete"
    candidate_count = payload.get("candidate_count")
    if not isinstance(candidate_count, int) or isinstance(candidate_count, bool) or candidate_count < 1:
        return "generated_patch_quarantine_incomplete"
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        return "generated_patch_quarantine_incomplete"
    model_version = payload.get("model_version")
    if not isinstance(model_version, str) or not model_version.strip():
        return "generated_patch_quarantine_incomplete"
    return None


def _read_generated_patch_file(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _coerce_candidate_count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 1 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed >= 1 else None
    return None


def _coerce_nonempty_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _merge_metadata(*sources: Mapping[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for source in sources:
        if not source:
            continue
        for key in ("candidate_count", "model", "model_version", *sorted(NON_PROMOTING_AUDIT_KEYS)):
            if key in source and source[key] is not None and key not in merged:
                merged[key] = source[key]
    return merged


def resolve_producer_metadata(
    *,
    target: Path | None,
    session_id: str | None,
    session_payload: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve generation metadata from session artifact, session payload, or env.

    Precedence (first wins per field): session ``generated-patch.json``, then
    ``session_payload["generated_patch"]``, then environment variables. Model
    may fall back to ``BRIGADE_CONTEXT_MODEL`` when the dedicated env is unset.
    """
    env = environ if environ is not None else os.environ
    file_payload: dict[str, Any] | None = None
    if target is not None and isinstance(session_id, str) and session_id:
        file_payload = _read_generated_patch_file(target / ".brigade" / "work" / session_id / "generated-patch.json")
    session_generated: Mapping[str, Any] | None = None
    if isinstance(session_payload, Mapping):
        raw = session_payload.get("generated_patch")
        if isinstance(raw, Mapping):
            session_generated = raw
    env_payload: dict[str, Any] = {}
    env_count = _coerce_candidate_count(env.get(_ENV_CANDIDATE_COUNT))
    if env_count is not None:
        env_payload["candidate_count"] = env_count
    env_model = _coerce_nonempty_str(env.get(_ENV_MODEL)) or _coerce_nonempty_str(env.get(_ENV_CONTEXT_MODEL))
    if env_model is not None:
        env_payload["model"] = env_model
    env_version = _coerce_nonempty_str(env.get(_ENV_MODEL_VERSION))
    if env_version is not None:
        env_payload["model_version"] = env_version
    return _merge_metadata(file_payload, session_generated, env_payload)


def stamp_quarantine_from_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Build a quarantine envelope when required fields are present; else None."""
    if not metadata:
        return None
    candidate_count = _coerce_candidate_count(metadata.get("candidate_count"))
    model = _coerce_nonempty_str(metadata.get("model"))
    model_version = _coerce_nonempty_str(metadata.get("model_version"))
    if candidate_count is None or model is None or model_version is None:
        return None
    audit = {key: metadata[key] for key in NON_PROMOTING_AUDIT_KEYS if key in metadata}
    return build_quarantine(
        candidate_count=candidate_count,
        model=model,
        model_version=model_version,
        audit=audit or None,
    )


def receipt_has_repository_tests(receipt: Mapping[str, Any]) -> bool:
    """True when the receipt carries at least one effectiveness (repo-test) check."""
    commands = receipt.get("commands")
    if not isinstance(commands, list):
        return False
    for command in commands:
        if not isinstance(command, dict):
            continue
        if command.get("check_role") == "effectiveness":
            check_id = command.get("check_id")
            if isinstance(check_id, str) and check_id:
                return True
    return False


def generated_patch_eligibility_reason(
    binding: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> str | None:
    """Return a stable reason when a generated patch remains unscorable.

    Callers must already know ``patch_source == "generated"``. Independence of
    the verifier session is checked separately in ``verify_trial.project_trial``.
    """
    quarantine = binding.get("generated_patch_quarantine")
    incomplete = validate_quarantine(quarantine)
    if incomplete is not None:
        return incomplete
    if not receipt_has_repository_tests(receipt):
        return "generated_patch_missing_repository_tests"
    return None
