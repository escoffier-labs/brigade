"""Review regressions for claim-loss cleanup and terminal classification."""

from __future__ import annotations

import _thread
import json
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest

from brigade import aboyeur, agents, cli, fleet_client, fleet_session_bootstrap, proc, runguard
from brigade import fleet_t3_transport as t3
from brigade.aboyeur import orchestrator, planning
from brigade import claim_loss_cancellation, run_budget, run_lifecycle, run_transport
from tests.test_aboyeur import _roster
from tests.run_test_helpers import run_aboyeur_guarded
from tests._posix import requires_process_groups
from tests.test_fleet_t3_transport import (
    FakePolicyClient,
    FakeT3Runner,
    REVISION,
    WINDOWS_NODE,
    _document,
    _eligible,
    _succeeded,
)


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

    def run(*, direct=False, transport="exec"):
        argv = [
            "run",
            "do something",
            "--roster",
            str(roster),
            "--cwd",
            str(ws),
            "--codex-transport",
            transport,
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


@pytest.mark.parametrize("result", ["error", "still_active", "watcher-error"])
def test_cancellation_failure_retains_run_lock(claimed_run, monkeypatch, capsys, result):
    ws, callbacks, run = claimed_run

    class Process:
        def poll(self):
            return None

    def terminate(*args, **kwargs):
        if result == "error":
            raise OSError("cancellation unavailable")

    if result == "watcher-error":

        def failed_watcher(self):
            raise RuntimeError("watcher unavailable")

        monkeypatch.setattr(proc.ProcessRegistry, "cancel", failed_watcher)
    monkeypatch.setattr(proc, "_terminate_processes", terminate)

    def active_run(*args, process_registry=None, **kwargs):
        process_registry.register(Process(), seat="coder")
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
    with pytest.raises(proc.ProcessCancelledError):
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
    with pytest.raises(proc.ProcessCancelledError):
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


@pytest.mark.parametrize("phase", ["dispatch", "appserver-start"])
def test_claim_loss_cannot_replace_retained_lock_failure(claimed_run, monkeypatch, phase):
    ws, callbacks, run = claimed_run
    monkeypatch.setattr(agents, "run_agent", lambda *args, **kwargs: agents.AgentResult(text="done", ok=True))
    if phase == "dispatch":
        record = run_lifecycle.record_dispatch_fact

        def failed_receipt(*args, **kwargs):
            if kwargs["event_type"] == "run.dispatch.requested":
                callbacks["lost"]("held")
                raise OSError("dispatch receipt unavailable")
            return record(*args, **kwargs)

        monkeypatch.setattr(run_lifecycle, "record_dispatch_fact", failed_receipt)
    else:

        class FailedServer:
            def __init__(self, **kwargs):
                pass

            def start(self):
                callbacks["lost"]("held")
                raise runguard.RetainRunLockError("app-server lifecycle unavailable")

            def close(self):
                pass

        monkeypatch.setattr(orchestrator.codex_appserver, "AppServer", FailedServer)
    assert run(direct=True, transport="exec" if phase == "dispatch" else "app-server") == 2
    assert runguard.lock_path(ws).exists(), "claim loss replaced the failure that required lock retention"


def test_budget_cancelled_parallel_wave_remains_budget_exhausted(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    first_started = threading.Event()
    cancelled = threading.Event()
    reserve = run_budget.BudgetCoordinator.reserve_and_record_dispatch
    cancel = proc.ProcessRegistry.cancel

    def expired_second_dispatch(self, *, seat, **kwargs):
        if seat == "reviewer":
            assert first_started.wait(5)
            self.clock = lambda: self._started_at + timedelta(seconds=7200)
        return reserve(self, seat=seat, **kwargs)

    def observed_cancel(self):
        result = cancel(self)
        cancelled.set()
        return result

    def run_agent(cli_ref, prompt, *, process_registry=None, **kwargs):
        if cli_ref == "ollama:llama3.3":
            first_started.set()
            assert cancelled.wait(5)
            result = proc.run([sys.executable, "-c", "pass"], process_registry=process_registry)
            return agents.AgentResult(text=result.stdout, ok=result.code == 0)
        return agents.AgentResult(
            text=json.dumps(
                {
                    "assignments": [
                        {"worker": "coder", "task": "implement", "stage": 1},
                        {"worker": "reviewer", "task": "review", "stage": 1},
                    ]
                }
            ),
            ok=True,
        )

    # Observe the already-running worker first, so its launch refusal cannot
    # hide behind the second worker's budget exception in completion ordering.
    monkeypatch.setattr(run_transport, "as_completed", lambda futures: iter(futures))
    monkeypatch.setattr(run_budget.BudgetCoordinator, "reserve_and_record_dispatch", expired_second_dispatch)
    monkeypatch.setattr(proc.ProcessRegistry, "cancel", observed_cancel)
    monkeypatch.setattr(agents, "run_agent", run_agent)
    output_dir = tmp_path / "run"
    try:
        rc = run_aboyeur_guarded(
            "build feature",
            _roster(),
            cwd=tmp_path,
            output_dir=output_dir,
            route_enabled=False,
            code_graph_enabled=False,
            evidence_enabled=False,
            run_budget_payload={
                "schema": "brigade.run_budget.v1",
                "schema_version": 1,
                "ceilings": {"wall_clock_seconds": 3600},
            },
        )
    except KeyboardInterrupt as exc:
        pytest.fail(f"budget wave escaped as operator cancellation: {exc}")
    assert rc == 1
    receipt = json.loads((output_dir / "run.json").read_text())
    assert receipt["failure"]["kind"] == "budget-exhausted"


@pytest.mark.parametrize("state", ["running", "succeeded"])
def test_submit_handle_is_persisted_before_claim_cancellation(tmp_path, state):
    scope = claim_loss_cancellation.ClaimLossCancellation()
    handles = []
    fake = FakeT3Runner({"doctor": _eligible(), "capacity": _eligible(), "submit": _succeeded(state=state)})

    def response_then_loss(command, **kwargs):
        response = fake(command, **kwargs)
        if command[1] == "submit":
            scope.request()
        return response

    client = t3.T3FleetClient(runner=response_then_loss, process_registry=scope.process_registry)
    resolution = t3.RouteResolution(route=t3.ROUTE_REMOTE, reason="selected", seat="coder", decision_id="dec-fixed")
    with pytest.raises(claim_loss_cancellation.ClaimLossCancelled):
        t3.execute_remote(
            resolution,
            "inspect",
            title="inspect",
            cwd=tmp_path,
            client=client,
            policy_client=FakePolicyClient(),
            parent_request_id="run-test",
            source_revision=REVISION,
            on_handle=lambda *handle: handles.append(handle),
        )
    assert len(handles) == 1, "a successful submit lost its recoverable remote handle"
    assert handles[0][1] == _succeeded()["request_id"]
    assert "wait" not in fake.verbs()


def test_cancellation_wait_uses_snapshot_after_unregister(monkeypatch):
    scope = claim_loss_cancellation.ClaimLossCancellation()
    snapshot = threading.Event()
    finish = threading.Event()

    class Process:
        def poll(self):
            return 0 if finish.is_set() else None

    processes = [Process() for _ in range(3)]
    for process in processes:
        scope.process_registry.register(process)

    def slow_batch(batch, **kwargs):
        assert len(batch) == 3
        for process in batch:
            scope.process_registry.unregister(process)
        snapshot.set()
        assert finish.wait(2)

    monkeypatch.setattr(proc, "_terminate_processes", slow_batch)
    scope.request()
    scope.__enter__()
    assert snapshot.wait(2)
    times = iter([0.0, 6.0])
    monkeypatch.setattr(claim_loss_cancellation, "time", SimpleNamespace(monotonic=lambda: next(times, 6.0)))
    wait = scope._done.wait

    def complete_batch(timeout):
        finish.set()
        return wait(1)

    monkeypatch.setattr(scope._done, "wait", complete_batch)
    try:
        scope.__exit__(None, None, None)
    finally:
        finish.set()
        scope._watcher.join(timeout=2)
    assert all(outcome.result == "interrupted" for outcome in scope.outcomes)


@pytest.mark.parametrize("boundary", ["stop", "deadline", "done-check", "outcomes"])
def test_heartbeat_interrupt_during_exit_resumes_cleanup(monkeypatch, boundary):
    scope = claim_loss_cancellation.ClaimLossCancellation()
    scope.request()
    scope.__enter__()
    assert scope._done.wait(2)
    fired = False

    def interrupt_once(function):
        def interrupted(*args, **kwargs):
            nonlocal fired
            if not fired:
                fired = True
                _thread.interrupt_main()
            return function(*args, **kwargs)

        return interrupted

    if boundary == "stop":
        monkeypatch.setattr(scope._stopped, "set", interrupt_once(scope._stopped.set))
    elif boundary == "deadline":
        original = proc.ProcessRegistry.cancellation_timeout.fget
        monkeypatch.setattr(proc.ProcessRegistry, "cancellation_timeout", property(interrupt_once(original)))
    elif boundary == "done-check":
        monkeypatch.setattr(scope._done, "is_set", interrupt_once(scope._done.is_set))
    else:
        original = proc.ProcessRegistry.cancellation_outcomes.fget
        monkeypatch.setattr(proc.ProcessRegistry, "cancellation_outcomes", property(interrupt_once(original)))
    error = None
    try:
        scope.__exit__(None, None, None)
    except BaseException as exc:
        error = exc
    assert fired
    assert error is None, f"heartbeat interrupt escaped cleanup or caused false retention: {error!r}"


def test_repeated_cancel_resignals_only_survivors(monkeypatch):
    class Process:
        running = True

        def poll(self):
            return None if self.running else 0

    process = Process()
    calls = []

    def terminate(batch, **kwargs):
        calls.append(batch)
        if len(calls) == 2:
            process.running = False

    monkeypatch.setattr(proc, "_terminate_processes", terminate)
    registry = proc.ProcessRegistry()
    registry.register(process, seat="coder")
    assert registry.cancel() == (proc.ProcessCancelOutcome("coder", "still_active"),)
    assert registry.cancel() == (proc.ProcessCancelOutcome("coder", "interrupted"),)
    registry.cancel()
    assert calls == [(process,), (process,)], "exited process identities must not be signalled again"


@pytest.mark.parametrize("phase", ["startup", "dispatch"])
def test_appserver_close_interrupt_preserves_inflight_retention(claimed_run, monkeypatch, phase):
    ws, callbacks, run = claimed_run

    class Server:
        def __init__(self, **kwargs):
            pass

        def start(self):
            if phase == "startup":
                raise runguard.RetainRunLockError("startup receipt unavailable")

        def close(self):
            callbacks["lost"]("held")
            _thread.interrupt_main()

    def failed_dispatch(*args, **kwargs):
        raise runguard.RetainRunLockError("dispatch receipt unavailable")

    monkeypatch.setattr(orchestrator.codex_appserver, "AppServer", Server)
    monkeypatch.setattr(planning, "dispatch", failed_dispatch)
    assert run(direct=True, transport="app-server") == 2
    assert runguard.lock_path(ws).exists(), "cleanup interrupt replaced the in-flight retention failure"


@pytest.mark.parametrize("resignal", [False, True])
def test_exit_uses_latest_survivor_state_and_releases_lock(tmp_path, monkeypatch, resignal):
    scope = claim_loss_cancellation.ClaimLossCancellation()

    class Process:
        running = True

        def poll(self):
            return None if self.running else 0

    process = Process()
    calls = []

    def terminate(batch, **kwargs):
        calls.append(batch)
        if len(calls) == 2:
            process.running = False

    monkeypatch.setattr(proc, "_terminate_processes", terminate)
    scope.process_registry.register(process, seat="coder")
    with runguard.run_lock(tmp_path):
        scope.request()
        with scope:
            assert scope._done.wait(2)
            assert scope.outcomes == (proc.ProcessCancelOutcome("coder", "still_active"),)
            if resignal:
                assert scope.process_registry.cancel() == (proc.ProcessCancelOutcome("coder", "interrupted"),)
            else:
                process.running = False
            scope.process_registry.unregister(process)
    assert not runguard.lock_path(tmp_path).exists()
    assert scope.outcomes == (proc.ProcessCancelOutcome("coder", "interrupted"),)
    assert calls == ([(process,), (process,)] if resignal else [(process,)])


@pytest.mark.parametrize("scheduler", ["waves", "dag"])
def test_interrupt_shutdown_preserves_worker_retention(tmp_path, monkeypatch, scheduler):
    started = threading.Event()
    interrupted = threading.Event()

    def worker(*args, **kwargs):
        started.set()
        assert interrupted.wait(5)
        raise runguard.RetainRunLockError("worker receipt unavailable")

    def interrupt_completion(futures):
        assert started.wait(5)
        raise KeyboardInterrupt

    monkeypatch.setattr(agents, "run_agent", worker)
    monkeypatch.setattr(run_transport, "as_completed", interrupt_completion)
    error = None
    try:
        with runguard.run_lock(tmp_path):
            run_transport.dispatch(
                [run_transport.Assignment(worker="coder", task="inspect", stage=1, covers=("inspect",))],
                _roster(),
                build_prompt=lambda *args, **kwargs: "inspect",
                run_appserver_worker=lambda *args, **kwargs: pytest.fail("unused app-server"),
                event_writer=lambda *args, **kwargs: None,
                cwd=tmp_path,
                scheduler=scheduler,
                route_dependencies={"inspect": ()},
                on_interrupt=interrupted.set,
            )
    except BaseException as exc:
        error = exc
    assert isinstance(error, runguard.RetainRunLockError), f"worker retention failure lost: {error!r}"
    assert isinstance(error.__context__, KeyboardInterrupt)
    assert runguard.lock_path(tmp_path).exists()


@pytest.mark.parametrize("granted", [False, True])
def test_remote_transport_supervision_requires_claim(claimed_run, monkeypatch, granted):
    _ws, _callbacks, run = claimed_run
    registries = []

    @contextmanager
    def claim(*args, **kwargs):
        yield fleet_client.ClaimDecision(granted, "ok")

    def transport(**kwargs):
        registries.append(kwargs.get("process_registry"))
        return None

    monkeypatch.setattr(fleet_client, "repo_claim", claim)
    monkeypatch.setattr(t3, "build_source_transport", transport)
    monkeypatch.setattr(agents, "run_agent", lambda *args, **kwargs: agents.AgentResult(text="done", ok=True))
    assert run(direct=True) == 0
    assert len(registries) == 1
    assert (registries[0] is not None) == granted


@pytest.mark.parametrize("link", ["context", "cause", "context-cycle"])
@pytest.mark.parametrize("cancelled", [KeyboardInterrupt, claim_loss_cancellation.ClaimLossCancelled])
def test_run_lock_preserves_chained_retention(tmp_path, link, cancelled):
    retained = runguard.RetainRunLockError("receipt unavailable")
    original_cause = OSError("receipt storage unavailable")
    retained.__cause__ = original_cause
    intermediate = RuntimeError("cleanup failed")
    interrupt = cancelled()
    if link == "cause":
        intermediate.__cause__ = retained
        interrupt.__cause__ = intermediate
    else:
        intermediate.__context__ = retained
        interrupt.__context__ = intermediate
        if link == "context-cycle":
            retained.__context__ = interrupt
    error = None
    try:
        with runguard.run_lock(tmp_path):
            raise interrupt
    except BaseException as exc:
        error = exc
    assert error is retained, f"exception chain lost retention: {error!r}"
    assert error.__cause__ is original_cause
    assert runguard.lock_path(tmp_path).exists()


@pytest.mark.parametrize("scheduler", ["waves", "dag"])
def test_claim_cancel_shutdown_preserves_sibling_retention(tmp_path, monkeypatch, scheduler):
    started = threading.Barrier(2)
    cancelled = threading.Event()
    retained = runguard.RetainRunLockError("sibling receipt unavailable")

    def worker(cli_ref, *args, **kwargs):
        started.wait(timeout=5)
        if cli_ref == "ollama:llama3.3":
            cancelled.set()
            raise claim_loss_cancellation.ClaimLossCancelled
        assert cancelled.wait(5)
        time.sleep(0.2)
        raise retained

    monkeypatch.setattr(agents, "run_agent", worker)
    error = None
    try:
        with runguard.run_lock(tmp_path):
            run_transport.dispatch(
                [
                    run_transport.Assignment(worker="coder", task="inspect", stage=1, covers=("inspect",)),
                    run_transport.Assignment(worker="reviewer", task="review", stage=1, covers=("review",)),
                ],
                _roster(),
                build_prompt=lambda *args, **kwargs: "inspect",
                run_appserver_worker=lambda *args, **kwargs: pytest.fail("unused app-server"),
                event_writer=lambda *args, **kwargs: None,
                cwd=tmp_path,
                scheduler=scheduler,
                route_dependencies={"inspect": (), "review": ()},
            )
    except BaseException as exc:
        error = exc
    assert error is retained, f"sibling retention failure lost: {error!r}"
    assert runguard.lock_path(tmp_path).exists()


@pytest.mark.parametrize("link", ["suppressed", "explicit-cause"])
def test_run_lock_ignores_hidden_historical_retention(tmp_path, link):
    with pytest.raises(RuntimeError, match="handled cleanup") as caught:
        with runguard.run_lock(tmp_path):
            try:
                raise runguard.RetainRunLockError("historical receipt failure")
            except runguard.RetainRunLockError:
                if link == "suppressed":
                    raise RuntimeError("handled cleanup") from None
                raise RuntimeError("handled cleanup") from ValueError("current cause")
    assert type(caught.value) is RuntimeError
    assert not runguard.lock_path(tmp_path).exists()


@pytest.mark.parametrize("depth,found", [(63, True), (64, False)])
def test_retention_chain_walk_is_bounded(depth, found):
    retained = runguard.RetainRunLockError("receipt unavailable")
    error = retained
    for _ in range(depth):
        wrapper = RuntimeError("cleanup")
        wrapper.__cause__ = error
        error = wrapper
    assert (runguard.retained_run_lock_error(error) is retained) == found


@pytest.mark.parametrize("finish", ["complete", "deadline"])
def test_retention_interrupt_waits_for_cancellation_exit(monkeypatch, finish):
    scope = claim_loss_cancellation.ClaimLossCancellation()
    cancelling = threading.Event()
    release = threading.Event()
    retained = runguard.RetainRunLockError("receipt unavailable")

    def cancel():
        cancelling.set()
        assert release.wait(5)
        return ()

    monkeypatch.setattr(scope.process_registry, "cancel", cancel)
    scope.request()
    scope.__enter__()
    assert cancelling.wait(2)
    wait = scope._done.wait
    calls = []
    if finish == "deadline":
        scope._shutdown_deadline = time.monotonic() + 0.05

    def interrupted_wait(timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise KeyboardInterrupt from retained
        if finish == "complete":
            release.set()
        return wait(timeout)

    monkeypatch.setattr(scope._done, "wait", interrupted_wait)
    try:
        with pytest.raises(runguard.RetainRunLockError) as caught:
            scope.__exit__(None, None, None)
        assert len(calls) >= 2, "retention escaped while the watcher was still cancelling"
        assert caught.value is retained
        if finish == "complete":
            assert scope._done.is_set()
        else:
            assert time.monotonic() >= scope._shutdown_deadline
    finally:
        release.set()
        scope._watcher.join(timeout=2)


@requires_process_groups
@pytest.mark.parametrize("blocked_command", ["rev-parse", "status"])
def test_claim_loss_stops_source_revision_before_delegation(tmp_path, monkeypatch, blocked_command):
    scope = claim_loss_cancellation.ClaimLossCancellation()
    ready = tmp_path / "revision-ready"
    finished = threading.Event()
    loss_time = []
    processes = []
    popen = subprocess.Popen
    policy = FakePolicyClient()
    runner = FakeT3Runner({"doctor": _eligible(), "capacity": _eligible(), "submit": _succeeded()})

    def revision_process(command, *args, **kwargs):
        if command[:2] == ["git", "-C"]:
            if command[3] == blocked_command:
                script = (
                    "import signal,time; from pathlib import Path; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    f"Path({str(ready)!r}).touch(); time.sleep(30)"
                )
            else:
                script = f"print({REVISION!r})" if command[3] == "rev-parse" else "pass"
            command = [sys.executable, "-c", script]
        process = popen(command, *args, **kwargs)
        processes.append(process)
        return process

    def lose_claim():
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            if finished.wait(0.01):
                return
        if ready.exists():
            loss_time.append(time.monotonic())
            scope.request()
        if not finished.wait(scope.process_registry.cancellation_timeout + 1):
            for process in processes:
                if process.poll() is None:
                    process.kill()

    monkeypatch.setattr(subprocess, "Popen", revision_process)
    transport = t3.build_source_transport(
        cwd=tmp_path,
        run_id="run-revision-test",
        snapshot={
            "state": "authoritative",
            "fleet_policy": {"active": True, "version": 1, "digest": "sha256:" + "a" * 64},
        },
        policy_client=policy,
        document=_document(),
        node_id=WINDOWS_NODE,
        client=t3.T3FleetClient(runner=runner),
        process_registry=scope.process_registry,
    )
    assert transport is not None
    watchdog = threading.Thread(target=lose_claim, daemon=True)
    watchdog.start()
    error = None
    try:
        with runguard.run_lock(tmp_path):
            with scope:
                transport("coder", "inspect", "inspect")
    except BaseException as exc:
        error = exc
    finally:
        finished.set()
        watchdog.join(timeout=2)
        proc._terminate_processes(tuple(processes), terminate_grace=0.01, kill_grace=0.1)
    assert loss_time, "revision command did not reach the blocked boundary"
    assert time.monotonic() - loss_time[0] < scope.process_registry.cancellation_timeout
    assert isinstance(error, claim_loss_cancellation.ClaimLossCancelled), repr(error)
    assert not policy.delegation_calls
    assert "submit" not in runner.verbs()
    assert all(process.poll() is not None for process in processes)
    assert not runguard.lock_path(tmp_path).exists()
