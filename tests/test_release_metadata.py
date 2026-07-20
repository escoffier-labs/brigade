from pathlib import Path

from brigade import __version__


ROOT = Path(__file__).resolve().parents[1]


def _release_changelog(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text()
    start = text.index(f"## [{version}]")
    end = text.index("\n## [", start + 1)
    return text[start:end]


def test_current_release_has_expected_v024_release_notes():
    assert __version__ == "0.24.0"

    text = _release_changelog(__version__)

    assert text.count("### Added") == 1
    assert text.count("### Fixed") == 1
    for expected in (
        "reports managed native component installation state",
        "Windows native acceptance now installs and exercises the supported component paths",
        "GraphTrail v0.4.0",
        "MiseLedger v0.6.0",
        "standalone repositories and release pipelines remain unchanged until Phase 4",
        "`brigade.code-reference.v1`",
        "evidence lookups before lexical fallback",
        "`brigade code sync|context|impact`",
        "`brigade evidence crawl|search|doctor`",
        "two minor releases or 90 days, whichever is longer",
        "terminalizes interrupted and stale runs",
        "refuses read-only execution for seats without that capability",
        "rejects empty tasks",
        "#374 / #375",
        "#379 / #381",
        "#361 / PR #389",
        "#362 / PR #390",
        "#382 / #383 / #384",
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
