from brigade.research import handoff
from brigade.research.types import Finding

def test_handoff_has_frontmatter_and_provenance():
    md = handoff.render_handoff(question="What is X?", markdown_report="## R\nbody",
                                findings=[Finding("/n/a.md", "a.md", "s", "e", "local"),
                                          Finding("http://e.com", "E", "s2", "e2", "web")],
                                stats={"rounds": 2})
    assert md.startswith("---")
    assert "destination: card" in md
    assert "Trusted (local)" in md and "Untrusted (web)" in md
    assert "What is X?" in md
