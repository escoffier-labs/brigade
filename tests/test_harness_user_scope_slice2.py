"""Issue #438 slice 2: OpenClaw/Kimi/Grok/Cursor/OpenCode user-scope harness profiles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import harness_profiles, mcp_cmd, skills_cmd

# Per-harness native user surfaces (relative to the temporary home).
SURFACES = {
    "openclaw": {
        "instruction": ".openclaw/workspace/AGENTS.md",
        "skills": ".openclaw/skills",
        "state": ".openclaw/brigade/install-state.json",
        "receipt": ".openclaw/brigade/profile-receipt.json",
        "mcp_config": ".openclaw/openclaw.json",
        "mcp_target": "openclaw",
    },
    "kimi": {
        "instruction": ".kimi-code/AGENTS.md",
        "skills": ".kimi-code/skills",
        "state": ".kimi-code/brigade/install-state.json",
        "receipt": ".kimi-code/brigade/profile-receipt.json",
        "mcp_config": ".kimi-code/mcp.json",
        "mcp_target": "kimi",
    },
    "grok": {
        "instruction": ".grok/AGENTS.md",
        "skills": ".grok/skills",
        "state": ".grok/brigade/install-state.json",
        "receipt": ".grok/brigade/profile-receipt.json",
        "mcp_config": ".grok/config.toml",
        "mcp_target": "grok",
    },
    "cursor": {
        "instruction": ".cursor/plugins/local/brigade-loop/rules/brigade-loop.mdc",
        "skills": ".cursor/skills",
        "state": ".cursor/brigade/install-state.json",
        "receipt": ".cursor/brigade/profile-receipt.json",
        "mcp_config": ".cursor/mcp.json",
        "mcp_target": "cursor",
    },
    "opencode": {
        "instruction": ".config/opencode/AGENTS.md",
        "skills": ".config/opencode/skills",
        "state": ".config/opencode/brigade/install-state.json",
        "receipt": ".config/opencode/brigade/profile-receipt.json",
        "mcp_config": ".config/opencode/opencode.json",
        "mcp_target": "opencode",
    },
}

SLICE2_HARNESSES = tuple(SURFACES)
# Harnesses whose instruction surface is a marked block inside a user-owned AGENTS.md.
MARKED_BLOCK_HARNESSES = ("openclaw", "kimi", "grok", "opencode")


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


def _sync_base(workspace: Path, target: str) -> list[str]:
    return ["harness", "sync", "--target", target, "--scope", "user", "--workspace", str(workspace)]


def _uninstall_base(workspace: Path, target: str) -> list[str]:
    return ["harness", "uninstall", "--target", target, "--scope", "user", "--workspace", str(workspace)]


def _doctor_base(workspace: Path, target: str) -> list[str]:
    return ["harness", "doctor", "--target", target, "--scope", "user", "--workspace", str(workspace)]


def _file_snapshot(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _add_reviewed_skill(workspace: Path, name: str = "reviewed") -> None:
    source = workspace / "sources" / name
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(f"# {name.title()}\n\nUse this skill.\n")
    (source / "skill.json").write_text(
        json.dumps(
            {
                "id": name,
                "title": name.title(),
                "version": "1.0.0",
                "required_tools": [],
                "required_mcp_servers": [],
                "supported_harnesses": list(harness_profiles.USER_SCOPE_HARNESS_IDS),
                "trust_level": "workspace",
                "tests": [],
            }
        )
    )
    assert skills_cmd.import_skill(target=workspace, source=source, json_output=True) == 0


def _workspace_with_stdio_server(tmp_path: Path, capsys, targets: list[str]) -> Path:
    workspace = _workspace(tmp_path)
    mcp_cmd.init(target=workspace, json_output=True)
    capsys.readouterr()
    mcp_cmd.add(
        target=workspace,
        name="brigade",
        command="brigade",
        args=["memory", "serve-mcp", "--stdio", "--target", "."],
        timeout=60,
        targets=targets,
        json_output=True,
    )
    capsys.readouterr()
    return workspace


def test_target_all_resolves_all_seven_user_scope_harnesses(tmp_path):
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    workspace.mkdir()
    profiles = harness_profiles.resolve_user_profiles(harness="all", home=home, workspace=workspace)
    assert tuple(profile.harness for profile in profiles) == (
        "claude",
        "codex",
        "openclaw",
        "kimi",
        "grok",
        "cursor",
        "opencode",
    )


def test_kimi_capability_probe_selects_surface_by_install(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    legacy_home = tmp_path / "legacy"
    profile = harness_profiles.resolve_user_profiles(harness="kimi", home=legacy_home, workspace=workspace)[0]
    assert profile.user_root == legacy_home / ".kimi-code"
    assert profile.mcp_path == legacy_home / ".kimi-code" / "mcp.json"

    newer_home = tmp_path / "newer"
    (newer_home / ".kimi").mkdir(parents=True)
    profile = harness_profiles.resolve_user_profiles(harness="kimi", home=newer_home, workspace=workspace)[0]
    assert profile.user_root == newer_home / ".kimi"
    assert profile.instruction_path == newer_home / ".kimi" / "AGENTS.md"
    assert profile.mcp_path == newer_home / ".kimi" / "mcp.json"

    both_home = tmp_path / "both"
    (both_home / ".kimi").mkdir(parents=True)
    (both_home / ".kimi-code").mkdir()
    profile = harness_profiles.resolve_user_profiles(harness="kimi", home=both_home, workspace=workspace)[0]
    assert profile.user_root == both_home / ".kimi"


@pytest.mark.parametrize("harness", SLICE2_HARNESSES)
def test_sync_dry_run_writes_nothing(tmp_path, monkeypatch, capsys, harness):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    assert cli.main(_sync_base(workspace, harness) + ["--json"]) == 0
    capsys.readouterr()
    assert _file_snapshot(home) == {}


@pytest.mark.parametrize("harness", SLICE2_HARNESSES)
def test_sync_write_then_resync_is_idempotent(tmp_path, monkeypatch, capsys, harness):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    base = _sync_base(workspace, harness)

    assert cli.main(base + ["--write", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["results"][0]["status"] == "updated"
    instruction = home / SURFACES[harness]["instruction"]
    assert instruction.is_file()
    first_text = instruction.read_text()
    snapshot = _file_snapshot(home)

    assert cli.main(base + ["--write", "--json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["results"][0]["status"] == "current"
    assert second["results"][0]["files_written"] == []
    assert instruction.read_text() == first_text
    assert _file_snapshot(home) == snapshot


@pytest.mark.parametrize("harness", MARKED_BLOCK_HARNESSES)
def test_hand_authored_instruction_section_survives_sync(tmp_path, monkeypatch, capsys, harness):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    instruction = home / SURFACES[harness]["instruction"]
    instruction.parent.mkdir(parents=True)
    instruction.write_text("# My notes\nKeep this paragraph.\n")

    assert cli.main(_sync_base(workspace, harness) + ["--write", "--json"]) == 0
    capsys.readouterr()
    text = instruction.read_text()
    assert "# My notes" in text
    assert "Keep this paragraph." in text
    assert harness_profiles.INSTRUCTION_START in text
    assert harness_profiles.managed_instruction_text().strip() in text


def test_cursor_managed_plugin_rule_hook_surface(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    hooks_json = home / ".cursor" / "hooks.json"
    hooks_json.parent.mkdir(parents=True)
    foreign_entry = {"command": "/usr/local/bin/foreign-hook"}
    hooks_json.write_text(json.dumps({"hooks": {"sessionStart": [foreign_entry]}}))

    assert cli.main(_sync_base(workspace, "cursor") + ["--write", "--json"]) == 0
    capsys.readouterr()

    rule = home / SURFACES["cursor"]["instruction"]
    assert rule.read_text().startswith("---\nalwaysApply: true\n---\n")
    assert (home / ".cursor" / "plugins" / "local" / "brigade-loop" / ".cursor-plugin" / "plugin.json").is_file()
    hook_script = home / ".cursor" / "hooks" / "brigade-session-start"
    assert hook_script.is_file()
    assert hook_script.stat().st_mode & 0o111
    entries = json.loads(hooks_json.read_text())["hooks"]["sessionStart"]
    assert foreign_entry in entries
    assert {"command": str(hook_script)} in entries


def test_cursor_edited_managed_rule_reports_conflict_and_preserves_edit(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    assert cli.main(_sync_base(workspace, "cursor") + ["--write", "--json"]) == 0
    capsys.readouterr()
    rule = home / SURFACES["cursor"]["instruction"]
    rule.write_text(rule.read_text() + "user edit\n")
    before = rule.read_bytes()

    assert cli.main(_sync_base(workspace, "cursor") + ["--write", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["status"] == "conflict"
    assert any(item["surface"] == "instruction" for item in payload["results"][0]["conflicts"])
    assert rule.read_bytes() == before

    assert cli.main(_doctor_base(workspace, "cursor") + ["--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["results"][0]["instruction_ready"] is False
    assert rule.read_bytes() == before


@pytest.mark.parametrize("harness", SLICE2_HARNESSES)
def test_uninstall_removes_only_brigade_owned_artifacts(tmp_path, monkeypatch, capsys, harness):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    instruction = home / SURFACES[harness]["instruction"]
    if harness in MARKED_BLOCK_HARNESSES:
        instruction.parent.mkdir(parents=True)
        instruction.write_text("# keep\n")
    else:
        hooks_json = home / ".cursor" / "hooks.json"
        hooks_json.parent.mkdir(parents=True)
        foreign_entry = {"command": "/usr/local/bin/foreign-hook"}
        hooks_json.write_text(json.dumps({"hooks": {"sessionStart": [foreign_entry]}}))

    assert cli.main(_sync_base(workspace, harness) + ["--write", "--json"]) == 0
    capsys.readouterr()

    # Dry-run uninstall removes nothing.
    assert cli.main(_uninstall_base(workspace, harness) + ["--json"]) == 0
    capsys.readouterr()
    assert (home / SURFACES[harness]["state"]).is_file()

    assert cli.main(_uninstall_base(workspace, harness) + ["--write", "--json"]) == 0
    capsys.readouterr()
    assert not (home / SURFACES[harness]["state"]).exists()
    assert not (home / SURFACES[harness]["receipt"]).exists()
    if harness in MARKED_BLOCK_HARNESSES:
        assert instruction.read_text() == "# keep\n"
    else:
        assert not instruction.exists()
        assert not (home / ".cursor" / "plugins").exists()
        assert not (home / ".cursor" / "hooks" / "brigade-session-start").exists()
        entries = json.loads((home / ".cursor" / "hooks.json").read_text())["hooks"]["sessionStart"]
        assert entries == [foreign_entry]


@pytest.mark.parametrize("harness", SLICE2_HARNESSES)
def test_doctor_reports_drift_without_mutating(tmp_path, monkeypatch, capsys, harness):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    assert cli.main(_sync_base(workspace, harness) + ["--write", "--json"]) == 0
    capsys.readouterr()
    state = home / SURFACES[harness]["state"]
    receipt = home / SURFACES[harness]["receipt"]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (state, receipt)}

    instruction = home / SURFACES[harness]["instruction"]
    instruction.write_text(instruction.read_text().replace("brigade", "drifted", 1))

    assert cli.main(_doctor_base(workspace, harness) + ["--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["results"][0]["ready"] is False
    assert report["results"][0]["instruction_ready"] is False
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (state, receipt)} == before


@pytest.mark.parametrize("harness", SLICE2_HARNESSES)
def test_skill_install_round_trip(tmp_path, monkeypatch, capsys, harness):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    _add_reviewed_skill(workspace)
    capsys.readouterr()

    assert cli.main(_sync_base(workspace, harness) + ["--write", "--json"]) == 0
    capsys.readouterr()
    installed = home / SURFACES[harness]["skills"] / "reviewed" / "SKILL.md"
    assert installed.is_file()

    assert cli.main(_uninstall_base(workspace, harness) + ["--write", "--json"]) == 0
    capsys.readouterr()
    assert not installed.exists()
    assert not (home / SURFACES[harness]["skills"] / "reviewed").exists()


@pytest.mark.parametrize("harness", SLICE2_HARNESSES)
def test_mcp_stdio_requires_allow_global_stdio_gate(tmp_path, monkeypatch, capsys, harness):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace_with_stdio_server(tmp_path, capsys, [SURFACES[harness]["mcp_target"]])
    config = home / SURFACES[harness]["mcp_config"]

    assert cli.main(_sync_base(workspace, harness) + ["--write", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["status"] == "conflict"
    assert not config.exists()

    assert cli.main(_sync_base(workspace, harness) + ["--allow-global-stdio", "--write", "--json"]) == 0
    capsys.readouterr()
    assert config.is_file()
    assert "brigade" in config.read_text()


@pytest.mark.parametrize("harness", SLICE2_HARNESSES)
def test_mcp_uninstall_removes_only_owned_server(tmp_path, monkeypatch, capsys, harness):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace_with_stdio_server(tmp_path, capsys, [SURFACES[harness]["mcp_target"]])
    config = home / SURFACES[harness]["mcp_config"]

    assert cli.main(_sync_base(workspace, harness) + ["--allow-global-stdio", "--write", "--json"]) == 0
    capsys.readouterr()
    assert "brigade" in config.read_text()

    assert cli.main(_uninstall_base(workspace, harness) + ["--write", "--json"]) == 0
    capsys.readouterr()
    assert "brigade" not in config.read_text()


def test_kimi_mcp_config_round_trip_preserves_foreign_keys(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace_with_stdio_server(tmp_path, capsys, ["kimi"])
    config = home / ".kimi-code" / "mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"theme": "dark", "mcpServers": {"foreign": {"command": "keep-me"}}}))

    assert cli.main(_sync_base(workspace, "kimi") + ["--allow-global-stdio", "--write", "--json"]) == 0
    capsys.readouterr()
    doc = json.loads(config.read_text())
    assert doc["theme"] == "dark"
    assert doc["mcpServers"]["foreign"] == {"command": "keep-me"}
    assert "brigade" in doc["mcpServers"]

    assert cli.main(_uninstall_base(workspace, "kimi") + ["--write", "--json"]) == 0
    capsys.readouterr()
    doc = json.loads(config.read_text())
    assert doc["theme"] == "dark"
    assert doc["mcpServers"] == {"foreign": {"command": "keep-me"}}


def test_kimi_probe_routes_sync_into_newer_surface(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace_with_stdio_server(tmp_path, capsys, ["kimi"])
    (home / ".kimi").mkdir()

    assert cli.main(_sync_base(workspace, "kimi") + ["--allow-global-stdio", "--write", "--json"]) == 0
    capsys.readouterr()
    assert (home / ".kimi" / "AGENTS.md").is_file()
    assert (home / ".kimi" / "mcp.json").is_file()
    assert not (home / ".kimi-code").exists()


def test_opencode_doctor_verify_mcp_reports_projection_drift(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace_with_stdio_server(tmp_path, capsys, ["opencode"])
    assert cli.main(_sync_base(workspace, "opencode") + ["--allow-global-stdio", "--write", "--json"]) == 0
    capsys.readouterr()

    assert cli.main(_doctor_base(workspace, "opencode") + ["--verify-mcp", "--json"]) == 0
    capsys.readouterr()

    config = home / ".config" / "opencode" / "opencode.json"
    config.write_text(config.read_text().replace("memory", "edited", 1))
    assert cli.main(_doctor_base(workspace, "opencode") + ["--verify-mcp", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["results"][0]["mcp"]["status"] == "conflict"


def test_cursor_sync_preserves_foreign_mcp_server(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace_with_stdio_server(tmp_path, capsys, ["cursor"])
    config = home / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mcpServers": {"foreign": {"command": "keep-me"}}}))

    assert cli.main(_sync_base(workspace, "cursor") + ["--allow-global-stdio", "--write", "--json"]) == 0
    capsys.readouterr()
    doc = json.loads(config.read_text())
    assert doc["mcpServers"]["foreign"] == {"command": "keep-me"}
    assert "brigade" in doc["mcpServers"]


def test_target_all_dry_run_reports_seven_results(tmp_path, monkeypatch, capsys):
    from brigade import cli

    _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    assert cli.main(_sync_base(workspace, "all") + ["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [result["harness"] for result in payload["results"]] == list(harness_profiles.USER_SCOPE_HARNESS_IDS)
    assert len(payload["results"]) == 7
