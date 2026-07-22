import json
from pathlib import Path

import pytest

from brigade import cli, cursor_user_cmd, harness_profiles


def _use_home(monkeypatch, home: Path) -> None:
    # The aggregate orchestration resolves homes through ``Path.home()``; patching
    # it (instead of ``cursor_user_cmd._home_dir``) keeps both the aggregate and
    # the legacy cursor helpers pointed at the temp home.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


def _row(payload):
    return payload["results"][0]


def test_cursor_user_install_dry_run_lists_all_surfaces_without_writing(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)

    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    paths = {item["path"] for item in _row(payload)["items"]}
    cursor = tmp_path / ".cursor"
    assert {
        str(cursor / "plugins" / "local" / "brigade-loop" / ".cursor-plugin" / "plugin.json"),
        str(cursor / "plugins" / "local" / "brigade-loop" / "rules" / "brigade-loop.mdc"),
        str(cursor / "hooks" / "brigade-session-start"),
        str(cursor / "hooks.json"),
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

    assert _row(first)["ready"] is True
    assert (cursor / "plugins" / "local" / "other-plugin" / "keep.txt").read_text() == "keep\n"
    assert (cursor / "plugins" / "local" / "brigade-loop" / "rules" / "brigade-loop.mdc").is_file()
    hook = cursor / "hooks" / "brigade-session-start"
    assert hook.is_file()
    assert hook.stat().st_mode & 0o111

    hooks = json.loads((cursor / "hooks.json").read_text())
    assert hooks["hooks"]["beforeSubmitPrompt"] == [{"command": "keep-hook"}]
    assert hooks["hooks"]["sessionStart"] == [{"command": str(hook)}]

    # MCP stage is pending: foreign mcp.json is preserved untouched
    mcp = json.loads((cursor / "mcp.json").read_text())
    assert mcp["settings"] == {"keep": True}
    assert mcp["mcpServers"]["foreign"] == {"command": "keep-server"}
    assert not (tmp_path / ".brigade").exists()
    assert not (cursor / "memory-handoffs").exists()

    assert cli.main(command) == 0
    second = json.loads(capsys.readouterr().out)
    assert _row(second)["files_written"] == []
    assert all(item["status"] == "current" for item in _row(second)["items"])


def test_cursor_user_doctor_checks_rule_skill_hook_and_mcp(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)
    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--write", "--json"]) == 0
    capsys.readouterr()

    assert cli.main(["harness", "doctor", "cursor", "--scope", "user", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert _row(payload)["ready"] is True
    checks = {item["id"]: item for item in _row(payload)["checks"]}
    assert checks["plugin-current"]["status"] == "OK"
    assert checks["rule-current"]["status"] == "OK"
    assert checks["hook-current"]["status"] == "OK"
    assert checks["session-hook"]["status"] == "OK"
    assert checks["skills-current"]["status"] == "OK"
    # MCP verification is pending; no mcp-* checks are emitted without --verify-mcp
    assert not any(k.startswith("mcp-") for k in checks)


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

    assert _row(payload)["conflicts"] == []
    assert (cursor / "plugins" / "local" / "other-plugin" / "keep.txt").read_text() == "keep\n"
    assert not (cursor / "plugins" / "local" / "brigade-loop").exists()
    assert not (cursor / "hooks" / "brigade-session-start").exists()
    hooks = json.loads((cursor / "hooks.json").read_text())
    assert hooks["hooks"]["sessionStart"] == [{"command": "keep-hook"}]
    # MCP pending: foreign mcp.json untouched
    mcp = json.loads((cursor / "mcp.json").read_text())
    assert mcp["mcpServers"] == {"foreign": {"command": "keep-server"}}


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

    # Only hooks.json is a co-owned generated surface; mcp.json is the pending MCP stage
    assert len(_row(payload)["conflicts"]) == 1
    assert hooks_path.read_text() == "{bad hooks"
    assert mcp_path.read_text() == "[]"


def test_cursor_user_install_reports_non_utf8_coowned_json_as_conflict(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)
    hooks_path = tmp_path / ".cursor" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_bytes(b"\xff\xfe")

    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--write", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)

    assert any(item["path"] == str(hooks_path) and item["status"] == "conflict" for item in _row(payload)["conflicts"])
    assert hooks_path.read_bytes() == b"\xff\xfe"


def test_cursor_user_doctor_fails_when_plugin_manifest_is_missing(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)
    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--write", "--json"]) == 0
    capsys.readouterr()
    manifest = tmp_path / ".cursor" / "plugins" / "local" / "brigade-loop" / ".cursor-plugin" / "plugin.json"
    manifest.unlink()

    assert cli.main(["harness", "doctor", "cursor", "--scope", "user", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    checks = {item["id"]: item for item in _row(payload)["checks"]}
    assert _row(payload)["ready"] is False
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
    assert str(hook) in _row(payload)["files_written"]
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
    # foreign-occupied plugins/local survives (other-plugin is foreign); the shared
    # hooks dir is not pruned so foreign hooks may keep it alive
    assert (tmp_path / ".cursor" / "plugins" / "local").is_dir()


def test_cursor_user_preserves_non_utf8_managed_file_as_conflict(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)
    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--write", "--json"]) == 0
    capsys.readouterr()
    rule = tmp_path / ".cursor" / "plugins" / "local" / "brigade-loop" / "rules" / "brigade-loop.mdc"
    rule.write_bytes(b"\xff\xfe")

    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--dry-run", "--json"]) == 1
    install_payload = json.loads(capsys.readouterr().out)
    assert any(
        item["path"] == str(rule) and item["status"] == "conflict" for item in _row(install_payload)["conflicts"]
    )

    assert cli.main(["harness", "uninstall", "cursor", "--scope", "user", "--write", "--json"]) == 1
    uninstall_payload = json.loads(capsys.readouterr().out)
    assert any(
        item["path"] == str(rule) and item["status"] == "conflict" for item in _row(uninstall_payload)["conflicts"]
    )
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
    assert _row(install_payload)["files_written"] == []
    assert any(
        item["path"] == str(state) and item["status"] == "conflict" for item in _row(install_payload)["conflicts"]
    )
    assert state.read_bytes() == b"\xff\xfe"

    assert cli.main(["harness", "uninstall", "cursor", "--scope", "user", "--write", "--json"]) == 1
    uninstall_payload = json.loads(capsys.readouterr().out)
    assert any(
        item["path"] == str(state) and item["status"] == "conflict" for item in _row(uninstall_payload)["conflicts"]
    )
    assert state.read_bytes() == b"\xff\xfe"
    assert rule.is_file()


def test_cursor_user_uninstall_reload_requires_an_actual_change(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)
    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--write", "--json"]) == 0
    capsys.readouterr()
    cursor = tmp_path / ".cursor"
    state_path = cursor / "brigade" / "install-state.json"
    state = json.loads(state_path.read_text())
    state["generated"]["files"] = {}
    # keep generated.hooks ownership so the missing sessionStart is a conflict
    state_path.write_text(json.dumps(state))
    hooks_path = cursor / "hooks.json"
    hooks = json.loads(hooks_path.read_text())
    hooks["hooks"].pop("sessionStart")
    hooks_path.write_text(json.dumps(hooks))

    assert cli.main(["harness", "uninstall", "cursor", "--scope", "user", "--write", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["reload_required"] is False


<<<<<<< HEAD
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
=======
# --- Issue #438 Task 6: Cursor v1 migration, generated-file adapter, adoption ---


def test_cursor_generated_files_returns_only_plugin_rule_and_hook(tmp_path, monkeypatch):
    _use_home(monkeypatch, tmp_path)
    root = cursor_user_cmd._cursor_root()

    generated = cursor_user_cmd.cursor_generated_files(root)

    surfaces = {record[2] for record in generated.values()}
    assert surfaces == {"plugin", "rule", "hook"}
    expected_paths = {
        str(root / "plugins" / "local" / "brigade-loop" / ".cursor-plugin" / "plugin.json"),
        str(root / "plugins" / "local" / "brigade-loop" / "rules" / "brigade-loop.mdc"),
        str(root / "hooks" / "brigade-session-start"),
    }
    assert {str(path) for path in generated} == expected_paths
    # legacy bundled skill copy is not co-owned by the generated adapter
    assert not any("skills" in path.parts and "brigade-work" in path.parts for path in generated)
    # the brigade-internal mcp catalog is not a generated surface
    assert not any("mcp.json" in path.name for path in generated)
    # co-owned config (hooks.json / ~/.cursor/mcp.json) is not generated
    assert not any(path.name == "hooks.json" for path in generated)
    # the hook retains its executable metadata
    hook_path = root / "hooks" / "brigade-session-start"
    assert generated[hook_path][1] is True


def test_cursor_rule_and_hook_text_include_work_loop_contract():
    for text in (cursor_user_cmd._rule_text(), cursor_user_cmd._hook_text()):
        assert "brigade run" in text
        assert "Memory Handoff" in text
        assert "standard Rocinante flow" in text
        assert "Never edit canonical memory directly" in text
        assert "brigade work brief" in text
        assert "brigade work verify run" in text
    # the rule keeps its always-applied frontmatter so legacy doctor still passes
    assert "alwaysApply: true" in cursor_user_cmd._rule_text()


def _seed_cursor_v1(home: Path) -> Path:
    root = home / ".cursor"
    for path, (text, executable, _surface) in cursor_user_cmd._desired_files(root).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if executable:
            path.chmod(0o755)
    mcp_path = root / "mcp.json"
    mcp_path.write_text(
        json.dumps(
            {"mcpServers": {**cursor_user_cmd._mcp_servers(), "foreign": {"command": "keep"}}},
            indent=2,
        )
        + "\n"
    )
    desired = cursor_user_cmd._desired_files(root)
    state = {
        "version": cursor_user_cmd.STATE_VERSION,
        "files": {
            cursor_user_cmd._relative(root, path): cursor_user_cmd._digest_text(text)
            for path, (text, _exec, _surface) in desired.items()
        },
        "mcp": {name: cursor_user_cmd._digest_value(value) for name, value in cursor_user_cmd._mcp_servers().items()},
        "hooks": {},
    }
    state_path = root / "brigade" / "install-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2) + "\n")
    return root


def test_cursor_migrate_v1_state_moves_ownership_and_names_retirable_mcp(tmp_path, monkeypatch):
    from brigade import __version__
    from brigade import harness_profiles

    _use_home(monkeypatch, tmp_path)
    root = _seed_cursor_v1(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_path = root / "brigade" / "install-state.json"
    legacy_state = json.loads(state_path.read_text())

    migration = cursor_user_cmd.migrate_v1_state(root=root, workspace=workspace, state=legacy_state)

    assert migration.error is None
    migrated = migration.state
    assert migrated["schema_version"] == harness_profiles.PROFILE_STATE_VERSION
    assert migrated["package_version"] == __version__
    assert migrated["harness"] == "cursor"
    assert migrated["workspace"] == str(workspace.resolve())
    assert migrated["instructions"] == {}
    assert migrated["skills"] == {}
    assert migrated["mcp"] == {}

    generated = migrated["generated"]
    assert set(generated) == {"files", "hooks", "created_directories"}
    # generated ownership contains only cursor_generated_files surfaces (plugin/rule/hook)
    expected_generated_rels = {
        cursor_user_cmd._relative(root, path) for path in cursor_user_cmd.cursor_generated_files(root)
    }
    assert set(generated["files"]) == expected_generated_rels
    assert generated["hooks"] == legacy_state["hooks"]
    assert generated["created_directories"] == []

    # legacy skill and mcp-catalog surfaces are returned for retirement, not owned
    desired = cursor_user_cmd._desired_files(root)
    expected_retire = sorted(
        path for path, (_text, _exec, surface) in desired.items() if surface in {"skill", "mcp-catalog"}
    )
    assert migration.retire_file_paths == tuple(expected_retire)
    assert all(path.is_absolute() for path in migration.retire_file_paths)
    # no plugin/rule/hook path leaks into retirement
    assert not any(path in cursor_user_cmd.cursor_generated_files(root) for path in migration.retire_file_paths)

    assert migration.retire_mcp_names == tuple(sorted(cursor_user_cmd._mcp_servers()))
    assert "foreign" not in migration.retire_mcp_names

    # migration is pure: no state or config writes
    assert json.loads(state_path.read_text()) == legacy_state
    live = json.loads((root / "mcp.json").read_text())
    assert "foreign" in live["mcpServers"]
    assert set(live["mcpServers"]) == {"brigade", "graphtrail", "miseledger", "foreign"}


def test_cursor_migrate_v1_state_rejects_malformed_sections_and_bad_mcp(tmp_path, monkeypatch):
    _use_home(monkeypatch, tmp_path)
    root = tmp_path / ".cursor"
    root.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_path = root / "mcp.json"
    mcp_path.write_text(json.dumps({"mcpServers": {**cursor_user_cmd._mcp_servers(), "foreign": {"command": "keep"}}}))

    base_files = {
        cursor_user_cmd._relative(root, path): cursor_user_cmd._digest_text(text)
        for path, (text, _e, _s) in cursor_user_cmd._desired_files(root).items()
    }
    base_mcp = {name: cursor_user_cmd._digest_value(value) for name, value in cursor_user_cmd._mcp_servers().items()}

    def migrate(state):
        return cursor_user_cmd.migrate_v1_state(root=root, workspace=workspace, state=state)

    # not version 1
    assert migrate({"version": 2, "files": {}, "hooks": {}, "mcp": {}}).error is not None
    # schema_version present (already v2-shaped)
    assert migrate({"version": 1, "schema_version": 2, "files": {}, "hooks": {}, "mcp": {}}).error is not None
    # files not an object
    assert migrate({"version": 1, "files": [], "hooks": {}, "mcp": {}}).error is not None
    # hooks not an object
    assert migrate({"version": 1, "files": {}, "hooks": [], "mcp": {}}).error is not None
    # mcp not an object
    assert migrate({"version": 1, "files": {}, "hooks": {}, "mcp": []}).error is not None
    # file digest not a string
    bad_files = dict(base_files)
    bad_files[next(iter(bad_files))] = 123
    assert migrate({"version": 1, "files": bad_files, "hooks": {}, "mcp": {}}).error is not None
    # mcp entry digest not a string
    bad_mcp = dict(base_mcp)
    bad_mcp[next(iter(bad_mcp))] = 123
    assert migrate({"version": 1, "files": base_files, "hooks": {}, "mcp": bad_mcp}).error is not None

    # unreadable mcp config
    mcp_path.write_bytes(b"\xff\xfe")
    result = migrate({"version": 1, "files": base_files, "hooks": {}, "mcp": base_mcp})
    assert result.error is not None
    assert result.state == {}
    assert result.retire_mcp_names == ()
    assert mcp_path.read_bytes() == b"\xff\xfe"

    # non-object mcp config
    mcp_path.write_text("[]")
    result = migrate({"version": 1, "files": base_files, "hooks": {}, "mcp": base_mcp})
    assert result.error is not None
    assert mcp_path.read_text() == "[]"

    # missing managed mcp entry (live config lost a managed name)
    mcp_path.write_text(json.dumps({"mcpServers": {"foreign": {"command": "keep"}}}))
    result = migrate({"version": 1, "files": base_files, "hooks": {}, "mcp": base_mcp})
    assert result.error is not None

    # edited managed mcp entry (live value no longer digest-matches the record)
    edited = {**cursor_user_cmd._mcp_servers(), "foreign": {"command": "keep"}}
    edited["brigade"] = {"command": "user-rewrote-brigade"}
    mcp_path.write_text(json.dumps({"mcpServers": edited}))
    result = migrate({"version": 1, "files": base_files, "hooks": {}, "mcp": base_mcp})
    assert result.error is not None
    assert "foreign" in json.loads(mcp_path.read_text())["mcpServers"]


def test_cursor_load_cursor_profile_state_passes_valid_v2_through_unchanged(tmp_path, monkeypatch):
    from brigade import __version__
    from brigade import harness_profile_cmd, harness_profiles

    _use_home(monkeypatch, tmp_path)
    root = tmp_path / ".cursor"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_path = root / "brigade" / "install-state.json"
    state_path.parent.mkdir(parents=True)
    v2 = harness_profile_cmd.empty_profile_state(workspace=workspace, harness="cursor")
    v2["generated"] = {"files": {"a": "d"}, "hooks": {}, "created_directories": []}
    state_path.write_text(json.dumps(v2))

    loaded = harness_profile_cmd.load_cursor_profile_state(state_path=state_path, workspace=workspace, root=root)

    assert loaded.error is None
    assert loaded.migration is None
    assert loaded.retire_mcp_names == ()
    assert loaded.state["schema_version"] == harness_profiles.PROFILE_STATE_VERSION
    assert loaded.state["harness"] == "cursor"
    assert loaded.state["package_version"] == __version__
    assert loaded.state["generated"]["files"] == {"a": "d"}
    # no migration writes occurred
    assert json.loads(state_path.read_text())["generated"]["files"] == {"a": "d"}


def test_cursor_load_cursor_profile_state_migrates_legacy_v1(tmp_path, monkeypatch):
    from brigade import harness_profile_cmd

    _use_home(monkeypatch, tmp_path)
    root = _seed_cursor_v1(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_path = root / "brigade" / "install-state.json"
    before = state_path.read_text()

    loaded = harness_profile_cmd.load_cursor_profile_state(state_path=state_path, workspace=workspace, root=root)

    assert loaded.error is None
    assert loaded.migration == "cursor-state-v1"
    assert loaded.retire_mcp_names == tuple(sorted(cursor_user_cmd._mcp_servers()))
    assert loaded.state["schema_version"] == 2
    assert loaded.state["harness"] == "cursor"
    assert loaded.state["workspace"] == str(workspace.resolve())
    assert loaded.state["skills"] == {}
    assert loaded.state["mcp"] == {}
    # loader must not persist migrated state or edit mcp config (Task 7 owns the transaction)
    assert state_path.read_text() == before
    assert "foreign" in json.loads((root / "mcp.json").read_text())["mcpServers"]


def test_cursor_load_cursor_profile_state_propagates_retire_file_paths(tmp_path, monkeypatch):
    from brigade import harness_profile_cmd

    _use_home(monkeypatch, tmp_path)
    root = _seed_cursor_v1(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_path = root / "brigade" / "install-state.json"

    loaded = harness_profile_cmd.load_cursor_profile_state(state_path=state_path, workspace=workspace, root=root)

    assert loaded.error is None
    assert loaded.migration == "cursor-state-v1"
    desired = cursor_user_cmd._desired_files(root)
    expected_retire = sorted(
        path for path, (_text, _exec, surface) in desired.items() if surface in {"skill", "mcp-catalog"}
    )
    assert loaded.retire_file_paths == tuple(expected_retire)


@pytest.mark.parametrize("tamper", ["edit", "delete", "unreadable"])
def test_cursor_migrate_v1_state_conflicts_when_retirement_candidate_tampered(tmp_path, monkeypatch, tamper):
    _use_home(monkeypatch, tmp_path)
    root = _seed_cursor_v1(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_path = root / "brigade" / "install-state.json"
    legacy_state = json.loads(state_path.read_text())

    desired = cursor_user_cmd._desired_files(root)
    retire_target = next(path for path, (_text, _exec, surface) in desired.items() if surface == "mcp-catalog")
    if tamper == "edit":
        retire_target.write_text("edited live content\n")
    elif tamper == "delete":
        retire_target.unlink()
    elif tamper == "unreadable":
        retire_target.write_bytes(b"\xff\xfe")

    migration = cursor_user_cmd.migrate_v1_state(root=root, workspace=workspace, state=legacy_state)

    assert migration.error is not None
    assert migration.state == {}
    assert migration.retire_mcp_names == ()
    assert migration.retire_file_paths == ()
    # migration is read-only: v1 state and live files are unchanged
    assert json.loads(state_path.read_text()) == legacy_state


def test_cursor_migrate_v1_state_conflicts_on_unexpected_v1_file_key(tmp_path, monkeypatch):
    _use_home(monkeypatch, tmp_path)
    root = _seed_cursor_v1(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_path = root / "brigade" / "install-state.json"
    legacy_state = json.loads(state_path.read_text())
    legacy_state["files"]["surprise/extra.txt"] = cursor_user_cmd._digest_text("surprise\n")
    (root / "surprise").mkdir(exist_ok=True)
    (root / "surprise" / "extra.txt").write_text("surprise\n")

    migration = cursor_user_cmd.migrate_v1_state(root=root, workspace=workspace, state=legacy_state)

    assert migration.error is not None
    assert migration.state == {}
    assert migration.retire_file_paths == ()


def test_cursor_migrate_v1_state_conflicts_when_expected_v1_file_key_missing(tmp_path, monkeypatch):
    _use_home(monkeypatch, tmp_path)
    root = _seed_cursor_v1(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_path = root / "brigade" / "install-state.json"
    on_disk = state_path.read_text()
    legacy_state = json.loads(on_disk)

    # drop one expected _desired_files key from the recorded v1 files so the
    # recorded shape is no longer the exact current _desired_files shape
    desired = cursor_user_cmd._desired_files(root)
    skill_rel = next(
        cursor_user_cmd._relative(root, path) for path, (_text, _exec, surface) in desired.items() if surface == "skill"
    )
    del legacy_state["files"][skill_rel]

    migration = cursor_user_cmd.migrate_v1_state(root=root, workspace=workspace, state=legacy_state)

    assert migration.error is not None
    assert migration.state == {}
    assert migration.retire_mcp_names == ()
    assert migration.retire_file_paths == ()
    # migration is read-only: the on-disk v1 state is unchanged
    assert state_path.read_text() == on_disk


def test_cursor_migrate_v1_state_conflicts_when_retirement_candidate_leaf_is_symlink(tmp_path, monkeypatch):
    _use_home(monkeypatch, tmp_path)
    root = _seed_cursor_v1(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_path = root / "brigade" / "install-state.json"
    on_disk = state_path.read_text()
    legacy_state = json.loads(on_disk)
    coowned_mcp_before = (root / "mcp.json").read_text()

    desired = cursor_user_cmd._desired_files(root)
    retire_target = next(path for path, (_text, _exec, surface) in desired.items() if surface == "mcp-catalog")
    # replace the retirement-candidate leaf with an in-root symlink to a regular
    # file holding the same bytes, so the stored digest still matches the target
    original_text = retire_target.read_text()
    retire_target.unlink()
    symlink_target = root / "symlink-target.txt"
    symlink_target.write_text(original_text)
    retire_target.symlink_to(symlink_target)

    migration = cursor_user_cmd.migrate_v1_state(root=root, workspace=workspace, state=legacy_state)

    assert migration.error is not None
    assert migration.state == {}
    assert migration.retire_mcp_names == ()
    assert migration.retire_file_paths == ()
    # nothing was modified: the symlink and its target survive, co-owned mcp intact
    assert retire_target.is_symlink()
    assert symlink_target.read_text() == original_text
    assert (root / "mcp.json").read_text() == coowned_mcp_before
    assert state_path.read_text() == on_disk


def test_cursor_migrate_v1_state_conflicts_when_intermediate_component_is_symlink(tmp_path, monkeypatch):
    _use_home(monkeypatch, tmp_path)
    root = _seed_cursor_v1(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_path = root / "brigade" / "install-state.json"
    on_disk = state_path.read_text()
    legacy_state = json.loads(on_disk)
    coowned_mcp_before = (root / "mcp.json").read_text()

    # make an intermediate component of a skill retirement candidate a symlink
    # to an in-root directory holding the same bytes, so digests still match
    bw = root / "skills" / "brigade-work"
    originals = {name: (bw / name).read_text() for name in ("SKILL.md", "skill.json", "CHANGELOG.md")}
    bw_real = root / "brigade-work-real"
    bw_real.mkdir()
    for name, text in originals.items():
        (bw_real / name).write_text(text)
    for name in originals:
        (bw / name).unlink()
    bw.rmdir()
    bw.symlink_to(bw_real)

    migration = cursor_user_cmd.migrate_v1_state(root=root, workspace=workspace, state=legacy_state)

    assert migration.error is not None
    assert migration.state == {}
    assert migration.retire_mcp_names == ()
    assert migration.retire_file_paths == ()
    # nothing was modified: the intermediate symlink and its in-root target survive
    assert bw.is_symlink()
    for name, text in originals.items():
        assert (bw_real / name).read_text() == text
    assert (root / "mcp.json").read_text() == coowned_mcp_before
    assert state_path.read_text() == on_disk


def test_cursor_migrate_v1_state_conflicts_on_path_escape_file_key(tmp_path, monkeypatch):
    _use_home(monkeypatch, tmp_path)
    root = _seed_cursor_v1(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_path = root / "brigade" / "install-state.json"
    legacy_state = json.loads(state_path.read_text())
    legacy_state["files"]["../escape.txt"] = cursor_user_cmd._digest_text("escape\n")
    (root.parent / "escape.txt").write_text("escape\n")

    migration = cursor_user_cmd.migrate_v1_state(root=root, workspace=workspace, state=legacy_state)

    assert migration.error is not None
    assert migration.state == {}
    assert migration.retire_file_paths == ()


# --- Issue #438 Task 7: Cursor v1 migration via the aggregate CLI ---


def _seed_brigade_work_registry(workspace: Path) -> Path:
    """Seed a workspace-reviewed brigade-work skill into the workspace registry.

    The bundled template is copied verbatim (so its rendered SKILL.md matches the
    legacy v1 copy byte-for-byte for cursor) and the metadata is augmented with
    ``trust_level=workspace`` and full harness support so the shared profile
    skill planner selects it for cursor.
    """
    import shutil

    from brigade import skills_cmd

    template = Path(cursor_user_cmd.__file__).parent / "templates" / "skills" / "brigade-work"
    source = workspace / "sources" / "brigade-work"
    source.parent.mkdir(parents=True)
    shutil.copytree(template, source)
    (source / "skill.json").write_text(
        json.dumps(
            {
                "id": "brigade-work",
                "title": "brigade-work",
                "version": "0.1.0",
                "description": "Route work through Brigade so verification, outcomes, evidence export, and handoffs are captured.",
                "required_tools": [],
                "required_mcp_servers": [],
                "supported_harnesses": list(harness_profiles.HARNESS_IDS),
                "trust_level": "workspace",
                "enabled": True,
                "tests": [],
            }
        )
    )
    assert skills_cmd.import_skill(target=workspace, source=source, json_output=True) == 0
    return source


def test_cursor_user_v1_state_migrates_and_retires_legacy_mcp_entries(tmp_path, monkeypatch, capsys):
    from brigade import cli

    _use_home(monkeypatch, tmp_path)
    root = _seed_cursor_v1(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # workspace-reviewed brigade-work registry package becomes the sole owner of
    # the skill surface after migration retires the legacy bundled copy
    _seed_brigade_work_registry(workspace)
    capsys.readouterr()  # drop import_skill JSON so the install payload parses cleanly

    assert (
        cli.main(
            [
                "harness",
                "install",
                "cursor",
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
    payload = json.loads(capsys.readouterr().out)
    result = payload["results"][0]

    migrated = json.loads((root / "brigade" / "install-state.json").read_text())
    assert migrated["schema_version"] == 2
    assert "version" not in migrated
    assert migrated["harness"] == "cursor"
    assert migrated["workspace"] == str(workspace.resolve())
    assert result["migration"] == "cursor-state-v1"

    servers = json.loads((root / "mcp.json").read_text())["mcpServers"]
    assert set(servers) == {"foreign"}  # legacy Brigade entries retired
    assert (root / "plugins" / "local" / "brigade-loop").is_dir()
    assert (root / "hooks" / "brigade-session-start").stat().st_mode & 0o111

    # the registry is the sole owner of the brigade-work skill surface: the
    # package files exist on disk and the v2 state owns them
    skill_dir = root / "skills" / "brigade-work"
    assert (skill_dir / "SKILL.md").is_file()
    assert (skill_dir / "skill.json").is_file()
    assert (skill_dir / "CHANGELOG.md").is_file()
    owned_skill_files = migrated["skills"]["brigade-work"]["files"]
    assert set(owned_skill_files) == {"SKILL.md", "skill.json", "CHANGELOG.md"}
    # generated ownership contains only plugin/rule/hook (no skill, no mcp-catalog)
    assert set(migrated["generated"]["files"]) == {
        cursor_user_cmd._relative(root, path) for path in cursor_user_cmd.cursor_generated_files(root)
    }
    # legacy brigade/mcp.json catalog is retired
    assert not (root / "brigade" / "mcp.json").exists()
    # retired legacy paths are reported in files_removed, never in files_written
    retired = {str(root / "brigade" / "mcp.json")}
    retired |= {str(p) for p in migrated["skills"]["brigade-work"]["files"]}  # not in files_written
    assert retired.isdisjoint(result["files_written"])
    assert str(root / "brigade" / "mcp.json") in result["files_removed"]
    # the retired legacy skill leaf paths are reported as removed
    for name in ("SKILL.md", "skill.json", "CHANGELOG.md"):
        assert str(root / "skills" / "brigade-work" / name) in result["files_removed"]


def test_cursor_user_v1_migration_conflict_makes_no_changes(tmp_path, monkeypatch, capsys):
    from brigade import cli

    _use_home(monkeypatch, tmp_path)
    root = _seed_cursor_v1(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_path = root / "brigade" / "install-state.json"
    mcp_path = root / "mcp.json"
    legacy_state_before = state_path.read_text()
    mcp_before = mcp_path.read_text()
    legacy_skill = root / "skills" / "brigade-work" / "SKILL.md"
    assert legacy_skill.is_file()

    # tamper with a retirement candidate so migration conflicts
    legacy_skill.write_text("edited\n")

    assert (
        cli.main(
            [
                "harness",
                "install",
                "cursor",
                "--scope",
                "user",
                "--workspace",
                str(workspace),
                "--write",
                "--json",
            ]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    row = payload["results"][0]
    assert row["status"] == "conflict"
    # zero writes: state, mcp, and legacy files are unchanged
    assert state_path.read_text() == legacy_state_before
    assert mcp_path.read_text() == mcp_before
    assert legacy_skill.read_text() == "edited\n"


def test_cursor_user_install_then_uninstall_removes_owned_surfaces_and_state_and_is_idempotent(
    tmp_path, monkeypatch, capsys
):
    from brigade import cli

    _use_home(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _seed_brigade_work_registry(workspace)
    cursor = tmp_path / ".cursor"

    install = ["harness", "install", "cursor", "--scope", "user", "--workspace", str(workspace), "--write", "--json"]
    uninstall = [
        "harness",
        "uninstall",
        "cursor",
        "--scope",
        "user",
        "--workspace",
        str(workspace),
        "--write",
        "--json",
    ]

    assert cli.main(install) == 0
    capsys.readouterr()
    state_path = cursor / "brigade" / "install-state.json"
    assert state_path.is_file()
    assert (cursor / "skills" / "brigade-work" / "SKILL.md").is_file()
    assert (cursor / "plugins" / "local" / "brigade-loop" / "rules" / "brigade-loop.mdc").is_file()
    assert (cursor / "hooks" / "brigade-session-start").is_file()

    assert cli.main(uninstall) == 0
    payload = json.loads(capsys.readouterr().out)
    row = payload["results"][0]
    assert row["conflicts"] == []
    assert row["ready"] is True
    # owned skill and generated files are removed
    assert not (cursor / "skills" / "brigade-work").exists()
    assert not (cursor / "plugins" / "local" / "brigade-loop").exists()
    assert not (cursor / "hooks" / "brigade-session-start").exists()
    # state file is removed once every owned section is empty
    assert not state_path.exists()

    # a second uninstall is idempotent: nothing to remove, no conflict, rc 0
    assert cli.main(uninstall) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["results"][0]["files_removed"] == []
    assert again["results"][0]["conflicts"] == []
    assert again["results"][0]["ready"] is True
    assert again["reload_required"] is False
<<<<<<< HEAD
>>>>>>> 7d7fab5 (feat(harness): add aggregate user profile CLI)
=======


# --- Issue #438 Task 8: Cursor --adopt and migration TOCTOU revalidation ---


def test_cursor_generated_foreign_file_conflicts_without_adopt(tmp_path, monkeypatch, capsys):
    from brigade import cli

    _use_home(monkeypatch, tmp_path)
    cursor = tmp_path / ".cursor"
    rule = cursor / "plugins" / "local" / "brigade-loop" / "rules" / "brigade-loop.mdc"
    rule.parent.mkdir(parents=True)
    rule.write_text("# someone else's rule\n")

    # without --adopt: foreign generated file is a conflict, no write
    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--write", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert any(item["path"] == str(rule) and item["status"] == "conflict" for item in _row(payload)["conflicts"])
    assert rule.read_text() == "# someone else's rule\n"


def test_cursor_generated_adopt_claims_file_preserves_unrelated_and_uninstall_removes_if_unchanged(
    tmp_path, monkeypatch, capsys
):
    from brigade import cli

    _use_home(monkeypatch, tmp_path)
    cursor = tmp_path / ".cursor"
    rule = cursor / "plugins" / "local" / "brigade-loop" / "rules" / "brigade-loop.mdc"
    rule.parent.mkdir(parents=True)
    rule.write_text("# someone else's rule\n")
    # unrelated hooks/config must survive
    (cursor / "hooks.json").write_text(
        json.dumps({"version": 1, "hooks": {"beforeSubmitPrompt": [{"command": "keep-hook"}]}})
    )
    (cursor / "mcp.json").write_text(json.dumps({"mcpServers": {"foreign": {"command": "keep-server"}}}))

    # --adopt --write replaces/claims the foreign generated file
    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--adopt", "--write", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert _row(payload)["ready"] is True
    assert str(rule) in _row(payload)["files_written"]
    assert rule.read_text() == cursor_user_cmd._rule_text()
    # unrelated hooks/config preserved
    assert json.loads((cursor / "hooks.json").read_text())["hooks"]["beforeSubmitPrompt"] == [{"command": "keep-hook"}]
    assert json.loads((cursor / "mcp.json").read_text())["mcpServers"]["foreign"] == {"command": "keep-server"}

    # subsequent doctor is ready
    assert cli.main(["harness", "doctor", "cursor", "--scope", "user", "--json"]) == 0
    capsys.readouterr()

    # uninstall removes the claimed file only because it is unchanged
    assert cli.main(["harness", "uninstall", "cursor", "--scope", "user", "--write", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert _row(out)["conflicts"] == []
    assert not rule.exists()
    # unrelated hooks/config still preserved
    assert json.loads((cursor / "hooks.json").read_text())["hooks"]["beforeSubmitPrompt"] == [{"command": "keep-hook"}]
    assert json.loads((cursor / "mcp.json").read_text())["mcpServers"]["foreign"] == {"command": "keep-server"}


def test_cursor_generated_adopt_then_edit_blocks_uninstall(tmp_path, monkeypatch, capsys):
    from brigade import cli

    _use_home(monkeypatch, tmp_path)
    cursor = tmp_path / ".cursor"
    rule = cursor / "plugins" / "local" / "brigade-loop" / "rules" / "brigade-loop.mdc"
    rule.parent.mkdir(parents=True)
    rule.write_text("# foreign\n")
    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--adopt", "--write", "--json"]) == 0
    capsys.readouterr()

    # user edits the claimed rule after adoption
    rule.write_text("# user edit after adopt\n")
    assert cli.main(["harness", "uninstall", "cursor", "--scope", "user", "--write", "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert any(item["path"] == str(rule) and item["status"] == "conflict" for item in _row(out)["conflicts"])
    assert rule.read_text() == "# user edit after adopt\n"


def test_cursor_v1_migration_carries_retirement_ownership_maps(tmp_path, monkeypatch):
    from brigade import harness_profile_cmd

    _use_home(monkeypatch, tmp_path)
    root = _seed_cursor_v1(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_path = root / "brigade" / "install-state.json"
    legacy_state = json.loads(state_path.read_text())

    migration = cursor_user_cmd.migrate_v1_state(root=root, workspace=workspace, state=legacy_state)
    assert migration.error is None
    # new ownership-map fields are present, immutable, and digest-shaped
    assert isinstance(migration.retire_file_ownership, tuple)
    assert isinstance(migration.retire_mcp_ownership, tuple)
    assert migration.retire_file_ownership
    assert migration.retire_mcp_ownership
    for path, digest in migration.retire_file_ownership:
        assert isinstance(path, Path)
        assert isinstance(digest, str) and len(digest) == 64
    for name, digest in migration.retire_mcp_ownership:
        assert isinstance(name, str) and isinstance(digest, str) and len(digest) == 64
    # existing tuple fields remain for compatibility
    assert migration.retire_file_paths and migration.retire_mcp_names

    # the loader propagates the ownership maps too
    loaded = harness_profile_cmd.load_cursor_profile_state(state_path=state_path, workspace=workspace, root=root)
    assert loaded.error is None
    assert loaded.migration == "cursor-state-v1"
    assert isinstance(loaded.retire_file_ownership, tuple)
    assert isinstance(loaded.retire_mcp_ownership, tuple)
    assert loaded.retire_file_ownership and loaded.retire_mcp_ownership


def test_cursor_retire_legacy_revalidates_files_and_reports_conflict_on_toctou_change(tmp_path, monkeypatch):
    from brigade import harness_profile_cmd

    _use_home(monkeypatch, tmp_path)
    root = _seed_cursor_v1(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_path = root / "brigade" / "install-state.json"
    mcp_path = root / "mcp.json"
    legacy_state = json.loads(state_path.read_text())
    mcp_before = mcp_path.read_text()

    migration = cursor_user_cmd.migrate_v1_state(root=root, workspace=workspace, state=legacy_state)
    assert migration.error is None

    # TOCTOU: change a retirement file after planning, before apply
    target_path, _stored = migration.retire_file_ownership[0]
    target_path.write_text("tampered at apply time\n")

    removed, conflicts = harness_profile_cmd._retire_cursor_legacy(
        root, migration.retire_file_ownership, migration.retire_mcp_ownership
    )
    # zero retirement mutations for the batch
    assert removed == []
    assert conflicts
    assert any(str(target_path) in (c.get("detail") or "") or c.get("path") == str(target_path) for c in conflicts)
    # changed target preserved
    assert target_path.read_text() == "tampered at apply time\n"
    # foreign + managed MCP keys all kept (no partial pop)
    servers = json.loads(mcp_path.read_text())["mcpServers"]
    assert "foreign" in servers
    assert "brigade" in servers
    assert mcp_path.read_text() == mcp_before


def test_cursor_retire_legacy_revalidates_mcp_and_reports_conflict_on_toctou_change(tmp_path, monkeypatch):
    from brigade import harness_profile_cmd

    _use_home(monkeypatch, tmp_path)
    root = _seed_cursor_v1(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_path = root / "brigade" / "install-state.json"
    mcp_path = root / "mcp.json"
    legacy_state = json.loads(state_path.read_text())

    migration = cursor_user_cmd.migrate_v1_state(root=root, workspace=workspace, state=legacy_state)
    assert migration.error is None

    # TOCTOU: edit a managed MCP server value after planning, before apply
    name, _stored = migration.retire_mcp_ownership[0]
    live = json.loads(mcp_path.read_text())
    live["mcpServers"][name] = {"command": "user-rewrote-" + name}
    mcp_path.write_text(json.dumps(live))

    removed, conflicts = harness_profile_cmd._retire_cursor_legacy(
        root, migration.retire_file_ownership, migration.retire_mcp_ownership
    )
    assert removed == []
    assert conflicts
    # foreign + edited managed key all kept (no partial pop)
    servers = json.loads(mcp_path.read_text())["mcpServers"]
    assert "foreign" in servers
    assert servers[name] == {"command": "user-rewrote-" + name}
    # legacy skill/mcp-catalog files are also preserved (zero mutations for the batch)
    desired = cursor_user_cmd._desired_files(root)
    for path, (_t, _e, surface) in desired.items():
        if surface in {"skill", "mcp-catalog"}:
            assert path.exists()


def test_cursor_v1_migration_toctou_does_not_persist_v2_on_retirement_conflict(tmp_path, monkeypatch, capsys):
    from brigade import cli, harness_profile_cmd

    _use_home(monkeypatch, tmp_path)
    root = _seed_cursor_v1(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _seed_brigade_work_registry(workspace)
    capsys.readouterr()
    state_path = root / "brigade" / "install-state.json"
    mcp_path = root / "mcp.json"
    v1_before = state_path.read_text()

    desired = cursor_user_cmd._desired_files(root)
    retire_target = next(p for p, (_t, _e, s) in desired.items() if s == "mcp-catalog")
    real_retire = harness_profile_cmd._retire_cursor_legacy

    def tampering(root_arg, file_own, mcp_own):
        # TOCTOU: change the retirement file after planning but before deletion
        retire_target.write_text("tampered at apply time\n")
        return real_retire(root_arg, file_own, mcp_own)

    monkeypatch.setattr(harness_profile_cmd, "_retire_cursor_legacy", tampering)

    assert (
        cli.main(
            ["harness", "install", "cursor", "--scope", "user", "--workspace", str(workspace), "--write", "--json"]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    row = _row(payload)
    assert row["status"] == "conflict"
    # v1 state remains on disk (schema v2 NOT persisted as if successful)
    assert state_path.read_text() == v1_before
    # foreign MCP keys kept; tampered retirement file preserved
    assert json.loads(mcp_path.read_text())["mcpServers"]["foreign"] == {"command": "keep"}
    assert retire_target.read_text() == "tampered at apply time\n"
    # no private digest/fingerprint leaks
    text = json.dumps(payload)
    assert "digest" not in text and "fingerprint" not in text
>>>>>>> ba8e21f (test(harness): cover user profile lifecycle)
