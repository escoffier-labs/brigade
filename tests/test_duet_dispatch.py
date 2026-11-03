"""Opt-in Duet dispatch admission and discovery regression evidence."""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade.dot.adapter import build_tools
from brigade.dot.contracts import DotError
from brigade.dot.profile import DUET
from brigade.dot.runtime_config import DUET_RUNTIME_SCHEMA, parse_runtime
from tests.test_duet_profile import _duet_document
from tests.test_dot_lifecycle import config
from brigade.dot import lifecycle
from tests.dot_support import requires_local_state

DISPATCH = frozenset(
    {
        "fleet_submit",
        "fleet_status",
        "fleet_wait",
        "fleet_recent",
        "fleet_capacity",
        "fleet_followup",
        "fleet_interrupt",
    }
)


def document():
    data = _duet_document()
    data["dispatch_enabled"] = True
    data["owner_authorization"] = {"version": "1", "text": "Authorized fixture work."}
    data["paths"]["fleet_config"] = str(Path("/srv/duet/fleet.toml").absolute())
    data["paths"]["bin"]["t3_fleet"] = str(Path("/usr/bin/t3-fleet").absolute())
    return data


@requires_local_state
def test_opt_in_discovery_and_capabilities(dot_private_dir):
    runtime = parse_runtime(document(), schema=DUET_RUNTIME_SCHEMA)
    runtime = replace(runtime, paths=replace(runtime.paths, state_dir=dot_private_dir))
    tools, state, _ = build_tools(runtime, DUET)
    try:
        assert tools.fleet is not None
        assert tools.profile.tools == DUET.tools | DISPATCH
        pytest.importorskip("mcp.server")
        listed = asyncio.run(lifecycle._server(config(), tools, tools.profile).list_tools())
        assert {tool.name for tool in listed} == DUET.tools | DISPATCH
        caps = tools.duet_capabilities()
        assert caps["dispatch"]["enabled"] is True
        assert set(caps["dispatch"]["tools"]) == DISPATCH
        assert set(caps["absent_tools"]) >= {"fleet_approve", "fleet_answer", "fleet_doctor"}
    finally:
        state.close()


@pytest.mark.parametrize(
    "key,value", [("max_open", 0), ("daily_limit", True), ("prompt_max_bytes", 0), ("repositories", ["bad alias"])]
)
def test_invalid_limits(key, value):
    data = document()
    data["dispatch_limits"] = {key: value}
    with pytest.raises(DotError, match="invalid_config"):
        parse_runtime(data, schema=DUET_RUNTIME_SCHEMA)


@pytest.fixture
def fleet(dot_private_dir):
    from brigade.dot.duet_dispatch import DuetFleetTools
    from brigade.dot.fleet_tools import _DotFleetRunner
    from brigade.dot.state import BridgeState
    from tests.test_dot_fleet_tools import CONFIG, FakeRun, FakeCommands, NOW

    data = document()
    data["dispatch_limits"] = {"repositories": ["brigade"], "max_open": 1, "daily_limit": 2, "prompt_max_bytes": 128}
    runtime = parse_runtime(data, schema=DUET_RUNTIME_SCHEMA)
    dot_private_dir.mkdir(mode=0o700)
    path = dot_private_dir / "fleet.toml"
    path.write_text(CONFIG)
    path.chmod(0o600)
    runtime = replace(runtime, paths=replace(runtime.paths, fleet_config=path))
    with BridgeState(dot_private_dir) as state:
        run = FakeRun()
        tools = DuetFleetTools(
            runtime,
            state,
            runner=_DotFleetRunner("/usr/bin/t3-fleet", "/usr/bin/git", run=run),
            commands=FakeCommands(),
            clock=lambda: NOW,
        )
        yield tools, run


def submit(tools, **kwargs):
    from tests.test_dot_fleet_tools import SHA

    return tools.fleet_submit(
        repository=kwargs.pop("repository", "brigade"),
        title="Fixture",
        prompt=kwargs.pop("prompt", "work"),
        host="alpha",
        seat="sol61",
        source_revision=SHA,
        **kwargs,
    )


@requires_local_state
def test_allowlist_and_prompt_refusals_do_not_dispatch(fleet):
    tools, run = fleet
    assert submit(tools, repository="remote-only")["error"] == "repository_not_allowed"
    assert submit(tools, prompt="é" * 65, read_only=True)["error"] == "prompt_too_large"
    assert run.calls == []


@requires_local_state
def test_caps_owned_handles_terminal_release_and_followups(fleet):
    import json
    from tests.test_dot_fleet_tools import REQUEST

    tools, run = fleet
    first = submit(tools)
    assert first["ok"]
    assert run.bodies()[0]["initiator_label"] == "duet"
    assert submit(tools)["error"] == "open_limit"
    assert tools.fleet_followup("foreign", "hello")["error"] == "unknown_request"
    assert tools.fleet_interrupt("foreign")["error"] == "unknown_request"
    assert tools.fleet_status("foreign")["error"] == "unknown_request"
    assert tools.fleet_followup(REQUEST, "é" * 65, read_only=True)["error"] == "prompt_too_large"
    assert tools.fleet_followup(REQUEST, "continue")["ok"]
    assert tools.fleet_followup(REQUEST, "continue")["error"] == "daily_limit"
    run.output = json.dumps({"schema": "t3-fleet.result.v1", "request_id": REQUEST, "state": "succeeded"})
    assert tools.fleet_status(REQUEST)["ok"]
    assert submit(tools)["error"] == "daily_limit"


@requires_local_state
def test_unknown_submit_keeps_open_reservation_after_restart(fleet):
    import subprocess
    from brigade.dot.duet_dispatch import DuetFleetTools

    tools, run = fleet
    run.raises = subprocess.TimeoutExpired("submit", 1)
    assert submit(tools)["error"] == "fleet_uncertain"
    restarted = DuetFleetTools(tools.runtime, tools.state, runner=tools.runner)
    assert submit(restarted)["error"] == "open_limit"


@requires_local_state
def test_parallel_admission_is_atomic(fleet):
    from concurrent.futures import ThreadPoolExecutor

    tools, run = fleet
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: submit(tools), range(4)))
    assert sum(bool(result.get("ok")) for result in results) == 1
    assert sum(result.get("error") == "open_limit" for result in results) == 3
    assert len(run.calls) == 1


def test_dispatch_preserves_isolation_check(monkeypatch):
    runtime = parse_runtime(document(), schema=DUET_RUNTIME_SCHEMA)
    monkeypatch.setattr(lifecycle, "_load_config", lambda *a, **kw: (SimpleNamespace(), runtime))
    monkeypatch.setattr(lifecycle, "_isolated_from_dot", lambda *a: False)
    with pytest.raises(DotError, match="not_isolated_from_dot"):
        lifecycle.build_listener_from_target(SimpleNamespace(), pack_id="duet")


@requires_local_state
def test_daily_budget_reset_and_terminal_followup_reacquires_slot(fleet):
    import json
    from datetime import timedelta
    from tests.test_dot_fleet_tools import REQUEST, NOW

    tools, run = fleet
    assert submit(tools)["ok"]
    assert tools.fleet_followup(REQUEST, "continue")["ok"]
    assert tools.fleet_followup(REQUEST, "continue")["error"] == "daily_limit"
    tools.clock = lambda: NOW + timedelta(days=1)
    run.output = json.dumps({"schema": "t3-fleet.result.v1", "request_id": REQUEST, "state": "succeeded"})
    assert tools.fleet_wait(REQUEST)["ok"]
    run.output = json.dumps({"schema": "t3-fleet.result.v1", "request_id": "other", "state": "running"})
    assert submit(tools)["ok"]
    assert tools.fleet_followup(REQUEST, "reopen")["error"] == "open_limit"


@requires_local_state
def test_parallel_daily_admission_is_atomic(fleet):
    import json
    from concurrent.futures import ThreadPoolExecutor

    tools, run = fleet
    limits = replace(tools.runtime.dispatch_limits, max_open=20, daily_limit=1)
    tools.runtime = replace(tools.runtime, dispatch_limits=limits)
    run.output = json.dumps({"schema": "t3-fleet.result.v1", "request_id": "one", "state": "running"})
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: submit(tools), range(4)))
    assert sum(bool(result.get("ok")) for result in results) == 1
    assert sum(result.get("error") == "daily_limit" for result in results) == 3


def test_shared_fleet_identity_is_not_isolated(monkeypatch, tmp_path):
    duet = parse_runtime(document(), schema=DUET_RUNTIME_SCHEMA)
    from tests.test_dot_runtime import _document

    dot = parse_runtime(_document())
    dot = replace(dot, paths=replace(dot.paths, fleet_config=duet.paths.fleet_config))
    instance = tmp_path / "dot.json"
    instance.touch()
    monkeypatch.setattr(lifecycle.grokbot_packs, "instance_config_path", lambda *a: instance)
    monkeypatch.setattr(lifecycle, "_load_config", lambda *a, **kw: (None, dot))
    assert not lifecycle._isolated_from_dot(tmp_path, duet)


@requires_local_state
def test_explicit_off_builds_no_client(dot_private_dir):
    data = _duet_document()
    data["dispatch_enabled"] = False
    runtime = parse_runtime(data, schema=DUET_RUNTIME_SCHEMA)
    runtime = replace(runtime, paths=replace(runtime.paths, state_dir=dot_private_dir))
    tools, state, _ = build_tools(runtime, DUET)
    try:
        assert tools.profile is DUET
        assert tools.fleet is None
        assert tools.duet_capabilities()["dispatch"] == {"enabled": False, "tools": [], "limits": None}
    finally:
        state.close()


@requires_local_state
def test_stale_terminal_status_cannot_release_new_followup(fleet):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from tests.test_dot_fleet_tools import REQUEST

    tools, run = fleet
    assert submit(tools)["ok"]
    entered, release = Event(), Event()
    original = tools.runner.call

    def call(action, payload, *, timeout):
        if action == "status":
            entered.set()
            assert release.wait(5)
            return {"request_id": REQUEST, "state": "succeeded"}
        return original(action, payload, timeout=timeout)

    tools.runner.call = call
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(tools.fleet_status, REQUEST)
        assert entered.wait(5)
        try:
            assert tools.fleet_followup(REQUEST, "continue")["ok"]
        finally:
            release.set()
        assert pending.result()["ok"]
    assert submit(tools)["error"] == "open_limit"


@pytest.mark.parametrize("state", ["succeeded", "failed", "interrupted", "cancelled", "completed"])
@requires_local_state
def test_every_terminal_state_releases_the_open_slot(fleet, state):
    import json
    from tests.test_dot_fleet_tools import REQUEST

    tools, run = fleet
    assert submit(tools)["ok"]
    assert submit(tools)["error"] == "open_limit"
    run.output = json.dumps({"schema": "t3-fleet.result.v1", "request_id": REQUEST, "state": state})
    assert tools.fleet_status(REQUEST)["ok"]
    run.output = json.dumps({"schema": "t3-fleet.result.v1", "request_id": "other", "state": "running"})
    assert submit(tools)["ok"]


def test_terminal_states_match_the_event_watcher():
    events = pytest.importorskip("brigade.dot.events")
    from brigade.dot.duet_dispatch import TERMINAL

    assert TERMINAL == events.TERMINAL


@requires_local_state
def test_terminal_observation_started_during_busy_followup_keeps_slot(fleet):
    import json
    from tests.test_dot_fleet_tools import REQUEST

    tools, run = fleet
    assert submit(tools)["ok"]
    # A followup has reserved the slot and is still waiting for the fleet to accept it.
    with tools.state.transaction() as connection:
        connection.execute("UPDATE duet_dispatch SET busy=1, generation=generation+1 WHERE request_id=?", (REQUEST,))
    stale = json.dumps({"schema": "t3-fleet.result.v1", "request_id": REQUEST, "state": "succeeded"})

    def observe_previous_turn_then_followup_lands(argv, **kwargs):
        # The followup completes while this status call is in flight.
        with tools.state.transaction() as connection:
            connection.execute("UPDATE duet_dispatch SET busy=0 WHERE request_id=?", (REQUEST,))
        run.output = stale
        return run(argv, **kwargs)

    tools.runner._run = observe_previous_turn_then_followup_lands
    assert tools.fleet_status(REQUEST)["ok"]
    run.output = json.dumps({"schema": "t3-fleet.result.v1", "request_id": "other", "state": "running"})
    assert submit(tools)["error"] == "open_limit"
