"""User-scope Claude work-loop hook install/update/status/uninstall."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from brigade import cli
from brigade.claude_hooks.install_cmd import (
    hooks_install,
    hooks_status,
    hooks_uninstall,
    hooks_update,
    status_payload,
)
from brigade.claude_hooks.package import (
    HOOK_SCRIPT_NAME,
    MANAGED_MARKER_KEY,
    PACKAGE_ID,
    PACKAGE_REF,
    managed_user_command,
)
from brigade.claude_hooks.paths import resolve_claude_home


FOREIGN_SETTINGS = {
    "permissions": {"allow": ["Bash(git *)"]},
    "unknownFutureKey": {"keep": True},
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "echo foreign-pretool",
                    }
                ],
            }
        ],
        "Notification": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "echo foreign-notification",
                    }
                ],
            }
        ],
    },
}


@pytest.fixture
def claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "fake-home"
    claude = home / ".claude-config"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    assert resolve_claude_home() == claude.resolve()
    return claude


def test_user_hooks_install_writes_script_and_settings(claude_home: Path):
    settings = claude_home / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(FOREIGN_SETTINGS, indent=2) + "\n")

    assert hooks_install(target=Path("."), scope="user") == 0

    script = claude_home / "hooks" / HOOK_SCRIPT_NAME
    assert script.is_file()
    assert script.stat().st_mode & stat.S_IXUSR
    payload = json.loads(settings.read_text())
    assert payload["permissions"] == FOREIGN_SETTINGS["permissions"]
    foreign_cmds = [
        handler["command"]
        for group in payload["hooks"]["PreToolUse"]
        for handler in group.get("hooks", [])
        if isinstance(handler, dict)
    ]
    assert "echo foreign-pretool" in foreign_cmds
    assert any(str(script) in cmd for cmd in foreign_cmds)
    assert payload["hooks"]["Notification"] == FOREIGN_SETTINGS["hooks"]["Notification"]
    for groups in payload["hooks"].values():
        for group in groups:
            for handler in group.get("hooks", []):
                if isinstance(handler, dict) and handler.get(MANAGED_MARKER_KEY):
                    assert handler[MANAGED_MARKER_KEY] == PACKAGE_REF

    sidecar = json.loads((claude_home / "brigade" / "claude-hooks.json").read_text())
    assert sidecar["package_id"] == PACKAGE_ID
    assert sidecar["scope"] == "user"
    assert sidecar["script_path"] == f"hooks/{HOOK_SCRIPT_NAME}"


def test_user_hooks_reinstall_is_idempotent(claude_home: Path):
    assert hooks_install(target=Path("."), scope="user") == 0
    settings = claude_home / "settings.json"
    script = claude_home / "hooks" / HOOK_SCRIPT_NAME
    before_settings = settings.read_text()
    before_script = script.read_text()
    assert hooks_install(target=Path("."), scope="user") == 0
    assert settings.read_text() == before_settings
    assert script.read_text() == before_script


def test_user_hooks_update_preserves_foreign_hooks(claude_home: Path):
    settings = claude_home / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(FOREIGN_SETTINGS, indent=2) + "\n")
    assert hooks_install(target=Path("."), scope="user") == 0

    payload = json.loads(settings.read_text())
    script = claude_home / "hooks" / HOOK_SCRIPT_NAME
    stale = managed_user_command("PreToolUse", script).replace("PreToolUse", "Stop")
    for group in payload["hooks"]["PreToolUse"]:
        for handler in group.get("hooks", []):
            if isinstance(handler, dict) and handler.get(MANAGED_MARKER_KEY) == PACKAGE_REF:
                handler["command"] = stale
    settings.write_text(json.dumps(payload, indent=2) + "\n")

    assert status_payload(Path("."), scope="user")["current"] is False
    assert hooks_update(target=Path("."), scope="user") == 0
    updated = json.loads(settings.read_text())
    cmds = [
        handler["command"]
        for group in updated["hooks"]["PreToolUse"]
        for handler in group.get("hooks", [])
        if isinstance(handler, dict)
    ]
    assert "echo foreign-pretool" in cmds
    assert stale not in cmds
    assert managed_user_command("PreToolUse", script) in cmds


def test_user_hooks_uninstall_removes_only_brigade_owned(claude_home: Path):
    settings = claude_home / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(FOREIGN_SETTINGS, indent=2) + "\n")
    assert hooks_install(target=Path("."), scope="user") == 0
    assert hooks_uninstall(target=Path("."), scope="user") == 0

    payload = json.loads(settings.read_text())
    assert payload["permissions"] == FOREIGN_SETTINGS["permissions"]
    assert payload["hooks"]["Notification"] == FOREIGN_SETTINGS["hooks"]["Notification"]
    cmds = [
        handler["command"]
        for group in payload["hooks"].get("PreToolUse", [])
        for handler in group.get("hooks", [])
        if isinstance(handler, dict)
    ]
    assert cmds == ["echo foreign-pretool"]
    assert not (claude_home / "hooks" / HOOK_SCRIPT_NAME).exists()
    assert not (claude_home / "brigade" / "claude-hooks.json").exists()


def test_user_hooks_status_reports_script_staleness(claude_home: Path):
    assert hooks_install(target=Path("."), scope="user") == 0
    script = claude_home / "hooks" / HOOK_SCRIPT_NAME
    script.write_text("# stale\n")

    status = status_payload(Path("."), scope="user")

    assert status["installed"] is True
    assert status["current"] is False
    assert status["script_current"] is False
    assert status["script_installed"] is True


def test_user_hooks_status_reports_missing_script(claude_home: Path):
    assert hooks_install(target=Path("."), scope="user") == 0
    (claude_home / "hooks" / HOOK_SCRIPT_NAME).unlink()

    status = status_payload(Path("."), scope="user")

    assert status["installed"] is False
    assert status["script_installed"] is False


def test_user_hooks_cli_roundtrip(claude_home: Path, capsys):
    settings = claude_home / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(FOREIGN_SETTINGS, indent=2) + "\n")

    assert cli.main(["work", "hooks", "install", "--scope", "user"]) == 0
    assert cli.main(["work", "hooks", "status", "--scope", "user", "--json"]) == 0
    status = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert status["scope"] == "user"
    assert status["installed"] is True

    assert cli.main(["work", "hooks", "update", "--scope", "user"]) == 0
    assert cli.main(["work", "hooks", "uninstall", "--scope", "user"]) == 0
    assert hooks_status(target=Path("."), scope="user", json_output=True) == 0


def test_resolve_claude_home_falls_back_to_dot_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "operator-home"
    home.mkdir()
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    assert resolve_claude_home() == (home / ".claude").resolve()


def test_user_hooks_uninstall_preserves_edited_script(claude_home: Path):
    assert hooks_install(target=Path("."), scope="user") == 0
    script = claude_home / "hooks" / HOOK_SCRIPT_NAME
    script.write_text("# user-edited\n")
    assert hooks_uninstall(target=Path("."), scope="user") == 0
    assert script.read_text() == "# user-edited\n"


def test_managed_user_command_quotes_paths_with_spaces(tmp_path):
    import shlex

    script = tmp_path / "Application Support" / "hooks" / "brigade-work-loop.py"
    command = managed_user_command("SessionStart", script)
    tokens = shlex.split(command)
    assert tokens[0] == str(script)
    assert tokens[1:] == ["--event", "SessionStart"]
