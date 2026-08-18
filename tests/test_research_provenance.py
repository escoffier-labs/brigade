from brigade.research.provenance import audit_citations, stamp_finding
from brigade.research.types import Finding, SourceEnvelope
from brigade import trust_gate


def finding_for(source_id: str, *, trust_label: str = "untrusted") -> Finding:
    finding = Finding(
        source_ids=(source_id,),
        title="Known source",
        summary="Supported fact",
        evidence="Evidence",
        trust="web",
        extraction_lane="luna",
        extracted_at="2026-08-13T12:02:00+00:00",
        parent_source_ids=(),
    )
    stamped = stamp_finding(finding, run_id="test-run", index=0)
    if trust_label != "untrusted":
        env = trust_gate.apply_trust_label(
            stamped.provenance or {},
            to_label=trust_label,
            assigned_by="ingest:test",
            assigned_at="2026-08-17T00:00:00+00:00",
        )
        return Finding(
            source_ids=stamped.source_ids,
            title=stamped.title,
            summary=stamped.summary,
            evidence=stamped.evidence,
            trust=stamped.trust,
            extraction_lane=stamped.extraction_lane,
            extracted_at=stamped.extracted_at,
            parent_source_ids=stamped.parent_source_ids,
            provenance=env,
        )
    return stamped


def test_source_envelope_has_stable_digest_and_id() -> None:
    first = SourceEnvelope.build(
        origin="web",
        provider="playwright",
        uri="https://example.test/a",
        content="same content",
        trust="web",
        acquired_at="2026-08-13T12:00:00+00:00",
    )
    second = SourceEnvelope.build(
        origin="web",
        provider="playwright",
        uri="https://example.test/a",
        content="same content",
        trust="web",
        acquired_at="2026-08-13T12:01:00+00:00",
    )

    assert first.source_id == second.source_id
    assert first.content_digest == second.content_digest
    assert first.source_id.startswith("src-")


def test_citation_audit_rejects_unknown_source() -> None:
    audit = audit_citations(
        "Supported [source:src-1111111111111111]. Unsupported [source:src-2222222222222222].",
        findings=[finding_for("src-1111111111111111")],
    )

    assert audit.accepted is False
    assert audit.unresolved == ("src-2222222222222222",)
    assert [item.status for item in audit.citations] == ["accepted", "unresolved"]


def test_citation_audit_rejects_zero_citations() -> None:
    audit = audit_citations("Answer with prose and no tokens.", findings=[finding_for("src-1111111111111111")])

    assert audit.accepted is False
    assert audit.citations == ()
    assert audit.unresolved == ()


def test_citation_audit_rejects_unknown_and_quarantined_findings() -> None:
    leaked = "unknown body must not be cited"
    unknown = finding_for("src-aaaaaaaaaaaaaaaa", trust_label="unknown")
    quarantined = finding_for("src-bbbbbbbbbbbbbbbb", trust_label="quarantined")
    report = "See [source:src-aaaaaaaaaaaaaaaa] and [source:src-bbbbbbbbbbbbbbbb]."
    audit = audit_citations(report, findings=[unknown, quarantined])
    assert audit.accepted is False
    assert [item.status for item in audit.citations] == ["rejected", "rejected"]
    assert leaked not in unknown.title


def test_finding_rejects_empty_source_ids() -> None:
    import pytest

    with pytest.raises((ValueError, TypeError), match="source_ids"):
        Finding(
            source_ids=(),
            title="Empty sources",
            summary="Invalid finding",
            evidence="None",
            trust="web",
            extraction_lane="luna",
            extracted_at="2026-08-13T12:02:00+00:00",
            parent_source_ids=(),
        )
