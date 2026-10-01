"""Source-backed OpenCode v2.0.0 contracts, without consumer execution.

Pinned source: anomalyco/opencode@63f7ceecbed2d7d9a627518d935dd963b9d4ac9f
packages/core/src/config/normalize.ts, packages/core/src/v1/config/migrate.ts,
packages/schema/src/mcp.ts, packages/core/test/config/normalization.test.ts.
"""

import json
from dataclasses import replace

import pytest

from brigade import localio, mcp_adapters as A, mcp_cmd
from tests._home import set_home


REMOTE = {"type": "remote", "url": "https://docs.example/mcp"}


def test_nested_duplicate_whole_entry_and_membership():
    text = json.dumps(
        {
            "mcp": {
                "docs": {**REMOTE, "headers": {"X-Old": "discard"}},
                "servers": {"docs": {**REMOTE, "disabled": True}},
                "timeout": {"startup": 30000},
            }
        }
    )
    view = A.inspect_opencode_config(text)
    assert view.servers == {"docs": {**REMOTE, "disabled": True}}
    assert view.duplicates == ("docs",)
    assert view.warnings
    server, _ = A.ADAPTERS["opencode"].from_provider("docs", view.servers["docs"])
    assert server.enabled is True
    assert server.opencode_native == {"disabled": True}


@pytest.mark.parametrize("name", ["servers", "timeout", "auth", "headers"])
def test_discriminated_flat_names_are_servers(name):
    text = json.dumps({"mcp": {name: REMOTE}})
    view = A.inspect_opencode_config(text)
    assert view.servers == {name: REMOTE}
    assert view.locations == {name: "flat"}
    assert view.new_server_location == "flat"
    rendered = A.ADAPTERS["opencode"].write_file(text, {name: {**REMOTE, "enabled": False}}, set())
    assert json.loads(rendered)["mcp"][name]["enabled"] is False


@pytest.mark.parametrize("harness", ["opencode", "opencode-user"])
@pytest.mark.parametrize("remove", [set(), {"docs"}])
def test_duplicate_writer_refuses_identical_entries(harness, remove):
    text = json.dumps({"mcp": {"docs": REMOTE, "servers": {"docs": REMOTE}}})
    with pytest.raises(ValueError, match="duplicate"):
        A.ADAPTERS[harness].write_file(text, {}, remove)


@pytest.mark.parametrize("text", ["", "{", "[]", '{"mcp":[]}', '{"mcp":{"servers":[]}}'])
def test_malformed_layout_refuses_mutation(text):
    with pytest.raises(ValueError, match="malformed"):
        A.ADAPTERS["opencode"].write_file(text, {"new": REMOTE}, set())


@pytest.mark.parametrize("nested", [False, True])
def test_shape_preserving_add_update_remove(nested):
    body = {"servers": {"docs": REMOTE}} if nested else {"docs": REMOTE}
    body.update(timeout={"startup": 12345}, opaque={"keep": True})
    text = json.dumps({"model": "example/model", "mcp": body})
    adapter = A.ADAPTERS["opencode"]
    doc = json.loads(
        adapter.write_file(text, {"new": REMOTE, "docs": {**REMOTE, "url": "https://new.example/mcp"}}, set())
    )
    entries = doc["mcp"]["servers"] if nested else doc["mcp"]
    assert entries["new"] == REMOTE
    assert entries["docs"]["url"] == "https://new.example/mcp"
    doc = json.loads(adapter.write_file(json.dumps(doc), {}, {"docs", "new"}))
    assert doc["mcp"]["timeout"] == {"startup": 12345}
    assert doc["mcp"]["opaque"] == {"keep": True}
    assert doc["model"] == "example/model"
    if nested:
        assert doc["mcp"]["servers"] == {}


def test_disjoint_mixed_locations_and_setting_collision():
    text = json.dumps({"mcp": {"flat": REMOTE, "servers": {"nested": REMOTE}, "timeout": {"startup": 1000}}})
    adapter = A.ADAPTERS["opencode"]
    result = json.loads(adapter.write_file(text, {"flat": REMOTE, "nested": REMOTE, "new": REMOTE}, set()))["mcp"]
    assert "flat" in result and "new" in result["servers"] and "nested" in result["servers"]
    with pytest.raises(ValueError, match="overwrite"):
        adapter.write_file('{"mcp":{"timeout":{"startup":1000}}}', {"timeout": REMOTE}, set())


@pytest.mark.parametrize("location", ["flat", "nested"])
def test_native_activation_timeout_auth_and_deep_copy(location):
    raw = {
        **REMOTE,
        "enabled": False,
        "timeout": 40002,
        "oauth": {"clientId": "example", "scope": "read", "clientSecret": "FAKE_SECRET"},
        "password": "FAKE_PASSWORD",
        "headers": {"Authorization": "FAKE_AUTH", "Cookie": "FAKE_COOKIE", "X-Ref": "${EXAMPLE_TOKEN}"},
    }
    server, dropped = A.ADAPTERS["opencode"].from_provider("docs", raw)
    assert server.enabled and server.timeout is None
    assert "clientSecret" not in server.opencode_native["oauth"]
    assert "password" not in server.opencode_native
    assert {"oauth.clientSecret", "password", "Authorization", "Cookie"} <= set(dropped)
    assert "Authorization" not in server.headers and "Cookie" not in server.headers
    encoded = A.server_to_dict(server)
    decoded, _ = A.server_from_dict("docs", encoded)
    encoded["opencode_native"]["oauth"]["scope"] = "changed"
    assert decoded.opencode_native["oauth"]["scope"] == "read"
    projected = A.opencode_merge_server(decoded, {}, location=location, native_keys={})
    assert projected.get("enabled" if location == "flat" else "disabled") is (location == "nested")
    assert projected["timeout"] == (40002 if location == "flat" else {"catalog": 40002, "execution": 40002})
    kept, _ = A.ADAPTERS["opencode"].from_provider("docs", raw, keep_secrets=True)
    assert kept.opencode_native["oauth"]["clientSecret"] == "FAKE_SECRET"


def test_timeout_conversion_and_unrepresentable_downgrade():
    server = A.CanonicalServer(name="docs", transport="http", url=REMOTE["url"], timeout=7)
    assert A.ADAPTERS["opencode"].to_provider(server)["timeout"] == 7000
    native = {**REMOTE, "timeout": {"startup": 30001, "catalog": 40002, "execution": 50003}, "disabled": True}
    projected = A.opencode_merge_server(
        server, native, location="nested", native_keys={"timeout": True, "disabled": True}
    )
    assert projected["timeout"] == {"startup": 30001, "catalog": 7000, "execution": 7000}
    imported, _ = A.ADAPTERS["opencode"].from_provider("docs", native)
    with pytest.raises(ValueError, match="timeout"):
        A.opencode_merge_server(imported, {}, location="flat", native_keys={})


@pytest.mark.parametrize("location", ["flat", "nested"])
@pytest.mark.parametrize("oauth", [False, {"clientId": "fake", "clientSecret": "FAKE_SECRET"}])
@pytest.mark.parametrize("explicit_native", [False, True])
def test_remote_to_local_drops_native_transport_fields(location, oauth, explicit_native):
    existing = {
        "type": "remote",
        "url": "https://example.invalid",
        "headers": {"Authorization": "fake"},
        "oauth": oauth,
        "codemode": False,
        "opaque": {"keep": True},
    }
    before = json.dumps(existing)
    server = A.CanonicalServer(
        name="docs",
        command="fake-command",
        env={"FAKE_ENV": {"ref": "FAKE_TOKEN"}},
        opencode_native={"oauth": oauth} if explicit_native else {},
    )
    projected = A.opencode_merge_server(
        server, existing, location=location, native_keys={"headers": ["Authorization"], "oauth": True}
    )
    assert projected == {
        "type": "local",
        "command": ["fake-command"],
        "environment": {"FAKE_ENV": "${FAKE_TOKEN}"},
        "enabled" if location == "flat" else "disabled": location == "flat",
        "codemode": False,
        "opaque": {"keep": True},
    }
    assert json.dumps(existing) == before


@pytest.mark.parametrize("location", ["flat", "nested"])
def test_local_to_remote_drops_local_fields_after_render(location):
    existing = {
        "type": "local",
        "command": ["fake-command"],
        "environment": {"FAKE_ENV": "fake"},
        "cwd": "./fake-workspace",
        "codemode": False,
        "opaque": {"keep": True},
    }
    server = A.CanonicalServer(name="docs", transport="http", url=REMOTE["url"], opencode_native={"oauth": False})
    projected = A.opencode_merge_server(server, existing, location=location, native_keys={})
    expected = {
        **REMOTE,
        "enabled" if location == "flat" else "disabled": location == "flat",
        "oauth": False,
        "codemode": False,
        "opaque": {"keep": True},
    }
    assert projected == expected
    body = {"servers": {"docs": existing}} if location == "nested" else {"docs": existing}
    rendered = A.ADAPTERS["opencode"].write_file(json.dumps({"mcp": body}), {"docs": projected}, set())
    assert A.ADAPTERS["opencode"].read_file(rendered)["docs"] == expected


@pytest.mark.parametrize("harness", ["opencode", "opencode-user"])
@pytest.mark.parametrize("location", ["flat", "nested"])
@pytest.mark.parametrize("to_remote", [False, True])
def test_sync_transport_change_preserves_options_and_final_fingerprint(
    tmp_path, monkeypatch, capsys, harness, location, to_remote
):
    set_home(monkeypatch, tmp_path / "home")
    adapter = A.ADAPTERS[harness]
    path = A.resolve_path(adapter, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    local = A.CanonicalServer(name="docs", command="fake-command", env={"FAKE_ENV": {"literal": "fake"}})
    remote = A.CanonicalServer(name="docs", transport="http", url=REMOTE["url"])
    initial, desired = (local, remote) if to_remote else (remote, local)
    native = adapter.to_provider(initial)
    native.update(codemode=False, opaque={"keep": True})
    if to_remote:
        native["cwd"] = "./fake-workspace"
    else:
        native.update(headers={"Authorization": "fake"}, oauth=False)
    body = {"servers": {"docs": native}} if location == "nested" else {"docs": native}
    path.write_text(json.dumps({"mcp": body}))
    options = dict(
        target=tmp_path,
        harness=harness,
        user_scope=harness.endswith("-user"),
        allow_global_stdio=True,
        write=True,
        json_output=True,
    )
    mcp_cmd._write_canonical(tmp_path, {"docs": initial})
    assert mcp_cmd.sync(**options, adopt=True) == 0
    capsys.readouterr()
    adopted = adapter.read_file(path.read_text())["docs"]
    if to_remote:
        assert adopted["cwd"] == "./fake-workspace"
    else:
        assert adopted["headers"] == {"Authorization": "fake"} and adopted["oauth"] is False
    mcp_cmd._write_canonical(tmp_path, {"docs": desired})
    assert mcp_cmd.sync(**options) == 0
    capsys.readouterr()
    actual = adapter.read_file(path.read_text())["docs"]
    incompatible = {"command", "environment", "cwd"} if to_remote else {"url", "headers", "oauth"}
    assert not incompatible.intersection(actual)
    assert actual["type"] == ("remote" if to_remote else "local")
    assert actual["codemode"] is False and actual["opaque"] == {"keep": True}
    record = json.loads(mcp_cmd.state_path(tmp_path).read_text())["ownership"][harness][adapter.path]["docs"]
    assert record["projected_fingerprint"] == localio.stable_hash(actual)
    before = path.read_bytes(), mcp_cmd.state_path(tmp_path).read_bytes()
    assert mcp_cmd.sync(**options) == 0
    assert json.loads(capsys.readouterr().out)["terminal_state"] == "unchanged"
    assert before == (path.read_bytes(), mcp_cmd.state_path(tmp_path).read_bytes())


@pytest.mark.parametrize("harness", ["opencode", "opencode-user"])
def test_sync_preserves_live_native_and_final_fingerprint(tmp_path, monkeypatch, capsys, harness):
    set_home(monkeypatch, tmp_path / "home")
    adapter = A.ADAPTERS[harness]
    path = A.resolve_path(adapter, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    server = A.CanonicalServer(
        name="docs", transport="http", url="https://new.example/mcp", headers={"authorization": {"ref": "NEW_TOKEN"}}
    )
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    native = {
        **REMOTE,
        "disabled": True,
        "timeout": {"startup": 30001, "catalog": 40002, "execution": 50003},
        "oauth": {"clientSecret": "FAKE_SECRET"},
        "headers": {"Authorization": "FAKE_AUTH", "X-Native": "keep"},
        "opaque": {"keep": True},
    }
    path.write_text(json.dumps({"mcp": {"servers": {"docs": native}, "timeout": {"startup": 60004}}}))
    options = dict(target=tmp_path, harness=harness, user_scope=harness.endswith("-user"), write=True, json_output=True)
    assert mcp_cmd.sync(**options, adopt=True) == 0
    capsys.readouterr()
    actual = json.loads(path.read_text())["mcp"]["servers"]["docs"]
    assert actual == {**native, "url": server.url, "headers": {"authorization": "${NEW_TOKEN}", "X-Native": "keep"}}
    state = json.loads(mcp_cmd.state_path(tmp_path).read_text())
    record = state["ownership"][harness][adapter.path]["docs"]
    assert record["projected_fingerprint"] == localio.stable_hash(actual)
    assert "FAKE_SECRET" not in json.dumps(record) and "FAKE_AUTH" not in json.dumps(record)
    before = path.read_bytes(), mcp_cmd.state_path(tmp_path).read_bytes()
    assert mcp_cmd.sync(**options) == 0
    assert json.loads(capsys.readouterr().out)["terminal_state"] == "unchanged"
    assert before == (path.read_bytes(), mcp_cmd.state_path(tmp_path).read_bytes())
    mcp_cmd._write_canonical(tmp_path, {"docs": replace(server, headers={})})
    assert mcp_cmd.sync(**options) == 0
    capsys.readouterr()
    assert json.loads(path.read_text())["mcp"]["servers"]["docs"]["headers"] == {"X-Native": "keep"}
    mcp_cmd._write_canonical(tmp_path, {"docs": replace(server, enabled=False)})
    assert mcp_cmd.sync(**options, prune=True) == 0
    assert json.loads(path.read_text())["mcp"]["servers"] == {}


def test_duplicate_import_warns_and_mutation_refuses_all_destinations(tmp_path, capsys):
    raw = {**REMOTE, "oauth": {"clientSecret": "FAKE_SECRET"}, "headers": {"Authorization": "FAKE_AUTH"}}
    path = tmp_path / "opencode.json"
    path.write_text(
        json.dumps({"mcp": {"docs": {**REMOTE, "url": "https://old.example/mcp"}, "servers": {"docs": raw}}})
    )
    mcp_cmd._write_canonical(tmp_path, {})
    assert mcp_cmd.import_servers(target=tmp_path, harness="opencode", merge=True, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["warnings"] and payload["layout_conflicts"] == [{"server": "docs", "locations": ["flat", "nested"]}]
    assert "FAKE_SECRET" not in mcp_cmd.canonical_path(tmp_path).read_text()
    assert "FAKE_AUTH" not in mcp_cmd.canonical_path(tmp_path).read_text()
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert (
        mcp_cmd.sync(
            target=tmp_path,
            harness="opencode",
            name="other",
            write=True,
            adopt=True,
            force=True,
            prune=True,
            json_output=True,
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["layout_conflicts"] == [{"server": "docs", "locations": ["flat", "nested"]}]
    assert "FAKE_SECRET" not in json.dumps(payload) and "FAKE_AUTH" not in json.dumps(payload)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert mcp_cmd.plan(target=tmp_path, harness="opencode", json_output=True) == 2
    assert json.loads(capsys.readouterr().out)["layout_conflicts"] == [
        {"server": "docs", "locations": ["flat", "nested"]}
    ]
    servers, errors, _ = mcp_cmd.load_canonical(tmp_path)
    assert not errors
    assert mcp_cmd._config_current_by_name(tmp_path, servers, ["opencode"], mcp_cmd._load_state(tmp_path)) == {
        "docs": False
    }


def test_safe_import_sync_keeps_live_oauth_secret_only(tmp_path, capsys):
    path = tmp_path / "opencode.json"
    native = {
        **REMOTE,
        "disabled": True,
        "oauth": {"clientId": "example", "scope": "read", "clientSecret": "FAKE_SECRET", "opaque": "keep-live-only"},
    }
    path.write_text(json.dumps({"mcp": {"servers": {"docs": native}}}))
    mcp_cmd._write_canonical(tmp_path, {})
    assert mcp_cmd.import_servers(target=tmp_path, harness="opencode", merge=True, json_output=True) == 0
    capsys.readouterr()
    assert "FAKE_SECRET" not in mcp_cmd.canonical_path(tmp_path).read_text()
    assert "keep-live-only" not in mcp_cmd.canonical_path(tmp_path).read_text()
    assert mcp_cmd.sync(target=tmp_path, harness="opencode", write=True, adopt=True, json_output=True) == 0
    capsys.readouterr()
    assert json.loads(path.read_text())["mcp"]["servers"]["docs"] == native
    record = json.loads(mcp_cmd.state_path(tmp_path).read_text())["ownership"]["opencode"]["opencode.json"]["docs"]
    assert record["opencode_native_keys"]["oauth"] == ["clientSecret", "opaque"]
    assert "FAKE_SECRET" not in json.dumps(record)
    before = path.read_bytes(), mcp_cmd.state_path(tmp_path).read_bytes()
    assert mcp_cmd.sync(target=tmp_path, harness="opencode", write=True, json_output=True) == 0
    capsys.readouterr()
    assert before == (path.read_bytes(), mcp_cmd.state_path(tmp_path).read_bytes())


@pytest.mark.parametrize("value", ["FAKE_AUTH", "Bearer ${DOCS_TOKEN}", "Bearer FAKE_AUTH-${DOCS_TOKEN}"])
@pytest.mark.parametrize("keep_secrets", [False, True])
def test_sensitive_header_import_omits_literals_without_changing_keep_secrets(value, keep_secrets):
    raw = {
        **REMOTE,
        "headers": {
            "Authorization": value,
            "Cookie": value,
            "X-API-Key": value,
            "Proxy-Authorization": "${PROXY_TOKEN}",
            "X-Public": "public",
        },
    }
    server, dropped = A.ADAPTERS["opencode"].from_provider("docs", raw, keep_secrets=keep_secrets)
    assert server.headers["Proxy-Authorization"] == {"ref": "PROXY_TOKEN"}
    assert server.headers["X-Public"] == {"literal": "public"}
    for key in ("Authorization", "Cookie", "X-API-Key"):
        if keep_secrets:
            assert server.headers[key] == {"literal": value}
        else:
            assert key not in server.headers and key in dropped
    if keep_secrets:
        assert dropped == []
    else:
        assert value not in json.dumps(A.server_to_dict(server))


@pytest.mark.parametrize("harness", ["opencode", "opencode-user"])
@pytest.mark.parametrize("location", ["flat", "nested"])
@pytest.mark.parametrize("value", ["FAKE_AUTH", "Bearer ${DOCS_TOKEN}", "Bearer FAKE_AUTH-${DOCS_TOKEN}"])
def test_safe_header_import_adoption_keeps_live_value_and_explicit_override(
    tmp_path, monkeypatch, capsys, harness, location, value
):
    set_home(monkeypatch, tmp_path / "home")
    adapter = A.ADAPTERS[harness]
    path = A.resolve_path(adapter, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    native = {**REMOTE, "headers": {"Authorization": value}}
    body = {"servers": {"docs": native}} if location == "nested" else {"docs": native}
    path.write_text(json.dumps({"mcp": body}))
    mcp_cmd._write_canonical(tmp_path, {})
    scope = dict(target=tmp_path, harness=harness, user_scope=harness.endswith("-user"), json_output=True)
    assert mcp_cmd.import_servers(**scope, merge=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["secrets_dropped"] == ["docs.Authorization"]
    assert payload["secrets_demoted"] == []
    assert value not in mcp_cmd.canonical_path(tmp_path).read_text()
    assert mcp_cmd.sync(**scope, write=True, adopt=True) == 0
    capsys.readouterr()
    assert adapter.read_file(path.read_text())["docs"]["headers"] == {"Authorization": value}
    record = json.loads(mcp_cmd.state_path(tmp_path).read_text())["ownership"][harness][adapter.path]["docs"]
    assert record["opencode_native_keys"]["headers"] == ["Authorization"]
    assert value not in json.dumps(record)
    before = path.read_bytes(), mcp_cmd.state_path(tmp_path).read_bytes()
    assert mcp_cmd.sync(**scope, write=True) == 0
    assert json.loads(capsys.readouterr().out)["terminal_state"] == "unchanged"
    assert before == (path.read_bytes(), mcp_cmd.state_path(tmp_path).read_bytes())
    server = A.CanonicalServer(
        name="docs", transport="http", url=REMOTE["url"], headers={"authorization": {"ref": "NEW_TOKEN"}}
    )
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    assert mcp_cmd.sync(**scope, write=True) == 0
    capsys.readouterr()
    assert adapter.read_file(path.read_text())["docs"]["headers"] == {"authorization": "${NEW_TOKEN}"}


def test_import_reports_dropped_credentials_without_claiming_reference(tmp_path, capsys):
    (tmp_path / "opencode.json").write_text(
        json.dumps(
            {
                "mcp": {
                    "docs": {
                        **REMOTE,
                        "password": "FAKE_PASSWORD",
                        "oauth": {"clientSecret": "FAKE_SECRET"},
                        "headers": {"Authorization": "FAKE_AUTH"},
                    }
                }
            }
        )
    )
    assert mcp_cmd.import_servers(target=tmp_path, harness="opencode") == 0
    output = capsys.readouterr().out
    for field in ("password", "oauth.clientSecret", "Authorization"):
        assert f"field dropped from canonical: docs.{field}" in output
    assert "demoted to ref" not in output
    assert "FAKE_" not in output


@pytest.mark.parametrize("value", ["FAKE_VALUE", 42, None, []])
def test_malformed_nested_entry_refuses_overwrite(value):
    text = json.dumps({"mcp": {"servers": {"docs": value}}})
    with pytest.raises(A.OpenCodeLayoutError, match="malformed"):
        A.ADAPTERS["opencode"].write_file(text, {"docs": REMOTE}, set())


@pytest.mark.parametrize("harness", ["opencode", "opencode-user"])
@pytest.mark.parametrize("merge", [False, True])
@pytest.mark.parametrize("value", [5, "FAKE_SECRET", None, [], True])
def test_import_refuses_malformed_nested_entry_without_writes(tmp_path, monkeypatch, capsys, harness, merge, value):
    set_home(monkeypatch, tmp_path / "home")
    path = A.resolve_path(A.ADAPTERS[harness], tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcp": {"servers": {"bad": value, "docs": REMOTE}}}))
    mcp_cmd._write_canonical(tmp_path, {})
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert (
        mcp_cmd.import_servers(
            target=tmp_path, harness=harness, user_scope=harness.endswith("-user"), merge=merge, json_output=True
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "malformed_layout"
    assert payload["errors"] == ["existing OpenCode mcp.servers entry is malformed"]
    assert "FAKE_SECRET" not in json.dumps(payload)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("harness", ["opencode", "opencode-user"])
@pytest.mark.parametrize("identical", [False, True])
def test_duplicate_import_keeps_whole_nested_entry_in_both_scopes(tmp_path, monkeypatch, capsys, harness, identical):
    set_home(monkeypatch, tmp_path / "home")
    path = A.resolve_path(A.ADAPTERS[harness], tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    nested = {**REMOTE, "disabled": True}
    flat = nested if identical else {**REMOTE, "url": "https://old.example/mcp", "headers": {"X-Old": "discard"}}
    path.write_text(json.dumps({"mcp": {"docs": flat, "servers": {"docs": nested}}}))
    before = path.read_bytes()
    mcp_cmd._write_canonical(tmp_path, {})
    assert (
        mcp_cmd.import_servers(
            target=tmp_path, harness=harness, user_scope=harness.endswith("-user"), merge=True, json_output=True
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["warnings"] == ["docs: duplicate flat/nested locations; nested entry takes whole-entry precedence"]
    assert payload["layout_conflicts"] == [{"server": "docs", "locations": ["flat", "nested"]}]
    servers, errors, _ = mcp_cmd.load_canonical(tmp_path)
    assert not errors and set(servers) == {"docs"}
    assert servers["docs"].url == REMOTE["url"] and servers["docs"].headers == {}
    assert servers["docs"].opencode_native == {"disabled": True}
    assert path.read_bytes() == before


@pytest.mark.parametrize("location", ["flat", "nested"])
@pytest.mark.parametrize("explicit_native", [False, True])
def test_explicit_timeout_precedes_native_conversion(location, explicit_native):
    native = {"timeout": {"startup": 10, "catalog": 20, "execution": 30}}
    server = A.CanonicalServer(
        name="docs", transport="http", url=REMOTE["url"], timeout=3, opencode_native=native if explicit_native else {}
    )
    projected = A.opencode_merge_server(server, {**REMOTE, **native}, location=location, native_keys={"timeout": True})
    assert projected["timeout"] == (3000 if location == "flat" else {"startup": 10, "catalog": 3000, "execution": 3000})


def test_sync_v1_timeout_in_nested_layout(tmp_path, capsys):
    path = tmp_path / "opencode.json"
    native = {**REMOTE, "timeout": 40002}
    path.write_text(json.dumps({"mcp": {"servers": {"docs": native}}}))
    mcp_cmd._write_canonical(tmp_path, {})
    assert mcp_cmd.import_servers(target=tmp_path, harness="opencode", merge=True, json_output=True) == 0
    capsys.readouterr()
    assert mcp_cmd.sync(target=tmp_path, harness="opencode", write=True, adopt=True, json_output=True) == 0
    capsys.readouterr()
    actual = A.ADAPTERS["opencode"].read_file(path.read_text())["docs"]
    assert actual["timeout"] == {"catalog": 40002, "execution": 40002}


@pytest.mark.parametrize("location", ["flat", "nested"])
def test_sync_explicit_timeout_overrides_native_startup_conversion(tmp_path, capsys, location):
    path = tmp_path / "opencode.json"
    native = {**REMOTE, "timeout": {"startup": 10}}
    body = {"servers": {"docs": native}} if location == "nested" else {"docs": native}
    path.write_text(json.dumps({"mcp": body}))
    server = A.CanonicalServer(name="docs", transport="http", url=REMOTE["url"], timeout=3)
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    assert mcp_cmd.sync(target=tmp_path, harness="opencode", write=True, adopt=True, json_output=True) == 0
    capsys.readouterr()
    actual = A.ADAPTERS["opencode"].read_file(path.read_text())["docs"]
    assert actual["timeout"] == (3000 if location == "flat" else {"startup": 10, "catalog": 3000, "execution": 3000})


@pytest.mark.parametrize("harness", ["codex", "vscode"])
def test_other_harness_sync_records_final_native_fingerprint(tmp_path, capsys, harness):
    adapter = A.ADAPTERS[harness]
    path = A.resolve_path(adapter, tmp_path)
    server = A.CanonicalServer(
        name="docs", transport="http", url=REMOTE["url"], headers={"X-Ref": {"ref": "DOCS_TOKEN"}}
    )
    mcp_cmd._write_canonical(tmp_path, {"docs": server})
    options = dict(target=tmp_path, harness=harness, write=True, json_output=True)
    assert mcp_cmd.sync(**options) == 0
    capsys.readouterr()
    actual = adapter.read_file(path.read_text())["docs"]
    record = json.loads(mcp_cmd.state_path(tmp_path).read_text())["ownership"][harness][adapter.path]["docs"]
    assert record["projected_fingerprint"] == localio.stable_hash(actual)
    before = path.read_bytes(), mcp_cmd.state_path(tmp_path).read_bytes()
    assert mcp_cmd.sync(**options) == 0
    assert json.loads(capsys.readouterr().out)["terminal_state"] == "unchanged"
    assert before == (path.read_bytes(), mcp_cmd.state_path(tmp_path).read_bytes())
