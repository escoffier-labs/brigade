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
