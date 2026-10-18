"""Fleet machine-mode payloads, admission prechecks, and durable operation outcomes."""

import asyncio
import json
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from brigade.dot.contracts import MAX_PROMPT_BYTES, DotError
from brigade.dot.events import EventService
from brigade.dot.fleet_tools import FleetTools, _DotFleetRunner, load_fleet_config
from brigade.dot.process import CommandResult, CommandRunner
from brigade.dot.runtime_config import parse_runtime
from brigade.dot.state import BridgeState
from tests.test_dot_events import FakeHub
from tests.test_dot_runtime import _document

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "abcdef01" * 5
REQUEST = "alpha-request-1"
CONFIG = """controller_host = "alpha"
[hosts.alpha]
[hosts.beta]
[hosts.retired]
enabled = false
[repositories.brigade]
default_branch = "main"
[repositories.brigade.paths]
alpha = "/srv/repos/brigade"
beta = "/srv/repos/brigade"
[repositories.remote-only]
default_branch = "main"
[repositories.remote-only.paths]
beta = "/srv/repos/remote"
"""
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeRun:
    def __init__(self):
        self.calls = []
        self.output = json.dumps({"schema": "t3-fleet.result.v1", "request_id": REQUEST, "state": "running"})
        self.code = 0
        self.raises = None

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.code, self.output, "")

    def bodies(self):
        return [json.loads(kwargs["input"]) for _, kwargs in self.calls]


class FakeCommands(CommandRunner):
    def __init__(self):
        self.calls = []
        self.results = [CommandResult(0, f"{SHA}\trefs/heads/main\n".encode())]

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]


@pytest.fixture
def fleet(dot_private_dir):
    with BridgeState(dot_private_dir) as state:
        runtime = parse_runtime(_document())
        runtime = replace(runtime, paths=replace(runtime.paths, fleet_config=dot_private_dir / "fleet.toml"))
        runtime.paths.fleet_config.write_text(CONFIG)
        runtime.paths.fleet_config.chmod(0o600)
        run, commands = FakeRun(), FakeCommands()
        tools = FleetTools(
            runtime,
            state,
            runner=_DotFleetRunner(
                runtime.paths.bin.t3_fleet,
                runtime.paths.bin.git,
                run=run,
                fleet_config=runtime.paths.fleet_config,
            ),
            commands=commands,
            clock=lambda: NOW,
        )
        yield tools, run, commands


def submit(tools, **extra):
    return tools.fleet_submit(
        **{
            "repository": "brigade",
            "title": "t",
            "prompt": "p",
            "host": "alpha",
            "seat": "sol61",
            "source_revision": SHA,
            **extra,
        }
    )


def expected_submit(**extra):
    return {
        "schema": "t3-fleet.request.v1",
        "action": "submit",
        "host": "alpha",
        "repository": "brigade",
        "source_revision": SHA,
        "title": "t",
        "prompt": "p\n\n## Owner authorization [1]\nOwner approved Dot automation.",
        "runtime_mode": "full-access",
        "interaction_mode": "default",
        "seat": "sol61",
        "initiator_kind": "agent",
        "initiator_label": "dot",
        **extra,
    }


def test_read_payloads_and_timeouts(fleet):
    tools, run, _ = fleet
    tools.fleet_capacity()
    tools.fleet_capacity(host="alpha")
    tools.fleet_doctor()
    tools.fleet_doctor(host="beta", repository="brigade")
    tools.fleet_status(REQUEST)
    tools.fleet_status(REQUEST, include_output=True)
    tools.fleet_wait(REQUEST, timeout_seconds=12)
    tools.fleet_wait(REQUEST, include_output=True)
    payloads = [
        ("capacity", {}),
        ("capacity", {"host": "alpha"}),
        ("doctor", {}),
        ("doctor", {"host": "beta", "repository": "brigade"}),
        ("status", {"request_id": REQUEST}),
        ("status", {"request_id": REQUEST, "include_output": True}),
        ("wait", {"request_id": REQUEST, "timeout": 12.0}),
        ("wait", {"request_id": REQUEST, "timeout": 30.0, "include_output": True}),
    ]
    assert run.bodies() == [{"schema": "t3-fleet.request.v1", "action": a, **p} for a, p in payloads]
    assert [kw["timeout"] for _, kw in run.calls] == [60, 60, 90, 90, 60, 60, 57, 75]
    assert all(
        argv == [tools.runtime.paths.bin.t3_fleet, p["action"]]
        for (argv, _), p in zip(run.calls, run.bodies(), strict=True)
    )


def test_capacity_summary_and_config_failure(fleet):
    tools, _, _ = fleet
    assert tools.fleet_capacity()["controller"] == {
        "controller_host": "alpha",
        "enrollment": "off",
        "enabled_hosts": ["alpha", "beta"],
        "repositories": {"brigade": ["alpha", "beta"], "remote-only": ["beta"]},
    }
    tools.runtime.paths.fleet_config.unlink()
    assert tools.fleet_capacity()["controller"] == {"error": "fleet_config_unavailable"}


def test_submit_exact_payload_and_journal(fleet):
    tools, run, commands = fleet
    result = submit(tools)
    assert run.bodies() == [expected_submit()]
    assert not commands.calls
    assert run.calls[0][1]["timeout"] == 150
    assert result["request_id"] == REQUEST
    assert result["source_revision"] == SHA
    assert result["source_revision_basis"] == "caller supplied"
    operation = tools.fleet_recent()["operations"][0]
    assert (operation["tool"], operation["request_id"], operation["outcome"]) == (
        "fleet_submit",
        REQUEST,
        "ok:running",
    )
    assert "prompt" not in operation["summary"]
    assert "p\n\n## Owner" not in json.dumps(operation)


def test_controls_and_long_runtime_ids(fleet):
    tools, run, _ = fleet
    control_id = "runtime-request:provider:codex:" + "turn%3A" * 32 + ":request-1"
    tools.fleet_followup(REQUEST, "Continue with tests.", read_only=True)
    tools.fleet_answer(REQUEST, control_id, {"q1": "yes"})
    tools.fleet_approve(REQUEST, control_id, "acceptForSession")
    tools.fleet_interrupt(REQUEST)
    payloads = [
        ("followup", {"prompt": "Continue with tests.", "runtime_mode": "full-access", "interaction_mode": "default"}),
        ("answer", {"input_id": control_id, "answers": {"q1": "yes"}}),
        ("approve", {"approval_id": control_id, "decision": "acceptForSession"}),
        ("interrupt", {}),
    ]
    assert run.bodies() == [
        {"schema": "t3-fleet.request.v1", "action": a, "request_id": REQUEST, **p} for a, p in payloads
    ]
    assert all(kw["timeout"] == 120 for _, kw in run.calls)
    recent = tools.fleet_recent()["operations"]
    assert [r["tool"] for r in recent] == ["fleet_interrupt", "fleet_approve", "fleet_answer", "fleet_followup"]
    assert all(r["request_id"] == REQUEST and r["outcome"] == "ok:running" for r in recent)


def test_origin_resolution_and_fallback_fail_closed(fleet):
    tools, run, commands = fleet
    result = submit(tools, source_revision=None, title="⠕ Fix issue 9")
    assert commands.calls[0][0] == [
        tools.runtime.paths.bin.git,
        "-C",
        "/srv/repos/brigade",
        "ls-remote",
        "--exit-code",
        "origin",
        "refs/heads/main",
    ]
    assert commands.calls[0][1]["timeout"] == 20
    assert commands.calls[0][1]["max_output"] == 65536
    assert "ls-remote" in result["source_revision_basis"]
    assert run.bodies()[0] == expected_submit(title="⠕ Fix issue 9")
    commands.calls.clear()
    commands.results = [CommandResult(2, b""), CommandResult(0, (OTHER_SHA + "\n").encode())]
    result = submit(tools, source_revision=None)
    assert commands.calls[1][0] == [
        tools.runtime.paths.bin.git,
        "-C",
        "/srv/repos/brigade",
        "rev-parse",
        "--verify",
        "--quiet",
        "refs/remotes/origin/main^{commit}",
    ]
    assert result["source_revision"] == OTHER_SHA
    assert "may be stale" in result["source_revision_basis"]
    commands.calls.clear()
    commands.results = [CommandResult(128, b"")]
    calls = len(run.calls)
    for repository in ("brigade", "remote-only"):
        with pytest.raises(DotError, match="^source_revision_unavailable$"):
            submit(tools, repository=repository, source_revision=None)
    assert len(run.calls) == calls


@pytest.mark.parametrize(
    ("extra", "error"),
    [
        ({"host": "auto"}, "explicit_host_required"),
        ({"seat": None}, "seat_required"),
        ({"workload": "review"}, "requires_enrollment"),
        ({"override_reason": "pin"}, "requires_enrollment"),
        ({"host": "retired"}, "unknown_host"),
        ({"repository": "nope"}, "unknown_repository"),
        ({"allow_queue": True}, "queue_unavailable"),
    ],
)
def test_legacy_refusals_before_commands_and_journal(fleet, extra, error):
    tools, run, commands = fleet
    assert submit(tools, **extra)["error"] == error
    assert not run.calls and not commands.calls and not tools.fleet_recent()["operations"]


def test_enrolled_overrides_and_queue(fleet):
    tools, run, _ = fleet
    tools.runtime.paths.fleet_config.write_text(
        CONFIG + '[policy]\nenrollment = "required"\n[outbox]\nenabled = true\n'
    )
    submit(tools, host="auto", seat=None, workload="implementation", allow_queue=True)
    expected = expected_submit(host="auto", workload="implementation", allow_queue=True)
    del expected["seat"]
    assert run.bodies() == [expected]
    for extra in ({"host": "beta", "seat": None}, {"host": "auto"}):
        assert submit(tools, **extra)["error"] == "override_reason_required"
    submit(tools, title="--help", override_reason="-x regression")
    assert run.bodies()[-1] == expected_submit(title="--help", override_reason="-x regression")
    assert len(run.calls) == 2


def test_legacy_outbox_allows_queue(fleet):
    tools, run, _ = fleet
    tools.runtime.paths.fleet_config.write_text(CONFIG + "[outbox]\nenabled = true\n")
    submit(tools, allow_queue=True)
    assert run.bodies() == [expected_submit(allow_queue=True)]


@pytest.mark.parametrize("method", ["submit", "followup"])
def test_authorization_and_byte_limit(fleet, method):
    tools, run, _ = fleet
    call = (
        (lambda p, **kw: submit(tools, prompt=p, **kw))
        if method == "submit"
        else (lambda p, **kw: tools.fleet_followup(REQUEST, p, **kw))
    )
    call("p")
    assert run.bodies()[-1]["prompt"] == expected_submit()["prompt"]
    call("é" * (MAX_PROMPT_BYTES // 2), read_only=True)
    with pytest.raises(DotError, match="^prompt_too_large$"):
        call("é" * (MAX_PROMPT_BYTES // 2))
    assert len(run.calls) == 2


def test_error_envelope_and_journal_code(fleet):
    tools, run, _ = fleet
    run.code = 3
    run.output = json.dumps(
        {
            "schema": "t3-fleet.error.v1",
            "request_id": REQUEST,
            "error": {
                "code": "policy_rejected",
                "reason": "admission-required",
                "message": "private",
                "detail": {"retry": False},
            },
        }
    )
    result = tools.fleet_followup(REQUEST, "go")
    assert result == {
        "error": "fleet_error",
        "fleet": {
            "code": "policy_rejected",
            "reason": "admission-required",
            "message": "private",
            "detail": {"retry": False},
        },
        "request_id": REQUEST,
        "exit_code": 3,
        "operation_id": 1,
    }
    recent = tools.fleet_recent()["operations"][0]
    assert recent["outcome"] == "fleet_error:policy_rejected"
    assert recent["summary"]["error_message"] == "private"
    run.output = json.dumps({"schema": "t3-fleet.error.v1", "error": {"code": "target_busy"}})
    assert tools.fleet_status(REQUEST) == {"error": "fleet_error", "fleet": {"code": "target_busy"}, "exit_code": 3}


@pytest.mark.parametrize(
    "method,args",
    [
        ("fleet_submit", None),
        ("fleet_followup", (REQUEST, "go")),
        ("fleet_answer", (REQUEST, "input-1", {"q": "yes"})),
        ("fleet_approve", (REQUEST, "approval-1", "accept")),
        ("fleet_interrupt", (REQUEST,)),
        ("fleet_status", (REQUEST,)),
        ("fleet_wait", (REQUEST,)),
        ("fleet_doctor", ()),
        ("fleet_capacity", ()),
    ],
)
def test_timeouts_and_protocol_errors(fleet, method, args):
    tools, run, _ = fleet
    run.raises = subprocess.TimeoutExpired("fleet", 1)
    call = (lambda: submit(tools)) if args is None else lambda: getattr(tools, method)(*args)
    mutating = method in {"fleet_submit", "fleet_followup", "fleet_answer", "fleet_approve", "fleet_interrupt"}
    result = call()
    assert result["error"] == ("fleet_uncertain" if mutating else "fleet_timeout")
    if mutating:
        assert result["next"] == "Do not resubmit. Check fleet_recent and fleet_status for the request."
        assert tools.fleet_recent()["operations"][0]["outcome"] == "fleet_uncertain"
    if method == "fleet_submit":
        assert result["request_id"] is None
    run.raises = None
    run.output = "not json"
    # Untrusted output after a mutating call means the side effect is unknown, as with a timeout.
    assert call()["error"] == ("fleet_uncertain" if mutating else "fleet_unavailable")


def test_mutating_protocol_failure_keeps_the_request_handle(fleet):
    tools, run, _ = fleet
    run.output = json.dumps({"schema": "t3-fleet.result.v1", "request_id": REQUEST, "state": "running"})
    run.code = 3
    result = submit(tools)
    assert result["error"] == "fleet_uncertain"
    assert result["next"] == "Do not resubmit. Check fleet_recent and fleet_status for the request."
    assert result["request_id"] == REQUEST
    operation = tools.fleet_recent()["operations"][0]
    assert (operation["request_id"], operation["outcome"]) == (REQUEST, "fleet_uncertain")
    assert tools.fleet_status(REQUEST)["error"] == "fleet_unavailable"


@pytest.mark.parametrize("kind", ["missing", "invalid", "writable", "symlink", "oversized", "directory"])
def test_config_fails_closed(fleet, kind):
    tools, run, _ = fleet
    path = tools.runtime.paths.fleet_config
    if kind in {"missing", "directory"}:
        path.unlink()
        if kind == "directory":
            path.mkdir(mode=0o700)
    elif kind == "invalid":
        path.write_text("hosts = []")
    elif kind == "writable":
        path.chmod(0o666)
    elif kind == "symlink":
        target = path.with_suffix(".real")
        path.rename(target)
        path.symlink_to(target)
    else:
        path.write_bytes(b"#" * 262145)
    with pytest.raises(DotError, match="^fleet_config_unavailable$"):
        load_fleet_config(path)
    assert not run.calls


def completion_service(tools, run):
    hub = FakeHub()
    service = EventService(
        tools.state,
        fleet_status=tools.fleet_status,
        hub=hub,
        hub_poll_seconds=30,
        callback_hosts=frozenset({"connectors.api.openai.com"}),
    )
    submit(tools)
    run.output = json.dumps({"schema": "t3-fleet.result.v1", "request_id": REQUEST, "state": "completed"})
    return service, hub


def test_completion_once_no_local_inbox_and_followup_reopens(fleet):
    tools, run, _ = fleet
    events, hub = completion_service(tools, run)
    asyncio.run(events.poll_requests())
    asyncio.run(events.poll_requests())
    assert hub.calls == [(REQUEST, 0, "completed")]
    with tools.state.transaction() as c:
        assert c.execute("SELECT count(*) FROM inbox").fetchone()[0] == 0
        assert c.execute("SELECT completed FROM tracked_requests").fetchone()[0] == 1
        assert c.execute("SELECT source FROM event_queue").fetchone()[0] == f"request:{REQUEST}:0"
    tools.fleet_followup(REQUEST, "Continue")
    asyncio.run(events.poll_requests())
    assert hub.calls[-1] == (REQUEST, 1, "completed")
    assert len(hub.calls) == 2


@pytest.mark.parametrize("failure", ["raises", "false", "generation"])
def test_completion_failure_or_race_keeps_open(fleet, failure):
    tools, run, _ = fleet
    events, hub = completion_service(tools, run)

    def send(*args):
        if failure == "raises":
            raise OSError("offline")
        if failure == "generation":
            tools.fleet_followup(REQUEST, "Continue")
            return True
        return False

    with patch.object(hub, "send_completion", side_effect=send):
        asyncio.run(events.poll_requests())
    with tools.state.transaction() as c:
        assert c.execute("SELECT completed FROM tracked_requests").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM event_queue").fetchone()[0] == 0
    # A successful queued delivery is accepted by the event service as True.
    asyncio.run(events.poll_requests())
    with tools.state.transaction() as c:
        assert c.execute("SELECT completed FROM tracked_requests").fetchone()[0] == 1


def test_poller_reconciles_every_twenty_and_flushes_after_outage(fleet):
    tools, run, _ = fleet
    events, hub = completion_service(tools, run)
    with (
        patch.object(hub, "sync", side_effect=DotError("hub_unavailable")) as sync,
        patch.object(hub, "reconcile") as reconcile,
        patch.object(hub, "flush_pending") as flush,
    ):
        for _ in range(20):
            asyncio.run(events.poll_hub())
    assert (sync.call_count, reconcile.call_count, flush.call_count) == (20, 1, 20)


def test_journal_error_message_keeps_bridge_bound(fleet):
    tools, run, _ = fleet
    message = "a" * 400
    run.output = json.dumps({"schema": "t3-fleet.error.v1", "error": {"code": "failed", "message": message}})
    tools.fleet_interrupt(REQUEST)
    assert tools.fleet_recent()["operations"][0]["summary"]["error_message"] == message[:300]


def test_runner_passes_the_validated_fleet_config(fleet):
    # Dot validates against runtime.paths.fleet_config; t3-fleet must read that same file.
    tools, run, _ = fleet
    tools.fleet_capacity()
    _, kwargs = run.calls[-1]
    assert kwargs["env"]["T3_FLEET_CONFIG"] == str(tools.runtime.paths.fleet_config)


def test_default_runner_carries_the_configured_fleet_file(fleet):
    tools, _, _ = fleet
    default = FleetTools(tools.runtime, tools.state)
    assert default.runner.fleet_config == tools.runtime.paths.fleet_config
