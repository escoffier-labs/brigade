"""Issue #438 slice 1: Claude/Codex user-scope harness sync/doctor/uninstall."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import harness_profile_cmd, harness_profiles, mcp_cmd


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


def _sync_base(workspace: Path, target: str = "codex") -> list[str]:
    return [
        "harness",
        "sync",
        "--target",
        target,
        "--scope",
        "user",
        "--workspace",
        str(workspace),
    ]


def test_slice1_targets_resolve_only_claude_and_codex(tmp_path):
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    workspace.mkdir()
    profiles = harness_profiles.resolve_slice1_profiles(harness="all", home=home, workspace=workspace)
    assert tuple(p.harness for p in profiles) == ("claude", "codex")


def test_sync_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    assert cli.main(_sync_base(workspace) + ["--json"]) == 0
    capsys.readouterr()
    assert not (home / ".codex").exists()


def test_sync_write_then_resync_is_idempotent(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    base = _sync_base(workspace)

    assert cli.main(base + ["--write", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["results"][0]["status"] == "updated"
    agents = home / ".codex" / "AGENTS.md"
    assert agents.is_file()
    first_text = agents.read_text()

    assert cli.main(base + ["--write", "--json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["results"][0]["status"] == "current"
    assert second["results"][0]["files_written"] == []
    assert agents.read_text() == first_text


def test_hand_authored_agents_section_survives_sync(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    agents = home / ".codex" / "AGENTS.md"
    agents.parent.mkdir(parents=True)
    agents.write_text("# My notes\nKeep this paragraph.\n")

    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 0
    capsys.readouterr()
    text = agents.read_text()
    assert "# My notes" in text
    assert "Keep this paragraph." in text
    assert harness_profiles.INSTRUCTION_START in text
    assert harness_profiles.managed_instruction_text().strip() in text


def test_claude_hand_authored_section_survives_sync(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    claude_md = home / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    claude_md.write_text("## Personal routing\nDo not remove me.\n")

    assert cli.main(_sync_base(workspace, "claude") + ["--write", "--json"]) == 0
    capsys.readouterr()
    text = claude_md.read_text()
    assert "Personal routing" in text
    assert harness_profiles.INSTRUCTION_START in text


def test_foreign_managed_block_reports_conflict(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    agents = home / ".codex" / "AGENTS.md"
    agents.parent.mkdir(parents=True)
    agents.write_text(f"{harness_profiles.INSTRUCTION_START}\nuser-owned body\n{harness_profiles.INSTRUCTION_END}\n")

    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    row = payload["results"][0]
    assert row["status"] == "conflict"
    assert "user-owned body" in agents.read_text()


def test_doctor_reports_drift_without_mutating(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    base = _sync_base(workspace)
    assert cli.main(base + ["--write", "--json"]) == 0
    capsys.readouterr()

    agents = home / ".codex" / "AGENTS.md"
    agents.write_text(agents.read_text().replace("brigade run", "brigade dispatch"))

    assert (
        cli.main(["harness", "doctor", "--target", "codex", "--scope", "user", "--workspace", str(workspace), "--json"])
        == 1
    )
    doctor = json.loads(capsys.readouterr().out)
    row = doctor["results"][0]
    assert row["ready"] is False
    assert row["instruction_ready"] is False


def test_uninstall_removes_only_brigade_owned_block(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    agents = home / ".codex" / "AGENTS.md"
    agents.parent.mkdir(parents=True)
    agents.write_text("# keep\n")

    assert cli.main(_sync_base(workspace) + ["--write", "--json"]) == 0
    capsys.readouterr()

    assert (
        cli.main(
            [
                "harness",
                "uninstall",
                "--target",
                "codex",
                "--scope",
                "user",
                "--workspace",
                str(workspace),
                "--write",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert agents.read_text() == "# keep\n"
    assert not (home / ".codex" / "brigade" / "install-state.json").exists()


def _workspace_with_stdio_server(tmp_path: Path, monkeypatch, capsys, targets: list[str]) -> Path:
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


@pytest.mark.parametrize(
    ("target", "mcp_target", "config_rel"),
    [
        ("codex", "codex-user", ".codex/config.toml"),
        ("claude", "claude-user", ".claude.json"),
    ],
)
def test_mcp_stdio_requires_allow_global_stdio_gate(tmp_path, monkeypatch, capsys, target, mcp_target, config_rel):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace_with_stdio_server(tmp_path, monkeypatch, capsys, [mcp_target])

    assert cli.main(_sync_base(workspace, target) + ["--write", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["status"] == "conflict"
    assert not (home / config_rel).exists()

    assert cli.main(_sync_base(workspace, target) + ["--allow-global-stdio", "--write", "--json"]) == 0
    capsys.readouterr()
    assert (home / config_rel).is_file()


def test_mcp_uninstall_removes_only_owned_server(tmp_path, monkeypatch, capsys):
    from brigade import cli

    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace_with_stdio_server(tmp_path, monkeypatch, capsys, ["codex-user"])
    codex_cfg = home / ".codex" / "config.toml"
    codex_cfg.parent.mkdir(parents=True)
    codex_cfg.write_text('[mcp_servers.foreign]\ncommand = "keep"\n')

    assert cli.main(_sync_base(workspace) + ["--allow-global-stdio", "--write", "--json"]) == 0
    capsys.readouterr()
    assert "brigade" in codex_cfg.read_text()
    assert "foreign" in codex_cfg.read_text()

    assert (
        cli.main(
            [
                "harness",
                "uninstall",
                "--target",
                "codex",
                "--scope",
                "user",
                "--workspace",
                str(workspace),
                "--write",
                "--json",
            ]
        )
        == 0
    )
    text = codex_cfg.read_text()
    assert "brigade" not in text
    assert "foreign" in text


def test_harness_sync_cli_dispatch_contract(tmp_path, monkeypatch):
    from brigade import cli

    calls: dict[str, dict] = {}

    def recorder(name):
        def _f(**kwargs):
            calls[name] = kwargs
            return 0

        return _f

    monkeypatch.setattr(harness_profile_cmd, "sync", recorder("sync"), raising=False)
    monkeypatch.setattr(harness_profile_cmd, "uninstall", recorder("uninstall"), raising=False)
    monkeypatch.setattr(harness_profile_cmd, "doctor", recorder("doctor"), raising=False)
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)

    for target in harness_profiles.USER_SCOPE_SLICE1_TARGETS:
        cli.main(["harness", "sync", "--target", target, "--scope", "user", "--json"])
        assert calls["sync"]["harness"] == target
        assert calls["sync"]["write"] is False

    ws = tmp_path / "ws"
    ws.mkdir()
    cli.main(
        [
            "harness",
            "sync",
            "--target",
            "all",
            "--scope",
            "user",
            "--workspace",
            str(ws),
            "--write",
            "--allow-global-stdio",
            "--adopt",
            "--json",
        ]
    )
    assert calls["sync"]["harness"] == "all"
    assert calls["sync"]["workspace"] == ws
    assert calls["sync"]["write"] is True
    assert calls["sync"]["allow_global_stdio"] is True
    assert calls["sync"]["adopt"] is True

    cli.main(["harness", "uninstall", "--target", "codex", "--scope", "user", "--json"])
    assert calls["uninstall"]["harness"] == "codex"

    cli.main(["harness", "doctor", "--target", "claude", "--scope", "user", "--verify-mcp", "--json"])
    assert calls["doctor"]["harness"] == "claude"
    assert calls["doctor"]["verify_mcp"] is True
