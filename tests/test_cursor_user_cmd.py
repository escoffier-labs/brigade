import json
from pathlib import Path

import pytest

from brigade import cli, cursor_user_cmd


def _use_home(monkeypatch, home: Path) -> None:
    monkeypatch.setattr(cursor_user_cmd, "_home_dir", lambda: home)


def test_cursor_user_install_dry_run_lists_all_surfaces_without_writing(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)

    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    paths = {item["path"] for item in payload["items"]}
    cursor = tmp_path / ".cursor"
    assert {
        str(cursor / "plugins" / "local" / "brigade-loop" / ".cursor-plugin" / "plugin.json"),
        str(cursor / "plugins" / "local" / "brigade-loop" / "rules" / "brigade-loop.mdc"),
        str(cursor / "skills" / "brigade-work" / "SKILL.md"),
        str(cursor / "hooks" / "brigade-session-start"),
        str(cursor / "hooks.json"),
        str(cursor / "brigade" / "mcp.json"),
        str(cursor / "mcp.json"),
    } <= paths
    assert payload["write"] is False
    assert payload["reload_required"] is True
    assert not cursor.exists()


def test_cursor_user_install_merges_all_surfaces_and_is_idempotent(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)
    cursor = tmp_path / ".cursor"
    (cursor / "plugins" / "local" / "other-plugin").mkdir(parents=True)
    (cursor / "plugins" / "local" / "other-plugin" / "keep.txt").write_text("keep\n")
    (cursor / "hooks.json").write_text(
        json.dumps({"version": 1, "hooks": {"beforeSubmitPrompt": [{"command": "keep-hook"}]}})
    )
    (cursor / "mcp.json").write_text(
        json.dumps({"settings": {"keep": True}, "mcpServers": {"foreign": {"command": "keep-server"}}})
    )

    command = ["harness", "install", "cursor", "--scope", "user", "--write", "--json"]
    assert cli.main(command) == 0
    first = json.loads(capsys.readouterr().out)

    assert first["ready"] is True
    assert (cursor / "plugins" / "local" / "other-plugin" / "keep.txt").read_text() == "keep\n"
    assert (cursor / "plugins" / "local" / "brigade-loop" / "rules" / "brigade-loop.mdc").is_file()
    assert (cursor / "skills" / "brigade-work" / "SKILL.md").is_file()
    hook = cursor / "hooks" / "brigade-session-start"
    assert hook.is_file()
    assert hook.stat().st_mode & 0o111

    hooks = json.loads((cursor / "hooks.json").read_text())
    assert hooks["hooks"]["beforeSubmitPrompt"] == [{"command": "keep-hook"}]
    assert hooks["hooks"]["sessionStart"] == [{"command": str(hook)}]

    mcp = json.loads((cursor / "mcp.json").read_text())
    assert mcp["settings"] == {"keep": True}
    assert mcp["mcpServers"]["foreign"] == {"command": "keep-server"}
    assert {"brigade", "graphtrail", "miseledger"} <= set(mcp["mcpServers"])
    assert (cursor / "brigade" / "mcp.json").is_file()
    assert not (tmp_path / ".brigade").exists()
    assert not (cursor / "memory-handoffs").exists()

    assert cli.main(command) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["files_written"] == []
    assert all(item["status"] == "current" for item in second["items"])


def test_cursor_user_doctor_checks_rule_skill_hook_and_mcp(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)
    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--write", "--json"]) == 0
    capsys.readouterr()

    assert cli.main(["harness", "doctor", "cursor", "--scope", "user", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["ready"] is True
    checks = {item["id"]: item for item in payload["checks"]}
    assert checks["rule-always-applied"]["status"] == "OK"
    assert checks["plugin-current"]["status"] == "OK"
    assert checks["skill-current"]["status"] == "OK"
    assert checks["session-hook"]["status"] == "OK"
    assert checks["mcp-brigade"]["status"] == "OK"
    assert checks["mcp-graphtrail"]["status"] == "OK"
    assert checks["mcp-miseledger"]["status"] == "OK"


def test_cursor_user_uninstall_removes_only_managed_files_and_blocks(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)
    cursor = tmp_path / ".cursor"
    (cursor / "plugins" / "local" / "other-plugin").mkdir(parents=True)
    (cursor / "plugins" / "local" / "other-plugin" / "keep.txt").write_text("keep\n")
    (cursor / "hooks.json").parent.mkdir(parents=True, exist_ok=True)
    (cursor / "hooks.json").write_text(
        json.dumps({"version": 1, "hooks": {"sessionStart": [{"command": "keep-hook"}]}})
    )
    (cursor / "mcp.json").write_text(json.dumps({"mcpServers": {"foreign": {"command": "keep-server"}}}))

    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--write", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["harness", "uninstall", "cursor", "--scope", "user", "--write", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["conflicts"] == []
    assert (cursor / "plugins" / "local" / "other-plugin" / "keep.txt").read_text() == "keep\n"
    assert not (cursor / "plugins" / "local" / "brigade-loop").exists()
    assert not (cursor / "skills" / "brigade-work").exists()
    assert not (cursor / "hooks" / "brigade-session-start").exists()
    hooks = json.loads((cursor / "hooks.json").read_text())
    assert hooks["hooks"]["sessionStart"] == [{"command": "keep-hook"}]
    mcp = json.loads((cursor / "mcp.json").read_text())
    assert mcp["mcpServers"] == {"foreign": {"command": "keep-server"}}


def test_cursor_user_uninstall_preserves_user_edited_managed_mcp_entry(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)
    cursor = tmp_path / ".cursor"
    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--write", "--json"]) == 0
    capsys.readouterr()
    mcp_path = cursor / "mcp.json"
    mcp = json.loads(mcp_path.read_text())
    mcp["mcpServers"]["graphtrail"] = {"command": "user-custom-graph"}
    mcp_path.write_text(json.dumps(mcp))

    assert cli.main(["harness", "uninstall", "cursor", "--scope", "user", "--write", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)

    assert any(item["path"] == str(mcp_path) and item["name"] == "graphtrail" for item in payload["conflicts"])
    live = json.loads(mcp_path.read_text())
    assert live["mcpServers"]["graphtrail"] == {"command": "user-custom-graph"}
    assert "brigade" not in live["mcpServers"]
    assert "miseledger" not in live["mcpServers"]


def test_cursor_user_install_preserves_managed_name_collisions(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    foreign = {"command": "foreign-brigade"}
    (cursor / "mcp.json").write_text(json.dumps({"mcpServers": {"brigade": foreign}}))

    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--write", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)

    assert any(item.get("name") == "brigade" and item["status"] == "conflict" for item in payload["items"])
    live = json.loads((cursor / "mcp.json").read_text())
    assert live["mcpServers"]["brigade"] == foreign
    assert "graphtrail" in live["mcpServers"]
    assert "miseledger" in live["mcpServers"]


def test_cursor_user_install_rejects_malformed_coowned_json(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    hooks_path = cursor / "hooks.json"
    mcp_path = cursor / "mcp.json"
    hooks_path.write_text("{bad hooks")
    mcp_path.write_text("[]")

    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--write", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)

    assert len(payload["conflicts"]) == 2
    assert hooks_path.read_text() == "{bad hooks"
    assert mcp_path.read_text() == "[]"


def test_cursor_user_install_reports_non_utf8_coowned_json_as_conflict(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)
    hooks_path = tmp_path / ".cursor" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_bytes(b"\xff\xfe")

    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--write", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)

    assert any(item["path"] == str(hooks_path) and item["status"] == "conflict" for item in payload["conflicts"])
    assert hooks_path.read_bytes() == b"\xff\xfe"


def test_cursor_user_doctor_fails_when_plugin_manifest_is_missing(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)
    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--write", "--json"]) == 0
    capsys.readouterr()
    manifest = tmp_path / ".cursor" / "plugins" / "local" / "brigade-loop" / ".cursor-plugin" / "plugin.json"
    manifest.unlink()

    assert cli.main(["harness", "doctor", "cursor", "--scope", "user", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    checks = {item["id"]: item for item in payload["checks"]}
    assert payload["ready"] is False
    assert checks["plugin-current"]["status"] == "FAIL"


def test_cursor_user_install_repairs_session_hook_executable_mode(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)
    install = ["harness", "install", "cursor", "--scope", "user", "--write", "--json"]
    assert cli.main(install) == 0
    capsys.readouterr()
    hook = tmp_path / ".cursor" / "hooks" / "brigade-session-start"
    hook.chmod(0o644)

    assert cli.main(["harness", "doctor", "cursor", "--scope", "user", "--json"]) == 1
    capsys.readouterr()
    assert cli.main(install) == 0
    payload = json.loads(capsys.readouterr().out)
    assert str(hook) in payload["files_written"]
    assert hook.stat().st_mode & 0o111
    assert cli.main(["harness", "doctor", "cursor", "--scope", "user", "--json"]) == 0


def test_cursor_user_dry_run_does_not_create_ownership_state(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)

    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--dry-run", "--json"]) == 0
    capsys.readouterr()
    assert not (tmp_path / ".cursor" / "brigade" / "install-state.json").exists()


def test_cursor_user_uninstall_does_not_remove_foreign_empty_ancestors(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)
    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--write", "--json"]) == 0
    capsys.readouterr()

    assert cli.main(["harness", "uninstall", "cursor", "--scope", "user", "--write", "--json"]) == 0
    capsys.readouterr()
    assert (tmp_path / ".cursor" / "plugins" / "local").is_dir()
    assert (tmp_path / ".cursor" / "skills").is_dir()
    assert (tmp_path / ".cursor" / "hooks").is_dir()


def test_cursor_user_preserves_non_utf8_managed_file_as_conflict(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)
    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--write", "--json"]) == 0
    capsys.readouterr()
    rule = tmp_path / ".cursor" / "plugins" / "local" / "brigade-loop" / "rules" / "brigade-loop.mdc"
    rule.write_bytes(b"\xff\xfe")

    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--dry-run", "--json"]) == 1
    install_payload = json.loads(capsys.readouterr().out)
    assert any(item["path"] == str(rule) and item["status"] == "conflict" for item in install_payload["conflicts"])

    assert cli.main(["harness", "uninstall", "cursor", "--scope", "user", "--write", "--json"]) == 1
    uninstall_payload = json.loads(capsys.readouterr().out)
    assert any(item["path"] == str(rule) and item["status"] == "conflict" for item in uninstall_payload["conflicts"])
    assert rule.read_bytes() == b"\xff\xfe"


def test_cursor_user_corrupt_ownership_state_blocks_install_and_uninstall(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)
    command = ["harness", "install", "cursor", "--scope", "user", "--write", "--json"]
    assert cli.main(command) == 0
    capsys.readouterr()
    cursor = tmp_path / ".cursor"
    state = cursor / "brigade" / "install-state.json"
    rule = cursor / "plugins" / "local" / "brigade-loop" / "rules" / "brigade-loop.mdc"
    state.write_bytes(b"\xff\xfe")

    assert cli.main(command) == 1
    install_payload = json.loads(capsys.readouterr().out)
    assert install_payload["files_written"] == []
    assert any(item["path"] == str(state) and item["status"] == "conflict" for item in install_payload["conflicts"])
    assert state.read_bytes() == b"\xff\xfe"

    assert cli.main(["harness", "uninstall", "cursor", "--scope", "user", "--write", "--json"]) == 1
    uninstall_payload = json.loads(capsys.readouterr().out)
    assert any(item["path"] == str(state) and item["status"] == "conflict" for item in uninstall_payload["conflicts"])
    assert state.read_bytes() == b"\xff\xfe"
    assert rule.is_file()


def test_cursor_user_unreadable_mcp_config_preserves_ownership_for_later_uninstall(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)
    install = ["harness", "install", "cursor", "--scope", "user", "--write", "--json"]
    assert cli.main(install) == 0
    capsys.readouterr()
    cursor = tmp_path / ".cursor"
    mcp_path = cursor / "mcp.json"
    state_path = cursor / "brigade" / "install-state.json"
    valid_mcp = mcp_path.read_bytes()
    mcp_path.write_bytes(b"\xff\xfe")

    assert cli.main(install) == 1
    capsys.readouterr()
    state = json.loads(state_path.read_text())
    assert set(state["mcp"]) == {"brigade", "graphtrail", "miseledger"}

    mcp_path.write_bytes(valid_mcp)
    assert cli.main(["harness", "uninstall", "cursor", "--scope", "user", "--write", "--json"]) == 0
    capsys.readouterr()
    live = json.loads(mcp_path.read_text())
    assert not {"brigade", "graphtrail", "miseledger"} & set(live["mcpServers"])


def test_cursor_user_uninstall_reload_requires_an_actual_change(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)
    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--write", "--json"]) == 0
    capsys.readouterr()
    cursor = tmp_path / ".cursor"
    state_path = cursor / "brigade" / "install-state.json"
    state = json.loads(state_path.read_text())
    state["files"] = {}
    state["mcp"] = {}
    state_path.write_text(json.dumps(state))
    hooks_path = cursor / "hooks.json"
    hooks = json.loads(hooks_path.read_text())
    hooks["hooks"].pop("sessionStart")
    hooks_path.write_text(json.dumps(hooks))

    assert cli.main(["harness", "uninstall", "cursor", "--scope", "user", "--write", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["reload_required"] is False


@pytest.mark.parametrize("missing_shape", ["file", "mcpServers-key"])
def test_cursor_user_uninstall_treats_missing_mcp_config_as_already_removed(
    tmp_path, monkeypatch, capsys, missing_shape
):
    _use_home(monkeypatch, tmp_path)
    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--write", "--json"]) == 0
    capsys.readouterr()
    cursor = tmp_path / ".cursor"
    mcp_path = cursor / "mcp.json"
    if missing_shape == "file":
        mcp_path.unlink()
    else:
        mcp_path.write_text(json.dumps({"settings": {"keep": True}}))

    assert cli.main(["harness", "uninstall", "cursor", "--scope", "user", "--write", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    mcp_items = [item for item in payload["items"] if item["surface"] == "mcp-config"]
    assert all(item["status"] == "absent" for item in mcp_items)
    assert not (cursor / "brigade" / "install-state.json").exists()
