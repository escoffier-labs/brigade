from brigade.research import report
from brigade.research.types import Finding


def _findings():
    return [
        Finding(
            source_ids=("/n/a.md",),
            title="a.md",
            summary="local summary",
            evidence="ev",
            trust="local",
            extraction_lane="luna",
            extracted_at="2026-08-13T12:00:00+00:00",
        ),
        Finding(
            source_ids=("cli://tool/abc",),
            title="Tool",
            summary="cli summary",
            evidence="ev",
            trust="cli",
            extraction_lane="luna",
            extracted_at="2026-08-13T12:00:00+00:00",
        ),
        Finding(
            source_ids=("https://browser.ex",),
            title="Browser",
            summary="browser summary",
            evidence="ev",
            trust="browser",
            extraction_lane="luna",
            extracted_at="2026-08-13T12:00:00+00:00",
        ),
        Finding(
            source_ids=("https://ex.com",),
            title="Ex",
            summary="web summary",
            evidence="ev",
            trust="web",
            extraction_lane="luna",
            extracted_at="2026-08-13T12:00:00+00:00",
        ),
    ]


def test_html_is_self_contained_and_separates_trust():
    findings = _findings()
    assert findings[0].source == "/n/a.md"
    html = report.render_html(question="Q", markdown_report="## R\nbody", findings=findings, stats={"rounds": 2})
    assert "<!DOCTYPE html>" in html and "<style>" in html
    assert "http" not in html.split("<style>")[0]  # no external asset before styles
    assert "Trusted (local)" in html and "Configured CLI" in html
    assert "Browser-assisted" in html and "Untrusted (web)" in html
    assert "/n/a.md" in html and "ex.com" in html
    assert "cli://tool/abc" in html


def test_markdown_includes_sources_block():
    md = report.render_markdown(question="Q", markdown_report="## R\nbody", findings=_findings())
    assert "## R" in md and "Sources" in md and "a.md" in md
