from brigade.research import types as t

import pytest


def test_finding_defaults_and_trust():
    f = t.Finding(
        source_ids=("/notes/a.md",),
        title="A",
        summary="s",
        evidence="e",
        trust="local",
        extraction_lane="luna",
        extracted_at="2026-08-13T12:00:00+00:00",
    )
    assert f.trust == "local"
    assert f.source_ids == ("/notes/a.md",)
    assert f.extraction_lane == "luna"
    assert f.extracted_at == "2026-08-13T12:00:00+00:00"
    assert f.parent_source_ids == ()
    assert f.source == "/notes/a.md"


def test_caps_from_overrides():
    caps = t.Caps.build(max_rounds=3)
    assert caps.max_rounds == 3
    assert caps.max_time > 0  # default retained
    assert caps.max_urls_per_round >= 1


def test_valid_trust_and_origin_match_literals() -> None:
    assert t.VALID_TRUST == frozenset({"local", "web", "cli", "browser", "browser-ai"})
    assert t.VALID_ORIGIN == frozenset({"local", "repository", "indexed-cli", "web", "browser-ai"})


def test_source_envelope_and_finding_reject_invalid_trust_origin() -> None:
    with pytest.raises(ValueError, match="invalid origin"):
        t.SourceEnvelope.build(
            origin={},  # type: ignore[arg-type]
            provider="web",
            uri="https://example.com",
            content="body",
            trust="web",
            acquired_at="2026-08-13T12:00:00+00:00",
        )
    with pytest.raises(ValueError, match="invalid trust"):
        t.SourceEnvelope.build(
            origin="web",
            provider="web",
            uri="https://example.com",
            content="body",
            trust="bogus",  # type: ignore[arg-type]
            acquired_at="2026-08-13T12:00:00+00:00",
        )
    with pytest.raises(ValueError, match="invalid trust"):
        t.Finding(
            source_ids=("src-1",),
            title="t",
            summary="s",
            evidence="e",
            trust={"nope"},  # type: ignore[arg-type]
            extraction_lane="luna",
            extracted_at="2026-08-13T12:00:00+00:00",
        )
    with pytest.raises(ValueError, match="invalid origin"):
        t.SourceEnvelope(
            source_id="src-1",
            origin="bogus",  # type: ignore[arg-type]
            provider="web",
            uri="https://example.com",
            content="body",
            content_digest="a" * 64,
            acquired_at="2026-08-13T12:00:00+00:00",
            trust="web",
        )
