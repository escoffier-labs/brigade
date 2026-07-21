from datetime import date, timedelta
from pathlib import Path

from brigade import __version__


ROOT = Path(__file__).resolve().parents[1]


def _release_changelog(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text()
    start = text.index(f"## [{version}]")
    end = text.index("\n## [", start + 1)
    return text[start:end]


def test_current_release_has_expected_v025_release_notes():
    assert __version__ == "0.25.0"
    assert "no release has shipped from this entry" not in (ROOT / "CHANGELOG.md").read_text()

    text = _release_changelog(__version__)

    assert text.count("### Added") == 1
    assert text.count("### Deprecated") == 1
    assert text.count("### Fixed") == 1
    for expected in (
        "one verified asset set for `brigade setup`",
        "explicit `stable` and `beta` update channels",
        "Phase 4A policy documents migration and archive gates",
        "The compatibility window is active as of the v0.25.0 publication",
        "does not authorize archival",
        "direct `miseledger` Cargo feature",
        "`graphtrail context --evidence`",
        "`graphtrail links`",
        "at least two minor GraphTrail releases or 90 days",
        "`brigade code sync`",
        "`brigade evidence crawl`",
        "In-house repository and release-input guard policies",
        "only approved agent co-author trailers",
        "outbound public policies still strip them",
        "completed closeout does not re-arm",
        "SessionFind command-list help output",
        "#364 / #398",
        "#399",
        "#392",
        "#386 / #396",
        "#380 / #395",
        "#393",
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
    published_date = date(2026, 7, 21)
    calendar_gate = date(2026, 10, 19)

    assert path.is_file()
    text = path.read_text()
    policy_text = " ".join(text.split())

    for expected in (
        "v0.25.0",
        "v0.27.0",
        "both the version gate and the calendar gate",
        "v0.26.0",
        "Phase 4A and Phase 4B are this policy's execution split within RFC Phase 4",
        "v0.25.0 is live, so the window began at T0 on 2026-07-21T00:50:15Z",
        "Status: Phase 4A policy is active. Phase 4B archival execution is not authorized.",
        "The UTC calendar gate date is 2026-10-19.",
        "The gate does not open until 2026-10-19T00:50:15Z.",
        "A release date alone does not authorize removal, and a version number alone does not authorize archival.",
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
        "At T0, the governed operation inventory for each v0.25.0 shim is its public subcommands, help behavior, and JSON contracts.",
        "For every shipped non-meta operation, Phase 4B requires either a behavior-equivalent Brigade-owned path or an explicit maintainer decision to retain the shim.",
        "An operation without a disposition blocks removal.",
        "`--help`, `--version`, and `version` are compatibility probes, not migration workflows that require replacement commands.",
        "They must remain functional for the compatibility window.",
        "This includes `sessionfind version`; its probe does not imply that it needs a user-workflow replacement command.",
        "must not be rewritten or force-pushed",
        "No feature work returns to either mirror.",
        "documents the interim import-history, commit-map, and authorship context",
        "Agent Pantry is out of scope.",
        "Publish graphtrail 0.5.0 from the Brigade monorepo as the final compatibility minor.",
        "Patch releases during the compatibility window are limited to security and release-integrity fixes.",
        "unyanked",
        "deprecated and maintenance-frozen",
        "Phase 4B archival execution is not authorized",
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
        "current compatibility-equivalent engine commands, not final Brigade-owned replacements.",
        "Because `miseledger` is in the same shim cohort as `sessionfind`,",
        "`sessionfind` is not removal-ready until a Brigade-owned session list/search facade exists",
        "tests prove equivalent filters and JSON behavior",
        "deprecation message names that Brigade command.",
        "A missing Brigade-owned session facade blocks Phase 4B.",
        "`brigade setup` is the distribution replacement command for `graphtrail-mcp`.",
        "It installs the Brigade-managed `graphtrail-mcp` binary.",
        "MCP clients retain the `graphtrail-mcp` protocol but must move their configuration to the managed absolute path.",
        "The `graphtrail-mcp` deprecation message must name `brigade setup`, the managed-path configuration change, and the earliest removal condition",
        "- [ ] Confirm both the version gate and the calendar gate",
        "- [ ] Capture the T0 shim operation inventory and disposition every shipped non-meta operation with a behavior-equivalent Brigade-owned path or an explicit maintainer decision to retain the shim.",
        "- [ ] Build and verify the Brigade-owned session list/search facade, including equivalent filters and JSON behavior, then name it in the `sessionfind` deprecation message.",
        "- [ ] Migrate `graphtrail-mcp` MCP client configuration to the Brigade-managed absolute path installed by `brigade setup`, and verify its deprecation message.",
        "- [ ] Audit and migrate Brigade-generated MCP configs, including `src/brigade/cursor_user_cmd.py`, from PATH-based `graphtrail-mcp` and `miseledger` commands to managed absolute paths.",
        "- [ ] Archive `escoffier-labs/graphtrail` (prohibited during Phase 4A)",
        "- [ ] Archive `escoffier-labs/miseledger` (prohibited during Phase 4A)",
    ):
        assert expected in policy_text

    assert "| Published at | 2026-07-21T00:50:15Z |" in text
    assert f"| UTC calendar gate | {calendar_gate.isoformat()} |" in text
    assert "| Exact 90-day timestamp | 2026-10-19T00:50:15Z |" in text
    assert (
        "| Current status | The compatibility window is active. Phase 4B archival execution remains unauthorized |"
    ) in text
    assert calendar_gate == published_date + timedelta(days=90)
    assert "- [x]" not in text.lower()
    assert text.count("- [ ]") == 15
