from pathlib import Path

from brigade import __version__


ROOT = Path(__file__).resolve().parents[1]


def _release_changelog(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text()
    start = text.index(f"## [{version}]")
    end = text.index("\n## [", start + 1)
    return text[start:end]


def test_current_release_has_one_fixed_section_with_recent_release_notes():
    text = _release_changelog(__version__)

    assert text.count("### Fixed") == 1
    for expected in (
        "sessionfind v0.6.0",
        "`--help`",
        "absolute managed-path",
        "usage-text validation",
        "#355",
        "PR #370",
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
