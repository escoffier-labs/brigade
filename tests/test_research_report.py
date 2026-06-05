from brigade.research import report
from brigade.research.types import Finding

def _findings():
    return [Finding("/n/a.md", "a.md", "local summary", "ev", "local"),
            Finding("cli://tool/abc", "Tool", "cli summary", "ev", "cli"),
            Finding("https://browser.ex", "Browser", "browser summary", "ev", "browser"),
            Finding("https://ex.com", "Ex", "web summary", "ev", "web")]

def test_html_is_self_contained_and_separates_trust():
    html = report.render_html(question="Q", markdown_report="## R\nbody",
                              findings=_findings(), stats={"rounds": 2})
    assert "<!DOCTYPE html>" in html and "<style>" in html
    assert "http" not in html.split("<style>")[0]      # no external asset before styles
    assert "Trusted (local)" in html and "Configured CLI" in html
    assert "Browser-assisted" in html and "Untrusted (web)" in html
    assert "/n/a.md" in html and "ex.com" in html
    assert "cli://tool/abc" in html

def test_markdown_includes_sources_block():
    md = report.render_markdown(question="Q", markdown_report="## R\nbody", findings=_findings())
    assert "## R" in md and "Sources" in md and "a.md" in md
