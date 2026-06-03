"""Tests for brigade init (CLI + install_selection behavior)."""
from __future__ import annotations

from pathlib import Path

import pytest

from brigade.install import install_selection
from brigade.selection import Selection


def _repo_sel() -> Selection:
    return Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])


def _workspace_sel() -> Selection:
    return Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[])


def _openclaw_sel() -> Selection:
    return Selection(
        depth="workspace",
        harnesses=["claude", "openclaw"],
        owner="openclaw",
        includes=[],
    )


def _hermes_sel() -> Selection:
    return Selection(
        depth="workspace",
        harnesses=["claude", "hermes"],
        owner="hermes",
        includes=[],
    )


def _generic_sel() -> Selection:
    return Selection(depth="workspace", harnesses=[], owner="this-repo", includes=[])


def _publisher_sel() -> Selection:
    return Selection(
        depth="repo",
        harnesses=["claude"],
        owner="claude",
        includes=["publisher"],
    )


def test_repo_install_lays_down_expected_files(tmp_target: Path):
    rc = install_selection(tmp_target, _repo_sel())
    assert rc == 0
    assert (tmp_target / "AGENTS.md").is_file()
    assert (tmp_target / "CLAUDE.md").is_file()
    assert (tmp_target / ".claude" / "memory-handoffs" / "TEMPLATE.md").is_file()
    assert (tmp_target / ".brigade" / "handoff-sources.example.json").is_file()
    assert (tmp_target / "rules" / "issue-tdd-loop.md").is_file()
    assert (tmp_target / "rules" / "acceptance-driven-work.md").is_file()
    rules = (tmp_target / "rules" / "issue-tdd-loop.md").read_text()
    assert "personal preference" in rules
    assert "remote issues" in rules
    assert (tmp_target / "hooks" / "pre-push").is_file()
    # pre-push must be executable
    mode = (tmp_target / "hooks" / "pre-push").stat().st_mode & 0o777
    assert mode & 0o100, f"hooks/pre-push not executable: {oct(mode)}"


def test_workspace_install_includes_memory_cards(tmp_target: Path):
    rc = install_selection(tmp_target, _workspace_sel())
    assert rc == 0
    for fname in (
        "AGENTS.md",
        "CLAUDE.md",
        "SOUL.md",
        "USER.md",
        "TOOLS.md",
        "MEMORY.md",
        "IDENTITY.md",
        "HEARTBEAT.md",
        "SAFETY_RULES.md",
        "INSTALL_FOR_AGENTS.md",
    ):
        assert (tmp_target / fname).is_file(), f"missing {fname}"
    for card in (
        "memory-architecture.md",
        "handoff-flow.md",
        "content-safety.md",
        "memory-scanner.md",
        "memory-care-staleness.md",
        "multi-workspace-handoff-admin.md",
        "tokenjuice-output-compaction.md",
        "chat-surface-crawlers.md",
        "pipeline-standups.md",
        "obsidian-notes.md",
        "backup-restic.md",
    ):
        assert (tmp_target / "memory" / "cards" / card).is_file(), f"missing card {card}"
    assert (tmp_target / "memory" / "handoff-inbox").is_dir()
    assert (tmp_target / ".claude" / "memory-handoffs" / "processed").is_dir()
    assert (tmp_target / ".brigade" / "memory-care.example.json").is_file()
    assert (tmp_target / ".brigade" / "chat-memory-sweep.example.json").is_file()
    assert (tmp_target / ".brigade" / "handoff-sources.example.json").is_file()
    assert (tmp_target / "rules" / "issue-tdd-loop.md").is_file()
    assert (tmp_target / "rules" / "acceptance-driven-work.md").is_file()
    # skill + script land at the right paths, executable bit on the script
    assert (tmp_target / "skills" / "note" / "SKILL.md").is_file()
    backup = tmp_target / "scripts" / "backup-restic.sh"
    assert backup.is_file()
    assert backup.stat().st_mode & 0o111, "backup-restic.sh should be executable"


def test_openclaw_install_extends_workspace(tmp_target: Path):
    rc = install_selection(tmp_target, _openclaw_sel())
    assert rc == 0
    # workspace files present
    assert (tmp_target / "MEMORY.md").is_file()
    # openclaw fragments present
    fragments_dir = tmp_target / ".brigade" / "openclaw"
    assert (fragments_dir / "model-aliases.openclaw.json").is_file()
    assert (fragments_dir / "ollama-memory-search.openclaw.json").is_file()
    assert (fragments_dir / "acp-escalation.openclaw.json").is_file()
    assert (fragments_dir / "memory-sweep-cron.openclaw.json").is_file()


def test_hermes_install_writes_experimental_fragments(tmp_target: Path):
    rc = install_selection(tmp_target, _hermes_sel())
    assert rc == 0
    fragments_dir = tmp_target / ".brigade" / "hermes"
    assert (fragments_dir / "workspace.harness.json").is_file()
    assert (fragments_dir / "memory-handoff.harness.json").is_file()
    assert (fragments_dir / "model-lanes.harness.json").is_file()
    assert (tmp_target / ".hermes" / "memory-handoffs" / "TEMPLATE.md").is_file()
    assert (tmp_target / ".hermes" / "memory-handoffs" / "processed").is_dir()


def test_generic_install_writes_baseline_workspace(tmp_target: Path):
    """`--harnesses none` still produces a workspace baseline with AGENTS.md + memory folders."""
    rc = install_selection(tmp_target, _generic_sel())
    assert rc == 0
    assert (tmp_target / "AGENTS.md").is_file()
    assert (tmp_target / "MEMORY.md").is_file()
    # No harness writer => no .claude/.codex inbox.
    assert not (tmp_target / ".claude" / "memory-handoffs").exists()
    assert not (tmp_target / ".codex" / "memory-handoffs").exists()


def test_publisher_include_writes_policies(tmp_target: Path):
    rc = install_selection(tmp_target, _publisher_sel())
    assert rc == 0
    assert (tmp_target / "hooks" / "pre-push").is_file()
    assert (tmp_target / ".brigade" / "policies" / "public-repo.json").is_file()
    assert (tmp_target / ".brigade" / "policies" / "public-content.json").is_file()


def test_dry_run_creates_no_files_or_dirs(tmp_target: Path):
    rc = install_selection(tmp_target, _workspace_sel(), dry_run=True)
    assert rc == 0
    # Dry-run must not even materialize the target directory.
    assert not tmp_target.exists()


def test_install_refuses_home_directory(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    rc = install_selection(tmp_path, _repo_sel())
    assert rc == 5
    assert not (tmp_path / "AGENTS.md").exists()


def test_install_allow_home_overrides(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    rc = install_selection(tmp_path, _repo_sel(), allow_home=True)
    assert rc == 0
    assert (tmp_path / "AGENTS.md").exists()


def test_install_refuses_overwrite_without_force(tmp_target: Path):
    tmp_target.mkdir()
    (tmp_target / "AGENTS.md").write_text("# pre-existing\n")
    rc = install_selection(tmp_target, _repo_sel())
    assert rc == 3
    # original content untouched
    assert (tmp_target / "AGENTS.md").read_text() == "# pre-existing\n"


def test_force_overwrites_existing(tmp_target: Path):
    tmp_target.mkdir()
    (tmp_target / "AGENTS.md").write_text("# pre-existing\n")
    rc = install_selection(tmp_target, _repo_sel(), force=True)
    assert rc == 0
    text = (tmp_target / "AGENTS.md").read_text()
    assert "# pre-existing" not in text
    assert "AGENTS.md" in text or "Memory Owner" in text


def test_memory_owner_placeholder_renders_per_selection(tmp_target: Path):
    install_selection(tmp_target, _openclaw_sel())
    agents = (tmp_target / "AGENTS.md").read_text()
    assert "OpenClaw" in agents
    assert "{{" not in agents and "}}" not in agents


def test_owner_override_renders_in_bootstrap(tmp_target: Path):
    sel = Selection(
        depth="repo",
        harnesses=["claude", "hermes"],
        owner="hermes",
        includes=[],
    )
    install_selection(tmp_target, sel)
    agents = (tmp_target / "AGENTS.md").read_text()
    assert "Hermes" in agents


def test_cli_parses_depth_harnesses(monkeypatch, tmp_path):
    from brigade.cli import _build_parser
    parser = _build_parser()
    ns = parser.parse_args([
        "init",
        "--target", str(tmp_path),
        "--depth", "workspace",
        "--harnesses", "claude,codex,openclaw",
        "--owner", "openclaw",
        "--include", "publisher",
    ])
    assert ns.depth == "workspace"
    assert ns.harnesses == "claude,codex,openclaw"
    assert ns.owner == "openclaw"
    assert ns.includes == ["publisher"]


def test_cli_rejects_unknown_harness(tmp_path):
    from brigade.cli import main
    rc = main([
        "init", "--target", str(tmp_path),
        "--harnesses", "claude,weird",
    ])
    assert rc != 0


def test_cli_invokes_prompt_when_no_selection_flags(monkeypatch, tmp_path):
    """init without any selection flags should call prompt_for_selection."""
    called = {}
    from brigade import cli
    from brigade.selection import Selection

    def fake_prompt():
        called["yes"] = True
        return Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])

    monkeypatch.setattr(cli, "prompt_for_selection", fake_prompt)
    rc = cli.main(["init", "--target", str(tmp_path)])
    assert rc == 0
    assert called.get("yes") is True


def test_cli_skips_prompt_when_depth_given(monkeypatch, tmp_path):
    from brigade import cli
    def fail():
        raise AssertionError("prompt should not be called")
    monkeypatch.setattr(cli, "prompt_for_selection", fail)
    rc = cli.main(["init", "--target", str(tmp_path), "--depth", "repo", "--harnesses", "claude"])
    assert rc == 0
