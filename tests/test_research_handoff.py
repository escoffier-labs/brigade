from brigade.research import handoff
from brigade.research.types import Finding
from brigade import handoff_cmd


def test_handoff_is_standard_no_card_document_handoff(tmp_path):
    findings = [
        Finding(
            source_ids=("/n/a.md",),
            title="a.md",
            summary="s",
            evidence="e",
            trust="local",
            extraction_lane="luna",
            extracted_at="2026-08-13T12:00:00+00:00",
        ),
        Finding(
            source_ids=("cli://tool/abc",),
            title="T",
            summary="s3",
            evidence="e3",
            trust="cli",
            extraction_lane="luna",
            extracted_at="2026-08-13T12:00:00+00:00",
        ),
        Finding(
            source_ids=("https://browser.example",),
            title="B",
            summary="s4",
            evidence="e4",
            trust="browser",
            extraction_lane="luna",
            extracted_at="2026-08-13T12:00:00+00:00",
        ),
        Finding(
            source_ids=("http://e.com",),
            title="E",
            summary="s2",
            evidence="e2",
            trust="web",
            extraction_lane="luna",
            extracted_at="2026-08-13T12:00:00+00:00",
        ),
    ]
    assert findings[0].source == "/n/a.md"
    md = handoff.render_handoff(
        question="What is X?",
        markdown_report="## R\nbody",
        findings=findings,
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
