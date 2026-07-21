"""RED: managed Claude hook settings merge + work hooks CLI (issue #249)."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import cli
from brigade.claude_hooks.install_cmd import hooks_install, hooks_status, hooks_uninstall, hooks_update, status_payload
from brigade.claude_hooks.package import (
    PACKAGE_ID,
    PACKAGE_VERSION,
    is_legacy_handler,
    managed_command,
)
from brigade.install import install_selection
from brigade.selection import Selection


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


def _wired_claude(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    sel = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    assert install_selection(target, sel) == 0
    return target


def test_hooks_install_preserves_foreign_hooks_and_unknown_keys(tmp_path: Path):
    target = _wired_claude(tmp_path)
    settings = target / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(FOREIGN_SETTINGS, indent=2) + "\n")

    assert hooks_install(target=target) == 0

    payload = json.loads(settings.read_text())
    assert payload["permissions"] == FOREIGN_SETTINGS["permissions"]
    assert payload["unknownFutureKey"] == {"keep": True}
    foreign_cmds = [
        h["command"] for group in payload["hooks"]["PreToolUse"] for h in group.get("hooks", []) if isinstance(h, dict)
    ]
    assert "echo foreign-pretool" in foreign_cmds
    assert any(c.startswith("brigade work hook-run") for c in foreign_cmds)
    assert payload["hooks"]["Notification"] == FOREIGN_SETTINGS["hooks"]["Notification"]

    sidecar = json.loads((target / ".brigade" / "claude-hooks.json").read_text())
    assert sidecar["package_id"] == PACKAGE_ID
    assert sidecar["package_version"] == PACKAGE_VERSION


def test_hooks_update_replaces_only_managed(tmp_path: Path):
    target = _wired_claude(tmp_path)
    settings = target / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(FOREIGN_SETTINGS, indent=2) + "\n")
    assert hooks_install(target=target) == 0

    # Simulate stale managed command version in settings.
    payload = json.loads(settings.read_text())
    stale = managed_command("PreToolUse").replace(f"@{PACKAGE_VERSION}", "@0.0.1")
    for group in payload["hooks"]["PreToolUse"]:
        for handler in group.get("hooks", []):
            if isinstance(handler, dict) and str(handler.get("command", "")).startswith("brigade work hook-run"):
                handler["command"] = stale
    settings.write_text(json.dumps(payload, indent=2) + "\n")

    assert status_payload(target)["current"] is False
    assert hooks_update(target=target) == 0
    updated = json.loads(settings.read_text())
    cmds = [
        h["command"] for group in updated["hooks"]["PreToolUse"] for h in group.get("hooks", []) if isinstance(h, dict)
    ]
    assert "echo foreign-pretool" in cmds
    assert stale not in cmds
    assert managed_command("PreToolUse") in cmds


def test_hooks_status_reports_complete_stale_package_as_installed(tmp_path: Path):
    target = _wired_claude(tmp_path)
    assert hooks_install(target=target) == 0
    settings = target / ".claude" / "settings.json"
    payload = json.loads(settings.read_text())
    for groups in payload["hooks"].values():
        for group in groups:
            for handler in group.get("hooks", []):
                command = handler.get("command")
                if isinstance(command, str) and command.startswith("brigade work hook-run"):
                    handler["command"] = command.replace(f"@{PACKAGE_VERSION}", "@0.0.1")
    settings.write_text(json.dumps(payload, indent=2) + "\n")

    status = status_payload(target)

    assert status["installed"] is True
    assert status["current"] is False
    assert status["managed_events"] == ["SessionStart", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop"]
    assert status["missing_events"] == []


def test_hooks_status_reports_matcher_drift_as_stale(tmp_path: Path):
    target = _wired_claude(tmp_path)
    assert hooks_install(target=target) == 0
    settings = target / ".claude" / "settings.json"
    payload = json.loads(settings.read_text())
    for group in payload["hooks"]["PreToolUse"]:
        group["matcher"] = "Edit"
    settings.write_text(json.dumps(payload, indent=2) + "\n")

    status = status_payload(target)

    assert status["installed"] is True
    assert status["current"] is False
    assert status["stale_events"] == ["PreToolUse"]


def test_hooks_uninstall_removes_mismatched_managed_command_but_preserves_prompt_handlers(tmp_path: Path):
    target = _wired_claude(tmp_path)
    settings = target / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    foreign_handlers = [
        {"type": "command", "command": managed_command("Stop")},
        {"type": "prompt", "command": managed_command("SessionStart")},
    ]
    settings.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": foreign_handlers}]}}, indent=2) + "\n")

    assert hooks_install(target=target) == 0
    assert hooks_uninstall(target=target) == 0

    payload = json.loads(settings.read_text())
    assert payload["hooks"]["SessionStart"] == [{"hooks": [foreign_handlers[1]]}]


def test_hooks_install_removes_managed_command_filed_under_wrong_event(tmp_path: Path):
    target = _wired_claude(tmp_path)
    settings = target / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    misplaced = {"type": "command", "command": managed_command("Stop")}
    settings.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [misplaced]}]}}, indent=2) + "\n")

    assert hooks_install(target=target) == 0

    payload = json.loads(settings.read_text())
    session_cmds = [
        handler["command"]
        for group in payload["hooks"]["SessionStart"]
        for handler in group.get("hooks", [])
        if isinstance(handler, dict)
    ]
    assert managed_command("Stop") not in session_cmds
    assert managed_command("SessionStart") in session_cmds


def test_hooks_uninstall_leaves_foreign(tmp_path: Path):
    target = _wired_claude(tmp_path)
    settings = target / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(FOREIGN_SETTINGS, indent=2) + "\n")
    assert hooks_install(target=target) == 0
    assert hooks_uninstall(target=target) == 0

    payload = json.loads(settings.read_text())
    assert payload["permissions"] == FOREIGN_SETTINGS["permissions"]
    assert payload["unknownFutureKey"] == {"keep": True}
    assert payload["hooks"]["Notification"] == FOREIGN_SETTINGS["hooks"]["Notification"]
    cmds = [
        h["command"]
        for group in payload["hooks"].get("PreToolUse", [])
        for h in group.get("hooks", [])
        if isinstance(h, dict)
    ]
    assert cmds == ["echo foreign-pretool"]
    assert not (target / ".brigade" / "claude-hooks.json").exists()


def test_hooks_uninstall_preserves_foreign_command_with_similar_prefix(tmp_path: Path):
    target = _wired_claude(tmp_path)
    settings = target / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    foreign = {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "brigade work hook-run-custom --event SessionStart",
                        }
                    ]
                }
            ]
        }
    }
    settings.write_text(json.dumps(foreign, indent=2) + "\n")

    assert hooks_install(target=target) == 0
    assert hooks_uninstall(target=target) == 0

    payload = json.loads(settings.read_text())
    commands = [handler["command"] for group in payload["hooks"]["SessionStart"] for handler in group["hooks"]]
    assert commands == ["brigade work hook-run-custom --event SessionStart"]


def test_hooks_install_rejects_unknown_managed_event_shape_without_writing(tmp_path: Path):
    target = _wired_claude(tmp_path)
    settings = target / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({"hooks": {"PreToolUse": {"futureShape": True}}}, indent=2).encode() + b"\n"
    settings.write_bytes(original)

    assert hooks_install(target=target) == 2
    assert settings.read_bytes() == original


def test_hooks_uninstall_preserves_unknown_foreign_event_shape(tmp_path: Path):
    target = _wired_claude(tmp_path)
    settings = target / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    foreign_shape = {"futureShape": True, "unknown": [1, 2, 3]}
    settings.write_text(json.dumps({"hooks": {"Notification": foreign_shape}}, indent=2) + "\n")

    assert hooks_uninstall(target=target) == 0

    payload = json.loads(settings.read_text())
    assert payload["hooks"]["Notification"] == foreign_shape


def test_hooks_uninstall_never_cleans_handlers_from_unknown_events(tmp_path: Path):
    target = _wired_claude(tmp_path)
    settings = target / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    foreign_group = {
        "hooks": [
            {
                "type": "command",
                "command": managed_command("SessionStart"),
            }
        ]
    }
    settings.write_text(json.dumps({"hooks": {"Notification": [foreign_group]}}, indent=2) + "\n")

    assert hooks_uninstall(target=target) == 0

    payload = json.loads(settings.read_text())
    assert payload["hooks"]["Notification"] == [foreign_group]


def test_work_hooks_cli_roundtrip(tmp_path: Path, capsys):
    target = _wired_claude(tmp_path)
    settings = target / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(FOREIGN_SETTINGS, indent=2) + "\n")

    assert cli.main(["work", "hooks", "install", "--target", str(target)]) == 0
    assert cli.main(["work", "hooks", "status", "--target", str(target), "--json"]) == 0
    status_out = capsys.readouterr().out
    status = json.loads(status_out.strip().splitlines()[-1])
    assert status["package_id"] == PACKAGE_ID
    assert status["installed"] is True

    assert cli.main(["work", "hooks", "update", "--target", str(target)]) == 0
    assert cli.main(["work", "hooks", "uninstall", "--target", str(target)]) == 0
    assert hooks_status(target=target, json_output=True) == 0


def test_claude_install_wires_project_hooks(tmp_path: Path):
    target = tmp_path / "fresh"
    sel = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    assert install_selection(target, sel) == 0

    settings = target / ".claude" / "settings.json"
    assert settings.is_file()
    payload = json.loads(settings.read_text())
    assert "SessionStart" in payload["hooks"]
    assert "PreToolUse" in payload["hooks"]
    assert "Stop" in payload["hooks"]
    sidecar = json.loads((target / ".brigade" / "claude-hooks.json").read_text())
    assert sidecar["package_id"] == PACKAGE_ID


def test_hooks_install_preserves_malformed_settings_on_error(tmp_path: Path):
    target = _wired_claude(tmp_path)
    settings = target / ".claude" / "settings.json"
    original = b"{not-json\n"
    settings.write_bytes(original)

    assert hooks_install(target=target) == 2
    assert settings.read_bytes() == original


def test_claude_templates_import_agents_md():
    from brigade.templates import template_root

    root = template_root()
    for rel in ("repo/CLAUDE.md", "workspace/CLAUDE.md"):
        text = (root / rel).read_text()
        assert any(line.strip() == "@AGENTS.md" for line in text.splitlines())


LEGACY_WORK_LOOP_CMD = "python3 hooks/brigade-work-loop.py --event SessionStart"


def _legacy_handler(event: str) -> dict[str, str]:
    return {
        "type": "command",
        "command": LEGACY_WORK_LOOP_CMD.replace("SessionStart", event),
    }


def test_hooks_install_removes_legacy_standalone_handler_and_is_idempotent(tmp_path: Path):
    target = _wired_claude(tmp_path)
    settings = target / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    seeded = {
        "hooks": {
            "SessionStart": [{"hooks": [_legacy_handler("SessionStart")]}],
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        _legacy_handler("PreToolUse"),
                        {"type": "command", "command": "echo foreign-pretool"},
                    ],
                }
            ],
        }
    }
    settings.write_text(json.dumps(seeded, indent=2) + "\n")

    assert hooks_install(target=target) == 0
    after_first = json.loads(settings.read_text())
    pre_cmds = [
        handler["command"]
        for group in after_first["hooks"]["PreToolUse"]
        for handler in group.get("hooks", [])
        if isinstance(handler, dict)
    ]
    assert "echo foreign-pretool" in pre_cmds
    assert not any("brigade-work-loop.py" in cmd for cmd in pre_cmds)
    session_cmds = [
        handler["command"]
        for group in after_first["hooks"]["SessionStart"]
        for handler in group.get("hooks", [])
        if isinstance(handler, dict)
    ]
    assert not any("brigade-work-loop.py" in cmd for cmd in session_cmds)
    assert any(cmd.startswith("brigade work hook-run") for cmd in session_cmds)

    before_second = settings.read_text()
    assert hooks_install(target=target) == 0
    assert settings.read_text() == before_second


def test_is_legacy_handler_anchors_to_executable_position():
    # False positives: mentions of the filename in non-executable positions.
    assert is_legacy_handler({"type": "command", "command": "grep -r brigade-work-loop.py ."}) is False
    assert is_legacy_handler({"type": "command", "command": "echo hi # brigade-work-loop.py"}) is False
    assert is_legacy_handler({"type": "command", "command": 'echo "docs/brigade-work-loop.py"'}) is False
    # Direct executable form.
    assert (
        is_legacy_handler(
            {
                "type": "command",
                "command": "python3 /home/u/.claude/hooks/brigade-work-loop.py --event PreToolUse",
            }
        )
        is True
    )
    assert (
        is_legacy_handler(
            {
                "type": "command",
                "command": "/opt/brigade/hooks/brigade-work-loop.py",
            }
        )
        is True
    )
    # Interpreter form with flags.
    assert (
        is_legacy_handler(
            {
                "type": "command",
                "command": "python3 -u /x/brigade-work-loop.py",
            }
        )
        is True
    )
    # Similar but not matching basenames.
    assert is_legacy_handler({"type": "command", "command": "/x/brigade-work-loop.py.bak"}) is False
    assert is_legacy_handler({"type": "command", "command": "/x/my-brigade-work-loop.py"}) is False
    # Interpreter form that runs a module or inline code, not the script.
    assert is_legacy_handler({"type": "command", "command": "python3 -c 'print(1)'"}) is False
    assert is_legacy_handler({"type": "command", "command": "python3 -m brigade_work_loop"}) is False


def test_hooks_install_removes_legacy_coexisting_with_managed_and_foreign(tmp_path: Path):
    target = _wired_claude(tmp_path)
    settings = target / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    seeded = {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        _legacy_handler("SessionStart"),
                        {"type": "command", "command": "echo foreign-session"},
                        {"type": "command", "command": managed_command("SessionStart")},
                    ]
                }
            ]
        }
    }
    settings.write_text(json.dumps(seeded, indent=2) + "\n")

    assert hooks_update(target=target) == 0

    payload = json.loads(settings.read_text())
    session_cmds = [
        handler["command"]
        for group in payload["hooks"]["SessionStart"]
        for handler in group.get("hooks", [])
        if isinstance(handler, dict)
    ]
    assert "echo foreign-session" in session_cmds
    assert not any("brigade-work-loop.py" in cmd for cmd in session_cmds)
    assert managed_command("SessionStart") in session_cmds


def test_hooks_install_removes_legacy_from_multiple_managed_events(tmp_path: Path):
    target = _wired_claude(tmp_path)
    settings = target / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    seeded = {
        "hooks": {
            "SessionStart": [{"hooks": [_legacy_handler("SessionStart")]}],
            "Stop": [{"hooks": [_legacy_handler("Stop")]}],
        }
    }
    settings.write_text(json.dumps(seeded, indent=2) + "\n")

    before = status_payload(target)
    assert before["legacy_handler_count"] == 2
    assert before["legacy_events"] == ["SessionStart", "Stop"]

    assert hooks_install(target=target) == 0

    payload = json.loads(settings.read_text())
    for event in ("SessionStart", "Stop"):
        cmds = [
            handler["command"]
            for group in payload["hooks"][event]
            for handler in group.get("hooks", [])
            if isinstance(handler, dict)
        ]
        assert not any("brigade-work-loop.py" in cmd for cmd in cmds)
        assert managed_command(event) in cmds

    after = status_payload(target)
    assert after["legacy_handler_count"] == 0
    assert after["legacy_events"] == []


def test_hooks_uninstall_removes_legacy_and_preserves_foreign(tmp_path: Path):
    target = _wired_claude(tmp_path)
    settings = target / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    seeded = {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        _legacy_handler("SessionStart"),
                        {"type": "command", "command": "echo foreign-session"},
                    ]
                }
            ]
        }
    }
    settings.write_text(json.dumps(seeded, indent=2) + "\n")

    assert hooks_uninstall(target=target) == 0

    payload = json.loads(settings.read_text())
    cmds = [
        handler["command"]
        for group in payload["hooks"]["SessionStart"]
        for handler in group.get("hooks", [])
        if isinstance(handler, dict)
    ]
    assert cmds == ["echo foreign-session"]


def test_status_payload_counts_legacy_and_foreign_as_disjoint(tmp_path: Path):
    target = _wired_claude(tmp_path)
    settings = target / ".claude" / "settings.json"
    payload = json.loads(settings.read_text())
    payload["hooks"]["SessionStart"] = [
        {
            "hooks": [
                _legacy_handler("SessionStart"),
                {"type": "command", "command": "echo foreign-1"},
            ]
        }
    ]
    payload["hooks"]["Stop"] = [
        {
            "hooks": [
                _legacy_handler("Stop"),
                {"type": "command", "command": "echo foreign-2"},
            ]
        }
    ]
    settings.write_text(json.dumps(payload, indent=2) + "\n")

    status = status_payload(target)
    assert status["legacy_handler_count"] == 2
    assert status["foreign_handler_count"] == 2
    assert status["legacy_events"] == ["SessionStart", "Stop"]
