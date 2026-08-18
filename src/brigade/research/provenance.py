# src/brigade/research/provenance.py
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from brigade import evidence_redaction, localio, provenance, trust_gate
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


def _finding_for_source(findings: Sequence[Finding], source_id: str) -> Finding | None:
    for finding in findings:
        if source_id in finding.source_ids:
            return finding
    return None


def audit_citations(report: str, findings: Sequence[Finding]) -> CitationAudit:
    known = {source_id for finding in findings for source_id in finding.source_ids}
    seen = tuple(dict.fromkeys(CITATION_PATTERN.findall(report)))
    citations: list[CitationRecord] = []
    unresolved: list[str] = []
    for source_id in seen:
        finding = _finding_for_source(findings, source_id)
        if source_id not in known or finding is None:
            citations.append(
                CitationRecord(token=citation_token(source_id), source_ids=(source_id,), status="unresolved")
            )
            unresolved.append(source_id)
            continue
        record = {"text": finding.text, "provenance": finding_envelope(finding)}
        if not trust_gate.citation_allowed(record):
            citations.append(
                CitationRecord(token=citation_token(source_id), source_ids=(source_id,), status="rejected")
            )
            unresolved.append(source_id)
            continue
        citations.append(CitationRecord(token=citation_token(source_id), source_ids=(source_id,), status="accepted"))
    return CitationAudit(
        accepted=bool(seen) and not unresolved, citations=tuple(citations), unresolved=tuple(unresolved)
    )


def map_legacy_trust_origin_modality(trust: str) -> tuple[str, str]:
    """Map legacy Finding.trust to envelope origin/modality only."""
    return _LEGACY_TRUST_ORIGIN_MODALITY.get(trust, ("unknown", "unknown"))


def _split_redacted_finding_text(text: str) -> tuple[str, str, str]:
    parts = text.split("\n", 2)
    title = parts[0] if parts else ""
    summary = parts[1] if len(parts) > 1 else ""
    evidence = parts[2] if len(parts) > 2 else ""
    return title, summary, evidence


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
    if provenance.validate_envelope(env, inbound_adapter=True):
        return False
    hashes = env.get("hashes")
    if not isinstance(hashes, Mapping):
        return False
    return hashes.get("content") == provenance.content_sha256(text)


def _expected_persist_identity(run_id: str | None, index: int) -> tuple[str, str, str | None]:
    run_label = _safe_label(run_id, "unknown")
    collection_id = f"research:{run_label}" if run_id else "research:findings"
    locator_value = f"research-finding:{run_label}:{index}"
    session_id = run_label if run_id else None
    return collection_id, locator_value, session_id


def _persist_identity_matches(env: Mapping[str, Any], *, run_id: str | None, index: int) -> bool:
    collection_id, locator_value, session_id = _expected_persist_identity(run_id, index)
    raw_locator = env.get("locator")
    locator = raw_locator if isinstance(raw_locator, Mapping) else {}
    raw_session = env.get("session")
    session = raw_session if isinstance(raw_session, Mapping) else {}
    return (
        env.get("collection_id") == collection_id
        and locator.get("value") == locator_value
        and session.get("id") == session_id
    )


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
    if (
        isinstance(existing, Mapping)
        and _envelope_matches_text(existing, text)
        and _persist_identity_matches(existing, run_id=run_id, index=index)
    ):
        payload["provenance"] = dict(existing)
        return payload

    redaction_record: dict[str, Any] | None = None
    redaction_failed = False
    if not inferred:
        origin_for_redaction, _modality = map_legacy_trust_origin_modality(str(payload.get("trust") or ""))
        persist_text, redaction_record, redaction_failed = evidence_redaction.apply_and_record(
            text,
            origin=origin_for_redaction,
        )
        if redaction_failed:
            title = evidence_redaction.FAILED_PLACEHOLDER
            summary = ""
            evidence = ""
        elif persist_text != text:
            title, summary, evidence = _split_redacted_finding_text(persist_text)
        payload["title"] = title
        payload["summary"] = summary
        payload["evidence"] = evidence
        text = finding_text(title, summary, evidence)
        payload["text"] = text

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
        if redaction_failed:
            trust_label = "quarantined"

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
        redaction=redaction_record,
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
        title=str(stamped["title"]),
        summary=str(stamped["summary"]),
        evidence=str(stamped["evidence"]),
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
) -> tuple[list[Any], int]:
    """Stamp findings; return payloads and the number of envelopes written."""
    stamped: list[Any] = []
    written = 0
    for index, item in enumerate(findings):
        if not isinstance(item, (Finding, Mapping)):
            stamped.append(item)
            continue
        before = None
        if isinstance(item, Mapping):
            before = item.get("provenance")
        elif isinstance(item, Finding):
            before = item.provenance
        payload = stamp_finding_payload(item, run_id=run_id, index=index, inferred=inferred, producer=producer)
        stamped.append(payload)
        after = payload.get("provenance")
        if after != before:
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


def _backfill_finding_list(findings: Sequence[Any], *, run_id: str) -> tuple[list[Any], int]:
    stamped: list[Any] = []
    wrote = 0
    for index, item in enumerate(findings):
        if not isinstance(item, (Finding, Mapping)):
            stamped.append(item)
            continue
        before = item.provenance if isinstance(item, Finding) else item.get("provenance")
        payload = stamp_finding_payload(item, run_id=run_id, index=index, inferred=True, producer=_BACKFILL_PRODUCER)
        stamped.append(payload)
        if payload.get("provenance") != before:
            wrote += 1
    return stamped, wrote
