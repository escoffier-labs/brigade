"""Profile contracts use the same pinned source as test_opencode_mcp_contract."""

import json
from types import SimpleNamespace

import pytest

from brigade import harness_profile_cmd as H, localio, mcp_adapters as A, mcp_cmd


@pytest.mark.parametrize("harness", ["opencode", "opencode-user"])
@pytest.mark.parametrize(
    "text",
    [None, "{}", '{"mcp":{"servers":{}}}', '{"mcp":{"docs":{"type":"remote","url":"https://docs.example/mcp"}}}'],
)
def test_empty_profile_verifies_without_canonical_catalog(tmp_path, harness, text):
    path = tmp_path / "opencode.json"
    if text is not None:
        path.write_text(text)
    profile = SimpleNamespace(mcp_harness=harness, mcp_path=path)
    state = {"mcp": {}}
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert H._verify_mcp(profile, state, tmp_path) == ({"status": "ready", "items": []}, True)
    assert state == {"mcp": {}}
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("harness", ["opencode", "opencode-user"])
@pytest.mark.parametrize(
    "text",
    [
        "{FAKE_SECRET",
        '{"mcp":[]}',
        '{"mcp":{"servers":[]}}',
        '{"mcp":{"servers":{"bad":"FAKE_SECRET"}}}',
        '{"mcp":{"docs":{"type":"remote","url":"https://docs.example/mcp"},"servers":{"docs":{"type":"remote","url":"https://docs.example/mcp","headers":{"Authorization":"FAKE_SECRET"}}}}}',
    ],
)
def test_empty_profile_verification_still_refuses_malformed_native(tmp_path, harness, text):
    path = tmp_path / "opencode.json"
    path.write_text(text)
    profile = SimpleNamespace(mcp_harness=harness, mcp_path=path)
    state = {"mcp": {}}
    before = path.read_bytes()
    result, ready = H._verify_mcp(profile, state, tmp_path)
    assert not ready and result["status"] == "conflict"
    assert result["items"][0]["status"] == "malformed"
    assert "FAKE_SECRET" not in json.dumps(result)
    assert state == {"mcp": {}} and path.read_bytes() == before
    assert not mcp_cmd.canonical_path(tmp_path).exists()


def test_custom_empty_nested_profile_path_and_ownership(tmp_path):
    path = tmp_path / "custom" / "opencode.json"
    path.parent.mkdir()
    path.write_text('{"mcp":{"servers":{},"opaque":{"keep":true}}}')
    profile = SimpleNamespace(mcp_harness="opencode-user", mcp_path=path)
    server = A.CanonicalServer(name="docs", transport="http", url="https://docs.example/mcp")
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    plan = H._mcp_plan(profile, {"mcp": {}}, tmp_path, allow_global_stdio=False, adopt=False)
    assert plan["conflicts"] == []
    rendered = plan["adapter"].write_file(plan["text"], plan["updates"], plan["remove"])
    doc = json.loads(rendered)
    actual = doc["mcp"]["servers"]["docs"]
    assert "docs" not in doc["mcp"] and actual["disabled"] is False
    assert doc["mcp"]["opaque"] == {"keep": True}
    assert plan["next"]["docs"]["projected_fingerprint"] == localio.stable_hash(actual)
    path.write_text(rendered)
    state = {"mcp": plan["next"]}
    again = H._mcp_plan(profile, state, tmp_path, allow_global_stdio=False, adopt=False)
    assert again["updates"] == {} and again["conflicts"] == []
    assert H._verify_mcp(profile, state, tmp_path)[1]
    uninstall = H._mcp_uninstall_plan(profile, state, tmp_path)
    assert uninstall["remove"] == {"docs"}
    assert json.loads(plan["adapter"].write_file(rendered, {}, uninstall["remove"]))["mcp"]["servers"] == {}


@pytest.mark.parametrize("location", ["flat", "nested"])
@pytest.mark.parametrize("to_remote", [False, True])
def test_custom_profile_transport_change_drops_incompatible_native_fields(tmp_path, location, to_remote):
    path = tmp_path / "custom.json"
    local = A.CanonicalServer(name="docs", command="fake-command")
    remote = A.CanonicalServer(name="docs", transport="http", url="https://example.invalid")
    initial, desired = (local, remote) if to_remote else (remote, local)
    native = A.ADAPTERS["opencode-user"].to_provider(initial)
    native.update(codemode=False, opaque={"keep": True})
    if to_remote:
        native.update(cwd="./fake-workspace", environment={"FAKE_ENV": "fake"})
    else:
        native.update(headers={"Authorization": "fake"}, oauth=False)
    body = {"servers": {"docs": native}} if location == "nested" else {"docs": native}
    path.write_text(json.dumps({"mcp": body}))
    profile = SimpleNamespace(mcp_harness="opencode-user", mcp_path=path)
    mcp_cmd._write_canonical(tmp_path, {"docs": desired})
    plan = H._mcp_plan(profile, {"mcp": {}}, tmp_path, allow_global_stdio=True, adopt=True)
    assert plan["conflicts"] == []
    rendered = plan["adapter"].write_file(plan["text"], plan["updates"], plan["remove"])
    actual = plan["adapter"].read_file(rendered)["docs"]
    incompatible = {"command", "environment", "cwd"} if to_remote else {"url", "headers", "oauth"}
    assert not incompatible.intersection(actual)
    assert actual["type"] == ("remote" if to_remote else "local")
    assert actual["codemode"] is False and actual["opaque"] == {"keep": True}
    assert plan["next"]["docs"]["projected_fingerprint"] == localio.stable_hash(actual)
    path.write_text(rendered)
    state = {"mcp": plan["next"]}
    again = H._mcp_plan(profile, state, tmp_path, allow_global_stdio=True, adopt=False)
    assert again["updates"] == {} and again["conflicts"] == []
    assert H._verify_mcp(profile, state, tmp_path)[1]


@pytest.mark.parametrize(
    "text",
    [
        '{"mcp":{"docs":{"type":"remote","url":"https://docs.example/mcp"},"servers":{"docs":{"type":"remote","url":"https://docs.example/mcp"}}}}',
        '{"mcp":{"servers":[]}}',
    ],
)
def test_profile_preflight_conflicts_without_writes(tmp_path, text):
    path = tmp_path / "custom.json"
    path.write_text(text)
    profile = SimpleNamespace(mcp_harness="opencode-user", mcp_path=path)
    server = A.CanonicalServer(name="docs", transport="http", url="https://docs.example/mcp")
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    state = {
        "mcp": {
            "docs": {
                "managed": True,
                "projected_fingerprint": localio.stable_hash({"type": "remote", "url": server.url}),
            }
        }
    }
    before = path.read_bytes(), json.dumps(state)
    assert H._mcp_plan(profile, state, tmp_path, allow_global_stdio=False, adopt=True)["conflicts"]
    assert H._mcp_uninstall_plan(profile, state, tmp_path)["conflicts"]
    assert not H._verify_mcp(profile, state, tmp_path)[1]
    assert before == (path.read_bytes(), json.dumps(state))


def test_profile_duplicate_diagnostics_and_empty_state_preflight(tmp_path):
    path = tmp_path / "custom.json"
    entry = {"type": "remote", "url": "https://docs.example/mcp", "headers": {"Authorization": "FAKE_SECRET"}}
    path.write_text(json.dumps({"mcp": {"docs": entry, "servers": {"docs": entry}}}))
    profile = SimpleNamespace(mcp_harness="opencode-user", mcp_path=path)
    state = {"mcp": {}}
    plan = H._mcp_plan(profile, state, tmp_path, allow_global_stdio=False, adopt=True)
    uninstall = H._mcp_uninstall_plan(profile, state, tmp_path)
    for result in (plan, uninstall):
        assert result["conflicts"][0]["reason"] == "duplicate_layout"
        assert result["conflicts"][0]["layout_conflicts"] == [{"server": "docs", "locations": ["flat", "nested"]}]
        assert "FAKE_SECRET" not in json.dumps(result["conflicts"])
    verify, ready = H._verify_mcp(profile, state, tmp_path)
    assert not ready
    assert verify["items"][0]["reason"] == "duplicate_layout"


@pytest.mark.parametrize("location", ["flat", "nested"])
@pytest.mark.parametrize("value", ["FAKE_AUTH", "Bearer ${DOCS_TOKEN}", "Bearer FAKE_AUTH-${DOCS_TOKEN}"])
def test_custom_profile_safe_import_preserves_live_header(tmp_path, location, value):
    path = tmp_path / "custom.json"
    native = {"type": "remote", "url": "https://docs.example/mcp", "headers": {"Authorization": value}}
    body = {"servers": {"docs": native}} if location == "nested" else {"docs": native}
    path.write_text(json.dumps({"mcp": body}))
    profile = SimpleNamespace(mcp_harness="opencode-user", mcp_path=path)
    server, _ = A.ADAPTERS["opencode-user"].from_provider("docs", native)
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    assert value not in mcp_cmd.canonical_path(tmp_path).read_text()
    plan = H._mcp_plan(profile, {"mcp": {}}, tmp_path, allow_global_stdio=False, adopt=True)
    assert plan["conflicts"] == []
    rendered = plan["adapter"].write_file(plan["text"], plan["updates"], plan["remove"])
    assert plan["adapter"].read_file(rendered)["docs"]["headers"] == {"Authorization": value}
    assert value not in json.dumps(plan["next"])
    path.write_text(rendered)
    state = {"mcp": plan["next"]}
    assert H._verify_mcp(profile, state, tmp_path)[1]
    override = A.CanonicalServer(
        name="docs", transport="http", url=server.url, headers={"authorization": {"ref": "NEW_TOKEN"}}
    )
    mcp_cmd._write_canonical(tmp_path, {"docs": override})
    plan = H._mcp_plan(profile, state, tmp_path, allow_global_stdio=False, adopt=True)
    assert plan["conflicts"] == []
    rendered = plan["adapter"].write_file(plan["text"], plan["updates"], plan["remove"])
    assert plan["adapter"].read_file(rendered)["docs"]["headers"] == {"authorization": "${NEW_TOKEN}"}


def test_profile_plan_copies_legacy_ownership_before_final_fingerprint(tmp_path):
    path = tmp_path / "custom.json"
    legacy = {"type": "remote", "url": "https://legacy.example/mcp"}
    path.write_text(json.dumps({"mcp": {"servers": {"legacy": legacy}}}))
    profile = SimpleNamespace(mcp_harness="opencode-user", mcp_path=path)
    server = A.CanonicalServer(name="docs", transport="http", url="https://docs.example/mcp")
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    record = {"managed": True, "legacy_migrated": True, "projected_fingerprint": localio.stable_hash(legacy)}
    state = {"mcp": {"legacy": record}}
    before = json.dumps(state)
    plan = H._mcp_plan(profile, state, tmp_path, allow_global_stdio=False, adopt=False)
    assert plan["conflicts"] == [] and plan["updates"]
    assert json.dumps(state) == before
    assert plan["next"]["legacy"] is not record
    plan["next"]["legacy"]["projected_fingerprint"] = "changed"
    assert json.dumps(state) == before


@pytest.mark.parametrize("location", ["flat", "nested"])
def test_profile_explicit_timeout_precedes_native_conversion(tmp_path, location):
    path = tmp_path / "custom.json"
    native = {"type": "remote", "url": "https://docs.example/mcp", "timeout": {"startup": 10}}
    body = {"servers": {"docs": native}} if location == "nested" else {"docs": native}
    path.write_text(json.dumps({"mcp": body}))
    profile = SimpleNamespace(mcp_harness="opencode-user", mcp_path=path)
    server = A.CanonicalServer(name="docs", transport="http", url=native["url"], timeout=3)
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    plan = H._mcp_plan(profile, {"mcp": {}}, tmp_path, allow_global_stdio=False, adopt=True)
    assert plan["conflicts"] == []
    rendered = plan["adapter"].write_file(plan["text"], plan["updates"], plan["remove"])
    actual = plan["adapter"].read_file(rendered)["docs"]
    assert actual["timeout"] == (3000 if location == "flat" else {"startup": 10, "catalog": 3000, "execution": 3000})


@pytest.mark.parametrize("harness", ["codex-user", "vscode"])
def test_other_profile_harness_records_final_native_fingerprint(tmp_path, harness):
    path = tmp_path / ("custom.toml" if harness == "codex-user" else "custom.json")
    profile = SimpleNamespace(mcp_harness=harness, mcp_path=path)
    server = A.CanonicalServer(
        name="docs", transport="http", url="https://docs.example/mcp", headers={"X-Ref": {"ref": "DOCS_TOKEN"}}
    )
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    plan = H._mcp_plan(profile, {"mcp": {}}, tmp_path, allow_global_stdio=False, adopt=False)
    assert plan["conflicts"] == []
    rendered = plan["adapter"].write_file(plan["text"], plan["updates"], plan["remove"])
    actual = plan["adapter"].read_file(rendered)["docs"]
    assert plan["next"]["docs"]["projected_fingerprint"] == localio.stable_hash(actual)
    path.write_text(rendered)
    state = {"mcp": plan["next"]}
    again = H._mcp_plan(profile, state, tmp_path, allow_global_stdio=False, adopt=False)
    assert again["updates"] == {} and again["conflicts"] == []
    assert H._verify_mcp(profile, state, tmp_path)[1]
