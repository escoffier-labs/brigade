"""Offline execution through the real guarded orchestrator and dispatcher."""

import json
import threading
from unittest.mock import Mock

import pytest

from brigade import aboyeur, agents, cli, run_tasks
from brigade.run_batch import SCHEMA


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "home"))
    config = tmp_path / ".brigade"
    config.mkdir()
    (config / "roster.toml").write_text("""orchestrator = "chef"
[limits]
max_workers = 2
[agents.chef]
cli = "codex"
role = "plan"
[agents.coder]
cli = "codex"
role = "implement"
""")
    monkeypatch.setattr(aboyeur.planning, "plan", Mock(side_effect=AssertionError("planner called")))
    monkeypatch.setattr(aboyeur.planning, "_run_orchestrator", Mock(side_effect=AssertionError("synthesis called")))
    calls = []

    def model(agent, prompt, **kwargs):
        calls.append(prompt)
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    return tmp_path, calls


def invoke(setup, capsys, tasks, request="req"):
    root, _ = setup
    path = root / "batch.json"
    path.write_text(json.dumps({"schema": SCHEMA, "request_id": request, "tasks": tasks}))
    rc = cli.main(["run", "batch", str(path), "--cwd", str(root), "--json"])
    captured = capsys.readouterr()
    assert captured.out, captured.err
    return rc, json.loads(captured.out), captured.err


def task(name, deps=()):
    return {"task_id": name, "seat": "coder", "text": "Do the same work", "depends_on": list(deps)}


def test_separate_results_retry_and_conflict(setup, capsys):
    tasks = [task("a"), task("b"), task("c", ["a", "b"])]
    rc, result, err = invoke(setup, capsys, tasks)
    assert rc == 0, err
    assert len(setup[1]) == 3
    assert "task_id: c" in setup[1][-1]
    assert set(result["tasks"]) == {"a", "b", "c"}
    assert len({t["result_id"] for t in result["tasks"].values()}) == 3
    assert all(t["status"] == "completed" and t["result_ref"] for t in result["tasks"].values())
    assert invoke(setup, capsys, tasks)[:2] == (0, result)
    assert len(setup[1]) == 3
    tasks[0]["text"] += " changed"
    rc, conflict, _ = invoke(setup, capsys, tasks)
    assert rc == 2
    assert conflict["error"]["code"] == "request-conflict"
    assert len(setup[1]) == 3


def test_bounded_waves(setup, capsys, monkeypatch):
    active = 0
    peak = 0
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def model(agent, prompt, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=10)
        with lock:
            active -= 1
        setup[1].append(prompt)
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    rc, result, err = invoke(setup, capsys, [task(str(i)) for i in range(4)])
    assert rc == 0, err
    assert peak == 2
    run_dir = setup[0] / ".brigade" / "runs" / result["run_id"]
    assert [a["stage"] for a in json.loads((run_dir / "plan.json").read_text())["assignments"]] == [0, 0, 0, 0]


def test_mixed_success_skips_dependents(setup, capsys, monkeypatch):
    def model(agent, prompt, **kwargs):
        setup[1].append(prompt)
        return agents.AgentResult(text="retained", ok="task_id: b" not in prompt)

    monkeypatch.setattr(agents, "run_agent", model)
    rc, result, err = invoke(setup, capsys, [task("a"), task("b"), task("c", ["a", "b"])])
    assert rc == 1, err
    assert result["tasks"]["a"]["status"] == "completed"
    assert result["tasks"]["b"]["status"] == "failed"
    assert result["tasks"]["c"]["status"] == "queued"
    assert result["tasks"]["c"]["not_run"] is True
    count = len(setup[1])
    invoke(setup, capsys, [task("a"), task("b"), task("c", ["a", "b"])])
    assert len(setup[1]) == count


def test_crash_unknown_never_relaunches(setup, capsys, monkeypatch):
    def crash(agent, prompt, **kwargs):
        setup[1].append(prompt)
        raise KeyboardInterrupt

    monkeypatch.setattr(agents, "run_agent", crash)
    rc, result, err = invoke(setup, capsys, [task("a")])
    assert rc == 1, err
    assert result["tasks"]["a"]["status"] == "unknown"
    assert len(setup[1]) == 1
    assert invoke(setup, capsys, [task("a")])[:2] == (1, result)
    assert len(setup[1]) == 1
    run_dir = setup[0] / ".brigade" / "runs" / result["run_id"]
    assert run_tasks.read_task_batch(run_dir, request_id="req").members[0].status == "unknown"


def test_cancellation_keeps_completed_sibling(setup, capsys, monkeypatch):
    completed = threading.Event()

    def model(agent, prompt, **kwargs):
        setup[1].append(prompt)
        if "task_id: a" in prompt:
            completed.set()
            return agents.AgentResult(text="retained result", ok=True)
        assert completed.wait(timeout=10)
        raise KeyboardInterrupt

    monkeypatch.setattr(agents, "run_agent", model)
    rc, result, err = invoke(setup, capsys, [task("a"), task("b")])
    assert rc == 1, err
    assert result["tasks"]["a"]["status"] == "completed"
    assert result["tasks"]["a"]["result_ref"]
    assert result["tasks"]["b"]["status"] == "unknown"
    assert invoke(setup, capsys, [task("a"), task("b")])[:2] == (1, result)
    assert len(setup[1]) == 2


def test_crash_before_fingerprint_allows_launch(setup, capsys):
    import hashlib

    run_dir = setup[0] / ".brigade" / "runs" / ("batch-" + hashlib.sha256(b"req").hexdigest())
    run_dir.mkdir(parents=True)
    rc, result, _ = invoke(setup, capsys, [task("a")])
    assert rc == 0
    assert result["tasks"]["a"]["status"] == "completed"
    assert len(setup[1]) == 1


def test_timeout_retains_success_and_reports_unfinished(setup, capsys, monkeypatch):
    def model(agent, prompt, **kwargs):
        setup[1].append(prompt)
        if "task_id: a" in prompt:
            return agents.AgentResult(text="retained", ok=True)
        return agents.AgentResult(text="partial", ok=False, timed_out=True, detail="timeout")

    monkeypatch.setattr(agents, "run_agent", model)
    rc, result, err = invoke(setup, capsys, [task("a"), task("b"), task("c", ["b"])])
    assert rc == 1, err
    assert result["tasks"]["a"]["status"] == "completed"
    assert result["tasks"]["b"]["status"] == "failed"
    assert result["tasks"]["c"]["status"] == "queued"
    assert result["tasks"]["c"]["not_run"] is True
    assert result["tasks"]["a"]["result_ref"]
    assert invoke(setup, capsys, [task("a"), task("b"), task("c", ["b"])])[:2] == (1, result)


def test_preflight_refusal_can_retry(setup, capsys, monkeypatch):
    from brigade.cli import run

    original = run.dispatch
    refused = Mock(return_value=2)
    monkeypatch.setattr(run, "dispatch", refused)
    rc, result, _ = invoke(setup, capsys, [task("a")])
    assert rc == 2
    assert setup[1] == []
    monkeypatch.setattr(run, "dispatch", original)
    rc, result, err = invoke(setup, capsys, [task("a")])
    assert rc == 0, err
    assert len(setup[1]) == 1


def test_invalid_second_seat_can_retry_after_roster_repair(setup, capsys):
    second = {**task("b"), "seat": "reviewer"}
    rc, _, err = invoke(setup, capsys, [task("a"), second])
    assert rc == 2
    assert "reviewer" in err
    assert setup[1] == []
    with (setup[0] / ".brigade" / "roster.toml").open("a") as stream:
        stream.write('\n[agents.reviewer]\ncli = "codex"\nrole = "review"\n')
    rc, result, err = invoke(setup, capsys, [task("a"), second])
    assert rc == 0, err
    assert all(t["status"] == "completed" for t in result["tasks"].values())


def test_complete_receipt_and_late_drift(setup, capsys, monkeypatch):
    from brigade import runguard

    rc, result, err = invoke(setup, capsys, [task("a"), task("b")])
    assert rc == 0, err
    run_dir = setup[0] / ".brigade" / "runs" / result["run_id"]
    assert (run_dir / "final.txt").is_file()
    assert (run_dir / "synthesis.json").is_file()
    workers = json.loads((run_dir / "worker-results.json").read_text())
    assert len(workers["results"]) == 2
    checks = 0

    def drift(*args, **kwargs):
        nonlocal checks
        checks += 1
        return "concurrent commit" if checks >= 3 else None

    monkeypatch.setattr(runguard, "detect_branch_head_drift", drift)
    rc, result, err = invoke(setup, capsys, [task("a")], request="drift")
    assert rc != 0
    assert result["run_status"] == "failed"
    assert "concurrent commit" in err


def test_batch_bypasses_preferences_and_resolves_cwd_at_execution(setup, capsys, monkeypatch):
    from brigade import run_preference

    path = setup[0] / "input.json"
    path.write_text(json.dumps({"schema": SCHEMA, "request_id": "prefs", "tasks": [task("a")]}))
    argv = ["run", "batch", str(path), "--json"]
    parsed = cli._build_parser_for(argv).parse_args(argv)
    assert parsed.cwd is None
    monkeypatch.chdir(setup[0])
    monkeypatch.setattr(run_preference, "refresh_cache", Mock(side_effect=AssertionError("preference called")))
    assert parsed.func(parsed) == 0
    assert json.loads(capsys.readouterr().out)["tasks"]["a"]["status"] == "completed"


def test_batch_pins_all_seats_against_health_fallback(setup, capsys, monkeypatch):
    from tests.test_aboyeur_orchestrator_health_routing import PerSeatFakeAdapter, _install_probe

    roster = setup[0] / ".brigade" / "roster.toml"
    roster.write_text(
        roster.read_text() + '\n[agents.reviewer]\ncli = "codex"\nrole = "review"\nfallback = ["coder"]\n'
    )
    _install_probe(monkeypatch, PerSeatFakeAdapter(unhealthy=frozenset({"reviewer"})))
    rc, _, err = invoke(setup, capsys, [task("a"), {**task("b"), "seat": "reviewer"}])
    assert rc == 2
    assert "reviewer" in err
    assert setup[1] == []


def test_batch_resolves_hub_only_seat_and_all_admissions(tmp_path):
    from brigade.run_batch_execute import BatchExecution
    from brigade.roster import Agent, Roster
    from tests.test_fleet_model_admission import _versioned_seat, _versioned_snapshot

    roster = Roster(
        orchestrator="chef",
        agents={"chef": Agent("chef", "codex", "plan"), "coder": Agent("coder", "codex", "implement")},
    )
    execution = BatchExecution({"tasks": [task("a"), {**task("b"), "seat": "reviewer"}]}, tmp_path, tmp_path)
    snapshot = _versioned_snapshot(
        *[_versioned_seat(seat, "openai", "gpt-6.1-sol", instance_id="codex") for seat in ("coder", "reviewer")]
    )
    resolution = execution.resolve_model_policy(roster, worker="coder", snapshot=snapshot)
    assert resolution.error is None
    assert resolution.roster.agents["reviewer"].model == "gpt-6.1-sol"
    assert {a["seat"] for a in resolution.receipt["admissions"]} == {"coder", "reviewer"}
    snapshot["seats"][1]["enabled"] = False
    assert "reviewer" in execution.resolve_model_policy(roster, worker="coder", snapshot=snapshot).error


def test_request_dispatch_lock_refuses_concurrent_retry(setup, capsys):
    from brigade.run_batch_execute import request_lock

    rc, result, err = invoke(setup, capsys, [task("a")])
    assert rc == 0, err
    run_dir = setup[0] / ".brigade" / "runs" / result["run_id"]
    with request_lock(run_dir):
        rc, result, _ = invoke(setup, capsys, [task("a")])
    assert rc == 2
    assert result["error"]["code"] == "request-busy"
    assert len(setup[1]) == 1


@pytest.mark.parametrize("explicit_cwd", [False, True])
def test_retry_from_git_subdirectory_uses_same_fence(setup, capsys, monkeypatch, explicit_cwd):
    import subprocess

    root = setup[0]
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".git" / "info" / "exclude").write_text("batch.json\n")
    subdir = root / "src"
    subdir.mkdir()
    rc, first, err = invoke(setup, capsys, [task("a")])
    assert rc == 0, err
    path = root / "batch.json"
    monkeypatch.chdir(subdir)
    argv = ["run", "batch", str(path), "--json"]
    if explicit_cwd:
        argv.extend(["--cwd", str(subdir)])
    assert cli.main(argv) == 0
    second = json.loads(capsys.readouterr().out)
    assert second == first
    assert len(setup[1]) == 1
    assert not (subdir / ".brigade" / "runs").exists()


@pytest.mark.parametrize("failure_kind", ["network-unavailable", "transport-unavailable", "capacity-exhausted"])
def test_batch_transport_failure_invokes_once(setup, capsys, monkeypatch, failure_kind):
    model = Mock(return_value=agents.AgentResult(text="first failure", ok=False, failure_kind=failure_kind))
    monkeypatch.setattr(agents, "run_agent", model)
    rc, result, err = invoke(setup, capsys, [task("a")])
    assert rc == 1, err
    assert model.call_count == 1
    assert result["tasks"]["a"]["status"] == "failed"
    receipt = setup[0] / ".brigade" / "runs" / result["run_id"] / result["tasks"]["a"]["result_ref"]
    assert json.loads(receipt.read_text())["text"] == "first failure"


def test_observation_failure_preserves_real_worker_result(setup, capsys, monkeypatch):
    from brigade.run_batch_execute import BatchExecution

    original = BatchExecution.observe
    observations = []

    def observe(self, result):
        observations.append(result)
        if len(observations) == 1:
            raise OSError("observation unavailable")
        original(self, result)

    monkeypatch.setattr(BatchExecution, "observe", observe)
    rc, result, err = invoke(setup, capsys, [task("a")])
    assert rc == 0, err
    assert "observation unavailable" in err
    assert len(observations) == 2
    assert all(r.ok and r.text == "done" for r in observations)
    assert result["tasks"]["a"]["status"] == "completed"
    assert len(setup[1]) == 1


@pytest.mark.parametrize("marker", [None, "{broken", "[]", '{"no-fingerprint": true}'])
def test_interrupted_reservation_only_retries_missing_marker(setup, capsys, marker):
    import hashlib

    run_dir = setup[0] / ".brigade" / "runs" / ("batch-" + hashlib.sha256(b"req").hexdigest())
    run_dir.mkdir(parents=True)
    if marker is not None:
        (run_dir / "batch-request.json").write_text(marker)
    rc, result, err = invoke(setup, capsys, [task("a")])
    if marker is None:
        assert rc == 0, err
        assert len(setup[1]) == 1
    else:
        assert rc == 2
        assert result["error"]["code"] == "request-unknown"
        assert not setup[1]


@pytest.mark.parametrize("ok", [True, False])
def test_batch_final_summary_reports_task_outcomes(setup, capsys, monkeypatch, ok):
    monkeypatch.setattr(agents, "run_agent", Mock(return_value=agents.AgentResult(text="output", ok=ok)))
    rc, result, err = invoke(setup, capsys, [task("a"), task("b", ["a"])])
    assert rc == (0 if ok else 1), err
    run_dir = setup[0] / ".brigade" / "runs" / result["run_id"]
    summary = json.loads((run_dir / "synthesis.json").read_text())["result"]
    assert "a: completed" in summary["text"] if ok else "a: failed" in summary["text"]
    assert "b: completed" in summary["text"] if ok else "b: skipped" in summary["text"]
    assert summary["ok"] is ok
