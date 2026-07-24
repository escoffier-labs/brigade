"""Shared helper coverage for the Claude/Codex user-profile layer."""

import json
from pathlib import Path

from brigade import __version__ as BRIGADE_VERSION
from brigade import harness_profile_cmd, harness_profiles


def _state(workspace: Path) -> dict:
    return {
        "schema_version": harness_profiles.PROFILE_STATE_VERSION,
        "package_version": BRIGADE_VERSION,
        "workspace": str(workspace.resolve()),
        "harness": "codex",
        "instructions": {},
        "skills": {},
        "generated": {},
        "mcp": {},
    }


def test_profiles_are_limited_to_the_slice_one_targets(tmp_path):
    profiles = harness_profiles.resolve_slice1_profiles(harness="all", home=tmp_path / "home", workspace=tmp_path)
    assert [profile.harness for profile in profiles] == ["claude", "codex"]


def test_instruction_plans_preserve_unmanaged_content_and_reject_foreign_block(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("# personal notes\n")
    desired = harness_profiles.managed_instruction_text()
    create = harness_profile_cmd.plan_instruction(path=path, desired=desired, state={})
    assert create.action == "create"
    path.write_text(create.rendered or "")
    removal = harness_profile_cmd.plan_instruction_removal(
        path=path, state={"instructions": {"digest": create.desired_digest}}
    )
    assert removal.action == "remove"
    assert removal.rendered == "# personal notes\n"
    path.write_text(f"{harness_profiles.INSTRUCTION_START}\nforeign\n{harness_profiles.INSTRUCTION_END}\n")
    conflict = harness_profile_cmd.plan_instruction(path=path, desired=desired, state={})
    assert conflict.status == "conflict"


def test_load_state_is_read_only_when_package_version_is_stale(tmp_path):
    path = tmp_path / "brigade" / "install-state.json"
    path.parent.mkdir()
    state = _state(tmp_path)
    state["package_version"] = "old"
    path.write_text(json.dumps(state))
    before = path.read_bytes(), path.stat().st_mtime_ns
    loaded = harness_profile_cmd.load_profile_state(state_path=path, workspace=tmp_path, harness="codex")
    assert loaded.error is None
    assert loaded.state["package_version"] == "old"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_load_state_migrates_legacy_cursor_install_state_read_only(tmp_path):
    root = tmp_path / "home" / ".cursor"
    path = root / "brigade" / "install-state.json"
    path.parent.mkdir(parents=True)
    legacy = {
        "version": 1,
        "package_version": "old",
        "files": {
            "plugins/local/brigade-loop/rules/brigade-loop.mdc": "a" * 64,
            "plugins/local/brigade-loop/.cursor-plugin/plugin.json": "b" * 64,
            "hooks/brigade-session-start": "c" * 64,
            "brigade/mcp.json": "d" * 64,
            "skills/brigade-work/SKILL.md": "e" * 64,
        },
        "hooks": {"sessionStart": "f" * 64},
        "mcp": {"brigade": "0123456789abcdef" + "0" * 48},
    }
    path.write_text(json.dumps(legacy))
    before = path.read_bytes(), path.stat().st_mtime_ns

    loaded = harness_profile_cmd.load_profile_state(state_path=path, workspace=tmp_path, harness="cursor")
    assert loaded.error is None
    assert loaded.migration == {"from": "legacy-install-v1", "adopted": {"files": 5, "hooks": 1, "mcp": 1}}
    state = loaded.state
    assert state["schema_version"] == harness_profiles.PROFILE_STATE_VERSION
    assert state["harness"] == "cursor"
    assert state["instructions"]["digest"] == "a" * 64
    assert state["instructions"]["created_file"] is True
    assert state["generated"]["plugins/local/brigade-loop/.cursor-plugin/plugin.json"]["digest"] == "b" * 64
    assert state["generated"]["hooks/brigade-session-start"]["digest"] == "c" * 64
    assert state["generated"]["brigade/mcp.json"]["digest"] == "d" * 64
    assert state["generated"]["hooks.json#sessionStart"]["entry_fingerprint"] == "f" * 64
    assert state["skills"]["brigade-work"]["files"] == {"SKILL.md": "e" * 64}
    # The projected fingerprint is the stable_hash-compatible prefix of the legacy digest.
    assert state["mcp"]["brigade"] == {"projected_fingerprint": "0123456789abcdef", "managed": True}
    # Migration is in-memory only; the on-disk legacy state is untouched until a sync write.
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before

    # Other harnesses keep the fail-closed version error.
    rejected = harness_profile_cmd.load_profile_state(state_path=path, workspace=tmp_path, harness="codex")
    assert rejected.error == "unsupported ownership state version: None"
    assert rejected.migration is None


def test_load_state_rejects_non_dict_legacy_cursor_sections(tmp_path):
    root = tmp_path / "home" / ".cursor"
    path = root / "brigade" / "install-state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "files": [], "hooks": {}, "mcp": {}}))
    loaded = harness_profile_cmd.load_profile_state(state_path=path, workspace=tmp_path, harness="cursor")
    assert loaded.error == "unsupported ownership state version: None"
    assert loaded.migration is None
