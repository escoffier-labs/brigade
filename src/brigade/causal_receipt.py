"""Causal lineage companion record for plan/run/verify/outcome/handoff.

Implements ``brigade.causal_receipt.v1`` (issue #493). The companion stores
only a stable subject reference and typed parent links. Producer, actor,
origin, modality, trust, locators, and content hashes stay on
``brigade.provenance-envelope.v1`` and must not be copied here.

Standard library only. Brigade is zero-runtime-dependency.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import provenance

SCHEMA = "brigade.causal_receipt.v1"
SCHEMA_VERSION = 1
MAX_PARENTS = 16
MAX_COMPACT_BYTES = 2048
MAX_DIAGNOSTIC_LEN = 240
MAX_DIAGNOSTICS = 8
ARTIFACT_FIELD = "causal_receipt"
HANDOFF_SIDECAR_SUFFIX = ".causal-receipt.json"

ARTIFACT_KINDS = frozenset(
    {
        "plan",
        "run",
        "worker-result",
        "synthesis",
        "verify",
        "outcome",
        "handoff",
    }
)
RELATIONS = frozenset(
    {
        "planned_from",
        "executed_from",
        "verified_from",
        "captured_from",
        "handed_off_from",
        "synthesized_from",
    }
)
LINK_SOURCES = frozenset({"recorded", "inferred"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_KEYS = frozenset(
    {
        "prompt",
        "prompts",
        "text",
        "output",
        "model_output",
        "tool_arguments",
        "tool_args",
        "retrieved",
        "retrieved_content",
        "stack",
        "stack_trace",
        "traceback",
        "password",
        "secret",
        "token",
        "credential",
        "credentials",
        "raw_text",
        "raw_messages",
        "origin",
        "trust",
        "modality",
        "attribution",
        "hashes",
        "locator",
        "repository",
        "session",
        "collection_id",
        "item_id",
        "captured_at",
        "ingested_at",
    }
)
_ALLOWED_TOP_LEVEL = frozenset({"schema", "schema_version", "subject", "parents", "parent_manifest"})
_ALLOWED_SUBJECT = frozenset({"kind", "id"})
_ALLOWED_PARENT = frozenset({"relation", "kind", "id", "digest", "link"})
_ALLOWED_MANIFEST = frozenset({"id", "digest"})


def _bound(message: str) -> str:
    if len(message) <= MAX_DIAGNOSTIC_LEN:
        return message
    return message[: MAX_DIAGNOSTIC_LEN - 1] + "…"


def _cap(errors: list[str]) -> list[str]:
    if len(errors) <= MAX_DIAGNOSTICS:
        return errors
    hidden = len(errors) - (MAX_DIAGNOSTICS - 1)
    return [*errors[: MAX_DIAGNOSTICS - 1], _bound(f"{hidden} additional lineage diagnostic(s) omitted")]


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX64.fullmatch(value))


def compact_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")


def receipt_digest(payload: Mapping[str, Any]) -> str:
    return provenance.sha256_bytes(compact_bytes(payload))


def artifact_key(kind: str, artifact_id: str) -> str:
    return f"{kind}:{artifact_id}"


def worker_result_id(run_id: str, worker: str, ordinal: int) -> str:
    return f"{run_id}:{worker}:{ordinal}"


def _collect_forbidden_keys(value: Any, *, found: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and key in _FORBIDDEN_KEYS:
                found.add(key)
            _collect_forbidden_keys(item, found=found)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _collect_forbidden_keys(item, found=found)


def _safe_id(value: Any, *, field: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value:
        errors.append(_bound(f"{field} must be a non-empty string"))
        return
    if not provenance.is_safe_identity_label(value):
        errors.append(_bound(f"{field} must be a safe identity label"))


def _validate_parent(parent: Any, *, index: int, errors: list[str]) -> None:
    label = f"parents[{index}]"
    if not isinstance(parent, Mapping):
        errors.append(_bound(f"{label} must be an object"))
        return
    unknown = sorted(set(parent.keys()) - _ALLOWED_PARENT)
    if unknown:
        errors.append(_bound(f"{label} has unsupported keys: {', '.join(unknown[:4])}"))
    relation = parent.get("relation")
    if relation not in RELATIONS:
        errors.append(_bound(f"{label}.relation is not a closed lineage relation"))
    kind = parent.get("kind")
    if kind not in ARTIFACT_KINDS:
        errors.append(_bound(f"{label}.kind is not a closed artifact kind"))
    _safe_id(parent.get("id"), field=f"{label}.id", errors=errors)
    digest = parent.get("digest")
    if digest is not None and not _valid_digest(digest):
        errors.append(_bound(f"{label}.digest must be a bare lowercase 64-char hex string"))
    link = parent.get("link")
    if link not in LINK_SOURCES:
        errors.append(_bound(f"{label}.link must be 'recorded' or 'inferred'"))


def validate_receipt(payload: Any) -> list[str]:
    """Return bounded diagnostics for a causal receipt. Empty means valid."""

    errors: list[str] = []
    if not isinstance(payload, Mapping):
        return [_bound("causal receipt must be a JSON object")]

    forbidden: set[str] = set()
    _collect_forbidden_keys(payload, found=forbidden)
    if forbidden:
        shown = ", ".join(sorted(forbidden)[:4])
        errors.append(_bound(f"causal receipt must not embed sensitive or provenance fields: {shown}"))

    unknown = sorted(set(payload.keys()) - _ALLOWED_TOP_LEVEL)
    if unknown:
        errors.append(_bound(f"causal receipt has unsupported keys: {', '.join(unknown[:4])}"))

    if payload.get("schema") != SCHEMA:
        errors.append(_bound(f"unsupported causal receipt schema {payload.get('schema')!r}"))
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        errors.append(_bound(f"unsupported causal receipt schema_version {version!r}"))

    subject = payload.get("subject")
    if not isinstance(subject, Mapping):
        errors.append(_bound("subject must be an object"))
    else:
        unknown_subject = sorted(set(subject.keys()) - _ALLOWED_SUBJECT)
        if unknown_subject:
            errors.append(_bound(f"subject has unsupported keys: {', '.join(unknown_subject[:4])}"))
        if subject.get("kind") not in ARTIFACT_KINDS:
            errors.append(_bound("subject.kind is not a closed artifact kind"))
        _safe_id(subject.get("id"), field="subject.id", errors=errors)

    parents = payload.get("parents")
    if not isinstance(parents, Sequence) or isinstance(parents, (str, bytes)):
        errors.append(_bound("parents must be a list"))
        parents = []
    elif len(parents) > MAX_PARENTS:
        errors.append(_bound(f"parent count exceeds {MAX_PARENTS}; use parent_manifest"))
    else:
        for index, parent in enumerate(parents):
            _validate_parent(parent, index=index, errors=errors)

    manifest = payload.get("parent_manifest")
    if manifest is not None:
        if not isinstance(manifest, Mapping):
            errors.append(_bound("parent_manifest must be an object"))
        else:
            unknown_manifest = sorted(set(manifest.keys()) - _ALLOWED_MANIFEST)
            if unknown_manifest:
                errors.append(_bound(f"parent_manifest has unsupported keys: {', '.join(unknown_manifest[:4])}"))
            _safe_id(manifest.get("id"), field="parent_manifest.id", errors=errors)
            if not _valid_digest(manifest.get("digest")):
                errors.append(_bound("parent_manifest.digest must be a bare lowercase 64-char hex string"))
            if parents:
                errors.append(_bound("parent_manifest replaces inline parents; parents must be empty"))

    if errors:
        return _cap(errors)

    encoded = compact_bytes(payload)
    if len(encoded) > MAX_COMPACT_BYTES:
        errors.append(_bound(f"causal receipt compact JSON size exceeds {MAX_COMPACT_BYTES} bytes"))
    return _cap(errors)


def parent_ref(
    *,
    relation: str,
    kind: str,
    artifact_id: str,
    digest: str | None = None,
    link: str = "recorded",
) -> dict[str, Any]:
    parent: dict[str, Any] = {
        "relation": relation,
        "kind": kind,
        "id": artifact_id,
        "link": link,
    }
    if digest is not None:
        parent["digest"] = digest
    return parent


def build_receipt(
    *,
    subject_kind: str,
    subject_id: str,
    parents: Sequence[Mapping[str, Any]] = (),
    parent_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "subject": {"kind": subject_kind, "id": subject_id},
        "parents": [dict(parent) for parent in parents],
    }
    if parent_manifest is not None:
        receipt["parent_manifest"] = dict(parent_manifest)
        receipt["parents"] = []
    errors = validate_receipt(receipt)
    if errors:
        raise ValueError("invalid causal receipt: " + "; ".join(errors))
    return receipt


def stamp_causal_receipt(payload: dict[str, Any], receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Attach a validated companion record. Does not rewrite a different existing record."""

    errors = validate_receipt(receipt)
    if errors:
        raise ValueError("invalid causal receipt: " + "; ".join(errors))
    payload[ARTIFACT_FIELD] = dict(receipt)
    return payload


def read_causal_receipt(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    receipt = payload.get(ARTIFACT_FIELD)
    return dict(receipt) if isinstance(receipt, Mapping) else None


def handoff_sidecar_path(handoff_path: Path) -> Path:
    path = Path(handoff_path)
    return path.with_name(path.name + HANDOFF_SIDECAR_SUFFIX)


def diagnose_parent(
    parent: Mapping[str, Any],
    *,
    resolved: Mapping[str, Any] | None,
    actual_digest: str | None = None,
) -> str | None:
    """Return a bounded diagnostic for a missing or mismatched parent. None if ok."""

    if resolved is None:
        return _bound(f"broken lineage: {parent.get('kind')!s}:{parent.get('id')!s} is missing")
    expected = parent.get("digest")
    if expected is None:
        return None
    if not _valid_digest(expected):
        return _bound("parent digest is malformed")
    if actual_digest is None:
        actual_digest = receipt_digest(resolved) if resolved.get("schema") == SCHEMA else None
    if actual_digest != expected:
        return _bound("parent digest mismatch")
    return None


def walk_ancestors(
    start: Mapping[str, Any],
    resolve: Callable[[str, str], Mapping[str, Any] | None],
    *,
    digest_of: Callable[[Mapping[str, Any]], str | None] | None = None,
) -> list[dict[str, Any]]:
    """Walk typed parents. Missing parents stay visible; they are not dropped."""

    errors = validate_receipt(start)
    if errors:
        raise ValueError("invalid causal receipt: " + "; ".join(errors))
    hops: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    queue: list[Mapping[str, Any]] = [start]
    while queue:
        current = queue.pop(0)
        subject = current.get("subject")
        if not isinstance(subject, Mapping):
            continue
        subject_kind = str(subject.get("kind") or "")
        subject_id = str(subject.get("id") or "")
        node_key = (subject_kind, subject_id)
        if node_key in seen:
            continue
        seen.add(node_key)
        parents = current.get("parents")
        if not isinstance(parents, Sequence) or isinstance(parents, (str, bytes)):
            continue
        for parent in parents:
            if not isinstance(parent, Mapping):
                hops.append(
                    {
                        "from": artifact_key(subject_kind, subject_id),
                        "status": "broken",
                        "diagnostic": _bound("broken lineage: parent entry is not an object"),
                    }
                )
                continue
            kind = str(parent.get("kind") or "")
            parent_id = str(parent.get("id") or "")
            resolved = resolve(kind, parent_id)
            actual = digest_of(resolved) if digest_of is not None and resolved is not None else None
            diagnostic = diagnose_parent(parent, resolved=resolved, actual_digest=actual)
            hop = {
                "from": artifact_key(subject_kind, subject_id),
                "relation": parent.get("relation"),
                "kind": kind,
                "id": parent_id,
                "link": parent.get("link"),
                "status": "ok"
                if diagnostic is None
                else ("digest_mismatch" if diagnostic == _bound("parent digest mismatch") else "broken"),
            }
            if diagnostic is not None:
                hop["diagnostic"] = diagnostic
            hops.append(hop)
            if resolved is not None and diagnostic is None:
                queue.append(resolved)
    return hops


def chain_kinds(hops: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return subject kinds traversed from hops, including the start's first hop source."""

    kinds: list[str] = []
    for hop in hops:
        source = hop.get("from")
        if isinstance(source, str) and ":" in source:
            kind = source.split(":", 1)[0]
            if not kinds or kinds[-1] != kind:
                kinds.append(kind)
        parent_kind = hop.get("kind")
        if isinstance(parent_kind, str) and (not kinds or kinds[-1] != parent_kind):
            kinds.append(parent_kind)
    return kinds


def recorded_plan(*, run_id: str) -> dict[str, Any]:
    return build_receipt(subject_kind="plan", subject_id=run_id, parents=())


def recorded_run(*, run_id: str, plan_digest: str | None = None) -> dict[str, Any]:
    return build_receipt(
        subject_kind="run",
        subject_id=run_id,
        parents=[
            parent_ref(
                relation="planned_from",
                kind="plan",
                artifact_id=run_id,
                digest=plan_digest,
                link="recorded",
            )
        ],
    )


def recorded_verify(*, verify_id: str, run_id: str | None, run_digest: str | None = None) -> dict[str, Any]:
    parents = []
    if run_id:
        parents.append(
            parent_ref(
                relation="executed_from",
                kind="run",
                artifact_id=run_id,
                digest=run_digest,
                link="recorded",
            )
        )
    return build_receipt(subject_kind="verify", subject_id=verify_id, parents=parents)


def recorded_outcome(
    *,
    subject_id: str,
    parent_kind: str,
    parent_id: str,
    parent_digest: str | None = None,
) -> dict[str, Any]:
    return build_receipt(
        subject_kind="outcome",
        subject_id=subject_id,
        parents=[
            parent_ref(
                relation="captured_from",
                kind=parent_kind,
                artifact_id=parent_id,
                digest=parent_digest,
                link="recorded",
            )
        ],
    )


def recorded_handoff(
    *,
    handoff_id: str,
    parent_kind: str,
    parent_id: str,
    parent_digest: str | None = None,
) -> dict[str, Any]:
    return build_receipt(
        subject_kind="handoff",
        subject_id=handoff_id,
        parents=[
            parent_ref(
                relation="handed_off_from",
                kind=parent_kind,
                artifact_id=parent_id,
                digest=parent_digest,
                link="recorded",
            )
        ],
    )


def recorded_synthesis(*, run_id: str, worker_parents: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    parents = [dict(parent) for parent in worker_parents]
    if len(parents) > MAX_PARENTS:
        manifest_id = f"{run_id}-parent-manifest"
        digest = receipt_digest({"parents": parents})
        return build_receipt(
            subject_kind="synthesis",
            subject_id=run_id,
            parent_manifest={"id": manifest_id, "digest": digest},
        )
    return build_receipt(subject_kind="synthesis", subject_id=run_id, parents=parents)


def synthesis_worker_parents(run_id: str, results: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    parents: list[dict[str, Any]] = []
    for ordinal, result in enumerate(results, start=1):
        if not isinstance(result, Mapping):
            continue
        worker = result.get("worker")
        if not isinstance(worker, str) or not worker:
            continue
        parents.append(
            parent_ref(
                relation="synthesized_from",
                kind="worker-result",
                artifact_id=worker_result_id(run_id, worker, ordinal),
                link="recorded",
            )
        )
    return parents
