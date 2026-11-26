"""Dot runtime limits through the connector and persistent fleet admission."""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from dataclasses import replace

import pytest

from brigade.dot.adapter import build_tools
from brigade.dot.contracts import MAX_PROMPT_BYTES, DotError
from brigade.dot.duet_dispatch import LimitedFleetTools
from brigade.dot.fleet_tools import FleetTools
from brigade.dot.runtime_config import DUET_RUNTIME_SCHEMA, parse_runtime
from tests.dot_support import requires_local_state
from tests.test_dot_fleet_tools import CONFIG, NOW, REQUEST, SHA, FakeRun
from tests.test_dot_runtime import _document
from tests.test_duet_dispatch import document as duet_document


LIMITS = {"repositories": ["brigade"], "max_open": 1, "daily_limit": 2, "prompt_max_bytes": 128}


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"unknown": 1},
        {"max_open": 0},
        {"daily_limit": True},
        {"prompt_max_bytes": 32769},
        {"repositories": ["bad alias"]},
    ],
)
def test_invalid_limits_match_duet(value):
    with pytest.raises(DotError) as duet:
        parse_runtime({**duet_document(), "dispatch_limits": value}, schema=DUET_RUNTIME_SCHEMA)
    with pytest.raises(DotError) as dot:
        parse_runtime({**_document(), "dispatch_limits": value})
    assert str(dot.value) == str(duet.value)


@pytest.mark.parametrize("limits", [{}, LIMITS])
def test_valid_limits_match_duet(limits):
    dot = parse_runtime({**_document(), "dispatch_limits": limits})
    duet = parse_runtime({**duet_document(), "dispatch_limits": limits}, schema=DUET_RUNTIME_SCHEMA)
    assert duet.dispatch_limits is not None
    assert dot.dispatch_limits == replace(
        duet.dispatch_limits, prompt_max_bytes=min(duet.dispatch_limits.prompt_max_bytes, MAX_PROMPT_BYTES)
    )


@pytest.fixture
def connector(dot_private_dir):
    dot_private_dir.mkdir(mode=0o700)
    config = dot_private_dir / "fleet.toml"
    config.write_text(CONFIG)
    config.chmod(0o600)
    run = FakeRun()
    states = []

    def build(limits=LIMITS):
        data = _document()
        if limits is not None:
            data["dispatch_limits"] = limits
        runtime = parse_runtime(data)
        runtime = replace(runtime, paths=replace(runtime.paths, state_dir=dot_private_dir, fleet_config=config))
        tools, state, _ = build_tools(runtime)
        states.append(state)
        assert tools.fleet is not None
        tools.fleet.runner._run = run
        tools.fleet.clock = lambda: NOW
        return tools, state

    yield build, run
    for state in states:
        state.close()


def call(tools, name, **arguments):
    return tools.call("fixture", name, arguments)


def submit(tools, **extra):
    return call(
        tools,
        "fleet_submit",
        **{
            "repository": "brigade",
            "title": "Fixture",
            "prompt": "work",
            "host": "alpha",
            "seat": "sol61",
            "source_revision": SHA,
            **extra,
        },
    )


@requires_local_state
def test_limits_and_capabilities(connector):
    build, run = connector
    tools, _ = build()
    assert call(tools, "dot_t3_capabilities")["fleet"]["limits"] == LIMITS
    assert submit(tools, repository="remote-only")["error"] == "repository_not_allowed"
    assert submit(tools, prompt="é" * 65, read_only=True)["error"] == "prompt_too_large"
    assert not run.calls
    assert submit(tools)["ok"]
    assert submit(tools)["error"] == "open_limit"
    assert call(tools, "fleet_followup", request_id=REQUEST, message="é" * 65)["error"] == "prompt_too_large"
    assert call(tools, "fleet_followup", request_id=REQUEST, message="continue")["ok"]
    assert call(tools, "fleet_followup", request_id=REQUEST, message="continue")["error"] == "daily_limit"
    assert call(tools, "fleet_interrupt", request_id=REQUEST)["ok"]


@requires_local_state
def test_definitive_failure_releases_open_slot(connector):
    build, run = connector
    tools, state = build()
    run.code = 1
    run.output = json.dumps({"schema": "t3-fleet.error.v1", "error": {"code": "target_busy"}})
    assert submit(tools)["error"] == "fleet_error"
    state.close()
    tools, _ = build()
    run.code = 0
    run.output = FakeRun().output
    assert submit(tools)["ok"]
    assert call(tools, "fleet_followup", request_id=REQUEST, message="continue")["error"] == "daily_limit"


@requires_local_state
def test_uncertain_followup_survives_restart_without_second_attempt(connector):
    build, run = connector
    tools, state = build()
    assert submit(tools)["ok"]
    run.raises = subprocess.TimeoutExpired("followup", 1)
    assert call(tools, "fleet_followup", request_id=REQUEST, message="continue")["error"] == "fleet_uncertain"
    state.close()
    tools, _ = build()
    assert call(tools, "fleet_followup", request_id=REQUEST, message="continue")["error"] == "request_busy"
    assert submit(tools)["error"] == "open_limit"
    assert len(run.calls) == 2


@requires_local_state
def test_missing_limits_preserves_capabilities_and_dispatch(connector):
    build, run = connector
    tools, _ = build(None)
    assert isinstance(tools.fleet, FleetTools)
    assert not isinstance(tools.fleet, LimitedFleetTools)
    assert "usage" not in call(tools, "dot_t3_capabilities")["fleet"]
    assert "limits" not in call(tools, "dot_t3_capabilities")["fleet"]
    assert submit(tools, repository="remote-only")["ok"]
    assert call(tools, "fleet_followup", request_id="existing", message="continue")["ok"]
    assert len(run.calls) == 2


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("fleet_answer", {"input_id": "input", "answers": {"question": "answer"}}),
        ("fleet_approve", {"approval_id": "approval", "decision": "accept"}),
    ],
)
@requires_local_state
def test_controls_check_known_repository_without_charging(connector, name, arguments):
    build, run = connector
    tools, state = build(None)
    assert submit(tools, repository="remote-only")["ok"]
    state.close()
    tools, _ = build()
    assert call(tools, name, request_id=REQUEST, **arguments)["error"] == "repository_not_allowed"
    assert call(tools, name, request_id="other", **arguments)["error"] == "repository_unknown"
    assert len(run.calls) == 1


@requires_local_state
def test_uncertain_submit_keeps_one_reservation_after_restart(connector):
    build, run = connector
    tools, state = build()
    run.raises = subprocess.TimeoutExpired("submit", 1)
    assert submit(tools)["error"] == "fleet_uncertain"
    state.close()
    tools, _ = build()
    assert submit(tools)["error"] == "open_limit"
    assert len(run.calls) == 1
    assert call(tools, "dot_t3_capabilities")["fleet"]["usage"] == {
        "open": 1,
        "attempts_used_today": 1,
        "open_without_request_id": 1,
    }


@requires_local_state
def test_terminal_status_releases_open_slot_after_restart(connector):
    build, run = connector
    tools, state = build()
    assert submit(tools)["ok"]
    state.close()
    tools, _ = build()
    assert submit(tools)["error"] == "open_limit"
    run.output = json.dumps({"schema": "t3-fleet.result.v1", "request_id": REQUEST, "state": "succeeded"})
    assert call(tools, "fleet_status", request_id=REQUEST)["ok"]
    run.output = json.dumps({"schema": "t3-fleet.result.v1", "request_id": "next", "state": "running"})
    assert submit(tools)["ok"]


@requires_local_state
def test_default_and_explicit_prompt_ceiling(connector):
    build, _ = connector
    for limits in ({}, {"prompt_max_bytes": 32768}):
        tools, state = build(limits)
        caps = call(tools, "dot_t3_capabilities")["fleet"]
        assert caps["limits"] == {
            "repositories": None,
            "max_open": 3,
            "daily_limit": 20,
            "prompt_max_bytes": MAX_PROMPT_BYTES,
        }
        assert caps["usage"] == {"open": 0, "attempts_used_today": 0, "open_without_request_id": 0}
        state.close()


@requires_local_state
def test_interrupt_can_stop_known_disallowed_and_unknown_requests(connector):
    build, run = connector
    tools, state = build(None)
    assert submit(tools, repository="remote-only")["ok"]
    state.close()
    tools, _ = build()
    for request_id in (REQUEST, "other"):
        assert call(tools, "fleet_interrupt", request_id=request_id)["ok"]
    assert len(run.calls) == 3


@requires_local_state
def test_followup_adopts_allowed_request_from_legacy_journal(connector):
    build, run = connector
    tools, state = build(None)
    assert submit(tools)["ok"]
    state.close()
    tools, _ = build()
    assert call(tools, "fleet_followup", request_id="other", message="continue")["error"] == "repository_unknown"
    assert call(tools, "fleet_followup", request_id=REQUEST, message="continue")["ok"]
    assert submit(tools)["error"] == "open_limit"
    assert call(tools, "fleet_answer", request_id=REQUEST, input_id="input", answers={"question": "answer"})["ok"]
    assert len(run.calls) == 3


@requires_local_state
def test_concurrent_submits_contend_for_last_open_slot(connector):
    build, run = connector
    tools, _ = build()
    ready = Barrier(2)

    def launch():
        ready.wait(timeout=5)
        return submit(tools)

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(launch) for _ in range(2)]
        results = [future.result(timeout=10) for future in pending]
    assert sum(bool(result.get("ok")) for result in results) == 1
    assert sum(result.get("error") == "open_limit" for result in results) == 1
    assert len(run.calls) == 1
