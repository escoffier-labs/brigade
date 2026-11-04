"""Fleet Steward app, gate, and MCP inventory tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from brigade.grokbot_fleet.contracts import TOOLS
from brigade.grokbot_fleet.lifecycle import (
    FleetListenerConfig,
    _FleetRequestGate,
    _invalid_fleet_tool_request,
)


def _config() -> FleetListenerConfig:
    return FleetListenerConfig(
        target=Path("."),
        bind_host="127.0.0.1",
        bind_port=8771,
        allowed_hosts=("127.0.0.1",),
        allowed_origins=(),
        bearer="F" * 32,
        runtime_path="/var/lib/fleet-steward/runtime.json",
        ledger_path="/var/lib/fleet-steward/ledger.json",
        action_state_path="/var/lib/fleet-steward/actions",
        approval_dir="/etc/fleet-steward/approvals",
    )


def test_request_gate_rejects_missing_bearer_and_unknown_origin():
    gate = _FleetRequestGate(_config())
    assert gate.reject_reason({}, 0) == "unauthorized"
    assert gate.reject_reason({"authorization": "Bearer " + ("F" * 32), "origin": "https://attacker.test"}, 0) == (
        "forbidden"
    )
    assert gate.reject_reason({"authorization": "Bearer " + ("F" * 32), "host": "127.0.0.1"}, 0) is None
    assert gate.reject_reason({"authorization": "Bearer " + ("F" * 32)}, 20_000) == "too-large"
    assert "F" * 32 not in repr(gate)


def test_invalid_fleet_tool_request_is_closed_and_exact():
    assert _invalid_fleet_tool_request(b"not-json") is False
    assert (
        _invalid_fleet_tool_request(
            json.dumps({"method": "tools/call", "params": {"name": "hidden_tool", "arguments": {}}}).encode()
        )
        is True
    )
    assert (
        _invalid_fleet_tool_request(
            json.dumps(
                {"method": "tools/call", "params": {"name": "fleet_overview", "arguments": {"extra": 1}}}
            ).encode()
        )
        is True
    )
    assert (
        _invalid_fleet_tool_request(
            json.dumps({"method": "tools/call", "params": {"name": "fleet_overview", "arguments": {}}}).encode()
        )
        is False
    )
    assert (
        _invalid_fleet_tool_request(
            json.dumps(
                {"method": "tools/call", "params": {"name": "propose_remediation", "arguments": {"finding_id": "x"}}}
            ).encode()
        )
        is False
    )
    assert set(TOOLS) == {
        "fleet_overview",
        "host_status",
        "incident_bundle",
        "propose_remediation",
        "service_health",
        "execute_remediation",
    }


def test_lifecycle_rejects_unsafe_runtime_before_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from brigade.grokbot_fleet.contracts import FleetError
    from brigade.grokbot_fleet.lifecycle import FleetListenerConfig, build_tools_from_config
    from tests.test_grokbot_fleet_runtime_config import VALID_RUNTIME

    built = {"count": 0}

    def boom(*_args, **_kwargs):
        built["count"] += 1
        raise AssertionError("tools must not be constructed")

    monkeypatch.setattr("brigade.grokbot_fleet.lifecycle.FleetStewardTools", boom)
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps(VALID_RUNTIME), encoding="utf-8")
    os.chmod(runtime, 0o644)
    actions = tmp_path / "actions"
    approvals = tmp_path / "approvals"
    actions.mkdir(mode=0o700)
    approvals.mkdir(mode=0o700)
    os.chmod(actions, 0o700)
    os.chmod(approvals, 0o700)
    config = FleetListenerConfig(
        target=tmp_path,
        bind_host="127.0.0.1",
        bind_port=8771,
        allowed_hosts=(),
        allowed_origins=(),
        bearer="F" * 32,
        runtime_path=str(runtime),
        ledger_path=str(tmp_path / "ledger.json"),
        action_state_path=str(actions),
        approval_dir=str(approvals),
    )
    with pytest.raises(FleetError) as tools_caught:
        build_tools_from_config(config, env={})
    assert tools_caught.value.code == "invalid_request"
    assert built["count"] == 0
