# content-guard: allow bearer-token file
"""t3-dispatch listener gate, early tool rejection, and unit rendering."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from brigade.grokbot_t3 import lifecycle

from tests.test_grokbot_t3_runtime import write_runtime

pytestmark = pytest.mark.usefixtures("t3_ledger_storage", "t3_owner_reads")

SECRET = "not-a-real-token-value-32chars!!"


def _config(**overrides):
    base = dict(
        target=Path("/tmp/x"),
        bind_host="127.0.0.1",
        bind_port=8778,
        allowed_hosts=("t3-dispatch.brigade.tools",),
        allowed_origins=(),
        bearer=SECRET,
        runtime_path="/r.json",
    )
    base.update(overrides)
    return lifecycle.T3DispatchListenerConfig(**base)


def test_gate_requires_bearer_and_allowed_host():
    gate = lifecycle._T3RequestGate(_config())
    ok = {"host": "t3-dispatch.brigade.tools", "authorization": f"Bearer {SECRET}"}
    assert gate.reject_reason(ok, 0) is None
    assert gate.reject_reason({"host": "127.0.0.1"}, 0) == "unauthorized"
    assert gate.reject_reason({**ok, "host": "evil.example"}, 0) == "forbidden"
    assert gate.reject_reason({**ok, "origin": "https://evil.example"}, 0) == "forbidden"
    assert gate.reject_reason({**ok, "authorization": "Bearer wrong"}, 0) == "unauthorized"
    assert gate.reject_reason(ok, lifecycle.MAX_REQUEST_BYTES + 1) == "too-large"


def test_repr_hides_bearer():
    assert SECRET not in repr(_config())


@pytest.mark.parametrize(
    "body,invalid",
    [
        ({"method": "tools/list"}, False),
        ({"method": "tools/call", "params": {"name": "t3_capacity", "arguments": {}}}, False),
        ({"method": "tools/call", "params": {"name": "t3_status", "arguments": {"request_id": "r1"}}}, False),
        ({"method": "tools/call", "params": {"name": "t3_shell", "arguments": {}}}, True),
        ({"method": "tools/call", "params": {"name": "t3_status", "arguments": {"request_id": "r1", "x": 1}}}, True),
        ({"method": "tools/call", "params": "nope"}, True),
    ],
)
def test_invalid_tool_request(body, invalid):
    assert lifecycle._invalid_tool_request(json.dumps(body).encode()) is invalid


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX systemd deployment: docs/grokbot-t3-dispatch.md:95")
def test_render_units(tmp_path, monkeypatch):
    runtime = write_runtime(tmp_path, writable_paths=["/srv/t3/state/t3-fleet"])
    monkeypatch.setattr(
        lifecycle,
        "_load_validated_instance",
        lambda target: {
            "bind": "127.0.0.1:8778",
            "allowed_hosts": ["t3-dispatch.brigade.tools"],
            "allowed_origins": [],
            "bearer": {"kind": "file", "path": "/srv/t3/config/origin.bearer"},
            "runtime_path": str(runtime),
        },
    )
    unit = lifecycle.render_unit(tmp_path, python="/venv/bin/python")
    assert "--pack t3-dispatch" in unit
    assert "--allow-host t3-dispatch.brigade.tools" in unit
    assert "--bearer-file /srv/t3/config/origin.bearer" in unit
    assert f"ReadWritePaths={tmp_path / 'state'} /srv/t3/state/t3-fleet" in unit
    service, timer = lifecycle.render_watch_units(tmp_path, python="/venv/bin/python")
    assert "Type=oneshot" in service
    for rendered in (unit, service):
        assert "After=network.target\n" in rendered
        assert "network-online" not in rendered
    assert "ExecStart=/venv/bin/python -I -c " in service
    assert f"brigade.grokbot_t3 watch --target {tmp_path}\n" in service
    assert "OnUnitActiveSec=1min" in timer and f"Unit={lifecycle.WATCH_SERVICE}" in timer


def test_write_watch_units_refuses_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "render_watch_units", lambda target, python=None: ("svc\n", "tmr\n"))
    out = tmp_path / "units"
    out.mkdir()
    paths = lifecycle.write_watch_units(tmp_path, out)
    assert [p.name for p in paths] == [lifecycle.WATCH_SERVICE, lifecycle.WATCH_TIMER]
    assert lifecycle.write_watch_units(tmp_path, out) == paths
    monkeypatch.setattr(lifecycle, "render_watch_units", lambda target, python=None: ("svc2\n", "tmr\n"))
    from brigade import grokbot_ops

    with pytest.raises(grokbot_ops.ServiceRenderError):
        lifecycle.write_watch_units(tmp_path, out)
    assert lifecycle.write_watch_units(tmp_path, out, force=True) == paths


def test_authenticated_mcp_submit_and_recent_cross_threads(tmp_path):
    pytest.importorskip("mcp.server", reason="requires the grokbot extra")
    TestClient = pytest.importorskip("starlette.testclient", reason="requires the grokbot extra").TestClient

    from brigade.grokbot_t3.ledger import Ledger
    from brigade.grokbot_t3.runtime_config import load_runtime
    from brigade.grokbot_t3.tools import T3DispatchTools
    from tests.test_grokbot_t3_tools import FakeRunner

    runtime = load_runtime(write_runtime(tmp_path))
    ledger = Ledger(runtime.ledger_path)
    tools = T3DispatchTools(runtime=runtime, runner=FakeRunner(), ledger=ledger)
    app = lifecycle.build_app(_config(target=tmp_path), tools)
    headers = {
        "authorization": f"Bearer {SECRET}",
        "accept": "application/json, text/event-stream",
    }
    with TestClient(app, base_url="http://127.0.0.1", headers=headers) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "regression", "version": "1"},
                },
            },
        )
        assert response.status_code == 200
        assert "error" not in response.json()
        response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
        listed = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
        assert set(listed) == set(lifecycle.c.T3_TOOLS)
        authorization = client.headers.pop("authorization")
        try:
            response = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "t3_submit",
                        "arguments": {
                            "repository": "brigade",
                            "prompt": "p",
                            "title": "t",
                            "caller": "Builder",
                        },
                    },
                },
            )
            assert response.status_code == 401
            assert ledger.open_count() == 0
        finally:
            client.headers["authorization"] = authorization
        for index, name, arguments in [
            (2, "t3_submit", {"repository": "brigade", "prompt": "p", "title": "t", "caller": "Builder"}),
            (3, "t3_recent", {}),
        ]:
            response = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": index,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            assert response.status_code == 200
            result = response.json()["result"]
            assert not result.get("isError"), result
            payload = json.loads(result["content"][0]["text"])
            assert "error" not in payload, payload
            if name == "t3_recent":
                assert payload["requests"][0]["request_id"] == "r1"
    assert ledger.get("r1")["last_state"] == "dispatching"
    ledger.close()


@pytest.mark.parametrize(
    "wake_valid,ledger_valid,paths",
    [(True, True, []), (False, True, ["/srv/t3/state"]), (True, False, ["/srv/t3/state"])],
)
@pytest.mark.usefixtures("t3_platform_admission")
def test_doctor_checks_wake_ledger_and_explicit_write_paths(tmp_path, monkeypatch, wake_valid, ledger_valid, paths):
    from brigade.grokbot_t3 import contracts as c
    from brigade.grokbot_t3.runtime_config import load_runtime
    from brigade.grokbot_t3.watch import WAKE_SCHEMA

    paths = [str(tmp_path / "writable") for _ in paths]
    runtime = load_runtime(write_runtime(tmp_path, writable_paths=paths))
    runtime.wake_path.parent.mkdir(mode=0o700)
    runtime.wake_path.write_text(
        json.dumps(
            {
                "schema": WAKE_SCHEMA,
                "targets": {"default": [{"url": "https://example.com/wake", "key_file": str(tmp_path / "wake.key")}]},
            }
        )
        if wake_valid
        else "invalid"
    )
    runtime.wake_path.chmod(0o600)
    if not ledger_valid:
        runtime.ledger_path.mkdir()  # Cannot open a directory as a writable SQLite file.
    monkeypatch.setattr(lifecycle, "_load_runtime_config", lambda target: (_config(), SECRET, runtime))
    monkeypatch.setattr(lifecycle.FleetRunner, "capacity", lambda self: {"hosts": []})
    monkeypatch.setattr(lifecycle.grokbot_ops, "_request_json", lambda *a, **k: {"ok": True, "service": c.SERVICE_NAME})
    checks = {check["check"]: check["status"] for check in lifecycle.doctor(tmp_path)}
    assert checks["wake-config"] == ("ok" if wake_valid else "fail")
    assert checks["ledger"] == ("ok" if ledger_valid else "fail")
    assert checks["writable-paths"] == ("ok" if paths else "warn")


def test_tools_factory_explicitly_selects_full_access(tmp_path, monkeypatch):
    from functools import partial
    from brigade.grokbot_t3.runtime_config import load_runtime

    runtime = load_runtime(write_runtime(tmp_path))
    monkeypatch.setattr(lifecycle, "FleetRunner", partial(lifecycle.FleetRunner, runtime_mode="read-only"))
    tools = lifecycle._tools_for(runtime)
    try:
        assert tools.runner.runtime_mode == "full-access"
    finally:
        tools.ledger.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX systemd deployment: docs/grokbot-t3-dispatch.md:95")
def test_render_intake_units(tmp_path, monkeypatch):
    runtime = write_runtime(tmp_path, writable_paths=["/srv/t3/state"])
    monkeypatch.setattr(lifecycle, "_load_validated_instance", lambda target: {"runtime_path": str(runtime)})
    service, timer = lifecycle.render_intake_units(tmp_path, python="/venv/bin/python")
    assert "ExecStart=/venv/bin/python -I -c " in service
    assert f"brigade.grokbot_t3 intake --target {tmp_path}\n" in service
    assert "Description=Brigade Grok Bot t3-dispatch issue intake\n" in service
    assert "Type=oneshot\n" in service and "TimeoutStartSec=900\n" in service
    assert "ProtectSystem=strict\n" in service
    assert f"ReadWritePaths={tmp_path / 'state'} /srv/t3/state\n" in service
    assert "OnBootSec=5min\n" in timer and "OnUnitActiveSec=15min\n" in timer
    assert "Unit=brigade-grokbot-t3-intake.service\n" in timer
    assert "Description=Run the t3-dispatch issue intake every 15 minutes\n" in timer
    assert "WantedBy=timers.target\n" in timer


def test_write_intake_units_refuses_overwrite(tmp_path, monkeypatch):
    from brigade import grokbot_ops

    monkeypatch.setattr(lifecycle, "render_intake_units", lambda target, python=None: ("svc\n", "timer\n"))
    paths = lifecycle.write_intake_units(tmp_path, tmp_path)
    assert [path.name for path in paths] == ["brigade-grokbot-t3-intake.service", "brigade-grokbot-t3-intake.timer"]
    assert lifecycle.write_intake_units(tmp_path, tmp_path) == paths
    monkeypatch.setattr(lifecycle, "render_intake_units", lambda target, python=None: ("changed\n", "timer\n"))
    with pytest.raises(grokbot_ops.ServiceRenderError):
        lifecycle.write_intake_units(tmp_path, tmp_path)
    assert lifecycle.write_intake_units(tmp_path, tmp_path, force=True) == paths


@pytest.mark.parametrize("failure,expected", [(None, 0), ("timeout", 124), ("oserror", 127)])
@pytest.mark.usefixtures("t3_platform_admission")
def test_intake_once_wires_bounded_gh_runner(tmp_path, monkeypatch, failure, expected):
    import subprocess
    from datetime import datetime, timezone
    from tests.test_grokbot_t3_runtime import intake_payload

    runtime = write_runtime(tmp_path, intake=intake_payload())
    monkeypatch.setattr(lifecycle, "_load_validated_instance", lambda target: {"runtime_path": str(runtime)})
    tools = object()
    monkeypatch.setattr(lifecycle, "_tools_for", lambda runtime: tools)
    calls = []

    def fake_process(argv, **kwargs):
        calls.append((argv, kwargs))
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 60, output="private output")
        if failure == "oserror":
            raise OSError("private error")
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="private stderr")

    monkeypatch.setattr(subprocess, "run", fake_process)

    def fake_intake(*, policy, runtime, tools: object, gh, now):
        assert tools is not None and policy is runtime.intake
        assert now.tzinfo == timezone.utc and abs((datetime.now(timezone.utc) - now).total_seconds()) < 5
        assert gh(["issue", "list"]) == (expected, "[]" if expected == 0 else "")
        return {"reason": "ok", "submitted": [], "held": [], "errors": []}

    monkeypatch.setattr(lifecycle, "run_intake_once", fake_intake)
    assert lifecycle.intake_once(tmp_path)["reason"] == "ok"
    assert calls == [
        (
            [str(Path("/usr/bin/gh").absolute()), "issue", "list"],
            {
                "capture_output": True,
                "text": True,
                "shell": False,
                "timeout": 60,
                "check": False,
                "stdin": subprocess.DEVNULL,
            },
        )
    ]


@pytest.mark.parametrize("errors,expected", [([], 0), ([{"repository": "example", "error": "gh-failed"}], 1)])
@pytest.mark.usefixtures("t3_platform_admission")
def test_intake_cli_prints_summary(tmp_path, monkeypatch, capsys, errors, expected):
    from brigade.grokbot_t3.__main__ import main

    summary = {"reason": "ok", "submitted": [], "held": [], "errors": errors}
    monkeypatch.setattr(lifecycle, "intake_once", lambda target: summary)
    assert main(["intake", "--target", str(tmp_path)]) == expected
    assert capsys.readouterr().out == json.dumps(summary, sort_keys=True) + "\n"


@pytest.mark.usefixtures("t3_platform_admission")
def test_install_intake_cli(tmp_path, monkeypatch, capsys):
    from brigade.grokbot_t3.__main__ import main

    monkeypatch.setattr(lifecycle, "render_intake_units", lambda target, python=None: ("svc\n", "timer\n"))
    assert main(["install-intake", "--target", str(tmp_path), "--out-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "units": ["brigade-grokbot-t3-intake.service", "brigade-grokbot-t3-intake.timer"]
    }


@pytest.mark.parametrize("messaging", [False, True])
@pytest.mark.parametrize("inventory_change", [None, "missing", "extra"])
@pytest.mark.usefixtures("t3_platform_admission")
def test_canary_requires_exact_runtime_tool_inventory(tmp_path, monkeypatch, messaging, inventory_change):
    from brigade import grokbot_ops

    from brigade.grokbot_t3.runtime_config import parse_runtime
    from tests.test_grokbot_t3_runtime import runtime_payload

    runtime = parse_runtime(
        runtime_payload(
            tmp_path,
            hub_messaging_enabled=messaging,
            hub_message_principal="agent:grokbot",
            hub_token_file=str(tmp_path / "principal.token"),
        )
    )
    expected = lifecycle.c.TOOLS if messaging else lifecycle.c.T3_TOOLS
    listed = set(expected)
    if inventory_change == "missing":
        listed.remove("hub_message_send" if messaging else "t3_submit")
    elif inventory_change == "extra":
        listed.add("inbox_reply")
    monkeypatch.setattr(lifecycle, "_load_runtime_config", lambda target: (_config(), SECRET, runtime))
    monkeypatch.setattr(
        grokbot_ops, "_request_json", lambda *args, **kwargs: {"ok": True, "service": lifecycle.c.SERVICE_NAME}
    )
    monkeypatch.setattr(grokbot_ops, "_anonymous_health_status", lambda *args: 401)
    monkeypatch.setattr(grokbot_ops, "_tools_list", lambda *args: [{"name": name} for name in listed])

    def capture(self):
        assert messaging, "disabled messaging called Hub"

    monkeypatch.setattr(lifecycle.HubInbox, "capture", capture)
    result = lifecycle.canary(tmp_path)
    assert result["ok"] is (inventory_change is None)
    if inventory_change:
        assert result["reason"] == "inventory-mismatch"
    else:
        assert set(result["tools"]) == expected


@pytest.mark.parametrize("messaging,dispatch", [(False, False), (False, True), (True, False), (True, True)])
def test_registration_uses_runtime_inventory_and_alias_contract(tmp_path, messaging, dispatch):
    from brigade.grokbot_t3.runtime_config import parse_runtime
    from tests.test_grokbot_t3_runtime import runtime_payload

    runtime = parse_runtime(
        runtime_payload(
            tmp_path,
            enabled=dispatch,
            hub_messaging_enabled=messaging,
            hub_message_principal="agent:grokbot",
            hub_token_file=str(tmp_path / "principal.token"),
        )
    )
    tools = lifecycle.T3DispatchTools(runtime=runtime, runner=None, ledger=None)

    MCPServer = pytest.importorskip("mcp.server.mcpserver", reason="requires the grokbot extra").MCPServer
    import asyncio

    definitions = lifecycle._register_tools(tools)
    server = MCPServer("inventory-regression", tools=definitions)
    listed = asyncio.run(server.list_tools())
    handlers = {tool.name: tool.fn for tool in definitions}
    expected = lifecycle.c.TOOLS if messaging else lifecycle.c.T3_TOOLS
    assert {tool.name for tool in listed} == expected
    assert "inbox_reply" not in handlers
    if messaging:
        import inspect

        assert set(inspect.signature(handlers["hub_message_send"]).parameters) == {
            "alias",
            "subject",
            "body",
            "ref",
            "priority",
            "thread",
            "kind",
            "client_id",
        }


def test_installed_sdk_publishes_closed_constrained_hub_schemas(tmp_path):
    import asyncio

    MCPServer = pytest.importorskip("mcp.server.mcpserver", reason="requires the grokbot extra").MCPServer
    Draft202012Validator = pytest.importorskip("jsonschema", reason="requires the grokbot extra").Draft202012Validator
    from brigade.grokbot_t3.runtime_config import parse_runtime
    from tests.test_grokbot_t3_runtime import runtime_payload

    runtime = parse_runtime(
        runtime_payload(
            tmp_path,
            hub_messaging_enabled=True,
            hub_message_principal="agent:grokbot",
            hub_token_file=str(tmp_path / "principal.token"),
        )
    )
    tools = lifecycle.T3DispatchTools(runtime=runtime, runner=None, ledger=None)
    server = MCPServer("schema-regression", tools=lifecycle._register_tools(tools))
    listed = asyncio.run(server.list_tools())
    httpx = pytest.importorskip("httpx2", reason="requires the installed MCP protocol transport")

    app = lifecycle.build_app(_config(target=tmp_path), tools)
    headers = {"authorization": f"Bearer {SECRET}", "accept": "application/json, text/event-stream"}

    async def wire_schemas():
        # Drive ASGI lifespan and HTTP in one event loop. TestClient's thread
        # portal cannot start in the restricted worker environment.
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        task = asyncio.create_task(app({"type": "lifespan", "asgi": {"version": "3.0"}}, incoming.get, outgoing.put))
        try:
            await incoming.put({"type": "lifespan.startup"})
            assert (await asyncio.wait_for(outgoing.get(), 5))["type"] == "lifespan.startup.complete"
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", headers=headers
            ) as client:
                initialized = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "schema-regression", "version": "1"},
                        },
                    },
                )
                assert initialized.status_code == 200 and "error" not in initialized.json()
                notification = await client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                assert notification.status_code == 202
                response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                assert response.status_code == 200
                return {tool["name"]: tool["inputSchema"] for tool in response.json()["result"]["tools"]}
        finally:
            await incoming.put({"type": "lifespan.shutdown"})
            await asyncio.wait_for(task, 5)

    wire = asyncio.run(wire_schemas())
    schemas = {tool.name: tool.input_schema for tool in listed}
    assert schemas == wire
    assert {name: schemas[name] for name in lifecycle.c.T3_TOOLS} == ORIGINAL_T3_INPUT_SCHEMAS
    assert set(schemas) == lifecycle.c.TOOLS
    cases = {
        "hub_inbox_list": ({}, [{"limit": 0}, {"limit": 51}, {"limit": True}]),
        "hub_inbox_ack": (
            {"ids": [1]},
            [{"ids": []}, {"ids": [1, 1]}, {"ids": [0]}, {"ids": [2**62]}, {"ids": list(range(1, 102))}],
        ),
        "hub_message_status": ({"ids": [1]}, [{"ids": list(range(1, 52))}, {"ids": [False]}]),
        "hub_message_send": (
            {"alias": "helper", "subject": "s", "body": "b"},
            [
                {"alias": "agent:miller"},
                {"priority": "invalid"},
                {"kind": "invalid"},
                {"thread": "/bad"},
                {"client_id": "bad/path"},
                {"subject": "s" * 201},
                {"body": "b" * 8193},
                {"subject": "   "},
                {"subject": "a\x00b"},
                {"body": " \n\t"},
                {"body": "a\x00b"},
                {"body": "a\x85b"},
                {"ref": "name\n"},
                {"thread": "name\n"},
                {"alias": "helper\n"},
            ],
        ),
    }
    for name, (valid, invalid) in cases.items():
        schema = schemas[name]
        assert schema["additionalProperties"] is False
        check = Draft202012Validator(schema)
        assert check.is_valid(valid)
        assert not check.is_valid(dict(valid, unknown=None))
        with pytest.raises(lifecycle.c.T3DispatchError):
            lifecycle.c.PARSERS[name](dict(valid, unknown=None))
        for change in invalid:
            value = dict(valid, **change)
            assert not check.is_valid(value), (name, value)
            with pytest.raises(lifecycle.c.T3DispatchError):
                lifecycle.c.PARSERS[name](value)
    nullable = dict(cases["hub_message_send"][0], ref=None, thread=None, client_id=None)
    assert Draft202012Validator(schemas["hub_message_send"]).is_valid(nullable)
    assert lifecycle.c.PARSERS["hub_message_send"](nullable)["ref"] is None
    # JSON Schema character bounds cannot express the authoritative UTF-8 byte limit.
    multibyte = dict(cases["hub_message_send"][0], subject="é" * 101)
    assert Draft202012Validator(schemas["hub_message_send"]).is_valid(multibyte)
    with pytest.raises(lifecycle.c.T3DispatchError):
        lifecycle.c.PARSERS["hub_message_send"](multibyte)

    async def unknown_key_call():
        from mcp.server.mcpserver.exceptions import ToolError

        with pytest.raises(ToolError):
            await server.call_tool("hub_inbox_list", {"unknown": None})

    asyncio.run(unknown_key_call())


def test_sdk_hub_calls_distinguish_null_missing_and_invalid_values(tmp_path, monkeypatch, subtests):
    pytest.importorskip("mcp.server", reason="requires the grokbot extra")
    TestClient = pytest.importorskip("starlette.testclient").TestClient
    from brigade.grokbot_t3.runtime_config import parse_runtime
    from tests.test_grokbot_t3_runtime import runtime_payload

    runtime = parse_runtime(
        runtime_payload(
            tmp_path,
            hub_messaging_enabled=True,
            hub_message_principal="agent:grokbot",
            hub_token_file=str(tmp_path / "principal.token"),
        )
    )
    tools = lifecycle.T3DispatchTools(runtime=runtime, runner=None, ledger=None)
    received = []

    def call(name, arguments):
        received.append((name, arguments))
        return {"ok": True}

    monkeypatch.setattr(tools, "call_tool", call)
    send = {"alias": "helper", "subject": "s", "body": "b"}
    valid = [
        ("hub_message_send", send),
        ("hub_message_send", dict(send, ref=None, thread=None, client_id=None)),
        ("hub_inbox_list", {}),
        ("hub_inbox_list", {"limit": None, "unacked_only": None}),
        ("hub_inbox_ack", {"ids": [1]}),
        ("hub_message_status", {"ids": [1]}),
    ]
    invalid = [("hub_message_send", {k: v for k, v in send.items() if k != key}) for key in send]
    invalid += [
        ("hub_message_send", dict(send, **change))
        for change in [
            {"alias": None},
            {"subject": None},
            {"body": None},
            {"body": {"answer": 42}},
            {"body": [1, 2]},
            {"subject": ""},
            {"body": " \n\t"},
            {"body": "a\x00b"},
            {"subject": "é" * 101},
            {"body": "é" * 4097},
            {"unknown": None},
        ]
    ]
    invalid += [
        ("hub_inbox_list", {"unknown": None}),
        ("hub_inbox_list", {"limit": True}),
        ("hub_inbox_ack", {"ids": "[1]"}),
        ("hub_inbox_ack", {"ids": [1, 1]}),
        ("hub_message_status", {"ids": [True]}),
        ("hub_message_status", {"ids": None}),
    ]
    app = lifecycle.build_app(_config(target=tmp_path), tools)
    headers = {"authorization": f"Bearer {SECRET}", "accept": "application/json, text/event-stream"}
    with TestClient(app, base_url="http://127.0.0.1", headers=headers) as client:
        for ident, (name, arguments) in enumerate(valid + invalid, 1):
            with subtests.test(name=name, arguments=arguments):
                before = len(received)
                response = client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": ident,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    },
                )
                if ident <= len(valid):
                    assert response.status_code == 200
                    result = response.json()["result"]
                    assert not result.get("isError"), result
                    # The SDK handler supplies list defaults but strips nullable send options.
                    if name == "hub_inbox_list":
                        expected = {"unacked_only": True, "limit": 20}
                    else:
                        expected = {k: v for k, v in arguments.items() if v is not None}
                        if name == "hub_message_send":
                            expected.update(priority="normal", kind="message")
                    assert received[-1] == (name, expected)
                    assert len(received) == before + 1
                else:
                    assert response.status_code == 400
                    assert response.json()["error"]["code"] == -32602
                    assert len(received) == before


# Captured from the original nine handlers with the installed MCP 2.3.0 SDK.
ORIGINAL_T3_INPUT_SCHEMAS = {
    "t3_answer": {
        "properties": {
            "answers": {"additionalProperties": True, "title": "Answers", "type": "object"},
            "input_id": {"title": "Input Id", "type": "string"},
            "request_id": {"title": "Request Id", "type": "string"},
        },
        "required": ["request_id", "input_id", "answers"],
        "title": "t3_answerArguments",
        "type": "object",
    },
    "t3_approve": {
        "properties": {
            "approval_id": {"title": "Approval Id", "type": "string"},
            "decision": {"title": "Decision", "type": "string"},
            "request_id": {"title": "Request Id", "type": "string"},
        },
        "required": ["request_id", "approval_id", "decision"],
        "title": "t3_approveArguments",
        "type": "object",
    },
    "t3_capacity": {"properties": {}, "title": "t3_capacityArguments", "type": "object"},
    "t3_followup": {
        "properties": {
            "message": {"title": "Message", "type": "string"},
            "request_id": {"title": "Request Id", "type": "string"},
        },
        "required": ["request_id", "message"],
        "title": "t3_followupArguments",
        "type": "object",
    },
    "t3_interrupt": {
        "properties": {"request_id": {"title": "Request Id", "type": "string"}},
        "required": ["request_id"],
        "title": "t3_interruptArguments",
        "type": "object",
    },
    "t3_recent": {
        "properties": {
            "caller": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Caller"},
            "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None, "title": "Limit"},
        },
        "title": "t3_recentArguments",
        "type": "object",
    },
    "t3_status": {
        "properties": {"request_id": {"title": "Request Id", "type": "string"}},
        "required": ["request_id"],
        "title": "t3_statusArguments",
        "type": "object",
    },
    "t3_submit": {
        "properties": {
            "base_ref": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Base Ref"},
            "caller": {"title": "Caller", "type": "string"},
            "host": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Host"},
            "interaction_mode": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": None,
                "title": "Interaction Mode",
            },
            "prompt": {"title": "Prompt", "type": "string"},
            "repository": {"title": "Repository", "type": "string"},
            "seat": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Seat"},
            "title": {"title": "Title", "type": "string"},
        },
        "required": ["repository", "prompt", "title", "caller"],
        "title": "t3_submitArguments",
        "type": "object",
    },
    "t3_wait": {
        "properties": {
            "request_id": {"title": "Request Id", "type": "string"},
            "timeout_s": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None, "title": "Timeout S"},
        },
        "required": ["request_id"],
        "title": "t3_waitArguments",
        "type": "object",
    },
}
