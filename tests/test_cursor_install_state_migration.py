"""Issue #468: migrate legacy Cursor install-state.json to profile schema v2."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import __version__ as BRIGADE_VERSION
from brigade import cli, cursor_user_cmd, harness_profile_cmd, harness_profiles
from tests._home import set_home


def _use_home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    set_home(monkeypatch, str(home))
    return home


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def _legacy_state(home: Path, capsys) -> dict:
    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--write", "--json"]) == 0
    capsys.readouterr()
    return json.loads((home / ".cursor" / "brigade" / "install-state.json").read_text())


def test_migrate_legacy_cursor_install_state_preserves_ownership_entries(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    workspace = _workspace(tmp_path)
    legacy = {
        "version": 1,
        "package_version": "0.24.0",
        "files": {
            "plugins/local/brigade-loop/rules/brigade-loop.mdc": "rule-digest",
            "plugins/local/brigade-loop/.cursor-plugin/plugin.json": "plugin-digest",
            "hooks/brigade-session-start": "hook-digest",
            "brigade/mcp.json": "catalog-digest",
            "skills/brigade-work/SKILL.md": "skill-md-digest",
            "skills/brigade-work/skill.json": "skill-json-digest",
        },
        "hooks": {
            "sessionStart": "start-entry-digest",
            "postToolUse": "post-entry-digest",
            "sessionEnd": "end-entry-digest",
        },
        "mcp": {
            "brigade": "79ccc5bec9aa60af2028383f22a1d1f21a2e9a2d313b580bf50e106a2ffe8992",
            "graphtrail": "239bb5bd3544ee1dc6603a7d85486728174a00f5770cb769d1982ce84fbb987e",
        },
    }

    migrated = harness_profile_cmd.migrate_legacy_cursor_install_state(
        state=legacy,
        workspace=workspace,
        harness="cursor",
    )

    assert migrated["schema_version"] == harness_profiles.PROFILE_STATE_VERSION
    assert migrated["package_version"] == "0.24.0"
    assert migrated["workspace"] == str(workspace.resolve())
    assert migrated["harness"] == "cursor"
    assert migrated["instructions"] == {
        "digest": "rule-digest",
        "created_file": True,
        "legacy_migrated": True,
    }
    assert migrated["generated"]["plugins/local/brigade-loop/.cursor-plugin/plugin.json"] == {
        "digest": "plugin-digest",
        "legacy_migrated": True,
    }
    assert migrated["generated"]["hooks/brigade-session-start"] == {
        "digest": "hook-digest",
        "legacy_migrated": True,
    }
    assert migrated["generated"]["brigade/mcp.json"] == {
        "digest": "catalog-digest",
        "legacy_migrated": True,
    }
    assert migrated["generated"]["hooks.json#sessionStart"] == {
        "entry_fingerprint": "start-entry-digest",
        "legacy_migrated": True,
    }
    assert migrated["generated"]["hooks.json#postToolUse"]["entry_fingerprint"] == "post-entry-digest"
    assert migrated["generated"]["hooks.json#sessionEnd"]["entry_fingerprint"] == "end-entry-digest"
    assert migrated["skills"]["brigade-work"]["files"] == {
        "SKILL.md": "skill-md-digest",
        "skill.json": "skill-json-digest",
    }
    assert migrated["skills"]["brigade-work"]["legacy_migrated"] is True
    assert migrated["mcp"]["brigade"] == {
        "projected_fingerprint": "79ccc5bec9aa60af",
        "managed": True,
        "legacy_migrated": True,
    }
    assert migrated["mcp"]["graphtrail"]["projected_fingerprint"] == "239bb5bd3544ee1d"


def test_load_profile_state_migrates_legacy_cursor_state(tmp_path):
    workspace = _workspace(tmp_path)
    state_path = tmp_path / "brigade" / "install-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "package_version": BRIGADE_VERSION,
                "files": {"plugins/local/brigade-loop/rules/brigade-loop.mdc": "digest"},
                "hooks": {},
                "mcp": {},
            }
        )
    )

    loaded = harness_profile_cmd.load_profile_state(
        state_path=state_path,
        workspace=workspace,
        harness="cursor",
    )

    assert loaded.error is None
    assert loaded.migrated_from_legacy is True
    assert loaded.state["schema_version"] == harness_profiles.PROFILE_STATE_VERSION
    assert loaded.state["instructions"]["digest"] == "digest"


def test_legacy_install_then_profile_sync_migrates_and_uninstalls(tmp_path, monkeypatch, capsys):
    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    hooks_path = home / ".cursor" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    foreign = {event: [{"command": f"keep-{event}"}] for event in ("sessionStart", "postToolUse", "sessionEnd")}
    hooks_path.write_text(json.dumps({"version": 1, "hooks": foreign}))
    legacy = _legacy_state(home, capsys)

    assert (
        cli.main(
            [
                "harness",
                "sync",
                "--target",
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
    sync_payload = json.loads(capsys.readouterr().out)
    assert sync_payload["ready"] is True
    assert sync_payload["results"][0]["migration"] == "legacy-v1"

    migrated = json.loads((home / ".cursor" / "brigade" / "install-state.json").read_text())
    assert migrated["schema_version"] == harness_profiles.PROFILE_STATE_VERSION
    assert migrated["instructions"]["digest"] == legacy["files"]["plugins/local/brigade-loop/rules/brigade-loop.mdc"]
    assert set(migrated["skills"]["brigade-work"]["files"]) == {
        "SKILL.md",
        "skill.json",
        "CHANGELOG.md",
    }
    assert set(migrated["mcp"]) == set(legacy["mcp"])
    assert {event for event in migrated["generated"] if event.startswith("hooks.json#")} == {
        "hooks.json#sessionStart",
        "hooks.json#postToolUse",
        "hooks.json#sessionEnd",
    }
    installed_hooks = json.loads(hooks_path.read_text())["hooks"]
    for event, entries in foreign.items():
        assert entries[0] in installed_hooks[event]
        assert (
            sum(
                entry
                == {"command": "exec brigade work presence-hook --harness cursor --stdin --context cursor-work-loop"}
                for entry in installed_hooks[event]
            )
            == 1
        )

    sync_command = [
        "harness",
        "sync",
        "--target",
        "cursor",
        "--scope",
        "user",
        "--workspace",
        str(workspace),
        "--write",
        "--json",
    ]
    assert cli.main(sync_command) == 0
    second_sync = json.loads(capsys.readouterr().out)
    assert second_sync["results"][0]["status"] == "current"
    assert second_sync["results"][0]["files_written"] == []

    doctor_command = [
        "harness",
        "doctor",
        "--target",
        "cursor",
        "--scope",
        "user",
        "--workspace",
        str(workspace),
        "--json",
    ]
    assert cli.main(doctor_command) == 0
    assert json.loads(capsys.readouterr().out)["results"][0]["ready"] is True

    assert (
        cli.main(
            [
                "harness",
                "uninstall",
                "--target",
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
    capsys.readouterr()
    assert not (home / ".cursor" / "brigade" / "install-state.json").exists()
    assert not (home / ".cursor" / "skills" / "brigade-work" / "SKILL.md").exists()
    assert not (home / ".cursor" / "plugins" / "local" / "brigade-loop" / "rules" / "brigade-loop.mdc").exists()
    uninstalled_hooks = json.loads(hooks_path.read_text())["hooks"]
    for event, entries in foreign.items():
        assert uninstalled_hooks[event] == entries


def test_sync_on_already_v2_state_is_unchanged(tmp_path, monkeypatch, capsys):
    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    base = [
        "harness",
        "sync",
        "--target",
        "cursor",
        "--scope",
        "user",
        "--workspace",
        str(workspace),
    ]

    assert cli.main(base + ["--write", "--json"]) == 0
    capsys.readouterr()
    state_path = home / ".cursor" / "brigade" / "install-state.json"
    receipt_path = home / ".cursor" / "brigade" / "profile-receipt.json"
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (state_path, receipt_path)}

    assert cli.main(base + ["--write", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["status"] == "current"
    assert payload["results"][0]["migration"] is None
    assert payload["results"][0]["files_written"] == []
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (state_path, receipt_path)} == before


def test_legacy_install_on_v2_state_reports_actionable_recovery(tmp_path, monkeypatch, capsys):
    _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    assert (
        cli.main(
            [
                "harness",
                "sync",
                "--target",
                "cursor",
                "--scope",
                "user",
                "--workspace",
                str(workspace),
                "--write",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert cli.main(["harness", "install", "cursor", "--scope", "user", "--write", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    conflict = next(item for item in payload["conflicts"] if item["surface"] == "ownership-state")
    assert "harness sync --target cursor" in conflict["detail"]
    assert "harness uninstall --target cursor" in conflict["detail"]
    assert "schema_version 2" in conflict["detail"]


_LIFECYCLE_EVENTS = ("sessionStart", "postToolUse", "sessionEnd")
_USER_SYNC = ["harness", "sync", "--target", "cursor", "--scope", "user", "--write", "--json"]


def _prior_migrated_state(home: Path, workspace: Path, capsys, *, versioned: bool) -> tuple[Path, Path]:
    """Rebuild what the earlier profile sync left behind: v2 state owning only sessionStart."""
    legacy = _legacy_state(home, capsys)
    state_path = home / ".cursor" / "brigade" / "install-state.json"
    migrated = harness_profile_cmd.migrate_legacy_cursor_install_state(
        state=legacy, workspace=workspace, harness="cursor"
    )
    for event in ("postToolUse", "sessionEnd"):
        del migrated["generated"][f"hooks.json#{event}"]
    state_path.write_text(json.dumps(migrated))
    hooks_path = home / ".cursor" / "hooks.json"
    doc = json.loads(hooks_path.read_text())
    assert set(doc["hooks"]) == set(_LIFECYCLE_EVENTS)
    if not versioned:
        doc.pop("version", None)
        hooks_path.write_text(json.dumps(doc))
    return state_path, hooks_path


def _owned_hook_events(state_path: Path) -> set[str]:
    generated = json.loads(state_path.read_text())["generated"]
    return {key.removeprefix("hooks.json#") for key in generated if key.startswith("hooks.json#")}


@pytest.mark.parametrize("versioned", [True, False], ids=["versioned", "versionless"])
def test_upgrade_sync_claims_legacy_lifecycle_entries_without_adopt(tmp_path, monkeypatch, capsys, versioned):
    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    state_path, hooks_path = _prior_migrated_state(home, workspace, capsys, versioned=versioned)
    assert _owned_hook_events(state_path) == {"sessionStart"}

    assert cli.main([*_USER_SYNC, "--workspace", str(workspace)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["ready"] is True
    assert payload["results"][0]["conflicts"] == []
    assert _owned_hook_events(state_path) == set(_LIFECYCLE_EVENTS)
    doc = json.loads(hooks_path.read_text())
    assert doc["version"] == 1
    for event in _LIFECYCLE_EVENTS:
        assert len(doc["hooks"][event]) == 1

    assert cli.main([*_USER_SYNC, "--workspace", str(workspace)]) == 0
    assert json.loads(capsys.readouterr().out)["results"][0]["status"] == "current"


def test_upgrade_sync_never_claims_an_edited_lifecycle_entry(tmp_path, monkeypatch, capsys):
    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    state_path, hooks_path = _prior_migrated_state(home, workspace, capsys, versioned=True)
    doc = json.loads(hooks_path.read_text())
    edited = {"command": "exec brigade work presence-hook --harness cursor --stdin --context my-own-edit"}
    doc["hooks"]["postToolUse"] = [edited]
    hooks_path.write_text(json.dumps(doc))

    assert cli.main([*_USER_SYNC, "--workspace", str(workspace)]) == 0
    capsys.readouterr()

    after = json.loads(hooks_path.read_text())["hooks"]
    assert after["postToolUse"][0] == edited
    state = json.loads(state_path.read_text())["generated"]
    managed_fingerprint = state["hooks.json#postToolUse"]["entry_fingerprint"]
    assert managed_fingerprint != cursor_user_cmd._digest_value(edited)
    assert [cursor_user_cmd._digest_value(entry) for entry in after["postToolUse"][1:]] == [managed_fingerprint]


def test_sync_without_a_legacy_migration_still_requires_adopt(tmp_path, monkeypatch, capsys):
    home = _use_home(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path)
    state_path, _hooks_path = _prior_migrated_state(home, workspace, capsys, versioned=False)
    state = json.loads(state_path.read_text())
    state["instructions"].pop("legacy_migrated")
    state["skills"]["brigade-work"].pop("legacy_migrated")
    state["mcp"] = {name: {**record, "legacy_migrated": None} for name, record in state["mcp"].items()}
    for record in state["generated"].values():
        record.pop("legacy_migrated", None)
    state_path.write_text(json.dumps(state))

    assert cli.main([*_USER_SYNC, "--workspace", str(workspace)]) != 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["ready"] is False
    names = {item["name"] for item in payload["results"][0]["conflicts"] if item.get("surface") == "hook"}
    assert names == {"postToolUse", "sessionEnd"}
    assert "version" not in json.loads((home / ".cursor" / "hooks.json").read_text())
