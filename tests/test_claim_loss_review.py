"""Review regressions for claim-loss cleanup and terminal classification."""

from __future__ import annotations

import _thread
import json
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import pytest

from brigade import aboyeur, agents, cli, fleet_client, fleet_session_bootstrap, proc, runguard
from brigade import fleet_t3_transport as t3
from brigade.aboyeur import planning
from tests._posix import requires_process_groups
from tests.test_fleet_t3_transport import FakePolicyClient, FakeT3Runner, REVISION, _eligible, _succeeded


@pytest.fixture
def claimed_run(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / ".brigade").mkdir(parents=True)
    roster = ws / "roster.toml"
    roster.write_text(
        'orchestrator = "chef"\n[agents.chef]\ncli = "codex"\nrole = "plan"\nmodel = "planner-fixture"\n'
        '[agents.coder]\ncli = "codex"\nrole = "code"\nmodel = "worker-fixture"\n'
    )
    callbacks = {}

    @contextmanager
    def claim(*args, **kwargs):
        callbacks["lost"] = kwargs["on_claim_lost"]
        yield fleet_client.ClaimDecision(True, "ok")

    monkeypatch.setattr(fleet_client, "repo_claim", claim)
    monkeypatch.setattr(fleet_client, "load_model_policy_snapshot", lambda: {"state": "unconfigured", "models": []})
    monkeypatch.setattr(fleet_session_bootstrap, "ensure_prompt", lambda prompt, **kwargs: prompt)

    def run(*, direct=False):
        argv = [
            "run",
            "do something",
            "--roster",
            str(roster),
            "--cwd",
            str(ws),
            "--codex-transport",
            "exec",
            "--no-code-graph",
            "--no-evidence",
            "--no-route",
        ]
        if direct:
            argv += ["--worker", "coder"]
        return cli.main(argv)

    return ws, callbacks, run


def _receipt(ws):
    return json.loads(next((ws / ".brigade" / "runs").glob("*/run.json")).read_text())


@pytest.mark.parametrize("phase", ["planning", "synthesis", "interrupt"])
def test_decorated_orchestrator_preserves_claim_loss_kind(claimed_run, monkeypatch, phase):
    ws, callbacks, run = claimed_run
    planner_calls = []

    def adapter(*args, model=None, **kwargs):
        if model == "planner-fixture":
            planner_calls.append(model)
            if (phase == "planning" and len(planner_calls) == 1) or (phase == "synthesis" and len(planner_calls) == 2):
                callbacks["lost"]("held")
                return agents.AgentResult(text="", ok=False, detail="canceled provider")
            return agents.AgentResult(
                text=json.dumps({"assignments": [{"worker": "coder", "task": "inspect it"}]}), ok=True
            )
        return agents.AgentResult(text="The requested inspection is complete.", ok=True)

    if phase == "interrupt":

        def interrupted_dispatch(*args, **kwargs):
            callbacks["lost"]("held")
            _thread.interrupt_main()

        monkeypatch.setattr(planning, "dispatch", interrupted_dispatch)
    monkeypatch.setattr(agents, "run_agent", adapter)
    assert run() == 2
    assert _receipt(ws)["failure"]["kind"] == "fleet-claim-lost"


@requires_process_groups
def test_lock_release_waits_for_registered_process_exit(claimed_run, monkeypatch):
    ws, callbacks, run = claimed_run
    ready = ws / "ready"
    processes = []
    released_active = []
    threads = []
    register = proc.ProcessRegistry.register
    release = runguard._release_lock

    def registered(self, process, **kwargs):
        register(self, process, **kwargs)
        processes.append(process)

    def released(path, ownership):
        released_active.append(any(process.poll() is None for process in processes))
        release(path, ownership)

    def active_run(*args, process_registry=None, **kwargs):
        thread = threading.Thread(
            target=proc.run,
            args=(
                [
                    sys.executable,
                    "-c",
                    "import signal,time; from pathlib import Path; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    f"Path({str(ready)!r}).touch(); time.sleep(30)",
                ],
            ),
            kwargs={"timeout": 15, "process_registry": process_registry},
            daemon=True,
        )
        threads.append(thread)
        thread.start()
        deadline = time.monotonic() + 5
        while not ready.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        callbacks["lost"]("held")
        return 0

    monkeypatch.setattr(proc.ProcessRegistry, "register", registered)
    monkeypatch.setattr(runguard, "_release_lock", released)
    monkeypatch.setattr(aboyeur, "run", active_run)
    try:
        assert run(direct=True) == 2
        assert released_active == [False]
    finally:
        proc._terminate_processes(tuple(processes), terminate_grace=0.01, kill_grace=0.1)
        for thread in threads:
            thread.join(timeout=2)


@pytest.mark.parametrize("result", ["error", "still_active"])
def test_cancellation_failure_retains_run_lock(claimed_run, monkeypatch, capsys, result):
    ws, callbacks, run = claimed_run
    monkeypatch.setattr(proc.ProcessRegistry, "cancel", lambda self: (proc.ProcessCancelOutcome("coder", result),))

    def active_run(*args, **kwargs):
        callbacks["lost"]("held")
        return 0

    monkeypatch.setattr(aboyeur, "run", active_run)
    assert run(direct=True) == 2
    assert runguard.lock_path(ws).exists()
    assert "cancellation" in capsys.readouterr().err


def test_cancellation_outcome_inspection_failure_retains_run_lock(claimed_run, monkeypatch):
    ws, callbacks, run = claimed_run

    def failed_inspection(self):
        raise OSError("process state unavailable")

    def active_run(*args, **kwargs):
        callbacks["lost"]("held")
        return 0

    monkeypatch.setattr(proc.ProcessRegistry, "cancellation_outcomes", property(failed_inspection))
    monkeypatch.setattr(aboyeur, "run", active_run)
    assert run(direct=True) == 2
    assert runguard.lock_path(ws).exists()


def test_canceled_registry_refuses_popen(monkeypatch):
    registry = proc.ProcessRegistry()
    registry.cancel()

    def forbidden(*args, **kwargs):
        raise AssertionError("canceled registry launched a child")

    monkeypatch.setattr(proc.subprocess, "Popen", forbidden)
    with pytest.raises(KeyboardInterrupt):
        proc.run([sys.executable, "-c", "pass"], process_registry=registry)


def test_registry_keeps_observed_cancellation_outcomes(monkeypatch):
    class Exited:
        def poll(self):
            return 0

    process = Exited()
    monkeypatch.setattr(proc, "_terminate_processes", lambda *args, **kwargs: None)
    registry = proc.ProcessRegistry()
    registry.register(process, seat="coder")
    observed = registry.cancel()
    registry.unregister(process)
    assert observed == (proc.ProcessCancelOutcome("coder", "interrupted"),)
    assert registry.cancel() == observed


def test_t3_without_registry_keeps_subprocess_run(monkeypatch):
    calls = []

    def plain(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(_eligible()), "")

    monkeypatch.setattr(t3.subprocess, "run", plain)
    monkeypatch.setattr(proc, "run", lambda *args, **kwargs: pytest.fail("unclaimed client was supervised"))
    assert t3.T3FleetClient().doctor()["hosts"]
    assert calls == [["t3-fleet", "doctor"]]


@pytest.mark.parametrize("boundary", ["delegation", "submit"])
def test_remote_cancellation_prevents_new_side_effects(tmp_path, boundary):
    registry = proc.ProcessRegistry()
    policy = FakePolicyClient()
    create = policy.create_delegation

    def create_and_cancel(**kwargs):
        value = create(**kwargs)
        registry.cancel()
        return value

    if boundary == "delegation":
        registry.cancel()
    else:
        policy.create_delegation = create_and_cancel
    runner = FakeT3Runner({"doctor": _eligible(), "capacity": _eligible(), "submit": _succeeded()})
    client = t3.T3FleetClient(runner=runner, process_registry=registry)
    resolution = t3.RouteResolution(route=t3.ROUTE_REMOTE, reason="selected", seat="coder", decision_id="dec-fixed")
    with pytest.raises(KeyboardInterrupt):
        t3.execute_remote(
            resolution,
            "inspect",
            title="inspect",
            cwd=tmp_path,
            client=client,
            policy_client=policy,
            parent_request_id="run-test",
            source_revision=REVISION,
        )
    assert len(policy.delegation_calls) == (0 if boundary == "delegation" else 1)
    assert "submit" not in runner.verbs()
