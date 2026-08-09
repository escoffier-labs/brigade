"""Shared helper coverage for the Claude/Codex user-profile layer."""

import json
from pathlib import Path

from brigade import __version__ as BRIGADE_VERSION
from brigade import harness_profile_cmd, harness_profiles, managed_block


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


def test_locally_modified_instruction_detail_points_at_sync_help(tmp_path):
    path = tmp_path / "AGENTS.md"
    desired = harness_profiles.managed_instruction_text()
    digest = harness_profile_cmd.digest_text(desired)
    stamped = harness_profile_cmd._block(desired)
    path.write_text(stamped.replace("brigade run", "brigade dispatch", 1), encoding="utf-8")

    plan = harness_profile_cmd.plan_instruction(
        path=path,
        desired=desired,
        state={"instructions": {"digest": digest}},
        harness="codex",
    )
    assert plan.status == "conflict"
    assert plan.detail is not None
    assert "brigade harness sync --target codex --scope user --help" in plan.detail
    assert "--adopt" not in plan.detail
    assert "--force" not in plan.detail
    assert "fix with:" not in plan.detail


def test_plan_instruction_stale_update_preserves_crlf_neighbors(tmp_path):
    path = tmp_path / "AGENTS.md"
    desired = harness_profiles.managed_instruction_text()
    old_body = "old instruction body\n"
    crlf_prefix = "# above\r\nkeep\r\n"
    crlf_suffix = "# below\r\nalso keep\r\n"
    crlf_block = harness_profile_cmd._block(old_body).replace("\n", "\r\n")
    path.write_bytes((crlf_prefix + crlf_block + crlf_suffix).encode("utf-8"))

    plan = harness_profile_cmd.plan_instruction(
        path=path,
        desired=desired,
        state={"instructions": {"digest": managed_block.body_hash(old_body)}},
    )
    assert plan.status == "stale"
    assert plan.action == "update"
    assert plan.rendered is not None
    assert plan.rendered.startswith(crlf_prefix)
    assert plan.rendered.endswith(crlf_suffix)
    assert "\r\n" in plan.rendered
    assert "\n" not in plan.rendered.replace("\r\n", "")


def test_plan_instruction_removal_preserves_crlf_neighbors(tmp_path):
    path = tmp_path / "AGENTS.md"
    desired = harness_profiles.managed_instruction_text()
    digest = managed_block.body_hash(desired)
    crlf_prefix = "# above\r\nkeep\r\n"
    crlf_suffix = "# below\r\nalso keep\r\n"
    crlf_block = harness_profile_cmd._block(desired).replace("\n", "\r\n")
    path.write_bytes((crlf_prefix + crlf_block + crlf_suffix).encode("utf-8"))

    plan = harness_profile_cmd.plan_instruction_removal(
        path=path,
        state={"instructions": {"digest": digest}},
    )
    assert plan.action == "remove"
    assert plan.rendered == crlf_prefix + crlf_suffix


def test_malformed_instruction_detail_points_at_sync_help(tmp_path):
    path = tmp_path / "AGENTS.md"
    desired = harness_profiles.managed_instruction_text()
    path.write_text(
        "<!-- BEGIN BRIGADE INTEGRATION broken -->\nbody\n<!-- END BRIGADE INTEGRATION -->\n",
        encoding="utf-8",
    )

    plan = harness_profile_cmd.plan_instruction(path=path, desired=desired, state={}, harness="codex")
    assert plan.status == "conflict"
    assert plan.detail is not None
    assert "brigade harness sync --target codex --scope user --help" in plan.detail
    assert "--adopt" not in plan.detail
    assert "--force" not in plan.detail
    assert "fix with:" not in plan.detail


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
