"""t3-dispatch subprocess boundary: machine-mode stdin JSON, error envelopes, git rev resolution."""

from __future__ import annotations

import json
import subprocess

import pytest

from brigade.grokbot_t3 import fleet

SHA = "0123456789abcdef0123456789abcdef01234567"


class FakeRun:
    def __init__(self, stdout="", returncode=0, raises=None):
        self.stdout = stdout
        self.returncode = returncode
        self.raises = raises
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr="")


def _ok(**fields):
    return json.dumps({"schema": "t3-fleet.result.v1", **fields})


def test_submit_sends_prompt_on_stdin_only():
    run = FakeRun(_ok(request_id="host-a-r1", state="dispatching", host="host-a"))
    runner = fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=run)
    result = runner.submit(
        host="host-a",
        repository="brigade",
        source_revision=SHA,
        title="[grokbot:Builder] Fix",
        seat="dsflash",
        prompt="SECRET PROMPT TEXT",
        interaction_mode="default",
        override_reason=None,
    )
    assert result["request_id"] == "host-a-r1"
    argv, kwargs = run.calls[0]
    assert argv == ["/bin/t3-fleet", "submit"]
    assert "SECRET PROMPT TEXT" not in " ".join(argv)
    body = json.loads(kwargs["input"])
    assert body["schema"] == "t3-fleet.request.v1"
    assert body["action"] == "submit"
    assert body["prompt"] == "SECRET PROMPT TEXT"
    assert body["runtime_mode"] == "full-access"
    # The Fleet Hub board shows who launched each T3 thread.
    assert body["initiator_kind"] == "agent"
    assert body["initiator_label"] == "grokbot"
    assert "override_reason" not in body
    assert kwargs["timeout"] == fleet.SUBMIT_TIMEOUT
    assert set(kwargs["env"]) <= set(fleet.ENV_KEYS)


def test_override_reason_is_sent_when_given():
    run = FakeRun(_ok(request_id="r1", state="queued"))
    fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=run).submit(
        host="auto",
        repository="brigade",
        source_revision=SHA,
        title="t",
        seat="dsflash",
        prompt="p",
        interaction_mode="plan",
        override_reason="why",
    )
    body = json.loads(run.calls[0][1]["input"])
    assert body["override_reason"] == "why"
    assert body["interaction_mode"] == "plan"


def test_error_envelope_raises_fleet_call_error():
    stdout = json.dumps(
        {
            "schema": "t3-fleet.error.v1",
            "request_id": "r9",
            "error": {"code": "policy_rejected", "reason": "model-admission-denied", "message": "m"},
        }
    )
    runner = fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=FakeRun(stdout, returncode=2))
    with pytest.raises(fleet.FleetCallError) as info:
        runner.status("r9")
    assert (info.value.code, info.value.reason, info.value.request_id) == (
        "policy_rejected",
        "model-admission-denied",
        "r9",
    )


def test_garbage_output_is_internal_error():
    runner = fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=FakeRun("not json"))
    with pytest.raises(fleet.FleetProtocolError) as info:
        runner.capacity()
    assert info.value.code == "internal_error"


def test_undecodable_output_is_a_protocol_error():
    run = FakeRun(raises=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"))
    with pytest.raises(fleet.FleetProtocolError) as info:
        fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=run).capacity()
    assert info.value.code == "internal_error"


def test_valid_result_with_nonzero_exit_keeps_request_id():
    run = FakeRun(_ok(request_id="host-a-r7", state="running"), returncode=3)
    with pytest.raises(fleet.FleetProtocolError) as info:
        fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=run).status("host-a-r7")
    assert info.value.request_id == "host-a-r7"


def test_missing_binary_is_definite_failure_not_protocol():
    run = FakeRun(raises=FileNotFoundError("t3-fleet"))
    with pytest.raises(fleet.FleetCallError) as info:
        fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=run).capacity()
    assert not isinstance(info.value, fleet.FleetProtocolError)


def test_timeout_raises_fleet_timeout():
    run = FakeRun(raises=subprocess.TimeoutExpired(["x"], 1))
    with pytest.raises(fleet.FleetTimeout):
        fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=run).status("r1")


def test_wait_passes_timeout_and_extends_subprocess_timeout():
    run = FakeRun(_ok(request_id="r1", state="running"))
    fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=run).wait("r1", 45)
    argv, kwargs = run.calls[0]
    assert json.loads(kwargs["input"])["timeout"] == 45.0
    assert kwargs["timeout"] == 45 + fleet.CALL_TIMEOUT


def test_answer_approve_followup_interrupt_payloads():
    run = FakeRun(_ok(request_id="r1", state="running"))
    runner = fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=run)
    runner.answer("r1", "q1", {"pick": "B"})
    runner.approve("r1", "a1", "accept")
    runner.followup("r1", "next step")
    runner.interrupt("r1")
    bodies = [json.loads(call[1]["input"]) for call in run.calls]
    assert bodies[0] == {
        "schema": "t3-fleet.request.v1",
        "action": "answer",
        "request_id": "r1",
        "input_id": "q1",
        "answers": {"pick": "B"},
    }
    assert bodies[1] == {
        "schema": "t3-fleet.request.v1",
        "action": "approve",
        "request_id": "r1",
        "approval_id": "a1",
        "decision": "accept",
    }
    assert bodies[2]["prompt"] == "next step" and bodies[2]["runtime_mode"] == "full-access"
    assert bodies[3] == {"schema": "t3-fleet.request.v1", "action": "interrupt", "request_id": "r1"}


def test_resolve_revision():
    run = FakeRun(f"{SHA}\trefs/heads/main\n")
    runner = fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=run)
    assert runner.resolve_revision("https://github.com/x/y.git", "main") == SHA
    assert run.calls[0][0] == [
        "/usr/bin/git",
        "ls-remote",
        "--exit-code",
        "https://github.com/x/y.git",
        "refs/heads/main",
    ]


@pytest.mark.parametrize("stdout,code", [("", 2), ("abc\trefs/heads/main\n", 0), (f"{SHA}\tx\n{SHA}\ty\n", 0)])
def test_resolve_revision_failures(stdout, code):
    runner = fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=FakeRun(stdout, returncode=code))
    with pytest.raises(fleet.RevisionError):
        runner.resolve_revision("https://github.com/x/y.git", "main")


def test_runtime_and_followup_interaction_modes_are_configurable():
    run = FakeRun(_ok(request_id="r1", state="running"))
    runner = fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=run, runtime_mode="read-only")
    runner.submit(
        host="host-a",
        repository="brigade",
        source_revision=SHA,
        title="t",
        seat="sol61",
        prompt="p",
        interaction_mode="plan",
        override_reason=None,
    )
    runner.followup("r1", "next", interaction_mode="plan")
    runner.followup("r1", "default interaction")
    bodies = [json.loads(call[1]["input"]) for call in run.calls]
    assert all(body["runtime_mode"] == "read-only" for body in bodies)
    assert [body["interaction_mode"] for body in bodies] == ["plan", "plan", "default"]


def test_child_env_preserves_fleet_config_without_unrelated_secrets(monkeypatch):
    monkeypatch.setenv("T3_FLEET_CONFIG", "/srv/t3/fleet.toml")
    monkeypatch.setenv("UNRELATED_SECRET", "not-a-real-secret")
    environment = fleet.child_env()
    assert environment["T3_FLEET_CONFIG"] == "/srv/t3/fleet.toml"
    assert "UNRELATED_SECRET" not in environment


def test_nested_result_envelope_is_flattened():
    stdout = json.dumps(
        {"schema": "t3-fleet.result.v1", "request_id": "host-a-r5", "result": {"state": "running", "host": "host-a"}}
    )
    result = fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=FakeRun(stdout)).status("host-a-r5")
    assert result["state"] == "running"
    assert result["host"] == "host-a"
    assert result["request_id"] == "host-a-r5"
    assert "result" not in result
