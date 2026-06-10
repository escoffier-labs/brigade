from brigade.research import handoff
from brigade.research.types import Finding
from brigade import handoff_cmd


def test_handoff_is_standard_no_card_document_handoff(tmp_path):
    md = handoff.render_handoff(
        question="What is X?",
        markdown_report="## R\nbody",
        findings=[
            Finding("/n/a.md", "a.md", "s", "e", "local"),
            Finding("cli://tool/abc", "T", "s3", "e3", "cli"),
            Finding("https://browser.example", "B", "s4", "e4", "browser"),
            Finding("http://e.com", "E", "s2", "e2", "web"),
        ],
        stats={"rounds": 2},
    )
    assert md.startswith("# Memory Handoff")
    assert "Recommended memory action\n\nno-card" in md
    assert "Target document\n\n.learnings/LEARNINGS.md" in md
    assert "Trusted (local)" in md and "Configured CLI" in md
    assert "Browser-assisted" in md and "Untrusted (web)" in md
    assert "What is X?" in md
    assert "\n### R\nbody" in md
    path = tmp_path / "handoff.md"
    path.write_text(md)
    assert handoff_cmd.lint_file(path).valid is True
