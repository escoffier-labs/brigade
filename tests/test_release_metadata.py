from pathlib import Path

from brigade import __version__


ROOT = Path(__file__).resolve().parents[1]


def _release_changelog(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text()
    start = text.index(f"## [{version}]")
    end = text.index("\n## [", start + 1)
    return text[start:end]


def test_current_release_has_expected_v0251_release_notes():
    assert __version__ == "0.25.1"
    assert "no release has shipped from this entry" not in (ROOT / "CHANGELOG.md").read_text()

    text = _release_changelog(__version__)

    assert text.count("### Added") == 1
    assert text.count("### Fixed") == 1
    assert text.count("### Removed") == 1
    assert text.count("### Deprecated") == 0
    for expected in (
        "owns crawler runtime selection",
        "read-only compatibility check before delegating to MiseLedger",
        "`NO_PENDING` queue no longer masks a failed producer",
        "preflight `cloudflare-ai-gateway/` seats",
        "`CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_GATEWAY_ID`",
        "more than one enabled producer can write the same memory-care queue artifact",
        "obsolete standalone `brigade-work-loop.py` hook",
        "skips an oversized JSONL line instead of aborting",
        "`solo-mise` command alias",
        "#404",
        "#394",
        "#403",
        "#397",
        "#420",
        "#421",
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


def test_phase_4a_compatibility_and_archive_policy_is_tracked():
    path = ROOT / "docs" / "phase-4a-compatibility-and-archive.md"

    assert path.is_file()
    text = path.read_text()
    policy_text = " ".join(text.split())

    for expected in (
        "v0.25.0",
        "Phase 4A and Phase 4B are this policy's execution split within RFC Phase 4",
        "The compatibility window is compressed by maintainer decision",
        "checklist items below are complete",
        "The dual gate is therefore waived.",
        "zero forks, zero known reverse dependencies",
        "No legal or dependency blocker exists.",
        "`graphtrail`",
        "`graphtrail-mcp`",
        "`miseledger`",
        "`sessionfind`",
        "`brigade search sync`",
        "`brigade search context`",
        "`brigade search impact`",
        "`brigade code sync`",
        "`brigade code context`",
        "`brigade code impact`",
        "[one-release fallback](update-channels.md)",
        "must not be rewritten or force-pushed",
        "No feature work returns to either mirror.",
        "documents the interim import-history, commit-map, and authorship context",
        "Agent Pantry is out of scope.",
        "no further crates.io releases ship",
        "unyanked",
        "deprecated and maintenance-frozen",
        "escoffier-labs/graphtrail",
        "escoffier-labs/miseledger",
        "agent-notify/#366 is out of scope",
        "https://github.com/escoffier-labs/brigade/issues/352#issuecomment-5018303485",
        "https://github.com/escoffier-labs/brigade/issues/352#issuecomment-5019220456",
        "https://github.com/escoffier-labs/brigade/issues/364",
        "https://github.com/escoffier-labs/brigade/issues/365",
        "`sessionfind list` | `miseledger sessions list`",
        "`sessionfind search <query>` | `miseledger sessions search <query>`",
        "`sessionfind <query>` | `miseledger sessions search <query>`",
        "| Compatibility invocation | Current compatibility-equivalent engine command |",
        "Existing databases, data paths, and schemas are non-destructive invariants.",
        "Archiving a mirror freezes it read-only on GitHub. It deletes nothing.",
        "- [ ] Confirm migration notices as ordinary commits on both mirrors.",
        "- [ ] Verify that neither standalone `master` branch was rewritten or force-pushed.",
        "- [ ] Update product and documentation links to the Brigade release path.",
        "- [ ] Archive `escoffier-labs/graphtrail`.",
        "- [ ] Archive `escoffier-labs/miseledger`.",
    ):
        assert expected in policy_text

    assert "| Published at | 2026-07-21T00:50:15Z |" in text
    assert "| Original dual gate | v0.27.0 + 2026-10-19 calendar gate (waived 2026-07-21) |" in text
    assert "| Current status | Window compressed. Phase 4B authorized pending checklist completion |" in text
    assert text.count("- [x]") == 3
    assert text.count("- [ ]") == 5
