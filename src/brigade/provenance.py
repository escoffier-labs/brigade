"""Provenance envelope builder, validator, and legacy read synthesis.

Implements ``brigade.provenance-envelope.v1`` (see
docs/proposals/provenance-envelope.md, Slice 1). The envelope stamps every
evidence item and inter-seat message with a versioned source/origin/trust
record plus an exact-byte SHA-256 content digest. This module is the shared
schema layer; ingestion, consumers, and CLI enforcement land in later slices.

Standard library only. Brigade is zero-runtime-dependency.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence

SCHEMA = "brigade.provenance-envelope.v1"
SCHEMA_VERSION = 1
TRUST_POLICY_SCHEMA = "brigade.trust-policy.v1"
TRUST_POLICY_VERSION = 1
LEGACY_DISPLAY = "UNKNOWN PROVENANCE - legacy item"
MAX_COMPACT_BYTES = 4096
MAX_REPOSITORY_REVISION_BYTES = 256

ORIGINS = frozenset({"operator-input", "workspace", "agent-session", "external-service", "external-web", "unknown"})
MODALITIES = frozenset({"human-written", "model-generated", "tool-output", "external-web", "mixed", "unknown"})
ATTRIBUTIONS = frozenset({"observed", "declared", "inferred"})
TRUST_LABELS = frozenset({"unknown", "untrusted", "reviewed", "verified", "quarantined"})
INJECTION_STATUSES = frozenset({"clean", "flagged", "pending", "error"})
LOCATOR_KINDS = frozenset({"repo-relative", "uri"})
CONTENT_SCOPES = frozenset({"item.text.utf8.v1", "message.text.utf8.v1"})
RAW_SCOPE = "exact_bytes"
HASH_ALGORITHM = "sha256"

# Closed names derived from the build_envelope/validate_envelope schema. Inbound
# adapters may retain only importer-owned identity aliases outside this set.
_ENVELOPE_METADATA_FIELDS = {
    "schema": ("schema", "schema_version"),
    "source": ("source", "source_system", "source_kind", "source_producer"),
    "origin": ("origin",),
    "repository": ("repository", "repository_id", "repo_id", "repository_revision"),
    "session": ("session", "session_id", "session_harness"),
    "collection": ("collection_id", "item_id"),
    "locator": ("locator", "locator_kind", "locator_value"),
    "classification": ("attribution", "modality"),
    "trust": (
        "trust",
        "trust_label",
        "trust_assigned_by",
        "trust_assigned_at",
        "trust_policy",
        "trust_policy_schema",
        "trust_policy_schema_version",
        "injection",
        "injection_status",
        "injection_count",
        "injection_rules",
    ),
    "hashes": (
        "hashes",
        "content_algorithm",
        "content_scope",
        "content",
        "content_hash",
        "content_hash_algorithm",
        "content_hash_scope",
        "raw_algorithm",
        "raw_scope",
        "raw",
        "raw_hash",
        "raw_hash_algorithm",
        "raw_hash_scope",
        "content_sha256",
        "raw_sha256",
        "content_digest",
        "raw_digest",
    ),
    "timestamps": ("captured_at", "ingested_at"),
}
ENVELOPE_METADATA_RESERVED_KEYS = frozenset(
    key for field_keys in _ENVELOPE_METADATA_FIELDS.values() for key in field_keys
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _is_legacy(env: Mapping[str, Any]) -> bool:
    return (
        env.get("origin") == "unknown"
        and env.get("modality") == "unknown"
        and env.get("attribution") == "inferred"
        and isinstance(env.get("trust"), Mapping)
        and env.get("trust", {}).get("label") == "unknown"
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_sha256(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX64.match(value))


def _is_absolute_locator(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value.startswith("/"):
        return True
    if value.startswith("\\"):
        return True
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        return True
    if value.lower().startswith("file:"):
        return True
    return False


def is_absolute_locator(value: Any) -> bool:
    """Return whether a locator value is an absolute filesystem path or file URI."""
    return _is_absolute_locator(value)


def is_safe_identity_label(value: Any) -> bool:
    """Return whether an identity label cannot be interpreted as a locator."""
    if not isinstance(value, str) or not value or value != value.strip() or "\\" in value:
        return False
    if _is_absolute_locator(value):
        return False
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        return False
    return ".." not in value.split("/")


def is_safe_repository_revision(value: Any) -> bool:
    """Return whether a repository revision is a bounded, non-locator label."""
    if not isinstance(value, str) or not value or value != value.strip() or "\\" in value:
        return False
    if len(value.encode("utf-8")) > MAX_REPOSITORY_REVISION_BYTES or _is_absolute_locator(value):
        return False
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
        return False
    return ".." not in value.split("/")


def validate_envelope(
    env: Any,
    *,
    inbound_adapter: bool = False,
    authority_proof: Mapping[str, Any] | None = None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(env, Mapping):
        return ["envelope must be a JSON object"]

    if env.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA!r}")
    if env.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")

    legacy = _is_legacy(env)

    src = env.get("source")
    if not isinstance(src, Mapping):
        errors.append("source must be an object")
    else:
        for key in ("system", "kind", "producer"):
            val = src.get(key)
            if not isinstance(val, str) or not val:
                errors.append(f"source.{key} must be a non-empty string")

    origin = env.get("origin")
    if origin not in ORIGINS:
        errors.append(f"origin {origin!r} is not in the closed set {sorted(ORIGINS)}")

    modality = env.get("modality")
    if modality not in MODALITIES:
        errors.append(f"modality {modality!r} is not in the closed set {sorted(MODALITIES)}")

    attribution = env.get("attribution")
    if attribution not in ATTRIBUTIONS:
        errors.append(f"attribution {attribution!r} is not in the closed set {sorted(ATTRIBUTIONS)}")

    repo = env.get("repository")
    if repo is None:
        if not legacy:
            errors.append("repository must be an object")
    elif not isinstance(repo, Mapping):
        errors.append("repository must be an object or null")
    else:
        rid = repo.get("id")
        if not isinstance(rid, str) or not rid:
            errors.append("repository.id must be a non-empty string")
        elif not is_safe_identity_label(rid):
            errors.append("repository.id must be a safe identity label")
        rev = repo.get("revision")
        if rev is not None and not isinstance(rev, str):
            errors.append("repository.revision must be a string or null")
        elif rev is not None and not is_safe_repository_revision(rev):
            errors.append("repository.revision must be a safe revision label")

    session = env.get("session")
    if session is None:
        if not legacy:
            errors.append("session must be an object")
    elif not isinstance(session, Mapping):
        errors.append("session must be an object or null")
    else:
        sid = session.get("id")
        if sid is not None and not isinstance(sid, str):
            errors.append("session.id must be a string or null")
        elif sid is not None and not is_safe_identity_label(sid):
            errors.append("session.id must be a safe identity label")
        harness = session.get("harness")
        if harness is not None and not isinstance(harness, str):
            errors.append("session.harness must be a string or null")
        elif harness is not None and not is_safe_identity_label(harness):
            errors.append("session.harness must be a safe identity label")

    for key in ("collection_id", "item_id"):
        val = env.get(key)
        if val is None:
            if not legacy:
                errors.append(f"{key} must be a string")
        elif not isinstance(val, str):
            errors.append(f"{key} must be a string or null")
        elif not is_safe_identity_label(val):
            errors.append(f"{key} must be a safe identity label")

    locator = env.get("locator")
    if locator is None:
        if not legacy:
            errors.append("locator must be an object")
    elif not isinstance(locator, Mapping):
        errors.append("locator must be an object or null")
    else:
        lkind = locator.get("kind")
        if lkind not in LOCATOR_KINDS:
            errors.append(f"locator.kind {lkind!r} is not in the closed set {sorted(LOCATOR_KINDS)}")
        lvalue = locator.get("value")
        if not isinstance(lvalue, str) or not lvalue:
            errors.append("locator.value must be a non-empty string")
        elif _is_absolute_locator(lvalue) or (
            lkind == "repo-relative" and ".." in lvalue.replace("\\", "/").split("/")
        ):
            errors.append(f"locator.value {lvalue!r} is unsafe; locator must be repo-relative or a non-file URI")

    trust = env.get("trust")
    if not isinstance(trust, Mapping):
        errors.append("trust must be an object")
    else:
        label = trust.get("label")
        if label not in TRUST_LABELS:
            errors.append(f"trust.label {label!r} is not in the closed set {sorted(TRUST_LABELS)}")
        assigned_by = trust.get("assigned_by")
        if not isinstance(assigned_by, str) or not assigned_by:
            errors.append("trust.assigned_by must be a non-empty string")
        assigned_at = trust.get("assigned_at")
        if assigned_at is not None and not isinstance(assigned_at, str):
            errors.append("trust.assigned_at must be a string or null")

        policy = trust.get("trust_policy")
        if not isinstance(policy, Mapping):
            errors.append("trust.trust_policy must be an object")
        else:
            if policy.get("schema") != TRUST_POLICY_SCHEMA:
                errors.append(f"trust.trust_policy.schema must be {TRUST_POLICY_SCHEMA!r}")
            if policy.get("schema_version") != TRUST_POLICY_VERSION:
                errors.append(f"trust.trust_policy.schema_version must be {TRUST_POLICY_VERSION}")

        injection = trust.get("injection")
        if not isinstance(injection, Mapping):
            errors.append("trust.injection must be an object")
        else:
            status = injection.get("status")
            if status not in INJECTION_STATUSES:
                errors.append(
                    f"trust.injection.status {status!r} is not in the closed set {sorted(INJECTION_STATUSES)}"
                )
            count = injection.get("count")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                errors.append("trust.injection.count must be a nonnegative integer")
            rules = injection.get("rules")
            if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
                errors.append("trust.injection.rules must be a list")
            else:
                for rule in rules:
                    if not isinstance(rule, str) or not rule:
                        errors.append("trust.injection.rules entries must be non-empty strings")

        if inbound_adapter and label in ("reviewed", "verified"):
            if not isinstance(authority_proof, Mapping):
                errors.append(
                    f"inbound adapter trust.label {label!r} requires authority_proof with assigned_by and label"
                )
            else:
                proof_keys = set(authority_proof.keys())
                if proof_keys != {"assigned_by", "label"}:
                    errors.append("authority_proof must have exactly assigned_by and label keys")
                else:
                    if authority_proof.get("assigned_by") != assigned_by:
                        errors.append("authority_proof.assigned_by must match trust.assigned_by")
                    if authority_proof.get("label") != label:
                        errors.append("authority_proof.label must match trust.label")

    hashes = env.get("hashes")
    if not isinstance(hashes, Mapping):
        errors.append("hashes must be an object")
    else:
        if hashes.get("content_algorithm") != HASH_ALGORITHM:
            errors.append(f"hashes.content_algorithm must be {HASH_ALGORITHM!r}")
        cscope = hashes.get("content_scope")
        if cscope not in CONTENT_SCOPES:
            errors.append(f"hashes.content_scope {cscope!r} is not in the closed set {sorted(CONTENT_SCOPES)}")
        content = hashes.get("content")
        if content is None:
            if not legacy:
                errors.append("hashes.content digest must be a bare lowercase 64-char hex string")
        elif not _valid_digest(content):
            errors.append("hashes.content digest must be a bare lowercase 64-char hex string")

        raw = hashes.get("raw")
        if raw is None:
            if hashes.get("raw_algorithm") is not None:
                errors.append("hashes.raw_algorithm must be null when hashes.raw is null")
            if hashes.get("raw_scope") is not None:
                errors.append("hashes.raw_scope must be null when hashes.raw is null")
        else:
            if not _valid_digest(raw):
                errors.append("hashes.raw digest must be a bare lowercase 64-char hex string")
            if hashes.get("raw_algorithm") != HASH_ALGORITHM:
                errors.append(f"hashes.raw_algorithm must be {HASH_ALGORITHM!r}")
            if hashes.get("raw_scope") != RAW_SCOPE:
                errors.append(f"hashes.raw_scope must be {RAW_SCOPE!r}")

    for key in ("captured_at", "ingested_at"):
        val = env.get(key)
        if val is not None and not isinstance(val, str):
            errors.append(f"{key} must be a string or null")

    if errors:
        return errors

    compact = json.dumps(env, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    if len(compact.encode("utf-8")) > MAX_COMPACT_BYTES:
        errors.append(f"envelope compact JSON size exceeds {MAX_COMPACT_BYTES} bytes")
    return errors


def build_envelope(
    *,
    source_system: str,
    source_kind: str,
    source_producer: str,
    origin: str,
    repository_id: str,
    repository_revision: str | None,
    session_id: str | None,
    session_harness: str | None,
    collection_id: str,
    item_id: str,
    locator_kind: str,
    locator_value: str,
    attribution: str,
    modality: str,
    trust_label: str,
    trust_assigned_by: str,
    trust_assigned_at: str | None,
    injection_status: str,
    injection_count: int,
    injection_rules: Sequence[str],
    text: str,
    raw_bytes: bytes | None,
    content_scope: str,
    captured_at: str | None,
    ingested_at: str | None,
) -> dict[str, Any]:
    content_digest = content_sha256(text)
    if raw_bytes is None:
        raw_digest: str | None = None
        raw_algorithm: str | None = None
        raw_scope: str | None = None
    else:
        raw_digest = sha256_bytes(raw_bytes)
        raw_algorithm = HASH_ALGORITHM
        raw_scope = RAW_SCOPE

    env: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "source": {
            "system": source_system,
            "kind": source_kind,
            "producer": source_producer,
        },
        "origin": origin,
        "repository": {"id": repository_id, "revision": repository_revision},
        "session": {"id": session_id, "harness": session_harness},
        "collection_id": collection_id,
        "item_id": item_id,
        "locator": {"kind": locator_kind, "value": locator_value},
        "attribution": attribution,
        "modality": modality,
        "trust": {
            "label": trust_label,
            "assigned_by": trust_assigned_by,
            "assigned_at": trust_assigned_at,
            "trust_policy": {
                "schema": TRUST_POLICY_SCHEMA,
                "schema_version": TRUST_POLICY_VERSION,
            },
            "injection": {
                "status": injection_status,
                "count": injection_count,
                "rules": list(injection_rules),
            },
        },
        "hashes": {
            "content_algorithm": HASH_ALGORITHM,
            "content_scope": content_scope,
            "content": content_digest,
            "raw_algorithm": raw_algorithm,
            "raw_scope": raw_scope,
            "raw": raw_digest,
        },
        "captured_at": captured_at,
        "ingested_at": ingested_at,
    }
    errors = validate_envelope(env)
    if errors:
        raise ValueError("invalid provenance envelope: " + "; ".join(errors))
    return env


def verify_content_digest(text: str, env: Mapping[str, Any] | None) -> bool:
    """Return True when ``hashes.content`` is absent or matches exact UTF-8 bytes."""

    if not isinstance(env, Mapping):
        return True
    hashes = env.get("hashes")
    if not isinstance(hashes, Mapping):
        return True
    content = hashes.get("content")
    if content is None:
        return True
    if not _valid_digest(content):
        return False
    return content == content_sha256(text)


def verify_raw_digest(raw: bytes | None, env: Mapping[str, Any] | None) -> bool:
    """Return True when ``hashes.raw`` is absent or matches the materialized bytes."""

    if not isinstance(env, Mapping):
        return True
    hashes = env.get("hashes")
    if not isinstance(hashes, Mapping):
        return True
    raw_digest = hashes.get("raw")
    if raw_digest is None:
        return True
    if raw is None or not _valid_digest(raw_digest):
        return False
    return raw_digest == sha256_bytes(raw)


def injection_status(env: Mapping[str, Any] | None) -> str:
    if not isinstance(env, Mapping):
        return ""
    trust = env.get("trust")
    if not isinstance(trust, Mapping):
        return ""
    injection = trust.get("injection")
    if not isinstance(injection, Mapping):
        return ""
    status = injection.get("status")
    return status if isinstance(status, str) else ""


def is_legacy_unknown(env: Mapping[str, Any] | None) -> bool:
    return (
        isinstance(env, Mapping)
        and _is_legacy(env)
        and (not isinstance(env.get("hashes"), Mapping) or env.get("hashes", {}).get("content") is None)
    )


def apply_bundle_integrity(bundle: dict[str, Any]) -> dict[str, Any]:
    """Keep mismatched bundle items metadata-only and preserve omission counters.

    Python fetch has no v1 forensic reveal path. Snippets and artifact bodies
    are stripped when ``integrity_mismatch`` is set or a provided envelope
    content digest does not match a materialized ``text`` field.

    Clean records are left unchanged: this does not invent
    ``integrity_mismatch: false`` or ``integrity_omitted: 0`` on a raw
    projection that did not already carry those fields.
    """

    raw_results = bundle.get("results")
    if not isinstance(raw_results, list):
        return bundle
    omitted = 0
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        env = item.get("provenance")
        if not isinstance(env, Mapping):
            metadata = item.get("metadata")
            if isinstance(metadata, Mapping):
                env = metadata.get("provenance")
        text = item.get("text")
        mismatch = bool(item.get("integrity_mismatch"))
        if isinstance(text, str) and isinstance(env, Mapping) and not verify_content_digest(text, env):
            mismatch = True
        if mismatch:
            item["integrity_mismatch"] = True
            item["snippet"] = ""
            item.pop("text", None)
            item.pop("summary", None)
            artifacts = item.get("artifacts")
            if isinstance(artifacts, list):
                for art in artifacts:
                    if isinstance(art, dict):
                        art.pop("text", None)
            omitted += 1
    existing = bundle.get("integrity_omitted")
    if omitted > 0 and (not isinstance(existing, int) or existing < omitted):
        bundle["integrity_omitted"] = omitted
    return bundle


def synthesize_legacy_provenance() -> tuple[dict[str, Any], str]:
    env: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "source": {"system": "legacy", "kind": "legacy", "producer": "legacy.read_synthesis"},
        "origin": "unknown",
        "repository": None,
        "session": None,
        "collection_id": None,
        "item_id": None,
        "locator": None,
        "attribution": "inferred",
        "modality": "unknown",
        "trust": {
            "label": "unknown",
            "assigned_by": "ingest:legacy.read_synthesis",
            "assigned_at": None,
            "trust_policy": {
                "schema": TRUST_POLICY_SCHEMA,
                "schema_version": TRUST_POLICY_VERSION,
            },
            "injection": {"status": "clean", "count": 0, "rules": []},
        },
        "hashes": {
            "content_algorithm": HASH_ALGORITHM,
            "content_scope": "item.text.utf8.v1",
            "content": None,
            "raw_algorithm": None,
            "raw_scope": None,
            "raw": None,
        },
        "captured_at": None,
        "ingested_at": None,
    }
    return env, LEGACY_DISPLAY
