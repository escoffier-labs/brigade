from brigade.research import report
from brigade.research.types import Finding, SourceEnvelope


def _envelope(*, origin, provider, uri, trust, content="body") -> SourceEnvelope:
    return SourceEnvelope.build(
        origin=origin,  # type: ignore[arg-type]
        provider=provider,
        uri=uri,
        content=content,
        trust=trust,  # type: ignore[arg-type]
        acquired_at="2026-08-13T12:00:00+00:00",
    )


def _findings_and_sources():
    local = _envelope(origin="local", provider="local", uri="/n/a.md", trust="local")
    cli = _envelope(origin="indexed-cli", provider="cli", uri="cli://tool/abc", trust="cli")
    browser = _envelope(origin="web", provider="playwright", uri="https://browser.ex", trust="browser")
    web = _envelope(origin="web", provider="web", uri="https://ex.com", trust="web")
    browser_ai = _envelope(
        origin="browser-ai",
        provider="gemini-browser",
        uri="https://browser-ai.ex/doc",
        trust="browser-ai",
        content="browser-ai claim",
    )
    findings = [
        Finding(
            source_ids=(local.source_id,),
            title="a.md",
            summary="local summary",
            evidence="ev",
            trust="local",
            extraction_lane="luna",
            extracted_at="2026-08-13T12:00:00+00:00",
        ),
        Finding(
            source_ids=(cli.source_id,),
            title="Tool",
            summary="cli summary",
            evidence="ev",
            trust="cli",
            extraction_lane="luna",
            extracted_at="2026-08-13T12:00:00+00:00",
        ),
        Finding(
            source_ids=(browser.source_id,),
            title="Browser",
            summary="browser summary",
            evidence="ev",
            trust="browser",
            extraction_lane="luna",
            extracted_at="2026-08-13T12:00:00+00:00",
        ),
        Finding(
            source_ids=(web.source_id,),
            title="Ex",
            summary="web summary",
            evidence="ev",
            trust="web",
            extraction_lane="luna",
            extracted_at="2026-08-13T12:00:00+00:00",
        ),
        Finding(
            source_ids=(browser_ai.source_id,),
            title="Browser AI Doc",
            summary="browser-ai summary",
            evidence="ev",
            trust="browser-ai",
            extraction_lane="luna",
            extracted_at="2026-08-13T12:00:00+00:00",
        ),
    ]
    sources = (local, cli, browser, web, browser_ai)
    return findings, sources


def test_html_is_self_contained_and_separates_trust():
    findings, sources = _findings_and_sources()
    assert findings[0].source == sources[0].source_id
    html = report.render_html(
        question="Q",
        markdown_report="## R\nbody",
        findings=findings,
        sources=sources,
        stats={"rounds": 2},
    )
    assert "<!DOCTYPE html>" in html and "<style>" in html
    assert "http" not in html.split("<style>")[0]  # no external asset before styles
    assert "Trusted (local)" in html and "Configured CLI" in html
    assert "Browser-assisted" in html and "Untrusted (web)" in html
    assert "Browser AI" in html
    assert "/n/a.md" in html and "ex.com" in html
    assert "cli://tool/abc" in html
    assert "https://browser-ai.ex/doc" in html
    assert "Browser AI Doc" in html
    # Source sections resolve IDs to URIs; immutable ids must not appear as locators.
    sources_block = html[html.index("Trusted (local)") :]
    for source in sources:
        assert source.uri in sources_block
        assert source.source_id not in sources_block


def test_markdown_includes_sources_block():
    findings, sources = _findings_and_sources()
    md = report.render_markdown(
        question="Q",
        markdown_report="## R\nbody",
        findings=findings,
        sources=sources,
    )
    assert "## R" in md and "Sources" in md and "a.md" in md
    assert "/n/a.md" in md
    assert "https://browser-ai.ex/doc" in md
    assert "[browser-ai]" in md
    for source in sources:
        assert source.source_id not in md.split("## Sources", 1)[1]
