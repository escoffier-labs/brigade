# src/brigade/research/claim_audit.py
"""Claim-level support audit for final research reports.

The citation audit (:func:`brigade.research.provenance.audit_citations`) is the
deterministic *syntax* check: every ``[source:<id>]`` token resolves. This
module is the *semantic* record: one validated record per factual claim the
independent reviewer checked, bound to the exact report digest and the
synthesis attempt that produced it.

Brigade validates the reviewer's records (IDs, spans, digests, enum values,
artifact bounds) and then derives acceptance itself. The reviewer's boolean is
retained as a veto but is never copied into ``accepted``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any

from .types import VALID_CLAIM_RESULTS, ClaimAudit, ClaimRecord, Finding

SCHEMA = "brigade.research.claim-audit.v1"
SCHEMA_VERSION = 1

# Artifact bounds. Records past these are rejected, never silently truncated.
MAX_CLAIMS = 200
MAX_EXPLANATION_CHARS = 500
MAX_SOURCE_IDS_PER_CLAIM = 20
MAX_CLAIM_ID_CHARS = 64
# An ``insufficient`` claim is only allowed when the report states the limit
# next to the claim: the limitation span must sit within this many characters.
LIMITATION_PROXIMITY_CHARS = 400

_RESULT_KEYS = ("backed", "disputed", "insufficient", "skipped")


class ClaimAuditError(ValueError):
    """Reviewer claim records failed validation."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def finding_fingerprint(finding: Finding) -> str:
    """Stable fingerprint of one persisted finding: sha256 of its exact text."""
    return "fnd-" + text_digest(finding.text)[:32]


def _findings_by_source(findings: Sequence[Finding]) -> dict[str, list[Finding]]:
    index: dict[str, list[Finding]] = {}
    for finding in findings:
        for source_id in finding.source_ids:
            index.setdefault(source_id, []).append(finding)
    return index


def _parse_span(raw: Any, *, report_len: int, label: str) -> tuple[int, int]:
    if isinstance(raw, Mapping):
        start, end = raw.get("start"), raw.get("end")
    elif isinstance(raw, (list, tuple)) and len(raw) == 2:
        start, end = raw
    else:
        raise ClaimAuditError("bad-span", f"{label}: span must be [start, end] or {{start, end}}")
    if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
        raise ClaimAuditError("bad-span", f"{label}: span offsets must be integers")
    if start < 0 or end > report_len or start >= end:
        raise ClaimAuditError("bad-span", f"{label}: span [{start}, {end}) is outside the report")
    return start, end


def _parse_id_list(raw: Any, *, label: str, field: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ClaimAuditError("malformed", f"{label}: {field} must be a list")
    if len(raw) > MAX_SOURCE_IDS_PER_CLAIM:
        raise ClaimAuditError("oversized", f"{label}: {field} exceeds {MAX_SOURCE_IDS_PER_CLAIM} entries")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item:
            raise ClaimAuditError("malformed", f"{label}: {field} entries must be non-empty strings")
        out.append(item)
    return tuple(dict.fromkeys(out))


def _resolve(source_ids: tuple[str, ...], index: Mapping[str, list[Finding]], *, label: str) -> tuple[str, ...]:
    fingerprints: list[str] = []
    for source_id in source_ids:
        matches = index.get(source_id)
        if not matches:
            raise ClaimAuditError("unknown-id", f"{label}: source {source_id} does not resolve to a persisted finding")
        fingerprints.extend(finding_fingerprint(f) for f in matches)
    return tuple(dict.fromkeys(fingerprints))


def _spans_adjacent(claim: tuple[int, int], limit: tuple[int, int]) -> bool:
    if limit[1] <= claim[0]:
        return claim[0] - limit[1] <= LIMITATION_PROXIMITY_CHARS
    if limit[0] >= claim[1]:
        return limit[0] - claim[1] <= LIMITATION_PROXIMITY_CHARS
    return True  # overlapping: the limit is stated inside the claim itself


def parse_claim_records(raw: Any, *, report: str, findings: Sequence[Finding]) -> tuple[ClaimRecord, ...]:
    """Validate reviewer claim records against the exact report and findings.

    Raises :class:`ClaimAuditError` with ``kind`` in ``malformed``,
    ``oversized``, ``bad-span``, ``unknown-id``, or ``bad-result``.
    """
    if raw is None:
        raw = []
    if not isinstance(raw, (list, tuple)):
        raise ClaimAuditError("malformed", "claims must be a list")
    if len(raw) > MAX_CLAIMS:
        raise ClaimAuditError("oversized", f"claims exceed {MAX_CLAIMS} records")
    index = _findings_by_source(findings)
    report_len = len(report)
    records: list[ClaimRecord] = []
    seen_ids: set[str] = set()
    for position, item in enumerate(raw):
        label = f"claim[{position}]"
        if not isinstance(item, Mapping):
            raise ClaimAuditError("malformed", f"{label}: record must be an object")
        claim_id = item.get("claim_id")
        if claim_id is None:
            claim_id = f"c{position + 1}"
        if not isinstance(claim_id, str) or not claim_id or len(claim_id) > MAX_CLAIM_ID_CHARS:
            raise ClaimAuditError("malformed", f"{label}: claim_id must be a short non-empty string")
        if claim_id in seen_ids:
            raise ClaimAuditError("malformed", f"{label}: duplicate claim_id {claim_id}")
        seen_ids.add(claim_id)
        span = _parse_span(item.get("span"), report_len=report_len, label=label)
        result = item.get("result")
        if not isinstance(result, str) or result not in VALID_CLAIM_RESULTS:
            raise ClaimAuditError("bad-result", f"{label}: result must be one of {sorted(VALID_CLAIM_RESULTS)}")
        explanation = item.get("explanation", "")
        if explanation is None:
            explanation = ""
        if not isinstance(explanation, str):
            raise ClaimAuditError("malformed", f"{label}: explanation must be a string")
        if len(explanation) > MAX_EXPLANATION_CHARS:
            raise ClaimAuditError("oversized", f"{label}: explanation exceeds {MAX_EXPLANATION_CHARS} chars")
        source_ids = _parse_id_list(item.get("source_ids"), label=label, field="source_ids")
        conflicting_ids = _parse_id_list(
            item.get("conflicting_source_ids"), label=label, field="conflicting_source_ids"
        )
        if result == "backed" and not source_ids:
            raise ClaimAuditError("malformed", f"{label}: backed claims must reference at least one source")
        if result == "disputed" and not conflicting_ids:
            raise ClaimAuditError("malformed", f"{label}: disputed claims must name the conflicting sources")
        fingerprints = _resolve(source_ids, index, label=label)
        conflicting_fingerprints = _resolve(conflicting_ids, index, label=label)
        limitation_span: tuple[int, int] | None = None
        raw_limit = item.get("limitation_span")
        if raw_limit is not None:
            limitation_span = _parse_span(raw_limit, report_len=report_len, label=f"{label}.limitation_span")
        limitation_stated = limitation_span is not None and _spans_adjacent(span, limitation_span)
        records.append(
            ClaimRecord(
                claim_id=claim_id,
                span=span,
                text_digest=text_digest(report[span[0] : span[1]]),
                source_ids=source_ids,
                finding_fingerprints=fingerprints,
                result=result,  # type: ignore[arg-type]
                explanation=explanation,
                conflicting_source_ids=conflicting_ids,
                conflicting_finding_fingerprints=conflicting_fingerprints,
                limitation_span=limitation_span,
                limitation_stated=limitation_stated,
            )
        )
    return tuple(records)


def derive_claim_audit(
    *,
    report: str,
    synthesis_attempt_id: str,
    reviewer_seat: str,
    review_attempt_id: str,
    reviewer_accepted: bool,
    claims: Sequence[ClaimRecord],
    reviewer_detail: str = "",
) -> ClaimAudit:
    """Compute counts and the acceptance decision from validated records.

    Acceptance requires: no ``disputed`` or ``skipped`` record, every
    ``insufficient`` record has a stated limitation next to the claim, and the
    reviewer did not veto. The reviewer's boolean alone can never accept a
    report whose records say otherwise.
    """
    counts = {key: 0 for key in _RESULT_KEYS}
    for record in claims:
        counts[record.result] += 1
    counts["total"] = len(claims)
    exceptions: list[dict[str, Any]] = []
    blockers: list[str] = []
    for record in claims:
        if record.result in ("disputed", "skipped"):
            blockers.append(f"{record.claim_id}:{record.result}")
        elif record.result == "insufficient":
            if record.limitation_stated:
                exceptions.append(
                    {
                        "claim_id": record.claim_id,
                        "reason": "insufficient-with-stated-limit",
                        "limitation_span": list(record.limitation_span or ()),
                    }
                )
            else:
                blockers.append(f"{record.claim_id}:insufficient-unstated")
    counts["unstated_insufficient"] = sum(1 for b in blockers if b.endswith(":insufficient-unstated"))
    counts["exceptions"] = len(exceptions)
    if blockers:
        detail = "claim support rejected: " + ", ".join(blockers[:10])
        accepted = False
    elif not reviewer_accepted:
        detail = reviewer_detail or "reviewer vetoed the report"
        accepted = False
    else:
        detail = "claim support accepted"
        accepted = True
    return ClaimAudit(
        report_digest=text_digest(report),
        synthesis_attempt_id=synthesis_attempt_id,
        reviewer_seat=reviewer_seat,
        review_attempt_id=review_attempt_id,
        claims=tuple(claims),
        counts=counts,
        exceptions=tuple(exceptions),
        reviewer_accepted=reviewer_accepted,
        accepted=accepted,
        detail=detail,
    )


def to_payload(audit: ClaimAudit) -> dict[str, Any]:
    payload = asdict(audit)
    payload["claims"] = [
        {
            **claim,
            "span": list(claim["span"]),
            "limitation_span": list(claim["limitation_span"]) if claim["limitation_span"] else None,
        }
        for claim in payload["claims"]
    ]
    payload["exceptions"] = [dict(item) for item in audit.exceptions]
    payload["counts"] = dict(audit.counts)
    payload["schema"] = SCHEMA
    payload["schema_version"] = SCHEMA_VERSION
    return payload


def _tuple_of_str(raw: Any, field: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"claim audit {field} must be a sequence")
    return tuple(str(item) for item in raw)


def _span_from_payload(raw: Any, field: str) -> tuple[int, int]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ValueError(f"claim audit {field} must be a two-item sequence")
    start, end = raw
    if not isinstance(start, int) or not isinstance(end, int):
        raise ValueError(f"claim audit {field} offsets must be integers")
    return start, end


def from_payload(payload: Mapping[str, Any]) -> ClaimAudit:
    """Rehydrate a persisted claim audit; raises ``ValueError`` on bad shape."""
    if payload.get("schema") != SCHEMA:
        raise ValueError("claim audit schema mismatch")
    required = ("report_digest", "synthesis_attempt_id", "reviewer_seat", "review_attempt_id", "accepted")
    for key in required:
        if key not in payload:
            raise ValueError(f"claim audit missing {key}")
    raw_claims = payload.get("claims") or ()
    if not isinstance(raw_claims, (list, tuple)):
        raise ValueError("claim audit claims must be a sequence")
    claims: list[ClaimRecord] = []
    for item in raw_claims:
        if not isinstance(item, Mapping):
            raise ValueError("claim audit claims must be objects")
        result = item.get("result")
        if result not in VALID_CLAIM_RESULTS:
            raise ValueError(f"claim audit record has invalid result {result!r}")
        raw_limit = item.get("limitation_span")
        claims.append(
            ClaimRecord(
                claim_id=str(item.get("claim_id") or ""),
                span=_span_from_payload(item.get("span"), "span"),
                text_digest=str(item.get("text_digest") or ""),
                source_ids=_tuple_of_str(item.get("source_ids"), "source_ids"),
                finding_fingerprints=_tuple_of_str(item.get("finding_fingerprints"), "finding_fingerprints"),
                result=result,
                explanation=str(item.get("explanation") or ""),
                conflicting_source_ids=_tuple_of_str(item.get("conflicting_source_ids"), "conflicting_source_ids"),
                conflicting_finding_fingerprints=_tuple_of_str(
                    item.get("conflicting_finding_fingerprints"), "conflicting_finding_fingerprints"
                ),
                limitation_span=_span_from_payload(raw_limit, "limitation_span") if raw_limit else None,
                limitation_stated=bool(item.get("limitation_stated")),
            )
        )
    raw_counts = payload.get("counts") or {}
    if not isinstance(raw_counts, Mapping):
        raise ValueError("claim audit counts must be an object")
    raw_exceptions = payload.get("exceptions") or ()
    if not isinstance(raw_exceptions, (list, tuple)):
        raise ValueError("claim audit exceptions must be a sequence")
    return ClaimAudit(
        report_digest=str(payload["report_digest"]),
        synthesis_attempt_id=str(payload["synthesis_attempt_id"]),
        reviewer_seat=str(payload["reviewer_seat"]),
        review_attempt_id=str(payload["review_attempt_id"]),
        claims=tuple(claims),
        counts={str(k): int(v) for k, v in raw_counts.items() if isinstance(v, int)},
        exceptions=tuple(dict(item) for item in raw_exceptions if isinstance(item, Mapping)),
        reviewer_accepted=bool(payload.get("reviewer_accepted")),
        accepted=bool(payload["accepted"]),
        detail=str(payload.get("detail") or ""),
    )


def matches_report(audit: ClaimAudit, *, report: str, synthesis_attempt_id: str) -> bool:
    """True when the audit binds to this exact report text and synthesis attempt."""
    return audit.report_digest == text_digest(report) and audit.synthesis_attempt_id == synthesis_attempt_id


def summary_counts(audit: ClaimAudit | Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Bounded status projection: counts and decision, never the records."""
    if audit is None:
        return None
    if isinstance(audit, ClaimAudit):
        counts = dict(audit.counts)
        accepted = audit.accepted
        digest = audit.report_digest
    else:
        raw_counts = audit.get("counts")
        counts = dict(raw_counts) if isinstance(raw_counts, Mapping) else {}
        accepted = bool(audit.get("accepted"))
        digest = str(audit.get("report_digest") or "")
    return {"accepted": accepted, "report_digest": digest, "counts": counts}


def summary_line(audit: ClaimAudit | Mapping[str, Any] | None) -> str:
    """Single human status line; ``claim audit unavailable`` for legacy runs."""
    summary = summary_counts(audit)
    if summary is None:
        return "claim_audit: unavailable"
    counts = summary["counts"]
    parts = ", ".join(f"{key}={counts.get(key, 0)}" for key in _RESULT_KEYS)
    status = "accepted" if summary["accepted"] else "rejected"
    return f"claim_audit: {status} ({counts.get('total', 0)} claims: {parts})"
