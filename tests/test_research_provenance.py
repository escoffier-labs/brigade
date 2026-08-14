from brigade.research.provenance import audit_citations
from brigade.research.types import Finding, SourceEnvelope


def finding_for(source_id: str) -> Finding:
    return Finding(
        source_ids=(source_id,),
        title="Known source",
        summary="Supported fact",
        evidence="Evidence",
        trust="web",
        extraction_lane="luna",
        extracted_at="2026-08-13T12:02:00+00:00",
        parent_source_ids=(),
    )


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
