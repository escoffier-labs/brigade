"""Tests for solo-mise init."""
from __future__ import annotations

from pathlib import Path

import pytest

from solo_mise import init as init_mod


def test_repo_profile_lays_down_expected_files(tmp_target: Path):
    rc = init_mod.run(target=tmp_target, profile_id="repo")
    assert rc == 0
    assert (tmp_target / "AGENTS.md").is_file()
    assert (tmp_target / "CLAUDE.md").is_file()
    assert (tmp_target / ".claude" / "memory-handoffs" / "TEMPLATE.md").is_file()
    assert (tmp_target / "hooks" / "pre-push").is_file()
    # pre-push must be executable
    mode = (tmp_target / "hooks" / "pre-push").stat().st_mode & 0o777
    assert mode & 0o100, f"hooks/pre-push not executable: {oct(mode)}"


def test_workspace_profile_includes_memory_cards(tmp_target: Path):
    rc = init_mod.run(target=tmp_target, profile_id="workspace")
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
        "chat-surface-crawlers.md",
        "pipeline-standups.md",
    ):
        assert (tmp_target / "memory" / "cards" / card).is_file(), f"missing card {card}"
    assert (tmp_target / "memory" / "handoff-inbox").is_dir()
    assert (tmp_target / ".claude" / "memory-handoffs" / "processed").is_dir()


def test_openclaw_profile_extends_workspace(tmp_target: Path):
    rc = init_mod.run(target=tmp_target, profile_id="openclaw")
    assert rc == 0
    # workspace files present
    assert (tmp_target / "MEMORY.md").is_file()
    # openclaw fragments present
    fragments_dir = tmp_target / ".solo-mise" / "openclaw"
    assert (fragments_dir / "model-aliases.openclaw.json").is_file()
    assert (fragments_dir / "ollama-memory-search.openclaw.json").is_file()
    assert (fragments_dir / "acp-escalation.openclaw.json").is_file()


def test_hermes_profile_writes_experimental_fragments(tmp_target: Path):
    rc = init_mod.run(target=tmp_target, profile_id="hermes")
    assert rc == 0
    fragments_dir = tmp_target / ".solo-mise" / "hermes"
    assert (fragments_dir / "workspace.harness.json").is_file()
    assert (fragments_dir / "memory-handoff.harness.json").is_file()
    assert (fragments_dir / "model-lanes.harness.json").is_file()


def test_generic_profile_writes_contract_docs(tmp_target: Path):
    rc = init_mod.run(target=tmp_target, profile_id="generic")
    assert rc == 0
    docs = tmp_target / ".solo-mise" / "generic"
    assert (docs / "memory-contract.md").is_file()
    assert (docs / "harness-adapter-checklist.md").is_file()


def test_publisher_profile_writes_policies(tmp_target: Path):
    rc = init_mod.run(target=tmp_target, profile_id="publisher")
    assert rc == 0
    assert (tmp_target / "hooks" / "pre-push").is_file()
    assert (tmp_target / ".solo-mise" / "policies" / "public-repo.json").is_file()
    assert (tmp_target / ".solo-mise" / "policies" / "public-content.json").is_file()


def test_unknown_profile_returns_error(tmp_target: Path):
    rc = init_mod.run(target=tmp_target, profile_id="nonsense")
    assert rc == 2


def test_dry_run_creates_no_files_or_dirs(tmp_target: Path):
    rc = init_mod.run(target=tmp_target, profile_id="workspace", dry_run=True)
    assert rc == 0
    # Dry-run must not even materialize the target directory.
    assert not tmp_target.exists()


def test_init_refuses_home_directory(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    rc = init_mod.run(target=tmp_path, profile_id="repo")
    assert rc == 5
    assert not (tmp_path / "AGENTS.md").exists()


def test_init_allow_home_overrides(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    rc = init_mod.run(target=tmp_path, profile_id="repo", allow_home=True)
    assert rc == 0
    assert (tmp_path / "AGENTS.md").exists()


def test_init_rejects_unsafe_profile_paths(tmp_target: Path, tmp_path: Path, monkeypatch):
    """Inject a hostile profile JSON and confirm we refuse to apply it."""
    # Build a fake template tree under tmp_path/fake_templates.
    fake_root = tmp_path / "fake_templates"
    (fake_root / "profiles").mkdir(parents=True)
    (fake_root / "profiles" / "evil.json").write_text(
        '{"id": "evil", "memory_owner_default": "x", '
        '"files": [{"src": "../../etc/passwd", "dst": "AGENTS.md"}]}'
    )
    from solo_mise import templates as tmpls

    monkeypatch.setattr(tmpls, "template_root", lambda: fake_root)
    rc = init_mod.run(target=tmp_target, profile_id="evil")
    assert rc == 6
    assert not (tmp_target / "AGENTS.md").exists()


def test_init_refuses_overwrite_without_force(tmp_target: Path):
    tmp_target.mkdir()
    (tmp_target / "AGENTS.md").write_text("# pre-existing\n")
    rc = init_mod.run(target=tmp_target, profile_id="repo")
    assert rc == 3
    # original content untouched
    assert (tmp_target / "AGENTS.md").read_text() == "# pre-existing\n"


def test_force_overwrites_existing(tmp_target: Path):
    tmp_target.mkdir()
    (tmp_target / "AGENTS.md").write_text("# pre-existing\n")
    rc = init_mod.run(target=tmp_target, profile_id="repo", force=True)
    assert rc == 0
    text = (tmp_target / "AGENTS.md").read_text()
    assert "# pre-existing" not in text
    assert "AGENTS.md" in text or "Memory Owner" in text


def test_memory_owner_placeholder_renders_per_profile(tmp_target: Path):
    init_mod.run(target=tmp_target, profile_id="openclaw")
    agents = (tmp_target / "AGENTS.md").read_text()
    assert "OpenClaw" in agents
    assert "{{" not in agents and "}}" not in agents


def test_harness_override(tmp_target: Path):
    init_mod.run(target=tmp_target, profile_id="repo", harness="hermes")
    agents = (tmp_target / "AGENTS.md").read_text()
    assert "Hermes" in agents
