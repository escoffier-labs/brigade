"""Instruction depth profiles and brief-as-SSoT wiring (issue #733)."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import brief_sections, harness_profile_cmd, harness_profiles, managed_block


def _use_home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    return home


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def test_minimal_profile_is_short_pointer_without_command_tables():
    body = harness_profiles.managed_instruction_text(profile="minimal")
    lines = [line for line in body.splitlines() if line.strip()]
    assert len(lines) <= 15
    assert "Work loop protocol" not in body
    assert "| Command |" not in body
    assert "brigade work brief" in body
    assert "Memory Handoff" in body
    assert body == brief_sections.render_static_instruction("minimal")


def test_full_profile_uses_same_sections_as_brief_renderer():
    full = harness_profiles.managed_instruction_text(profile="full")
    assert full == brief_sections.render_sections(brief_sections.FULL_SECTIONS)
    assert "## Session close" in full
    assert "Work loop protocol" in full
    for item in brief_sections.session_close_items():
        assert item in full


def test_effective_profile_is_richest_active_requirement():
    assert brief_sections.effective_profile(["minimal", "full"]) == "full"
    assert brief_sections.effective_profile(["full", "minimal"]) == "full"
    assert brief_sections.effective_profile(["minimal"]) == "minimal"
    assert brief_sections.effective_profile([]) is None


def test_shared_file_full_then_minimal_keeps_full(tmp_path, monkeypatch):
    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    shared = home / "shared" / "AGENTS.md"
    shared.parent.mkdir(parents=True)

    def _profiles(*, harness: str, home: Path, workspace: Path):
        selected = ("claude", "codex") if harness == "all" else (harness,)
        out = []
        for name in selected:
            root = home / f".{name}"
            out.append(
                harness_profiles.HarnessProfile(
                    harness=name,
                    user_root=root,
                    instruction_path=shared,
                    skills_root=root / "skills",
                    state_path=root / "brigade" / "install-state.json",
                    receipt_path=root / "brigade" / "profile-receipt.json",
                    mcp_harness=f"{name}-user" if name != "claude" else "claude-user",
                    reload_hint=f"restart {name}",
                )
            )
        return tuple(out)

    monkeypatch.setattr(harness_profiles, "resolve_user_profiles", _profiles)

    assert (
        harness_profile_cmd.sync(
            harness="codex", workspace=workspace, write=True, profile="full", json_output=True, home=home
        )
        == 0
    )
    text = shared.read_text(encoding="utf-8")
    assert "profile:full" in text
    assert "Work loop protocol" in text

    assert (
        harness_profile_cmd.sync(
            harness="claude", workspace=workspace, write=True, profile="minimal", json_output=True, home=home
        )
        == 0
    )
    text = shared.read_text(encoding="utf-8")
    assert "profile:full" in text
    assert "Work loop protocol" in text
    assert text.count("BEGIN BRIGADE INTEGRATION") == 1


def test_shared_file_minimal_then_full_converges(tmp_path, monkeypatch):
    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    shared = home / "shared" / "AGENTS.md"
    shared.parent.mkdir(parents=True)

    def _profiles(*, harness: str, home: Path, workspace: Path):
        selected = ("claude", "codex") if harness == "all" else (harness,)
        out = []
        for name in selected:
            root = home / f".{name}"
            out.append(
                harness_profiles.HarnessProfile(
                    harness=name,
                    user_root=root,
                    instruction_path=shared,
                    skills_root=root / "skills",
                    state_path=root / "brigade" / "install-state.json",
                    receipt_path=root / "brigade" / "profile-receipt.json",
                    mcp_harness=f"{name}-user" if name != "claude" else "claude-user",
                    reload_hint=f"restart {name}",
                )
            )
        return tuple(out)

    monkeypatch.setattr(harness_profiles, "resolve_user_profiles", _profiles)

    assert (
        harness_profile_cmd.sync(
            harness="claude", workspace=workspace, write=True, profile="minimal", json_output=True, home=home
        )
        == 0
    )
    assert "profile:minimal" in shared.read_text(encoding="utf-8")

    assert (
        harness_profile_cmd.sync(
            harness="codex", workspace=workspace, write=True, profile="full", json_output=True, home=home
        )
        == 0
    )
    text = shared.read_text(encoding="utf-8")
    assert "profile:full" in text
    assert "Work loop protocol" in text


def test_uninstall_last_full_consumer_drops_shared_file_to_minimal(tmp_path, monkeypatch):
    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    shared = home / "shared" / "AGENTS.md"
    shared.parent.mkdir(parents=True)

    def _profiles(*, harness: str, home: Path, workspace: Path):
        selected = ("claude", "codex") if harness == "all" else (harness,)
        out = []
        for name in selected:
            root = home / f".{name}"
            out.append(
                harness_profiles.HarnessProfile(
                    harness=name,
                    user_root=root,
                    instruction_path=shared,
                    skills_root=root / "skills",
                    state_path=root / "brigade" / "install-state.json",
                    receipt_path=root / "brigade" / "profile-receipt.json",
                    mcp_harness=f"{name}-user" if name != "claude" else "claude-user",
                    reload_hint=f"restart {name}",
                )
            )
        return tuple(out)

    monkeypatch.setattr(harness_profiles, "resolve_user_profiles", _profiles)

    assert (
        harness_profile_cmd.sync(
            harness="claude", workspace=workspace, write=True, profile="minimal", json_output=True, home=home
        )
        == 0
    )
    assert (
        harness_profile_cmd.sync(
            harness="codex", workspace=workspace, write=True, profile="full", json_output=True, home=home
        )
        == 0
    )
    assert "profile:full" in shared.read_text(encoding="utf-8")

    assert (
        harness_profile_cmd.uninstall(harness="codex", workspace=workspace, write=True, json_output=True, home=home)
        == 0
    )
    text = shared.read_text(encoding="utf-8")
    assert "profile:minimal" in text
    assert "Work loop protocol" not in text
    assert (home / ".claude" / "brigade" / "install-state.json").is_file()
    assert not (home / ".codex" / "brigade" / "install-state.json").exists()


def test_profile_override_beats_hook_default(tmp_path, monkeypatch):
    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(harness_profile_cmd, "hooks_inject_brief", lambda profile: True)

    assert (
        harness_profile_cmd.sync(
            harness="claude", workspace=workspace, write=True, profile="full", json_output=True, home=home
        )
        == 0
    )
    text = (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "profile:full" in text
    state = json.loads((home / ".claude" / "brigade" / "install-state.json").read_text(encoding="utf-8"))
    assert state["instructions"]["required_profile"] == "full"
    assert state["instructions"]["effective_profile"] == "full"


def test_doctor_flags_minimal_when_hooks_inactive(tmp_path, monkeypatch, capsys):
    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(harness_profile_cmd, "hooks_inject_brief", lambda profile: True)
    assert harness_profile_cmd.sync(harness="claude", workspace=workspace, write=True, json_output=True, home=home) == 0
    text = (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "profile:minimal" in text

    monkeypatch.setattr(harness_profile_cmd, "hooks_inject_brief", lambda profile: False)
    assert harness_profile_cmd.doctor(harness="claude", workspace=workspace, json_output=True, home=home) == 1
    out = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(out)
    result = payload["results"][0]
    assert result["ready"] is False
    surfaces = {item["surface"] for item in result["conflicts"]}
    assert "instruction-profile" in surfaces or "instruction" in surfaces


def test_marker_profile_mismatch_is_stale_even_when_body_matches():
    body = harness_profiles.managed_instruction_text(profile="minimal")
    stamped_full = managed_block.render_block(body, profile="full")
    assessment = managed_block.assess_block(stamped_full, desired=body, profile="minimal")
    assert assessment.status == managed_block.STATUS_STALE
    assert "profile is stale" in (assessment.detail or "")
