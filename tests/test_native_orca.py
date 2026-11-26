"""Governed Orca contract tests pinned to Orca 1.4.268."""

from __future__ import annotations

import json
import os
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from brigade import agents, native_child_admission, native_child_capacity, native_child_claim, roster, run_journal
from brigade.native_child_host import NativeChildError
from brigade.native_orca_host import HOST, target_for_seat
from brigade.native_orca_dispatch import LaunchBinding, launch_argv, parse_start, request_key
from brigade.native_orca_observations import cancel_argv, classify_show, observe_argv, parse_stop, result_argv

from tests.test_native_t3_admission import _claim_code, _enroll_run, _workspace

VERSION = "1.4.268"
RUN = "run_000000000001"
TASK = "task_000000000002"
DISPATCH = "ctx_000000000003"
BRIGADE_RUN = "orca-" + "a" * 64
ATTEMPT = "b" * 64
REVISION = "c" * 40
ROSTER = """
orchestrator = "lead"
[agents.lead]
cli = "codex"
role = "lead"
[agents.builder]
cli = "{agent}"
role = "builder"
[orca_targets.builder]
agent = "{agent}"
worktree = "{worktree}"
orca_version = "{version}"
{options}
"""


def _fixture(name: str) -> dict:
    fixture = json.loads((Path(__file__).parent / "fixtures" / "native_orca" / f"{name}.json").read_text())
    assert fixture["orca_version"] == VERSION and fixture["source"].startswith("src/")
    return fixture["payload"]


def _roster(agent="codex", worktree="new-child", version=VERSION, options=""):
    return roster.load_roster(
        Path("example-roster.toml"),
        text=ROSTER.format(agent=agent, worktree=worktree, version=version, options=options),
    )


def _binding() -> LaunchBinding:
    return LaunchBinding(BRIGADE_RUN, "build", ATTEMPT, RUN, TASK, request_key(BRIGADE_RUN, "build", ATTEMPT))


def _start() -> dict:
    payload = _fixture("worker-start")
    payload["result"]["mutation"]["requestId"] = _binding().retry_key
    return payload


def _target():
    return target_for_seat(_roster(), "builder", source_revision=REVISION)


def _worker():
    return parse_start(_binding(), _target(), _start(), exit_code=0)


@pytest.mark.parametrize("agent", ["claude", "codex", "opencode", "antigravity", "pi", "muse"])
def test_declared_agents_bind_to_core_admission(agent):
    loaded = _roster(agent=agent, options="max_live = 1")
    target = target_for_seat(loaded, "builder", source_revision=REVISION)
    assert target.orca_version == VERSION
    assert HOST.declarations(loaded)["builder"]["max_live"] == 1
    assert native_child_admission.evaluate(
        HOST, loaded, "builder", target, hub=native_child_admission.HubAuthority("unenrolled")
    ).admitted


@pytest.mark.parametrize(
    ("agent", "worktree", "options", "code"),
    [
        ("codex", "new-child", 'effort = "high"', "model-required"),
        ("pi", "new-child", 'model = "example"', "model-unsupported"),
        ("pi", "new-child", 'model = "provider/example"\neffort = "low"', None),
        ("muse", "new-child", 'model = "example"\neffort = "max"', None),
        ("opencode", "new-child", 'model = "provider/example"', "worktree-unsupported"),
        ("opencode", "identity:wt2:local:example", 'model = "provider/example"', None),
        ("opencode", "identity:wt2:local:example", 'model = "provider/example"\neffort = "low"', "effort-unsupported"),
        ("codex", "--on", "", "worktree-unsupported"),
        ("codex", "identity:wt2:ssh%3Aexample:checkout", "", "worktree-unsupported"),
        ("codex", "id:example::checkout", "", "worktree-unsupported"),
    ],
)
def test_model_effort_and_placement_rules(agent, worktree, options, code):
    loaded = _roster(agent=agent, worktree=worktree, options=options)
    assert HOST.structural_gap(HOST.resolve_target(loaded, "builder", REVISION)) == code


def test_new_orca_version_is_valid_enrollment_data():
    target = target_for_seat(_roster(version="1.4.271"), "builder", source_revision=REVISION)
    assert target.orca_version == "1.4.271"


def test_malformed_orca_version_is_refused():
    with pytest.raises(NativeChildError) as exc:
        target_for_seat(_roster(version="1.4.269 extra"), "builder", source_revision=REVISION)
    assert exc.value.code == "orca-unsupported"


def test_launch_uses_the_retained_request_and_local_bound_task():
    binding = _binding()
    assert binding.retry_key is not None and UUID(binding.retry_key).version == 8
    assert binding.retry_key != request_key(BRIGADE_RUN, "build", "d" * 64)
    assert binding.retry_key != request_key(BRIGADE_RUN, "review", ATTEMPT)
    assert binding.retry_key != request_key("orca-" + "e" * 64, "build", ATTEMPT)
    assert launch_argv(binding, _target()) == (
        "orca-ide",
        "orchestration",
        "worker-start",
        "--environment",
        "local",
        "--retry-request",
        binding.retry_key,
        "--run",
        RUN,
        "--task",
        TASK,
        "--worktree",
        "new-child",
        "--base-branch",
        REVISION,
        "--agent",
        "codex",
        "--json",
        "--full",
    )
    for key in (None, "", "wrong"):
        with pytest.raises(NativeChildError) as exc:
            launch_argv(replace(binding, retry_key=key), _target())
        assert exc.value.code == "retry-request-invalid"


@pytest.mark.parametrize("declared_version", [VERSION, "1.4.271"])
def test_start_replay_correlates_the_same_attempt(declared_version):
    # The receipt is the 1.4.268 wire fixture. The declaration is separate data.
    target = replace(_target(), orca_version=declared_version)
    payload = _start()
    first = parse_start(_binding(), target, payload, exit_code=0)
    payload["result"]["mutation"]["replayed"] = True
    assert parse_start(_binding(), target, payload, exit_code=0) == first
    assert (first.run_id, first.task_id, first.dispatch_id) == (RUN, TASK, DISPATCH)
    assert first.binding.brigade_attempt_id == ATTEMPT
    assert first.orca_version == declared_version


@pytest.mark.parametrize(
    ("state", "exit_code", "code"),
    [
        ("outcome_unknown", 0, "start-unknown"),
        ("failed", 1, "start-failed"),
        ("ready", 1, "start-unknown"),
    ],
)
def test_non_ready_or_ambiguous_start_refuses(state, exit_code, code):
    payload = _start()
    payload["result"]["state"] = state
    payload["result"]["lastError"] = "synthetic private detail"
    with pytest.raises(NativeChildError) as exc:
        parse_start(_binding(), _target(), payload, exit_code=exit_code)
    assert exc.value.code == code
    assert "synthetic private detail" not in str(exc.value)


@pytest.mark.parametrize("mismatch", ["runId", "taskId", "dispatchId", "mutation", "launch"])
def test_start_cannot_bind_unrelated_receipt(mismatch):
    payload = _start()
    payload["result"][mismatch] = "wrong"
    with pytest.raises(NativeChildError):
        parse_start(_binding(), _target(), payload, exit_code=0)


def test_task_not_startable_is_a_definite_refusal():
    payload = _fixture("task-not-startable")
    with pytest.raises(NativeChildError) as exc:
        parse_start(_binding(), _target(), payload, exit_code=1)
    assert exc.value.code == "start-refused"
    assert "Synthetic" not in str(exc.value)


def test_missing_receipt_does_not_correlate():
    with pytest.raises(NativeChildError) as exc:
        parse_start(_binding(), _target(), None, exit_code=1)
    assert exc.value.code == "start-unknown"


def test_observe_cancel_and_read_address_only_the_bound_dispatch():
    common = ("orca-ide", "orchestration")
    suffix = ("--environment", "local", "--dispatch", DISPATCH)
    worker = _worker()
    assert observe_argv(worker) == (*common, "worker-show", *suffix, "--json", "--full")
    assert cancel_argv(worker)[:7] == (*common, "worker-stop", *suffix)
    assert cancel_argv(worker)[7] == "--retry-request"
    cancel_key = cancel_argv(worker)[8]
    assert UUID(cancel_key).version == 8 and cancel_key != worker.binding.retry_key
    assert result_argv(worker) == (*common, "worker-read", *suffix, "--source", "transcript", "--json")


def test_human_wait_is_healthy_but_missing_agent_wait_is_unknown():
    show = _fixture("worker-show")
    observation = show["result"]["observation"]
    assert classify_show(_worker(), show).status == "running"
    observation["agentWait"] = {"source": "hook", "reason": "synthetic private reason"}
    waiting = classify_show(_worker(), show)
    assert (waiting.family, waiting.status) == ("active", "waiting")
    assert "private reason" not in repr(waiting)
    del observation["agentWait"]
    unknown = classify_show(_worker(), show)
    assert (unknown.family, unknown.reason) == ("awaiting", "missing-evidence")


@pytest.mark.parametrize("state", ["pending", "dispatched", "completed", "failed", "circuit_broken"])
def test_dispatch_states_without_a_result(state):
    show = _fixture("worker-show")
    show["result"]["dispatch"]["status"] = state
    observation = classify_show(_worker(), show)
    assert (
        observation.family
        == {
            "pending": "awaiting",
            "dispatched": "active",
            "completed": "awaiting",
            "failed": "awaiting",
            "circuit_broken": "refused",
        }[state]
    )


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
@pytest.mark.parametrize("show_name", ["worker-show-structured", "worker-show-released"])
def test_worker_done_and_exact_transcript_make_a_quiescent_result(outcome, show_name):
    show, done, transcript = _fixture(show_name), _fixture("worker-done"), _fixture("worker-read")
    status = "completed" if outcome == "succeeded" else "failed"
    show["result"]["dispatch"]["status"] = status
    show["result"]["worker"]["state"] = outcome
    mail = done["result"]["messages"][0]
    mail["payload"] = mail["payload"].replace("succeeded", outcome)
    transcript["result"]["status"]["worker"] = outcome
    obs = classify_show(_worker(), show, worker_done=done, transcript=transcript)
    assert (obs.family, obs.status, obs.containment) == ("terminal-candidate", status, "quiescent")
    assert obs.terminal_run_id == RUN
    assert "synthetic worker text" not in repr(obs)
    assert "synthetic transcript text" not in repr(obs)


def test_repeated_worker_done_still_settles_with_a_stable_result():
    # Orca 1.4.268 accepts a repeated worker_done with the same outcome as a duplicate
    # (worker-report-settlement.ts), so the mailbox can hold two matching rows.
    show, transcript = _fixture("worker-show-structured"), _fixture("worker-read")
    show["result"]["dispatch"]["status"] = "completed"
    show["result"]["worker"]["state"] = "succeeded"
    transcript["result"]["status"]["worker"] = "succeeded"
    done = _fixture("worker-done")
    first = done["result"]["messages"][0]
    repeat = json.loads(json.dumps(first))
    repeat["id"] = "msg_ffffffffffff"
    done["result"]["messages"].append(repeat)
    reordered = json.loads(json.dumps(done))
    reordered["result"]["messages"].reverse()
    obs = classify_show(_worker(), show, worker_done=done, transcript=transcript)
    again = classify_show(_worker(), show, worker_done=reordered, transcript=transcript)
    assert (obs.family, obs.status, obs.containment) == ("terminal-candidate", "completed", "quiescent")
    assert obs == again


@pytest.mark.parametrize("show_name", ["worker-show-resting", "worker-show-kept-open"])
def test_settled_terminal_worker_stays_open_until_released(show_name):
    done, transcript = _fixture("worker-done"), _fixture("worker-read")
    awaiting = classify_show(_worker(), _fixture(show_name), worker_done=done, transcript=transcript)
    assert (awaiting.family, awaiting.reason) == ("awaiting", "containment-unconfirmed")
    released = classify_show(_worker(), _fixture("worker-show-released"), worker_done=done, transcript=transcript)
    assert released.status == "completed"


@pytest.mark.parametrize(
    "gap",
    [
        "liveness",
        "addressable",
        "body",
        "report",
        "mail-run",
        "rejected-report",
        "dispatch",
        "read",
        "clipped",
        "remote",
    ],
)
def test_incomplete_or_conflicting_result_evidence_stays_open(gap):
    show, done, transcript = _fixture("worker-show-structured"), _fixture("worker-done"), _fixture("worker-read")
    mail = done["result"]["messages"][0]
    if gap == "liveness":
        show["result"]["observation"]["status"] = "unverifiable"
    elif gap == "addressable":
        show["result"]["observation"]["addressable"] = True
    elif gap == "body":
        mail["payload"] = "invalid"
    elif gap == "report":
        mail["payload"] = mail["payload"].replace("succeeded", "failed")
    elif gap == "mail-run":
        mail["run_id"] = "run_000000000005"
    elif gap == "rejected-report":
        report = json.loads(mail["payload"])
        report["_orcaLifecycleRejection"] = {"code": "sender_not_assignee", "reason": "synthetic refusal"}
        mail["payload"] = json.dumps(report)
    elif gap == "dispatch":
        show["result"]["dispatch"]["id"] = "ctx_000000000005"
    elif gap == "read":
        transcript["result"]["source"] = "terminal"
    elif gap == "clipped":
        transcript["result"]["contentComplete"] = False
    elif gap == "remote":
        show["result"]["coordinatedFrom"] = "paired"
    assert classify_show(_worker(), show, worker_done=done, transcript=transcript).family != "terminal-candidate"


@pytest.mark.parametrize("show_name", ["worker-show-stopped", "worker-show-structured-stopped"])
def test_only_a_proven_stop_can_return_cancelled(show_name):
    show = _fixture(show_name)
    stop = _fixture("worker-stop")
    stop["result"]["mutation"]["requestId"] = cancel_argv(_worker())[8]
    assert parse_stop(_worker(), stop, exit_code=0).family == "awaiting"
    assert parse_stop(_worker(), stop, exit_code=0, show=show).status == "cancelled"
    stop["result"]["helpers"] = [{"outcome": "kept_open"}]
    assert parse_stop(_worker(), stop, exit_code=0, show=show).family == "awaiting"
    stop["result"]["helpers"] = []
    stop["ok"] = False
    stop["result"]["state"] = "stop_unknown"
    assert parse_stop(_worker(), stop, exit_code=1, show=show).family == "awaiting"


def test_terminal_release_must_belong_to_the_worker():
    show = _fixture("worker-show-released")
    show["result"]["terminalResource"]["ownerDispatchId"] = "ctx_000000000005"
    result = classify_show(_worker(), show, worker_done=_fixture("worker-done"), transcript=_fixture("worker-read"))
    assert (result.family, result.reason) == ("awaiting", "containment-unconfirmed")


def test_roster_refuses_a_seat_declared_for_both_native_hosts():
    with pytest.raises(ValueError, match="native host"):
        _roster(options="[t3_targets.builder]\nmax_live = 1")


def test_muse_has_a_clear_headless_refusal():
    assert _roster(agent="muse").agents["builder"].cli == "muse"
    with pytest.raises(ValueError, match="Orca-only"):
        agents.build_argv("muse", "synthetic prompt")
    result = agents.run_agent("muse", "synthetic prompt")
    assert (result.ok, result.failure_kind) == (False, "native-only")
    assert "Orca-only" in result.detail


def _orca_run(workspace: Path, *, launched: bool) -> Path:
    directory = native_child_capacity.runs_root(workspace) / BRIGADE_RUN
    directory.mkdir(parents=True)
    (directory / "run.json").write_text(json.dumps({"kind": HOST.run_kind, "lock_workspace": str(workspace)}))
    path = directory / "events" / "lifecycle.jsonl"
    run_journal.ensure_journal(path)
    if launched:
        run_journal.append_event(
            path,
            run_id=directory.name,
            event_type="task.launch-claimed",
            payload={"task_id": "build", "seat": "other-seat", "attempt_id": ATTEMPT, "dispatch_attempt": 1},
            idempotency_key="example-launch",
            expected_previous_sequence=0,
        )
    return directory


@pytest.mark.skipif(os.name == "nt", reason="capacity scans bound run directories")
def test_live_t3_unit_blocks_orca_core_claim(tmp_path):
    workspace = _workspace(tmp_path, max_workers=1)
    t3_run = _enroll_run(workspace, tmp_path, request_id="test-cross-host-claim")
    assert _claim_code(workspace, t3_run) is None
    orca_run = _orca_run(workspace, launched=False)
    loaded = replace(_roster(options="max_live = 2"), max_workers=1)
    current = SimpleNamespace(
        result=SimpleNamespace(launch_claim=None, handle=SimpleNamespace(spec=SimpleNamespace(seat="builder")))
    )

    def unexpected(*_args, **_kwargs):
        pytest.fail("capacity denied claim reached admission or budget")

    ops = native_child_claim.ClaimOps(
        capacity_lock=native_child_capacity.capacity_lock,
        governed_occupancy=lambda ws: native_child_capacity.governed_occupancy(HOST, ws),
        assess=lambda current_roster, seat, occupancy: native_child_capacity.assess(
            HOST, current_roster, seat, occupancy
        ),
        owned_run=lambda *_args: nullcontext(),
        load_roster=lambda _ws: loaded,
        run_meta=lambda *_args: {},
        read_events=lambda _directory: [],
        verify=lambda *_args: {"build": current},
        evaluate=unexpected,
        batch_ref=unexpected,
        coordinator=unexpected,
        record_claim=unexpected,
    )
    with pytest.raises(NativeChildError) as exc:
        native_child_claim.run_claim(
            HOST,
            ops,
            workspace=workspace,
            run_dir=orca_run,
            task_id="build",
            hub=native_child_admission.HubAuthority("unenrolled"),
        )
    assert exc.value.code == "capacity-denied"
    occupancy = native_child_capacity.governed_occupancy(HOST, workspace)
    decision = native_child_capacity.assess(HOST, loaded, "builder", occupancy)
    assert (decision.global_free, decision.seat_free) == (0, 2)


@pytest.mark.skipif(os.name == "nt", reason="capacity scans bound run directories")
def test_live_orca_unit_blocks_t3_claim(tmp_path):
    workspace = _workspace(tmp_path, max_workers=1)
    t3_run = _enroll_run(workspace, tmp_path, request_id="test-cross-host-claim")
    _orca_run(workspace, launched=True)
    assert _claim_code(workspace, t3_run) == "capacity-denied"


@pytest.mark.skipif(os.name == "nt", reason="capacity scans bound run directories")
@pytest.mark.parametrize("outcome", ["completed", "cancelled"])
def test_orca_terminal_outcome_frees_capacity_only_after_ack(tmp_path, outcome):
    workspace = _workspace(tmp_path, max_workers=1)
    t3_run = _enroll_run(workspace, tmp_path, request_id="test-cross-host-ack")
    orca_run = _orca_run(workspace, launched=True)
    worker = _worker()
    if outcome == "completed":
        observation = classify_show(
            worker,
            _fixture("worker-show-structured"),
            worker_done=_fixture("worker-done"),
            transcript=_fixture("worker-read"),
        )
    else:
        stop = _fixture("worker-stop")
        stop["result"]["mutation"]["requestId"] = cancel_argv(worker)[8]
        observation = parse_stop(worker, stop, exit_code=0, show=_fixture("worker-show-stopped"))
    assert (observation.family, observation.status) == ("terminal-candidate", outcome)

    journal = orca_run / "events" / "lifecycle.jsonl"
    result_id = "d" * 64
    run_journal.append_event(
        journal,
        run_id=orca_run.name,
        event_type="task.observed",
        payload={
            "task_id": "build",
            "attempt_id": ATTEMPT,
            "status": observation.status,
            "containment": observation.containment,
            "result_id": result_id,
            "artifact_kind": "receipt",
            "artifact_digest": observation.result_transfer_id,
            "provider_task_id": DISPATCH,
            "original_run_id": RUN,
            "provider_result_id": None,
        },
        idempotency_key="example-result",
        expected_previous_sequence=1,
    )
    assert _claim_code(workspace, t3_run) == "capacity-denied"
    run_journal.append_event(
        journal,
        run_id=orca_run.name,
        event_type="task.acknowledged",
        payload={"task_id": "build", "result_id": result_id},
        idempotency_key="example-ack",
        expected_previous_sequence=2,
    )
    assert _claim_code(workspace, t3_run) is None
