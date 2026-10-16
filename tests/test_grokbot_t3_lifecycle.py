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
