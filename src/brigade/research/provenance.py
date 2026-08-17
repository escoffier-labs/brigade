# src/brigade/research/provenance.py
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from brigade import localio, provenance
from brigade.untrusted import scan_handoff_injection_heuristics

from .types import CitationAudit, CitationRecord, Finding, finding_text

CITATION_PATTERN = re.compile(r"\[source:(src-[a-f0-9]{16})\]")

# Legacy Finding.trust maps only to origin/modality. It cannot assign envelope trust.
_LEGACY_TRUST_ORIGIN_MODALITY: dict[str, tuple[str, str]] = {
    "local": ("workspace", "tool-output"),
    "web": ("external-web", "external-web"),
    "cli": ("external-service", "tool-output"),
    "browser": ("external-web", "external-web"),
    "browser-ai": ("external-web", "model-generated"),
}

_NEW_PRODUCER = "research.extract"
_CHECKPOINT_PRODUCER = "research.registry.save_checkpoint"
_FINDINGS_PRODUCER = "research.registry.write_findings"
_BACKFILL_PRODUCER = "research.provenance.backfill"


def citation_token(source_id: str) -> str:
    return f"[source:{source_id}]"


def audit_citations(report: str, findings: Sequence[Finding]) -> CitationAudit:
    known = {source_id for finding in findings for source_id in finding.source_ids}
    seen = tuple(dict.fromkeys(CITATION_PATTERN.findall(report)))
    citations = tuple(
        CitationRecord(
            token=citation_token(source_id),
            source_ids=(source_id,),
            status="accepted" if source_id in known else "unresolved",
        )
        for source_id in seen
    )
    unresolved = tuple(source_id for source_id in seen if source_id not in known)
    return CitationAudit(accepted=bool(seen) and not unresolved, citations=citations, unresolved=unresolved)


def map_legacy_trust_origin_modality(trust: str) -> tuple[str, str]:
    """Map legacy Finding.trust to envelope origin/modality only."""
    return _LEGACY_TRUST_ORIGIN_MODALITY.get(trust, ("unknown", "unknown"))


def _injection_trust(text: str) -> tuple[str, str, int, list[str]]:
    try:
        hits = scan_handoff_injection_heuristics(text)
        warnings = [hit for hit in hits if hit.severity == "warning"]
    except Exception:
        return "quarantined", "pending", 0, []
    if warnings:
        rules = sorted({hit.rule for hit in warnings if isinstance(hit.rule, str) and hit.rule})
        return "quarantined", "flagged", len(warnings), rules
    return "untrusted", "clean", 0, []


def _safe_label(value: object, fallback: str) -> str:
    if isinstance(value, str) and provenance.is_safe_identity_label(value):
        return value
    return fallback


def _envelope_matches_text(env: Mapping[str, Any], text: str) -> bool:
    if provenance.validate_envelope(env):
        return False
    hashes = env.get("hashes")
    if not isinstance(hashes, Mapping):
        return False
    return hashes.get("content") == provenance.content_sha256(text)


def stamp_finding_payload(
    item: Finding | Mapping[str, Any],
    *,
    run_id: str | None = None,
    index: int = 0,
    inferred: bool = False,
    producer: str = _CHECKPOINT_PRODUCER,
    ingested_at: str | None = None,
) -> dict[str, Any]:
    """Stamp one finding dict with exact text and a locally assigned envelope.

    Legacy ``Finding.trust`` is copied through as a compatibility field and is
    never used as ``trust.label``.
    """
    if is_dataclass(item) and not isinstance(item, type):
        payload = asdict(item)
    elif isinstance(item, Mapping):
        payload = dict(item)
    else:
        raise TypeError("finding payload must be a Finding or mapping")

    title = str(payload.get("title") or "")
    summary = str(payload.get("summary") or "")
    evidence = str(payload.get("evidence") or "")
    text = finding_text(title, summary, evidence)
    payload["text"] = text

    existing = payload.get("provenance")
    if isinstance(existing, Mapping) and _envelope_matches_text(existing, text):
        payload["provenance"] = dict(existing)
        return payload

    legacy_trust = str(payload.get("trust") or "")
    origin, modality = map_legacy_trust_origin_modality(legacy_trust)
    now = ingested_at or localio.utc_now_iso()
    captured = str(payload.get("extracted_at") or "") or now
    source_ids = payload.get("source_ids") or ()
    first_source = str(source_ids[0]) if isinstance(source_ids, (list, tuple)) and source_ids else f"finding-{index}"
    run_label = _safe_label(run_id, "unknown")
    item_id = _safe_label(first_source, f"finding-{index}")
    collection_id = f"research:{run_label}" if run_id else "research:findings"
    locator_value = f"research-finding:{run_label}:{index}"

    if inferred:
        scanned_label, injection_status, injection_count, injection_rules = _injection_trust(text)
        trust_label = "quarantined" if scanned_label == "quarantined" else "unknown"
        attribution = "inferred"
        assigned_by = f"ingest:{_BACKFILL_PRODUCER}"
        source_producer = _BACKFILL_PRODUCER
    else:
        trust_label, injection_status, injection_count, injection_rules = _injection_trust(text)
        attribution = "observed"
        assigned_by = f"ingest:{producer}"
        source_producer = producer
        if trust_label in {"reviewed", "verified"}:
            trust_label = "untrusted"

    payload["provenance"] = provenance.build_envelope(
        source_system="research",
        source_kind=legacy_trust if legacy_trust in _LEGACY_TRUST_ORIGIN_MODALITY else "unknown",
        source_producer=source_producer,
        origin=origin,
        repository_id="unknown",
        repository_revision=None,
        session_id=_safe_label(run_id, "unknown") if run_id else None,
        session_harness=None,
        collection_id=_safe_label(collection_id, "research:findings"),
        item_id=item_id,
        locator_kind="uri",
        locator_value=locator_value,
        attribution=attribution,
        modality=modality,
        trust_label=trust_label,
        trust_assigned_by=assigned_by,
        trust_assigned_at=now,
        injection_status=injection_status,
        injection_count=injection_count,
        injection_rules=injection_rules,
        text=text,
        raw_bytes=None,
        content_scope="item.text.utf8.v1",
        captured_at=captured,
        ingested_at=now,
    )
    return payload


def stamp_finding(
    finding: Finding,
    *,
    run_id: str | None = None,
    index: int = 0,
    producer: str = _NEW_PRODUCER,
) -> Finding:
    stamped = stamp_finding_payload(finding, run_id=run_id, index=index, producer=producer)
    return Finding(
        source_ids=finding.source_ids,
        title=finding.title,
        summary=finding.summary,
        evidence=finding.evidence,
        trust=finding.trust,
        extraction_lane=finding.extraction_lane,
        extracted_at=finding.extracted_at,
        parent_source_ids=finding.parent_source_ids,
        text=stamped["text"],
        provenance=stamped["provenance"],
    )


def finding_envelope(finding: Finding) -> dict[str, Any]:
    env = finding.provenance
    if isinstance(env, Mapping) and not provenance.validate_envelope(env):
        return dict(env)
    synthesized, _display = provenance.synthesize_legacy_provenance()
    return synthesized


def envelope_label(env: Mapping[str, Any]) -> str:
    raw_trust = env.get("trust")
    trust = raw_trust if isinstance(raw_trust, Mapping) else {}
    return f"origin={env.get('origin')} modality={env.get('modality')} trust={trust.get('label')}"


def split_finding_text(finding: Finding) -> tuple[str, str, str]:
    text = finding.text or finding_text(finding.title, finding.summary, finding.evidence)
    parts = text.split("\n", 2)
    title = parts[0] if parts else ""
    summary = parts[1] if len(parts) > 1 else ""
    evidence = parts[2] if len(parts) > 2 else ""
    return title, summary, evidence


def stamp_finding_list(
    findings: Sequence[Any],
    *,
    run_id: str | None = None,
    inferred: bool = False,
    producer: str = _FINDINGS_PRODUCER,
) -> tuple[list[dict[str, Any]], int]:
    """Stamp findings; return payloads and the number of envelopes written."""
    stamped: list[dict[str, Any]] = []
    written = 0
    for index, item in enumerate(findings):
        if not isinstance(item, (Finding, Mapping)):
            continue
        before = None
        if isinstance(item, Mapping):
            before = item.get("provenance")
        elif isinstance(item, Finding):
            before = item.provenance
        payload = stamp_finding_payload(item, run_id=run_id, index=index, inferred=inferred, producer=producer)
        stamped.append(payload)
        after = payload.get("provenance")
        if after is not before:
            written += 1
    return stamped, written


def backfill_research_provenance(target: Path) -> dict[str, Any]:
    """Infer envelopes and text projections on legacy research findings.

    Does not change legacy ``Finding.trust`` and never assigns reviewed/verified.
    """
    from . import registry

    scanned = 0
    stamped = 0
    unchanged = 0
    missing = 0
    inferred = 0
    runs = registry.list_runs(target)
    for run in runs:
        run_id = str(run.get("run_id") or "")
        if not run_id:
            continue
        checkpoint = registry.load_checkpoint(target, run_id)
        if isinstance(checkpoint, dict):
            findings = checkpoint.get("findings")
            if isinstance(findings, list) and findings:
                scanned += len(findings)
                updated, wrote = _backfill_finding_list(findings, run_id=run_id)
                if wrote:
                    checkpoint = dict(checkpoint)
                    checkpoint["findings"] = updated
                    registry.save_checkpoint(target, run_id, checkpoint)
                    stamped += wrote
                    inferred += wrote
                unchanged += len(updated) - wrote
                missing += sum(1 for item in findings if not _has_matching_envelope(item))
        findings_path = registry.run_dir(target, run_id) / registry.FINDINGS_ARTIFACT
        payload = localio.read_json_dict(findings_path)
        if not isinstance(payload, dict):
            continue
        findings = payload.get("findings")
        if not isinstance(findings, list) or not findings:
            continue
        scanned += len(findings)
        updated, wrote = _backfill_finding_list(findings, run_id=run_id)
        if wrote:
            payload = dict(payload)
            payload["findings"] = updated
            from brigade import receipt_schema

            localio.write_json(findings_path, receipt_schema.stamp_research_findings(payload))
            stamped += wrote
            inferred += wrote
        unchanged += len(updated) - wrote
        missing += sum(1 for item in findings if not _has_matching_envelope(item))
    return {
        "target": str(target),
        "scanned_findings": scanned,
        "stamped": stamped,
        "unchanged": unchanged,
        "missing_envelope": missing,
        "inferred": inferred,
        "trusted": 0,
    }


def _has_matching_envelope(item: object) -> bool:
    if isinstance(item, Finding):
        env = item.provenance
        text = item.text or finding_text(item.title, item.summary, item.evidence)
    elif isinstance(item, Mapping):
        env = item.get("provenance")
        text = finding_text(
            str(item.get("title") or ""), str(item.get("summary") or ""), str(item.get("evidence") or "")
        )
    else:
        return False
    return isinstance(env, Mapping) and _envelope_matches_text(env, text)


def _backfill_finding_list(findings: Sequence[Any], *, run_id: str) -> tuple[list[dict[str, Any]], int]:
    stamped: list[dict[str, Any]] = []
    wrote = 0
    for index, item in enumerate(findings):
        if not isinstance(item, (Finding, Mapping)):
            continue
        if _has_matching_envelope(item):
            if isinstance(item, Finding):
                stamped.append(stamp_finding_payload(item, run_id=run_id, index=index, inferred=True))
            else:
                payload = dict(item)
                payload["text"] = finding_text(
                    str(payload.get("title") or ""),
                    str(payload.get("summary") or ""),
                    str(payload.get("evidence") or ""),
                )
                stamped.append(payload)
            continue
        stamped.append(
            stamp_finding_payload(item, run_id=run_id, index=index, inferred=True, producer=_BACKFILL_PRODUCER)
        )
        wrote += 1
    return stamped, wrote
