from brigade.research import handoff
from brigade.research.types import Finding, SourceEnvelope
from brigade import handoff_cmd


def _envelope(*, origin, provider, uri, trust, content="body") -> SourceEnvelope:
    return SourceEnvelope.build(
        origin=origin,  # type: ignore[arg-type]
        provider=provider,
        uri=uri,
        content=content,
        trust=trust,  # type: ignore[arg-type]
        acquired_at="2026-08-13T12:00:00+00:00",
    )


def test_handoff_is_standard_no_card_document_handoff(tmp_path):
    local = _envelope(origin="local", provider="local", uri="/n/a.md", trust="local")
    cli = _envelope(origin="indexed-cli", provider="cli", uri="cli://tool/abc", trust="cli")
    browser = _envelope(
        origin="web", provider="playwright", uri="https://browser.example", trust="browser"
    )
    web = _envelope(origin="web", provider="web", uri="http://e.com", trust="web")
    browser_ai = _envelope(
        origin="browser-ai",
        provider="gemini-browser",
        uri="https://browser-ai.example/doc",
        trust="browser-ai",
        content="ai claim",
    )
    findings = [
        Finding(
            source_ids=(local.source_id,),
            title="a.md",
            summary="s",
            evidence="e",
            trust="local",
            extraction_lane="luna",
            extracted_at="2026-08-13T12:00:00+00:00",
        ),
        Finding(
            source_ids=(cli.source_id,),
            title="T",
            summary="s3",
            evidence="e3",
            trust="cli",
            extraction_lane="luna",
            extracted_at="2026-08-13T12:00:00+00:00",
        ),
        Finding(
            source_ids=(browser.source_id,),
            title="B",
            summary="s4",
            evidence="e4",
            trust="browser",
            extraction_lane="luna",
            extracted_at="2026-08-13T12:00:00+00:00",
        ),
        Finding(
            source_ids=(web.source_id,),
            title="E",
            summary="s2",
            evidence="e2",
            trust="web",
            extraction_lane="luna",
            extracted_at="2026-08-13T12:00:00+00:00",
        ),
        Finding(
            source_ids=(browser_ai.source_id,),
            title="AI Doc",
            summary="s5",
            evidence="e5",
            trust="browser-ai",
            extraction_lane="luna",
            extracted_at="2026-08-13T12:00:00+00:00",
        ),
    ]
    sources = (local, cli, browser, web, browser_ai)
    assert findings[0].source == local.source_id
    md = handoff.render_handoff(
        question="What is X?",
        markdown_report="## R\nbody",
        findings=findings,
        sources=sources,
        stats={"rounds": 2},
    )
    assert md.startswith("# Memory Handoff")
    assert "Recommended memory action\n\nno-card" in md
    assert "Target document\n\n.learnings/LEARNINGS.md" in md
    assert "Trusted (local)" in md and "Configured CLI" in md
    assert "Browser-assisted" in md and "Untrusted (web)" in md
    assert "Browser AI" in md
    assert "What is X?" in md
    assert "\n### R\nbody" in md
    assert "/n/a.md" in md
    assert "https://browser-ai.example/doc" in md
    sources_block = md[md.index("Trusted (local)") :]
    for source in sources:
        assert source.uri in sources_block
        assert source.source_id not in sources_block
    path = tmp_path / "handoff.md"
    path.write_text(md)
    assert handoff_cmd.lint_file(path).valid is True
