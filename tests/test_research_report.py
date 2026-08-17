from brigade.research import report
from brigade.research.provenance import stamp_finding
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
        stamp_finding(
            Finding(
                source_ids=(local.source_id,),
                title="a.md",
                summary="local summary",
                evidence="ev",
                trust="local",
                extraction_lane="luna",
                extracted_at="2026-08-13T12:00:00+00:00",
            ),
            index=0,
        ),
        stamp_finding(
            Finding(
                source_ids=(cli.source_id,),
                title="Tool",
                summary="cli summary",
                evidence="ev",
                trust="cli",
                extraction_lane="luna",
                extracted_at="2026-08-13T12:00:00+00:00",
            ),
            index=1,
        ),
        stamp_finding(
            Finding(
                source_ids=(browser.source_id,),
                title="Browser",
                summary="browser summary",
                evidence="ev",
                trust="browser",
                extraction_lane="luna",
                extracted_at="2026-08-13T12:00:00+00:00",
            ),
            index=2,
        ),
        stamp_finding(
            Finding(
                source_ids=(web.source_id,),
                title="Ex",
                summary="web summary",
                evidence="ev",
                trust="web",
                extraction_lane="luna",
                extracted_at="2026-08-13T12:00:00+00:00",
            ),
            index=3,
        ),
        stamp_finding(
            Finding(
                source_ids=(browser_ai.source_id,),
                title="Browser AI Doc",
                summary="browser-ai summary",
                evidence="ev",
                trust="browser-ai",
                extraction_lane="luna",
                extracted_at="2026-08-13T12:00:00+00:00",
            ),
            index=4,
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
    assert "origin=workspace modality=tool-output trust=untrusted" in html
    assert "origin=external-web modality=external-web trust=untrusted" in html
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
    assert "origin=external-web modality=model-generated trust=untrusted" in md
    assert "origin=workspace modality=tool-output trust=untrusted" in md
    for source in sources:
        assert source.source_id not in md.split("## Sources", 1)[1]


def test_render_escapes_hostile_source_metadata_in_html_and_markdown():
    hostile_source = _envelope(
        origin="web",
        provider="playwright",
        uri="https://example.com/search?q=<script>alert(1)</script>&x=1#\x00frag",
        trust="web",
        content="body with <script>evil()</script>",
    )
    hostile_finding = Finding(
        source_ids=(hostile_source.source_id,),
        title=(
            'Evil <script>alert("xss")</script> <img src=x onerror=alert(1)> '
            "[breakout](http://evil.com)\r\n## Injected Heading\x00"
        ),
        summary="Hostile summary <img src=x onerror=alert(1)> [link](javascript:alert(1))\x00",
        evidence="evidence",
        trust="web",
        extraction_lane="luna\x00lane",
        extracted_at="2026-08-13T12:00:00+00:00",
    )

    # HTML rendering must escape HTML tags, script injection, and attribute breakouts
    html = report.render_html(
        question="Hostile Q <script>alert('q')</script>",
        markdown_report="## Report\nbody",
        findings=[hostile_finding],
        sources=[hostile_source],
        stats={"rounds": 1},
    )
    assert "<script>" not in html
    assert "<img src=x" not in html
    assert "&lt;script&gt;" in html
    assert "\x00" not in html

    # Markdown rendering must escape/sanitize link breakouts, raw newlines injecting headings, and control chars
    md = report.render_markdown(
        question="Hostile Q",
        markdown_report="## Report\nbody",
        findings=[hostile_finding],
        sources=[hostile_source],
    )
    # Check that injected heading does not appear as a top-level section in markdown
    assert "\n## Injected Heading" not in md
    assert "\x00" not in md
    assert "\r" not in md
    # Report body stays authored; only source-line metadata is neutralized.
    assert "## Report\nbody" in md.split("## Sources", 1)[0]
    sources_md = md.split("## Sources", 1)[1]
    assert "<script>" not in sources_md
    assert "<img" not in sources_md
    assert "[breakout](http://evil.com)" not in sources_md
    assert "Evil" in sources_md
    assert "breakout" in sources_md
    assert "example.com" in sources_md


def test_html_renders_full_multiline_summary() -> None:
    source = _envelope(origin="web", provider="web", uri="https://ex.com/doc", trust="web")
    finding = Finding(
        source_ids=(source.source_id,),
        title="Title",
        summary="First sentence.\nSecond sentence with the real conclusion.",
        evidence="EV",
        trust="web",
        extraction_lane="luna",
        extracted_at="2026-08-13T12:00:00+00:00",
    )
    html = report.render_html(
        question="Q",
        markdown_report="body",
        findings=[finding],
        sources=[source],
        stats={"rounds": 1},
    )
    assert "First sentence." in html
    assert "Second sentence with the real conclusion." in html
    md = report.render_markdown(
        question="Q",
        markdown_report="body",
        findings=[finding],
        sources=[source],
    )
    assert "Title" in md
    assert "https://ex.com/doc" in md


def test_html_stats_render_middle_dot_without_double_escape() -> None:
    findings, sources = _findings_and_sources()
    html = report.render_html(
        question="Q",
        markdown_report="body",
        findings=findings[:1],
        sources=sources[:1],
        stats={"rounds": 2, "findings": "<b>1</b>"},
    )
    assert "&middot;" in html
    assert "&amp;middot;" not in html
    assert "&lt;b&gt;1&lt;/b&gt;" in html
