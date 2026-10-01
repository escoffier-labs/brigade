"""Field semantics and transaction regressions for the additive fidelity report."""

import json
from dataclasses import replace

import pytest

from brigade import mcp_adapters as A, mcp_fidelity as F, mcp_cmd, operator_cmd, projection
from tests._home import set_home


REMOTE = A.CanonicalServer(name="docs", transport="http", url="https://example.invalid/mcp")
STATUSES = {"preserved", "transformed", "unsupported", "intentionally_omitted"}


def _assess(server=REMOTE, harness="codex", *, location="flat", live=None, native_keys=None):
    live = live or {}
    native_keys = native_keys or {}
    if harness.startswith("opencode"):
        projected = A.opencode_merge_server(server, live, location=location, native_keys=native_keys)
    elif harness.startswith("codex"):
        projected = A.codex_merge_server(server, live, A.codex_native_auth(live))
    else:
        projected = A.ADAPTERS[harness].to_provider(server)
    return F.evaluate(
        server,
        harness,
        scope="user" if A.ADAPTERS[harness].user_scope else "project",
        location=location,
        projected=projected,
        live=live,
        native_keys=native_keys,
    )


def _field(row, name):
    return next(f for f in row["fields"] if f["field"] == name)


def test_report_version_counts_order_and_render_are_derived_from_rows():
    partial = _assess(replace(REMOTE, timeout=7, targets=("codex",), description="catalog copy"))
    full = _assess(REMOTE, "opencode")
    report = F.build_report([full, partial], [])
    assert report == F.build_report([partial, full], [])
    assert report["version"] == 1
    assert {t["harness"]: t["state"] for t in report["targets"]} == {"codex": "partial", "opencode": "ok"}
    assert partial["faithful"] is False and full["faithful"] is True
    fields = [f for row in report["servers"] for f in row["fields"]]
    for status in STATUSES:
        assert report["summary"][status] == sum(f["status"] == status for f in fields)
    assert report["summary"]["blocking"] == 0
    assert _field(partial, "targets")["reason"] == "routing_only"
    assert _field(partial, "enabled")["reason"] == "catalog_membership"
    assert _field(partial, "description")["reason"] == "catalog_only"
    for row in report["servers"]:
        for f in row["fields"]:
            assert f["status"] in STATUSES
            if f["status"] != "preserved":
                assert any(row["harness"] in line and f["reason"] in line for line in F.render_lines(report))


@pytest.mark.parametrize("harness", ["claude", "cursor", "vscode", "grok-user"])
def test_other_adapters_are_explicitly_unevaluated(harness):
    row = _assess(harness=harness)
    assert row["evaluated"] is False and row["faithful"] is None
    assert _field(row, "projection")["reason"] == "adapter_unevaluated"
    assert F.build_report([row], [])["targets"][0]["state"] == "unevaluated"


@pytest.mark.parametrize("harness", ["codex", "codex-user", "opencode", "opencode-user"])
@pytest.mark.parametrize("transport", ["stdio", "http", "sse"])
def test_transport_and_timeout_describe_emission(harness, transport):
    server = replace(REMOTE, transport=transport, command="fake-command", args=("fake-arg",), timeout=7)
    row = _assess(server, harness)
    timeout = _field(row, "timeout")
    if harness.startswith("codex") and server.is_remote:
        assert timeout["status"] == "unsupported" and timeout["blocking"] is False
        assert timeout["reason"] == "remote_timeout_unsupported"
    else:
        assert timeout["status"] == ("transformed" if harness.startswith("opencode") else "preserved")
        assert timeout["units"] == ("milliseconds" if harness.startswith("opencode") else "seconds")
    if harness.startswith("opencode") and transport == "sse":
        assert _field(row, "transport")["status"] == "unsupported"
        assert _field(row, "transport")["reason"] == "sse_distinction_unsupported"
    if not server.is_remote and harness.startswith("opencode"):
        assert _field(row, "command")["status"] == "transformed"


@pytest.mark.parametrize("harness", ["codex", "claude", "opencode"])
@pytest.mark.parametrize(
    "auth",
    [
        {"headers": {"FAKE_HEADER": {"literal": "FAKE_SECRET"}}},
        {"http_headers": {"FAKE_HEADER": "FAKE_SECRET"}},
        {"env_http_headers": {"FAKE_HEADER": "FAKE_REF"}},
        {"bearer_token_env_var": "FAKE_REF"},
        {"opencode_native": {"oauth": False}},
    ],
)
def test_explicit_remote_auth_on_stdio_blocks_even_unevaluated(harness, auth):
    row = _assess(replace(REMOTE, transport="stdio", command="fake-command", **auth), harness)
    assert any(f["blocking"] and f["reason"] == "auth_transport_incompatible" for f in row["fields"])
    assert F.build_report([row], [])["targets"][0]["state"] == "blocked"


@pytest.mark.parametrize("harness", ["codex", "claude"])
@pytest.mark.parametrize(
    "metadata,blocked",
    [
        ({"oauth": False}, True),
        ({"enabled": False}, True),
        ({"disabled": True}, True),
        ({"enabled": True}, False),
        ({"disabled": False}, False),
        ({"enabled": True, "disabled": True}, False),
        ({"enabled": False, "disabled": False}, True),
    ],
)
def test_cross_family_security_and_effective_activation(harness, metadata, blocked):
    row = _assess(replace(REMOTE, opencode_native={**metadata, "timeout": 7001}), harness)
    assert any(f["blocking"] for f in row["fields"]) is blocked
    assert _field(row, "opencode_native.timeout")["status"] == "unsupported"
    for key in metadata:
        assert _field(row, f"opencode_native.{key}")["status"] in STATUSES


@pytest.mark.parametrize("location", ["flat", "nested"])
def test_native_timeout_activation_and_startup(location):
    server = replace(REMOTE, timeout=7, opencode_native={"enabled": False})
    live = {"type": "remote", "timeout": {"startup": 30001, "catalog": 1000, "execution": 1000}}
    row = _assess(server, "opencode", location=location, live=live, native_keys={"timeout": ["startup"]})
    assert _field(row, "opencode_native.enabled")["destination"] == ("enabled" if location == "flat" else "disabled")
    if location == "nested":
        assert _field(row, "native.timeout.startup")["status"] == "preserved"
    else:
        assert _field(row, "native.timeout.startup")["status"] == "unsupported"
    native_row = _assess(replace(REMOTE, opencode_native={"timeout": 7001}), "opencode", location=location)
    assert _field(native_row, "opencode_native.timeout")["reason"] == (
        "native_timeout_to_nested" if location == "nested" else "native_timeout_preserved"
    )


def test_auth_precedence_retention_conversion_and_secret_free_output():
    server = replace(
        REMOTE,
        headers={"FAKE_HEADER": {"literal": "FAKE_GENERIC"}, "FAKE_OTHER": {"ref": "FAKE_REFERENCE"}},
        http_headers={"FAKE_HEADER": "FAKE_STATIC"},
        bearer_token_env_var="FAKE_TOKEN",
    )
    live = {"env_http_headers": {"FAKE_RETAINED": "FAKE_LIVE_REF"}}
    row = _assess(server, live=live, native_keys={"env_http_headers": ["FAKE_RETAINED"]})
    assert _field(row, "headers.shadowed")["reason"] == "native_auth_precedence"
    assert _field(row, "headers")["status"] == "transformed"
    assert _field(row, "native.env_http_headers")["reason"] == "retained_native_auth"
    report = F.build_report([row], [])
    rendered = json.dumps(report) + "\n".join(F.render_lines(report))
    for sentinel in [
        "FAKE_HEADER",
        "FAKE_GENERIC",
        "FAKE_OTHER",
        "FAKE_REFERENCE",
        "FAKE_STATIC",
        "FAKE_TOKEN",
        "FAKE_RETAINED",
        "FAKE_LIVE_REF",
        REMOTE.url,
    ]:
        assert sentinel not in rendered


def test_explicit_native_replacement_and_transport_cleanup_are_intentional():
    live = {"http_headers": {"Authorization": "FAKE_LIVE_SECRET"}}
    row = _assess(replace(REMOTE, http_headers={"Authorization": "FAKE_REPLACEMENT"}), live=live)
    assert _field(row, "native.http_headers")["reason"] == "explicit_canonical_replacement"
    row = _assess(
        replace(REMOTE, transport="stdio", command="fake-command"),
        live=live,
        native_keys={"http_headers": ["Authorization"]},
    )
    assert _field(row, "native.http_headers")["reason"] == "transport_cleanup"
    assert not any(f["blocking"] for f in row["fields"])


@pytest.mark.parametrize(
    "reason",
    [
        "native_auth_collision",
        "auth_interpolation_unsupported",
        "native_timeout_flat_unsupported",
        "projection_invalid",
    ],
)
def test_refusals_use_stable_blocking_reasons(reason):
    row = F.evaluate(REMOTE, "codex", scope="project", error_code=reason)
    assert row["faithful"] is False
    assert any(f["reason"] == reason and f["blocking"] for f in row["fields"])


# Command assessment is shared by plan, sync and doctor, but never mutates during
# diagnostics. Keep these assertions separate from serializer unit contracts.


def _seed(target, servers):
    mcp_cmd._write_canonical(target, {s.name: s for s in servers})


def _payload(capsys):
    return json.loads(capsys.readouterr().out)


def _snapshot(target):
    return {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()}


def _targets(monkeypatch, *harnesses):
    monkeypatch.setattr(mcp_cmd, "_configured_harnesses", lambda _target: set(harnesses))


def test_committed_remote_timeout_has_partial_fidelity(tmp_path, capsys):
    _seed(tmp_path, [replace(REMOTE, timeout=7)])
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, json_output=True) == 0
    payload = _payload(capsys)
    assert payload["projection"]["terminal_state"] == "committed"
    row = payload["fidelity"]["servers"][0]
    assert row["faithful"] is False
    assert _field(row, "timeout")["status"] == "unsupported"
    assert _field(row, "timeout")["blocking"] is False
    assert "timeout" not in A.ADAPTERS["codex"].read_file((tmp_path / ".codex/config.toml").read_text())["docs"]


@pytest.mark.parametrize("harness", ["codex", "claude"])
@pytest.mark.parametrize("options", [{}, {"force": True, "adopt": True}])
@pytest.mark.parametrize("loss", ["oauth", "disabled", "stdio_auth"])
def test_security_loss_is_atomic_with_safe_sibling(tmp_path, monkeypatch, capsys, harness, options, loss):
    _targets(monkeypatch, harness, "opencode")
    unsafe = replace(REMOTE, opencode_native={"oauth": False} if loss == "oauth" else {"disabled": True})
    if loss == "stdio_auth":
        unsafe = replace(
            REMOTE,
            transport="stdio",
            command="FAKE_COMMAND",
            headers={"FAKE_HEADER_NAME": {"ref": "FAKE_CREDENTIAL_NAME"}},
        )
    safe = replace(REMOTE, name="safe", targets=("opencode",))
    _seed(tmp_path, [unsafe, safe])
    before = _snapshot(tmp_path)
    assert mcp_cmd.sync(target=tmp_path, write=True, json_output=True, **options) == 2
    payload = _payload(capsys)
    assert _snapshot(tmp_path) == before
    assert payload["items"] and payload["counts"]["conflict"]
    assert {r["harness"] for r in payload["fidelity"]["targets"]} == {harness, "opencode"}
    assert any(r["server"] == "safe" for r in payload["fidelity"]["servers"])
    assert next(t for t in payload["fidelity"]["targets"] if t["harness"] == harness)["state"] == "blocked"
    text_report = json.dumps(payload["fidelity"])
    for sentinel in ("FAKE_COMMAND", "FAKE_HEADER_NAME", "FAKE_CREDENTIAL_NAME", REMOTE.url):
        assert sentinel not in text_report


def test_native_auth_conflict_keeps_partial_write_rc_one(tmp_path, monkeypatch, capsys):
    _targets(monkeypatch, "codex", "opencode")
    native = tmp_path / ".codex/config.toml"
    native.parent.mkdir()
    native.write_text(
        '[mcp_servers.docs]\nurl="https://example.invalid/mcp"\n[mcp_servers.docs.http_headers]\nFAKE_AUTH_HEADER="FAKE_NATIVE_SECRET"\n'
    )
    original = replace(REMOTE, targets=("codex",), headers={"FAKE_AUTH_HEADER": {"ref": "FAKE_ORIGINAL"}})
    _seed(tmp_path, [original])
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, adopt=True, json_output=True) == 0
    _payload(capsys)
    before_native = native.read_bytes()
    changed = replace(original, headers={"FAKE_AUTH_HEADER": {"ref": "FAKE_CHANGED"}})
    _seed(tmp_path, [changed, replace(REMOTE, name="safe", targets=("opencode",))])
    assert mcp_cmd.sync(target=tmp_path, write=True, json_output=True) == 1
    payload = _payload(capsys)
    assert payload["projection"]["terminal_state"] == "committed"
    assert native.read_bytes() == before_native
    assert (tmp_path / "opencode.json").is_file()
    row = next(r for r in payload["fidelity"]["servers"] if r["harness"] == "codex")
    assert any(f["reason"] == "native_auth_collision" and f["blocking"] for f in row["fields"])
    assert "FAKE_AUTH_HEADER" not in json.dumps(row)


@pytest.mark.parametrize("call", ["plan", "sync", "doctor"])
def test_projection_refusal_has_value_free_report_and_text(tmp_path, monkeypatch, capsys, call):
    _targets(monkeypatch, "codex", "opencode")
    server = replace(REMOTE, headers={"FAKE_HEADER_NAME": {"literal": "Bearer ${FAKE_REF}/FAKE_SECRET"}})
    _seed(tmp_path, [server])
    before = _snapshot(tmp_path)
    fn = getattr(mcp_cmd, call)
    assert fn(target=tmp_path, json_output=True) == (1 if call == "doctor" else 2)
    report = _payload(capsys)["fidelity"]
    assert any(f["reason"] == "auth_interpolation_unsupported" for r in report["servers"] for f in r["fields"])
    assert fn(target=tmp_path) == (1 if call == "doctor" else 2)
    text = capsys.readouterr().out
    assert "auth_interpolation_unsupported" in text
    for sentinel in ("FAKE_HEADER_NAME", "FAKE_REF", "FAKE_SECRET", REMOTE.url):
        assert sentinel not in json.dumps(report) + text
    assert before == _snapshot(tmp_path)


@pytest.mark.parametrize("call", ["plan", "sync", "doctor"])
def test_exclusions_respect_filters_without_action_count_changes(tmp_path, monkeypatch, capsys, call):
    _targets(monkeypatch, "codex", "opencode")
    _seed(
        tmp_path,
        [
            replace(REMOTE, enabled=False),
            replace(REMOTE, name="routed", targets=("claude",)),
            replace(REMOTE, name="active"),
        ],
    )
    fn = getattr(mcp_cmd, call)
    assert fn(target=tmp_path, json_output=True) == 0
    payload = _payload(capsys)
    assert len(payload["fidelity"]["excluded"]) == 4
    assert len(payload["fidelity"]["servers"]) == 2
    if call != "doctor":
        assert sum(payload["counts"].values()) == len(payload["items"]) == 2
        assert fn(target=tmp_path, name="docs", json_output=True) == 0
        filtered = _payload(capsys)
        assert len(filtered["fidelity"]["excluded"]) == 2
        assert not filtered["items"]


@pytest.mark.parametrize("harness", ["codex", "opencode", "claude"])
@pytest.mark.parametrize("missing", [True, False])
def test_doctor_read_only_missing_and_foreign_native(tmp_path, monkeypatch, capsys, harness, missing):
    _targets(monkeypatch, harness)
    _seed(tmp_path, [REMOTE])
    path = A.resolve_path(A.ADAPTERS[harness], tmp_path)
    if not missing:
        path.parent.mkdir(parents=True, exist_ok=True)
        foreign = replace(REMOTE, url="https://foreign.example.invalid/mcp")
        path.write_text(A.ADAPTERS[harness].write_file(None, {"docs": A.ADAPTERS[harness].to_provider(foreign)}, set()))
    before = _snapshot(tmp_path)
    outputs = []
    for _ in range(2):
        assert mcp_cmd.doctor(target=tmp_path, json_output=True) == 0
        outputs.append(_payload(capsys))
    assert outputs[0] == outputs[1]
    assert outputs[0]["fidelity"]["targets"][0]["state"] == ("unevaluated" if harness == "claude" else "ok")
    assert before == _snapshot(tmp_path)


@pytest.mark.parametrize(
    "harness,text", [("opencode", '{"mcp":{"servers":[]}}'), ("codex", "[mcp_servers"), ("claude", '{"mcpServers":[]}')]
)
@pytest.mark.parametrize("call", ["doctor", "sync", "plan"])
def test_malformed_native_is_blocked_continues_other_targets(tmp_path, monkeypatch, capsys, harness, text, call):
    sibling = "opencode" if harness != "opencode" else "codex"
    _targets(monkeypatch, harness, sibling)
    _seed(tmp_path, [REMOTE])
    path = A.resolve_path(A.ADAPTERS[harness], tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    before = _snapshot(tmp_path)
    fn = getattr(mcp_cmd, call)
    kwargs = {"write": True} if call == "sync" else {}
    assert fn(target=tmp_path, json_output=True, **kwargs) == (1 if call == "doctor" else 2)
    report = _payload(capsys)["fidelity"]
    assert {r["harness"] for r in report["targets"]} == {harness, sibling}
    bad = next(r for r in report["servers"] if r["harness"] == harness)
    assert bad["evaluated"] is False and bad["faithful"] is None
    assert _field(bad, "projection")["reason"] == "native_config_malformed"
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("input_id", [[], {}, ["FAKE_SECRET"], {"FAKE_HEADER": "FAKE_SECRET"}, None, 7, True])
@pytest.mark.parametrize("call", ["plan", "sync", "doctor"])
@pytest.mark.parametrize("empty_catalog", [True, False])
def test_malformed_vscode_input_ids_refuse_without_mutation(
    tmp_path, monkeypatch, capsys, input_id, call, empty_catalog
):
    _targets(monkeypatch, "vscode", "opencode")
    _seed(
        tmp_path,
        []
        if empty_catalog
        else [replace(REMOTE, targets=("vscode",)), replace(REMOTE, name="safe", targets=("opencode",))],
    )
    path = A.resolve_path(A.ADAPTERS["vscode"], tmp_path)
    path.parent.mkdir()
    path.write_text(json.dumps({"servers": {}, "inputs": [{"id": input_id, "description": "FAKE_DESCRIPTION"}]}))
    before = _snapshot(tmp_path)
    expected_rc = 1 if call == "doctor" else 2
    for write in [False, True] if call == "sync" else [False]:
        kwargs = {"write": write, "force": True, "adopt": True} if call == "sync" else {}
        assert getattr(mcp_cmd, call)(target=tmp_path, json_output=True, **kwargs) == expected_rc
        payload = _payload(capsys)
        report = payload["fidelity"]
        assert {r["harness"] for r in report["targets"]} == {"vscode", "opencode"}
        assert next(r for r in report["targets"] if r["harness"] == "vscode")["state"] == "blocked"
        bad = next(r for r in report["servers"] if r["harness"] == "vscode")
        assert bad["server"] == "*" and bad["scope"] == "project"
        assert _field(bad, "projection")["reason"] == "native_config_malformed"
        if call == "doctor":
            assert payload["valid"] is False
            assert {"severity": "error", "message": "vscode/*: projection native_config_malformed"} in payload["issues"]
        else:
            assert payload["errors"] == [f"{path}: native_config_malformed"]
            assert any(i["harness"] == "vscode" and i["file"] == ".vscode/mcp.json" for i in payload["items"])
        if not empty_catalog:
            assert any(r["harness"] == "opencode" and r["server"] == "safe" for r in report["servers"])
        assert _snapshot(tmp_path) == before
        assert not (tmp_path / mcp_cmd.STATE_REL).exists()
        assert not list(tmp_path.rglob("journal.json"))
        assert getattr(mcp_cmd, call)(target=tmp_path, **kwargs) == expected_rc
        rendered = capsys.readouterr().out
        assert "vscode" in rendered and "native_config_malformed" in rendered
        for sentinel in ("FAKE_SECRET", "FAKE_HEADER", "FAKE_DESCRIPTION"):
            assert sentinel not in json.dumps(payload) + rendered
        assert _snapshot(tmp_path) == before


def test_valid_vscode_inputs_preserve_read_only_bytes_and_sync_entries(tmp_path, monkeypatch, capsys):
    _targets(monkeypatch, "vscode")
    _seed(tmp_path, [replace(REMOTE, headers={"Authorization": {"ref": "FAKE_TOKEN"}})])
    path = A.resolve_path(A.ADAPTERS["vscode"], tmp_path)
    path.parent.mkdir()
    inputs = [{"id": "FAKE_TOKEN", "type": "promptString", "description": "FAKE_DESCRIPTION", "password": False}]
    native = {"servers": {}, "inputs": inputs, "FAKE_SETTING": {"nested": [1, 2]}}
    path.write_text(json.dumps(native, indent=4) + "\n")
    before = _snapshot(tmp_path)
    for call in ("plan", "sync", "doctor"):
        assert getattr(mcp_cmd, call)(target=tmp_path, json_output=True) == 0
        _payload(capsys)
        assert _snapshot(tmp_path) == before
    assert mcp_cmd.sync(target=tmp_path, harness="vscode", write=True, json_output=True) == 0
    _payload(capsys)
    after = json.loads(path.read_text())
    assert after["inputs"] == inputs
    assert after["FAKE_SETTING"] == native["FAKE_SETTING"]
    assert after["servers"]["docs"]["headers"]["Authorization"] == "${input:FAKE_TOKEN}"
    synced = _snapshot(tmp_path)
    assert mcp_cmd.sync(target=tmp_path, harness="vscode", write=True, json_output=True) == 0
    _payload(capsys)
    assert _snapshot(tmp_path) == synced


@pytest.mark.parametrize("harness", ["codex-user", "opencode-user"])
def test_user_scope_and_layout_reports(tmp_path, monkeypatch, capsys, harness):
    set_home(monkeypatch, tmp_path / "home")
    server = replace(REMOTE, timeout=7, targets=(harness,))
    _seed(tmp_path, [server])
    path = A.resolve_path(A.ADAPTERS[harness], tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if harness == "opencode-user":
        path.write_text('{"mcp":{"servers":{}}}')
    before = _snapshot(tmp_path)
    assert mcp_cmd.plan(target=tmp_path, harness=harness, user_scope=True, json_output=True) == 0
    report = _payload(capsys)["fidelity"]
    row = report["servers"][0]
    assert row["scope"] == "user"
    if harness == "opencode-user":
        assert row["layout"] == "nested"
    assert _snapshot(tmp_path) == before
    assert mcp_cmd.sync(target=tmp_path, harness=harness, user_scope=True, json_output=True) == 0
    assert _payload(capsys)["fidelity"] == report
    assert _snapshot(tmp_path) == before


def test_doctor_default_project_targets_only(tmp_path, monkeypatch, capsys):
    set_home(monkeypatch, tmp_path / "home")
    _targets(monkeypatch, "codex", "opencode")
    _seed(tmp_path, [REMOTE])
    user = A.resolve_path(A.ADAPTERS["codex-user"], tmp_path)
    user.parent.mkdir(parents=True)
    user.write_text("FAKE_INVALID_CONFIG")
    assert mcp_cmd.doctor(target=tmp_path, json_output=True) == 0
    assert {r["harness"] for r in _payload(capsys)["fidelity"]["targets"]} == {"codex", "opencode"}


@pytest.mark.parametrize("boundary", ["commit:0:before", "commit:0:after"])
def test_transaction_restore_keeps_prewrite_fidelity(tmp_path, capsys, boundary):
    _seed(tmp_path, [replace(REMOTE, timeout=7)])
    assert mcp_cmd.sync(target=tmp_path, harness="codex", json_output=True) == 0
    report = _payload(capsys)["fidelity"]
    planned = mcp_cmd.build_sync_plan(target=tmp_path, harness="codex")
    assert planned.projection is not None
    before = {
        m.destination: m.destination.read_bytes() if m.destination.exists() else None
        for m in planned.projection.mutations
    }
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, inject_failure=boundary, json_output=True) == 2
    payload = _payload(capsys)
    assert payload["terminal_state"] == "restored"
    assert payload["fidelity"] == report
    assert before == {p: p.read_bytes() if p.exists() else None for p in before}


@pytest.mark.parametrize("error", [projection.DriftError, projection.OverlapBlockedError])
def test_transaction_early_error_keeps_report(tmp_path, monkeypatch, capsys, error):
    _seed(tmp_path, [REMOTE])
    assert mcp_cmd.sync(target=tmp_path, harness="codex", json_output=True) == 0
    report = _payload(capsys)["fidelity"]

    def fail(*_args, **_kwargs):
        raise error("fixture refusal")

    monkeypatch.setattr(projection, "execute", fail)
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, json_output=True) == 2
    assert _payload(capsys)["fidelity"] == report


@pytest.mark.parametrize("call", ["plan", "sync", "doctor"])
def test_catalog_error_has_empty_report(tmp_path, capsys, call):
    assert getattr(mcp_cmd, call)(target=tmp_path, json_output=True) == (1 if call == "doctor" else 2)
    assert _payload(capsys)["fidelity"]["version"] == 1


@pytest.mark.parametrize("call,write", [("plan", False), ("sync", False), ("sync", True)])
def test_hermes_yaml_uses_existing_parser_and_preserves_siblings(tmp_path, monkeypatch, capsys, call, write):
    set_home(monkeypatch, tmp_path / "home")
    _seed(tmp_path, [REMOTE])
    adapter = A.ADAPTERS["hermes"]
    path = A.resolve_path(adapter, tmp_path)
    path.parent.mkdir(parents=True)
    text = "model: fake-model\nmcp_servers:\n  keep:\n    command: fake-command\n    args:\n      - fake-arg\n"
    path.write_text(text)
    assert adapter.read_file(text) == {"keep": {"command": "fake-command", "args": ["fake-arg"]}}
    before = _snapshot(tmp_path)
    kwargs = {"write": write} if call == "sync" else {}
    assert getattr(mcp_cmd, call)(target=tmp_path, harness="hermes", user_scope=True, json_output=True, **kwargs) == 0
    payload = _payload(capsys)
    row = payload["fidelity"]["servers"][0]
    assert row["harness"] == "hermes" and row["scope"] == "user"
    assert row["evaluated"] is False and row["faithful"] is None
    assert payload["fidelity"]["targets"][0]["state"] == "unevaluated"
    if write:
        assert payload["projection"]["terminal_state"] == "committed"
        assert path.read_text().startswith("model: fake-model\n")
        live = adapter.read_file(path.read_text())
        assert live["keep"] == {"command": "fake-command", "args": ["fake-arg"]}
        assert live["docs"]["url"] == REMOTE.url
    else:
        assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("call", ["plan", "sync", "doctor"])
@pytest.mark.parametrize("harness", ["codex", "opencode"])
def test_native_provenance_refusals_are_controlled(tmp_path, monkeypatch, capsys, call, harness):
    _targets(monkeypatch, harness)
    if harness == "codex":
        _seed(tmp_path, [REMOTE])
        path = A.resolve_path(A.ADAPTERS[harness], tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text('[mcp_servers.docs]\nurl="https://example.invalid/mcp"\nhttp_headers={ FAKE_HEADER=123456 }\n')
        reason = "projection_invalid"
    else:
        _seed(
            tmp_path,
            [
                replace(
                    REMOTE, http_headers={"FAKE_HEADER": "FAKE_SECRET"}, env_http_headers={"FAKE_HEADER": "FAKE_REF"}
                )
            ],
        )
        reason = "native_auth_unrepresentable"
    before = _snapshot(tmp_path)
    kwargs = {"write": True} if call == "sync" else {}
    assert getattr(mcp_cmd, call)(target=tmp_path, json_output=True, **kwargs) == (1 if call == "doctor" else 2)
    payload = _payload(capsys)
    row = payload["fidelity"]["servers"][0]
    assert any(f["blocking"] and f["reason"] == reason for f in row["fields"])
    assert _snapshot(tmp_path) == before
    assert getattr(mcp_cmd, call)(target=tmp_path, **kwargs) == (1 if call == "doctor" else 2)
    text = capsys.readouterr().out
    assert reason in text
    for sentinel in ("FAKE_HEADER", "FAKE_SECRET", "FAKE_REF", "123456"):
        assert sentinel not in json.dumps(payload["fidelity"]) + text


def test_generic_codex_auth_reports_actual_distinct_destinations():
    server = replace(
        REMOTE,
        headers={
            "FAKE_STATIC_HEADER": {"literal": "FAKE_SECRET"},
            "FAKE_COMPLETE_HEADER": {"ref": "FAKE_COMPLETE_REF"},
            "Authorization": {"literal": "Bearer ${FAKE_TOKEN_REF}"},
        },
    )
    row = _assess(server)
    converted = [f for f in row["fields"] if f["field"] == "headers"]
    assert {f["destination"] for f in converted} == {"http_headers", "env_http_headers", "bearer_token_env_var"}
    assert {f["reason"] for f in converted} == {
        "generic_static_to_http_headers",
        "complete_header_reference",
        "bearer_token_reference",
    }
    assert all(f["status"] == "transformed" for f in converted)


def test_cross_form_native_auth_replacement_is_reported():
    row = _assess(
        replace(REMOTE, bearer_token_env_var="FAKE_TOKEN"), live={"http_headers": {"Authorization": "FAKE_SECRET"}}
    )
    assert _field(row, "native.http_headers")["reason"] == "explicit_canonical_replacement"


def test_retained_native_integer_timeout_to_nested_is_transformed():
    row = _assess(REMOTE, "opencode", location="nested", live={"timeout": 7001}, native_keys={"timeout": True})
    timeout = _field(row, "native.timeout")
    assert timeout["status"] == "transformed"
    assert timeout["reason"] == "native_timeout_to_nested"


def test_doctor_preserves_secret_warnings_and_fidelity_omits_credential_names(tmp_path, monkeypatch, capsys):
    _targets(monkeypatch, "codex")
    _seed(
        tmp_path,
        [
            replace(
                REMOTE,
                headers={"FAKE_SECRET_HEADER": {"literal": "FAKE_SECRET_VALUE"}},
                http_headers={"FAKE_NATIVE_SECRET_HEADER": "FAKE_NATIVE_VALUE"},
                env={"FAKE_SECRET_ENV": {"literal": "FAKE_ENV_VALUE"}},
            )
        ],
    )
    assert mcp_cmd.doctor(target=tmp_path, json_output=True) == 0
    payload = _payload(capsys)
    assert any("inlined secret" in i["message"] for i in payload["issues"])
    assert mcp_cmd.doctor(target=tmp_path) == 0
    text = capsys.readouterr().out
    expected = [
        message
        for severity, message in A.validate_server(next(iter(mcp_cmd.load_canonical(tmp_path)[0].values())))
        if severity == "warn"
    ]
    assert all(any(i["message"] == message for i in payload["issues"]) for message in expected)
    assert all(message in text for message in expected)
    rendered = "\n".join(line for line in text.splitlines() if line.startswith("fidelity")) + json.dumps(
        payload["fidelity"]
    )
    for sentinel in (
        "FAKE_SECRET_HEADER",
        "FAKE_SECRET_VALUE",
        "FAKE_NATIVE_SECRET_HEADER",
        "FAKE_NATIVE_VALUE",
        "FAKE_SECRET_ENV",
        "FAKE_ENV_VALUE",
    ):
        assert sentinel not in rendered


@pytest.mark.parametrize("text", ["", " \n\t"])
@pytest.mark.parametrize(
    "harness", ["claude", "cursor", "vscode", "claude-user", "antigravity", "openclaw", "cursor-user", "kimi-user"]
)
def test_json_blank_configs_preserve_adapter_acceptance(tmp_path, monkeypatch, capsys, harness, text):
    set_home(monkeypatch, tmp_path / "home")
    _targets(monkeypatch, harness)
    _seed(tmp_path, [REMOTE])
    adapter = A.ADAPTERS[harness]
    path = A.resolve_path(adapter, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    before = _snapshot(tmp_path)
    strict = harness in {"cursor-user", "kimi-user"}
    if not adapter.user_scope:
        assert mcp_cmd.doctor(target=tmp_path, json_output=True) == 0
        _payload(capsys)
        assert _snapshot(tmp_path) == before
    assert mcp_cmd.sync(
        target=tmp_path, harness=harness, user_scope=adapter.user_scope, write=True, json_output=True
    ) == (2 if strict else 0)
    payload = _payload(capsys)
    if strict:
        assert payload["errors"] == [f"{path}: existing JSON configuration is invalid; refusing to overwrite"]
        assert payload["fidelity"]["servers"][0]["fields"][0]["reason"] == "native_config_malformed"
        assert _snapshot(tmp_path) == before
    else:
        raw = adapter.read_file(path.read_text())["docs"]
        assert adapter.from_provider("docs", raw)[0].url == REMOTE.url


def test_operator_native_auth_collision_warns_and_writes_safe_sibling(tmp_path, monkeypatch, capsys):
    _targets(monkeypatch, "codex", "opencode")
    path = tmp_path / ".codex/config.toml"
    path.parent.mkdir()
    path.write_text(
        '[mcp_servers.docs]\nurl="https://example.invalid/mcp"\n[mcp_servers.docs.http_headers]\nFAKE_AUTH_HEADER="FAKE_SECRET"\n'
    )
    original = replace(REMOTE, targets=("codex",), headers={"FAKE_AUTH_HEADER": {"ref": "FAKE_ORIGINAL"}})
    _seed(tmp_path, [original])
    assert mcp_cmd.sync(target=tmp_path, harness="codex", write=True, adopt=True, json_output=True) == 0
    _payload(capsys)
    before_native = path.read_bytes()
    _seed(
        tmp_path,
        [
            replace(original, headers={"FAKE_AUTH_HEADER": {"ref": "FAKE_CHANGED"}}),
            replace(REMOTE, name="safe", targets=("opencode",)),
        ],
    )
    before = _snapshot(tmp_path)
    assert mcp_cmd.doctor(target=tmp_path, json_output=True) == 0
    doctor = _payload(capsys)
    assert any(i["severity"] == "warn" and "native_auth_collision" in i["message"] for i in doctor["issues"])
    assert next(t for t in doctor["fidelity"]["targets"] if t["harness"] == "codex")["state"] == "blocked"
    assert _snapshot(tmp_path) == before
    assert operator_cmd.sync_mcp(target=tmp_path, write=True, json_output=True) == 1
    payload = _payload(capsys)
    assert payload["doctor"]["valid"] is True
    assert payload["sync"]["projection"]["terminal_state"] == "committed"
    assert path.read_bytes() == before_native
    assert A.ADAPTERS["opencode"].read_file((tmp_path / "opencode.json").read_text())["safe"]["url"] == REMOTE.url


@pytest.mark.parametrize(
    "message,reason",
    [
        ("OpenCode server would overwrite an existing setting/container", "native_setting_collision"),
        ("unsupported native TOML option type; refusing to overwrite", "projection_invalid"),
        ("existing JSON configuration is invalid; refusing to overwrite", "native_config_malformed"),
        ("existing JSON configuration must be an object; refusing to overwrite", "native_config_malformed"),
        ("existing mcpServers section must be an object; refusing to overwrite", "native_config_malformed"),
        ("existing OpenCode mcp.servers is malformed", "native_config_malformed"),
    ],
)
def test_serializer_error_categories_are_specific(message, reason):
    assert F.projection_error_code(ValueError(message)) == reason


@pytest.mark.parametrize("call", ["plan", "sync"])
@pytest.mark.parametrize(
    "loss,hint,reason",
    [
        ("interpolation", "use native header or bearer environment references", "auth_interpolation_unsupported"),
        ("native_auth", "scope the server to Codex", "native_auth_unrepresentable"),
    ],
)
def test_known_serializer_refusals_keep_safe_hints(tmp_path, monkeypatch, capsys, call, loss, hint, reason):
    harness = "codex" if loss == "interpolation" else "claude"
    _targets(monkeypatch, harness)
    server = (
        replace(REMOTE, headers={"FAKE_HEADER": {"literal": "Bearer ${FAKE_REF}/FAKE_SECRET"}})
        if loss == "interpolation"
        else replace(REMOTE, http_headers={"FAKE_HEADER": "FAKE_SECRET"}, env_http_headers={"FAKE_HEADER": "FAKE_REF"})
    )
    _seed(tmp_path, [server])
    assert getattr(mcp_cmd, call)(target=tmp_path, json_output=True) == 2
    payload = _payload(capsys)
    assert any(error.startswith("docs: ") and hint in error for error in payload["errors"])
    assert reason in json.dumps(payload["fidelity"])
    assert getattr(mcp_cmd, call)(target=tmp_path) == 2
    text = capsys.readouterr().out
    assert hint in text and reason in text
    for sentinel in ("FAKE_HEADER", "FAKE_REF", "FAKE_SECRET", REMOTE.url):
        assert sentinel not in json.dumps(payload["fidelity"]) + text


def test_blocking_loss_detail_names_server_and_deduplicates_reasons(tmp_path, capsys):
    _seed(
        tmp_path,
        [
            replace(
                REMOTE,
                transport="stdio",
                command="fake-command",
                headers={"FAKE_HEADER": {"ref": "FAKE_REF"}},
                http_headers={"FAKE_NATIVE_HEADER": "FAKE_SECRET"},
            )
        ],
    )
    assert mcp_cmd.plan(target=tmp_path, harness="codex", json_output=True) == 2
    payload = _payload(capsys)
    assert payload["errors"] == ["docs: auth_transport_incompatible"]


@pytest.mark.parametrize("call", ["plan", "sync"])
def test_unknown_serializer_refusal_is_value_free(tmp_path, monkeypatch, capsys, call):
    _seed(tmp_path, [REMOTE])

    def fail(*_args, **_kwargs):
        raise ValueError("FAKE_HEADER FAKE_SECRET would overwrite an existing setting/container")

    monkeypatch.setattr(mcp_cmd, "_project_server", fail)
    assert getattr(mcp_cmd, call)(target=tmp_path, harness="codex", json_output=True) == 2
    payload = _payload(capsys)
    assert payload["errors"] == ["docs: projection_invalid"]
    assert "FAKE_SECRET" not in json.dumps(payload)


def test_unsupported_native_toml_value_keeps_safe_refusal_and_category():
    with pytest.raises(ValueError) as refused:
        A._native_toml_value({"FAKE_OPTION": {"FAKE_SECRET"}})
    assert F.projection_error_code(refused.value) == "projection_invalid"
    assert F.projection_error_message(refused.value) == "unsupported native TOML option type; refusing to overwrite"


@pytest.mark.parametrize("text", ["[mcp_servers\nFAKE_SECRET", 'FAKE_SECRET = "unterminated'])
@pytest.mark.parametrize("call", ["plan", "sync", "doctor"])
def test_malformed_toml_content_never_appears_in_new_diagnostics(tmp_path, monkeypatch, capsys, text, call):
    _targets(monkeypatch, "codex")
    _seed(tmp_path, [REMOTE])
    path = A.resolve_path(A.ADAPTERS["codex"], tmp_path)
    path.parent.mkdir()
    path.write_text(text)
    assert getattr(mcp_cmd, call)(target=tmp_path, json_output=True) == (1 if call == "doctor" else 2)
    assert "FAKE_SECRET" not in json.dumps(_payload(capsys))
    assert getattr(mcp_cmd, call)(target=tmp_path) == (1 if call == "doctor" else 2)
    rendered = capsys.readouterr().out
    assert "native_config_malformed" in rendered
    assert "FAKE_SECRET" not in rendered
