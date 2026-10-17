# content-guard: allow bearer-token file
"""t3-dispatch listener gate, early tool rejection, and unit rendering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade.grokbot_t3 import lifecycle

from tests.test_grokbot_t3_runtime import write_runtime

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
    assert f"ExecStart=/venv/bin/python -m brigade.grokbot_t3 watch --target {tmp_path}" in service
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
def test_doctor_checks_wake_ledger_and_explicit_write_paths(tmp_path, monkeypatch, wake_valid, ledger_valid, paths):
    from brigade.grokbot_t3 import contracts as c
    from brigade.grokbot_t3.runtime_config import load_runtime
    from brigade.grokbot_t3.watch import WAKE_SCHEMA

    runtime = load_runtime(write_runtime(tmp_path, writable_paths=paths))
    runtime.wake_path.parent.mkdir(mode=0o700)
    runtime.wake_path.write_text(
        json.dumps(
            {
                "schema": WAKE_SCHEMA,
                "targets": {"default": [{"url": "https://example.com/wake", "key_file": "/srv/keys/wake"}]},
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
