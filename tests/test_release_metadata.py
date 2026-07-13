from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _unreleased_changelog() -> str:
    text = (ROOT / "CHANGELOG.md").read_text()
    start = text.index("## [Unreleased]")
    end = text.index("\n## [", start + 1)
    return text[start:end]


def test_unreleased_has_one_added_section_with_recent_release_notes():
    text = _unreleased_changelog()

    assert text.count("### Added") == 1
    for expected in (
        "brigade run --wait",
        "adapter execution modes",
        "GraphTrail personalized ranking benchmark",
        "typed run transport and receipt modules",
    ):
        assert expected in text


def test_repo_memory_handoff_template_matches_agents_guidance():
    path = ROOT / ".claude" / "memory-handoffs" / "TEMPLATE.md"

    assert path.is_file()
    text = path.read_text()
    for heading in (
        "# Memory Handoff",
        "## Type",
        "## Title",
        "## Summary",
        "## Durable facts",
        "## Evidence",
        "## Recommended memory action",
    ):
        assert heading in text
