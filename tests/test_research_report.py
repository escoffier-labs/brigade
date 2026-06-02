from brigade.research import report
from brigade.research.types import Finding

def _findings():
    return [Finding("/n/a.md", "a.md", "local summary", "ev", "local"),
            Finding("https://ex.com", "Ex", "web summary", "ev", "web")]

def test_html_is_self_contained_and_separates_trust():
    html = report.render_html(question="Q", markdown_report="## R\nbody",
                              findings=_findings(), stats={"rounds": 2})
    assert "<!DOCTYPE html>" in html and "<style>" in html
    assert "http" not in html.split("<style>")[0]      # no external asset before styles
    assert "Trusted (local)" in html and "Untrusted (web)" in html
    assert "/n/a.md" in html and "ex.com" in html

def test_markdown_includes_sources_block():
    md = report.render_markdown(question="Q", markdown_report="## R\nbody", findings=_findings())
    assert "## R" in md and "Sources" in md and "a.md" in md
