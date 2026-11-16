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
    monkeypatch.setattr(aboyeur, "plan", Mock(side_effect=AssertionError("planner called")))
    monkeypatch.setattr(aboyeur, "build_synth_prompt", Mock(side_effect=AssertionError("synthesis called")))
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


def test_crash_before_fingerprint_refuses_launch(setup, capsys):
    import hashlib

    run_dir = setup[0] / ".brigade" / "runs" / ("batch-" + hashlib.sha256(b"req").hexdigest())
    run_dir.mkdir(parents=True)
    rc, result, _ = invoke(setup, capsys, [task("a")])
    assert rc == 2
    assert result["error"]["code"] == "request-unknown"
    assert setup[1] == []


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
