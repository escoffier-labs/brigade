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


def _lossy_native_auth_workspace(tmp_path, monkeypatch):
    from brigade import mcp_adapters, mcp_cmd

    (tmp_path / ".brigade").mkdir()
    server, _ = mcp_adapters.server_from_dict(
        "docs",
        {
            "transport": "http",
            "url": "https://mcp.example.com",
            "http_headers": {"Authorization": "fake-fallback"},
            "env_http_headers": {"authorization": "FAKE_HEADER"},
        },
    )
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    profile = harness_profiles.resolve_slice1_profiles(harness="claude", home=tmp_path / "home", workspace=tmp_path)[0]
    return profile


def test_mcp_plan_reports_unprojectable_server_as_conflict(tmp_path, monkeypatch):
    profile = _lossy_native_auth_workspace(tmp_path, monkeypatch)
    state = {**_state(tmp_path), "harness": "claude"}
    plan = harness_profile_cmd._mcp_plan(profile, state, tmp_path, allow_global_stdio=True, adopt=False)
    assert [(c["name"], c["status"]) for c in plan["conflicts"]] == [("docs", "conflict")]
    assert "cannot preserve Codex native" in plan["conflicts"][0]["detail"]


def test_verify_mcp_reports_unprojectable_server_as_conflict(tmp_path, monkeypatch):
    profile = _lossy_native_auth_workspace(tmp_path, monkeypatch)
    from brigade import mcp_adapters

    path = profile.mcp_path or mcp_adapters.resolve_path(mcp_adapters.ADAPTERS[profile.mcp_harness], tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"docs": {"url": "https://mcp.example.com"}}}))
    state = {**_state(tmp_path), "harness": "claude", "mcp": {"docs": {"managed": True}}}
    payload, ok = harness_profile_cmd._verify_mcp(profile, state, tmp_path)
    assert ok is False
    assert payload["status"] == "conflict"
    assert payload["items"][0]["status"] == "conflict"


def _codex_profile_workspace(tmp_path, raw):
    from brigade import mcp_adapters, mcp_cmd

    (tmp_path / ".brigade").mkdir()
    server, _ = mcp_adapters.server_from_dict("docs", raw)
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    profile = harness_profiles.resolve_slice1_profiles(harness="codex", home=tmp_path / "home", workspace=tmp_path)[0]
    path = profile.mcp_path or mcp_adapters.resolve_path(mcp_adapters.ADAPTERS[profile.mcp_harness], tmp_path)
    return profile, path


def _apply_mcp(profile, state, workspace, path, *, adopt=False):
    plan = harness_profile_cmd._mcp_plan(profile, state, workspace, allow_global_stdio=True, adopt=adopt)
    assert plan["conflicts"] == []
    if plan["updates"] or plan["remove"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plan["adapter"].write_file(plan["text"], plan["updates"], plan["remove"]))
    state["mcp"] = plan["next"]
    return plan


_AUTH_FIELDS = ("bearer_token_env_var", "http_headers", "env_http_headers")


def test_profile_removes_managed_native_auth_when_canonical_drops_it(tmp_path):
    from brigade import mcp_adapters, mcp_cmd

    raw = {
        "transport": "http",
        "url": "https://mcp.example.com",
        "bearer_token_env_var": "FAKE_TOKEN",
        "http_headers": {"X-Fake": "fake"},
        "env_http_headers": {"X-Fake-Env": "FAKE_ENV"},
    }
    profile, path = _codex_profile_workspace(tmp_path, raw)
    state = {**_state(tmp_path), "harness": "codex"}
    _apply_mcp(profile, state, tmp_path, path)
    live = mcp_adapters.tomllib.loads(path.read_text())["mcp_servers"]["docs"]
    assert all(field in live for field in _AUTH_FIELDS)

    server, _ = mcp_adapters.server_from_dict("docs", {"transport": "http", "url": "https://mcp.example.com"})
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    _, ok = harness_profile_cmd._verify_mcp(profile, state, tmp_path)
    assert ok is False
    plan = _apply_mcp(profile, state, tmp_path, path)
    assert [i["action"] for i in plan["items"]] == ["update"]
    live = mcp_adapters.tomllib.loads(path.read_text())["mcp_servers"]["docs"]
    assert not any(field in live for field in _AUTH_FIELDS)
    _, ok = harness_profile_cmd._verify_mcp(profile, state, tmp_path)
    assert ok is True
    again = _apply_mcp(profile, state, tmp_path, path)
    assert [i["action"] for i in again["items"]] == ["none"]


def test_profile_adopt_preserves_genuine_native_auth(tmp_path):
    from brigade import mcp_adapters, mcp_cmd

    profile, path = _codex_profile_workspace(tmp_path, {"transport": "http", "url": "https://mcp.example.com"})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[mcp_servers.docs]\nurl = "https://mcp.example.com"\nbearer_token_env_var = "NATIVE_TOKEN"\n')
    state = {**_state(tmp_path), "harness": "codex"}
    _apply_mcp(profile, state, tmp_path, path, adopt=True)
    assert state["mcp"]["docs"]["native_auth_keys"] == {"bearer_token_env_var": True}
    server, _ = mcp_adapters.server_from_dict("docs", {"transport": "http", "url": "https://mcp.example.com/v2"})
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    _apply_mcp(profile, state, tmp_path, path, adopt=True)
    assert state["mcp"]["docs"]["native_auth_keys"] == {"bearer_token_env_var": True}
    live = mcp_adapters.tomllib.loads(path.read_text())["mcp_servers"]["docs"]
    assert live["bearer_token_env_var"] == "NATIVE_TOKEN"
    assert live["url"] == "https://mcp.example.com/v2"
    _, ok = harness_profile_cmd._verify_mcp(profile, state, tmp_path)
    assert ok is True
