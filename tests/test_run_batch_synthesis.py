"""Optional batch synthesis: one budgeted, receipted call separate from tasks."""

import hashlib
import json
import os
from unittest.mock import Mock

import pytest

from brigade import agents, cli, run_batch_synthesis
from brigade.aboyeur import planning
from brigade.run_batch import SCHEMA
from tests._home import set_home
from tests.test_run_batch_execute import task

# Reuse the guarded-orchestrator batch harness fixture from the execution tests.
pytest_plugins = ("tests.test_run_batch_execute",)

SYNTHESIS = {"text": "Summarize the results", "seat": "writer"}


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    set_home(monkeypatch, tmp_path / "home")


def add_writer(root):
    roster = root / ".brigade" / "roster.toml"
    roster.write_text(roster.read_text() + '\n[agents.writer]\ncli = "codex"\nrole = "review"\n')


def run(setup, capsys, tasks, *, synthesis=None, extra_args=()):
    root, _ = setup
    payload = {"schema": SCHEMA, "request_id": "req", "tasks": tasks}
    if synthesis is not None:
        payload["synthesis"] = synthesis
    path = root / "batch.json"
    path.write_text(json.dumps(payload))
    rc = cli.main(["run", "batch", str(path), "--cwd", str(root), "--json", *extra_args])
    captured = capsys.readouterr()
    assert captured.out, captured.err
    return rc, json.loads(captured.out), captured.err


def run_dir_of(setup, result):
    return setup[0] / ".brigade" / "runs" / result["run_id"]


def synth_receipt(run_dir):
    return json.loads((run_dir / "batch-synthesis.json").read_text())


def envelopes(run_dir):
    path = run_dir / "message-envelopes.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def synthesized(setup, monkeypatch, *, ok=True, text="joined answer", crash=False):
    root, calls = setup
    receipts = []

    def fake(roster, prompt, *, seat=None, **kwargs):
        receipts.append(seat)
        calls.append("synthesis")
        if crash:
            raise KeyboardInterrupt
        return agents.AgentResult(
            text=text,
            ok=ok,
            detail="" if ok else "synthesis failed",
            failure_phase=None if ok else "synthesis",
            failure_kind=None if ok else "agent-error",
        )

    monkeypatch.setattr(planning, "_run_orchestrator", fake)
    return receipts


def task_calls(calls):
    return [call for call in calls if isinstance(call, str) and call.startswith("You are Brigade worker")]


def test_absent_synthesis_makes_no_call_and_keeps_output(setup, capsys):
    rc, result, err = run(setup, capsys, [task("a")])
    assert rc == 0, err
    assert result["synthesis"] is None
    run_dir = run_dir_of(setup, result)
    assert not (run_dir / "batch-synthesis.json").exists()
    # The per-task summary consumed by existing callers is unchanged.
    summary = json.loads((run_dir / "synthesis.json").read_text())["result"]
    assert "a: completed" in summary["text"] and summary["ok"] is True


def test_synthesis_runs_once_on_requested_seat_after_tasks(setup, capsys, monkeypatch):
    root, calls = setup
    add_writer(root)
    seats = synthesized(setup, monkeypatch)
    rc, result, err = run(setup, capsys, [task("a"), task("b")], synthesis=SYNTHESIS)
    assert rc == 0, err
    assert seats == ["writer"]
    assert len(task_calls(calls)) == 2 and calls[-1] == "synthesis"
    run_dir = run_dir_of(setup, result)
    receipt = synth_receipt(run_dir)
    assert receipt["status"] == "completed"
    assert receipt["seat"] == "writer"
    assert [entry["task_id"] for entry in receipt["inputs"]] == ["a", "b"]
    assert all(entry["result_id"] for entry in receipt["inputs"])
    assert receipt["output_ref"] == "final.txt"
    data = (run_dir / "final.txt").read_bytes()
    # Compare the bytes actually on disk: a text-mode rewrite would translate
    # the trailing newline on Windows and break both values.
    assert receipt["output_bytes"] == len(data)
    assert receipt["output_sha256"] == hashlib.sha256(data).hexdigest()
    assert result["synthesis"]["status"] == "completed"
    assert result["synthesis"]["seat"] == "writer"
    assert all(entry["status"] == "completed" for entry in result["tasks"].values())
    synthesis = [entry for entry in envelopes(run_dir) if entry["phase"] == "synthesis"]
    request = next(entry for entry in synthesis if entry["envelope"]["source"]["kind"] == "synthesis-request")
    answer = next(entry for entry in synthesis if entry["envelope"]["source"]["kind"] == "synthesis-result")
    assert request["to_seat"] == "writer" and answer["from_seat"] == "writer"
    # Exactly one dispatch unit is reserved for the synthesis seat.
    events = [
        json.loads(line) for line in (run_dir / "events" / "lifecycle.jsonl").read_text().splitlines() if line.strip()
    ]
    requested = [
        event
        for event in events
        if event["event_type"] == "run.dispatch.requested" and event["payload"]["seat"] == "writer"
    ]
    assert len(requested) == 1


def test_synthesis_failure_keeps_task_results_and_exits_one(setup, capsys, monkeypatch):
    root, calls = setup
    add_writer(root)
    synthesized(setup, monkeypatch, ok=False, text="")
    rc, result, err = run(setup, capsys, [task("a"), task("b")], synthesis=SYNTHESIS)
    assert rc == 1
    assert "synthesis: failed" in err
    assert all(entry["status"] == "completed" for entry in result["tasks"].values())
    run_dir = run_dir_of(setup, result)
    assert synth_receipt(run_dir)["status"] == "failed"
    # The task-derived summary still reports the completed tasks.
    summary = json.loads((run_dir / "synthesis.json").read_text())["result"]
    assert summary["ok"] is True and "a: completed" in summary["text"]
    assert "a: completed" in (run_dir / "final.txt").read_text()


def test_budget_exhaustion_refuses_synthesis_without_calling(setup, capsys, monkeypatch):
    root, calls = setup
    add_writer(root)
    seats = synthesized(setup, monkeypatch)
    budget = root / "budget.json"
    budget.write_text(
        json.dumps(
            {
                "schema": "brigade.run_budget.v1",
                "schema_version": 1,
                "ceilings": {"worker_dispatch_count": 1},
            }
        )
    )
    rc, result, err = run(setup, capsys, [task("a")], synthesis=SYNTHESIS, extra_args=("--run-budget", str(budget)))
    assert rc == 1
    assert "synthesis: refused-budget" in err
    assert seats == []
    assert len(calls) == 1
    assert result["synthesis"]["status"] == "refused-budget"
    assert result["tasks"]["a"]["status"] == "completed"
    assert synth_receipt(run_dir_of(setup, result))["status"] == "refused-budget"


def test_retry_after_completed_synthesis_makes_no_second_call(setup, capsys, monkeypatch):
    root, calls = setup
    add_writer(root)
    seats = synthesized(setup, monkeypatch)
    rc, first, err = run(setup, capsys, [task("a")], synthesis=SYNTHESIS)
    assert rc == 0, err
    assert seats == ["writer"]
    rc, second, err = run(setup, capsys, [task("a")], synthesis=SYNTHESIS)
    assert rc == 0, err
    assert seats == ["writer"]
    assert len(task_calls(calls)) == 1
    assert second == first


def test_settled_retry_runs_synthesis_only(setup, capsys, monkeypatch):
    root, calls = setup
    add_writer(root)
    seats = synthesized(setup, monkeypatch)
    original = run_batch_synthesis.perform
    monkeypatch.setattr(run_batch_synthesis, "perform", Mock(side_effect=KeyboardInterrupt))
    rc, first, err = run(setup, capsys, [task("a")], synthesis=SYNTHESIS)
    assert rc == 1
    assert first["tasks"]["a"]["status"] == "completed"
    run_dir = run_dir_of(setup, first)
    assert not (run_dir / "batch-synthesis.json").exists()
    assert not (run_dir / "batch-synthesis-dispatch.json").exists()
    monkeypatch.setattr(run_batch_synthesis, "perform", original)
    rc, second, err = run(setup, capsys, [task("a")], synthesis=SYNTHESIS)
    assert rc == 0, err
    # Only the owed synthesis call ran; the task never launched again.
    assert len(task_calls(calls)) == 1
    assert seats == ["writer"]
    assert second["tasks"]["a"]["status"] == "completed"
    assert synth_receipt(run_dir)["status"] == "completed"


def test_reserved_dispatch_without_receipt_is_indeterminate(setup, capsys, monkeypatch):
    root, calls = setup
    add_writer(root)
    seats = synthesized(setup, monkeypatch, crash=True)
    rc, first, err = run(setup, capsys, [task("a")], synthesis=SYNTHESIS)
    assert rc == 1
    assert first["tasks"]["a"]["status"] == "completed"
    run_dir = run_dir_of(setup, first)
    assert (run_dir / "batch-synthesis-dispatch.json").is_file()
    rc, second, err = run(setup, capsys, [task("a")], synthesis=SYNTHESIS)
    assert rc == 1
    # The reserved call may have run, so it is never called a second time.
    assert seats == ["writer"]
    assert synth_receipt(run_dir)["status"] == "indeterminate"
    assert second["synthesis"]["status"] == "indeterminate"
    assert "synthesis: indeterminate" in err


def test_unadmitted_synthesis_seat_refuses_before_any_launch(setup, capsys, monkeypatch):
    root, calls = setup
    monkeypatch.setattr(planning, "_run_orchestrator", Mock(side_effect=AssertionError("synthesis called")))
    rc, result, err = run(setup, capsys, [task("a")], synthesis={"text": "Summarize", "seat": "ghost"})
    assert rc == 2
    assert result["error"]["code"] == "invalid-seat"
    assert calls == []
    assert not list(root.glob(".brigade/runs/*/batch-result-*.json"))


def lifecycle(run_dir):
    path = run_dir / "events" / "lifecycle.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def dispatch_attempts(run_dir, seat):
    return [
        event["payload"].get("attempt")
        for event in lifecycle(run_dir)
        if event["event_type"] == "run.dispatch.requested" and event["payload"].get("seat") == seat
    ]


def sidecar_document(**overrides):
    """A document with exactly the closed sidecar key set."""
    payload = {
        "schema": run_batch_synthesis.SCHEMA,
        "request_id": "req",
        "seat": "writer",
        "status": "completed",
        "inputs": [{"task_id": "a", "result_id": "result-a"}],
        "output_ref": "final.txt",
        "output_sha256": "0" * 64,
        "output_bytes": 1,
        "reason": None,
        "error_class": None,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def settled_without_synthesis(setup, capsys, monkeypatch):
    """Accept a batch and settle its tasks while leaving synthesis owed."""
    root, _calls = setup
    add_writer(root)
    original = run_batch_synthesis.perform
    monkeypatch.setattr(run_batch_synthesis, "perform", Mock(side_effect=KeyboardInterrupt))
    rc, first, err = run(setup, capsys, [task("a")], synthesis=SYNTHESIS)
    assert rc == 1, err
    assert first["tasks"]["a"]["status"] == "completed"
    run_dir = run_dir_of(setup, first)
    assert not (run_dir / "batch-synthesis.json").exists()
    assert not (run_dir / "batch-synthesis-dispatch.json").exists()
    monkeypatch.setattr(run_batch_synthesis, "perform", original)
    return run_dir


def poisoned_retry(setup, capsys, monkeypatch, run_dir):
    """Retry an owed synthesis after poisoning one receipt path."""
    root, calls = setup
    seats = synthesized(setup, monkeypatch)
    before = len(calls)
    rc, result, err = run(setup, capsys, [task("a")], synthesis=SYNTHESIS)
    return rc, result, err, len(calls) - before, seats, run_dir


def test_symlinked_synthesis_sidecar_never_reserves_or_calls(setup, capsys, monkeypatch, settled_without_synthesis):
    root, _calls = setup
    run_dir = settled_without_synthesis
    target = root / "elsewhere.json"
    target.write_text(json.dumps(sidecar_document()))
    os.symlink(target, run_dir / "batch-synthesis.json")

    rc, result, err, new_calls, seats, _ = poisoned_retry(setup, capsys, monkeypatch, run_dir)

    assert rc == 1
    assert new_calls == 0 and seats == []
    assert dispatch_attempts(run_dir, "writer") == []
    assert result["synthesis"]["status"] == "indeterminate"
    assert result["synthesis"]["reason"] == "sidecar-invalid"
    assert "synthesis: indeterminate" in err
    # The untrusted entry is left exactly as found: no fact was written over it.
    assert (run_dir / "batch-synthesis.json").is_symlink()


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"{", id="malformed-json"),
        pytest.param(json.dumps(sidecar_document(status="weird")).encode(), id="open-status"),
        pytest.param(json.dumps(sidecar_document(extra="field")).encode(), id="extra-key"),
        pytest.param(b"x" * (64 * 1024 + 1), id="oversized"),
    ],
)
def test_untrusted_synthesis_sidecar_never_reserves_or_calls(
    setup, capsys, monkeypatch, settled_without_synthesis, raw
):
    run_dir = settled_without_synthesis
    (run_dir / "batch-synthesis.json").write_bytes(raw)

    rc, result, err, new_calls, seats, _ = poisoned_retry(setup, capsys, monkeypatch, run_dir)

    assert rc == 1
    assert new_calls == 0 and seats == []
    assert dispatch_attempts(run_dir, "writer") == []
    assert result["synthesis"]["status"] == "indeterminate"
    assert result["synthesis"]["reason"] == "sidecar-invalid"
    assert (run_dir / "batch-synthesis.json").read_bytes() == raw


@pytest.mark.parametrize("kind", ["malformed", "symlink"])
def test_stray_synthesis_dispatch_marker_never_reserves_or_calls(
    setup, capsys, monkeypatch, settled_without_synthesis, kind
):
    root, _calls = setup
    run_dir = settled_without_synthesis
    marker = run_dir / "batch-synthesis-dispatch.json"
    if kind == "symlink":
        target = root / "marker.json"
        target.write_text(json.dumps({"seat": "writer", "attempt": 1}))
        os.symlink(target, marker)
    else:
        marker.write_bytes(b"{}")

    rc, result, err, new_calls, seats, _ = poisoned_retry(setup, capsys, monkeypatch, run_dir)

    assert rc == 1
    assert new_calls == 0 and seats == []
    assert dispatch_attempts(run_dir, "writer") == []
    assert result["synthesis"]["status"] == "indeterminate"
    assert result["synthesis"]["reason"] == "marker-invalid"
    assert json.loads((run_dir / "batch-synthesis.json").read_text())["reason"] == "marker-invalid"


def test_codex_cloud_synthesis_seat_refuses_before_any_task_launches(setup, capsys, monkeypatch):
    root, calls = setup
    roster = root / ".brigade" / "roster.toml"
    roster.write_text(
        roster.read_text()
        + '\n[agents.cloud]\ncli = "codex-cloud:task-123"\nrole = "implement"\ncloud_safe_mode = true\n'
    )
    rc, result, err = run(setup, capsys, [task("a")], synthesis={"text": "Summarize", "seat": "cloud"})
    assert rc == 2
    assert result["error"]["code"] == "invalid-seat"
    assert "codex-cloud" in result["error"]["message"]
    assert calls == []
    assert not list(root.glob(".brigade/runs/*/batch-result-*.json"))


def test_unhealthy_synthesis_seat_refuses_before_any_task_launches(setup, capsys, monkeypatch):
    from brigade import seat_health
    from tests.test_aboyeur_orchestrator_health_routing import PerSeatFakeAdapter

    root, calls = setup
    roster = root / ".brigade" / "roster.toml"
    roster.write_text(roster.read_text() + '\n[agents.writer]\ncli = "claude"\nrole = "review"\n')
    # A distinct CLI gives the synthesis seat its own health fingerprint, so the
    # probe cannot reuse the healthy coder seat's cached checks.
    real_probe = type(seat_health.SeatHealthProbe())
    adapter = PerSeatFakeAdapter(unhealthy=frozenset({"writer"}))
    monkeypatch.setattr(seat_health, "SeatHealthProbe", lambda **kwargs: real_probe(adapter=adapter))
    rc, _, err = run(setup, capsys, [task("a")], synthesis=SYNTHESIS)
    assert rc == 2
    assert "writer" in err
    assert calls == []


def test_synthesis_runs_the_real_orchestrator_seat_routing_and_lease(setup, capsys, monkeypatch):
    from brigade.run_batch_execute import BatchExecution
    from tests.test_run_batch_execute import ORIGINALS

    root, _calls = setup
    roster = root / ".brigade" / "roster.toml"
    roster.write_text(roster.read_text() + '\n[agents.writer]\ncli = "codex"\nrole = "review"\nmodel = "gpt-6.1-sol"\n')
    # Only the provider call is mocked: seat routing, the model lease, and the
    # orchestrator admission all run their production code.
    monkeypatch.setattr(planning, "_run_orchestrator", ORIGINALS["_run_orchestrator"])
    seen = []

    def provider(cli, prompt, **kwargs):
        seen.append((cli, kwargs.get("model"), prompt.splitlines()[0]))
        if prompt.startswith("You are the Brigade orchestrator"):
            return agents.AgentResult(text="joined answer", ok=True)
        return agents.AgentResult(text="task done", ok=True)

    monkeypatch.setattr(agents, "run_agent", provider)
    contexts = []
    original_context = BatchExecution.synthesis_context

    def spy(self, **kwargs):
        context = original_context(self, **kwargs)
        contexts.append(context)
        return context

    monkeypatch.setattr(BatchExecution, "synthesis_context", spy)

    rc, result, err = run(setup, capsys, [task("a")], synthesis=SYNTHESIS)

    assert rc == 0, err
    assert contexts and contexts[0].model_lease is not None
    assert [entry for entry in seen if entry[2].startswith("You are the Brigade orchestrator")] == [
        ("codex", "gpt-6.1-sol", "You are the Brigade orchestrator. Synthesize the final answer for the user.")
    ]
    run_dir = run_dir_of(setup, result)
    data = (run_dir / "final.txt").read_bytes()
    assert data == b"joined answer\n"
    receipt = synth_receipt(run_dir)
    assert receipt["status"] == "completed"
    assert receipt["reason"] is None and receipt["error_class"] is None
    assert receipt["output_sha256"] == hashlib.sha256(data).hexdigest()
    assert receipt["output_bytes"] == len(data)


def test_task_and_synthesis_on_one_seat_keep_distinct_dispatch_attempts(setup, capsys, monkeypatch):
    root, calls = setup
    seats = synthesized(setup, monkeypatch)
    rc, result, err = run(setup, capsys, [task("a")], synthesis={"text": "Summarize", "seat": "coder"})
    assert rc == 0, err
    assert seats == ["coder"]
    run_dir = run_dir_of(setup, result)
    assert sorted(dispatch_attempts(run_dir, "coder")) == [1, 2]
    marker = json.loads((run_dir / "batch-synthesis-dispatch.json").read_text())
    assert marker == {"seat": "coder", "attempt": 2}
    assert synth_receipt(run_dir)["seat"] == "coder"


def test_synthesis_only_retry_keeps_plan_and_candidate_set_bytes(setup, capsys, monkeypatch):
    root, _calls = setup
    add_writer(root)
    synthesized(setup, monkeypatch)
    original = run_batch_synthesis.perform
    monkeypatch.setattr(run_batch_synthesis, "perform", Mock(side_effect=KeyboardInterrupt))
    rc, first, err = run(setup, capsys, [task("a")], synthesis=SYNTHESIS)
    assert rc == 1
    run_dir = run_dir_of(setup, first)
    before = {name: (run_dir / name).read_bytes() for name in ("plan.json", "candidate-set.json", "plan-attempts.json")}
    assert before["candidate-set.json"]
    monkeypatch.setattr(run_batch_synthesis, "perform", original)

    rc, second, err = run(setup, capsys, [task("a")], synthesis=SYNTHESIS)

    assert rc == 0, err
    for name, data in before.items():
        assert (run_dir / name).read_bytes() == data, f"synthesis-only retry rewrote {name}"
    assert synth_receipt(run_dir)["status"] == "completed"


def test_unknown_task_settles_the_retry_at_exit_one(setup, capsys, monkeypatch):
    from tests.test_run_batch_execute import ORIGINALS

    root, _calls = setup
    add_writer(root)

    def crash(cli, prompt, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(agents, "run_agent", crash)
    rc, first, err = run(setup, capsys, [task("a", read_only=False)], synthesis=SYNTHESIS)
    assert rc == 1
    assert first["tasks"]["a"]["status"] == "unknown"
    run_dir = run_dir_of(setup, first)
    assert not (run_dir / "batch-synthesis.json").exists()

    # The owed synthesis call runs for real; the unknown task still owns the exit code.
    monkeypatch.setattr(planning, "_run_orchestrator", ORIGINALS["_run_orchestrator"])
    monkeypatch.setattr(agents, "run_agent", lambda cli, prompt, **kwargs: agents.AgentResult(text="joined", ok=True))
    rc, second, err = run(setup, capsys, [task("a", read_only=False)], synthesis=SYNTHESIS)

    assert rc == 1
    assert second["tasks"]["a"]["status"] == "unknown"
    receipt = synth_receipt(run_dir)
    assert receipt["status"] == "completed"
    # The failed terminal receipt must not rewrite synthesis's exact bytes.
    data = (run_dir / "final.txt").read_bytes()
    assert receipt["output_bytes"] == len(data)
    assert receipt["output_sha256"] == hashlib.sha256(data).hexdigest()
