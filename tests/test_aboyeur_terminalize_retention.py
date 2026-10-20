"""Issue #1756: a cleanup error racing a retained run lock must not terminalize run.json."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade import fleet_t3_transport, run_budget, runguard, seat_health
from brigade.aboyeur import orchestrator, planning
from brigade.roster import Agent, Roster
from tests.run_test_helpers import run_aboyeur_guarded


def test_planning_retention_preserves_receipt_and_lock(tmp_path, monkeypatch):
    retained = runguard.RetainRunLockError("planning receipt unavailable")

    def plan(*args, **kwargs):
        raise retained

    monkeypatch.setattr(planning, "plan", plan)
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "codex", "plan and synthesize"),
            "coder": Agent("coder", "codex", "write code"),
        },
    )
    output_dir = tmp_path / "run"
    workspace = tmp_path / "workspace"
    with pytest.raises(runguard.RetainRunLockError) as excinfo:
        run_aboyeur_guarded(
            "build feature",
            roster,
            output_dir=output_dir,
            lock_workspace=workspace,
            route_enabled=False,
        )

    assert excinfo.value is retained
    receipt = json.loads((output_dir / "run.json").read_text())
    assert receipt["status"] == "planning"
    assert "finished_at" not in receipt
    assert runguard.lock_path(workspace).exists()


@pytest.mark.parametrize("boundary", ["remote-transport", "budget-compatibility"])
@pytest.mark.parametrize("cleanup_failure", ["none", "control", "app", "both"])
def test_retention_closes_active_servers_preserving_receipt_and_lock(tmp_path, monkeypatch, boundary, cleanup_failure):
    retained = runguard.RetainRunLockError("receipt unavailable")
    output_dir = tmp_path / "run"
    workspace = tmp_path / "workspace"
    original: list[bytes] = []
    events: list[str] = []

    def close(server):
        events.append(f"{server}-close")
        if cleanup_failure in {server, "both"}:
            try:
                raise OSError(f"{server} cleanup failed") from None
            except OSError as exc:
                # Cleanup must preserve retention without incidental chaining.
                exc.__context__ = None
                raise

    class AppServer:
        def __init__(self, **kwargs):
            pass

        def start(self):
            events.append("app-start")

        def close(self):
            close("app")

    class ControlServer:
        def __init__(self, path, registry):
            pass

        def start(self):
            events.append("control-start")
            return orchestrator.run_control.ControlTransport(kind="unix", path=str(output_dir / "control.sock"))

        def close(self):
            close("control")

    def fail(*args, **kwargs):
        assert events == ["app-start", "control-start"]
        original.append((output_dir / "run.json").read_bytes())
        try:
            raise retained
        except runguard.RetainRunLockError:
            if boundary == "budget-compatibility":
                raise run_budget.BudgetCompatibilityError("unknown budget", code="unknown-schema")  # noqa: B904
            raise OSError("remote transport failed")  # noqa: B904

    monkeypatch.setattr(orchestrator.codex_appserver, "AppServer", AppServer)
    monkeypatch.setattr(orchestrator.run_control, "ControlServer", ControlServer)
    if boundary == "budget-compatibility":
        monkeypatch.setattr(orchestrator.artifacts, "_build_budget_coordinator", fail)
    else:
        monkeypatch.setattr(fleet_t3_transport, "build_source_transport", fail)
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "codex", "plan and synthesize"),
            "coder": Agent("coder", "codex", "write code"),
        },
    )

    with pytest.raises(runguard.RetainRunLockError) as excinfo:
        run_aboyeur_guarded(
            "build feature",
            roster,
            worker="coder",
            output_dir=output_dir,
            lock_workspace=workspace,
            codex_transport="app-server",
            route_enabled=False,
        )

    assert excinfo.value is retained
    assert original
    assert (output_dir / "run.json").read_bytes() == original[0]
    assert runguard.lock_path(workspace).exists()
    assert events == ["app-start", "control-start", "control-close", "app-close"]


@pytest.mark.parametrize(
    "boundary",
    [
        "preflight",
        "budget-compatibility",
        "dispatch-budget-policy",
        "reserve-budget-policy",
        "retry-probe",
        "retry-receipt",
        "attempt-receipt",
        "handoff",
        "remote-transport",
        "health-probe",
        "health-receipt",
    ],
)
def test_caught_retention_context_preserves_real_receipt_and_lock(tmp_path, monkeypatch, boundary):
    retained = runguard.RetainRunLockError("receipt unavailable")
    output_dir = tmp_path / "run"
    workspace = tmp_path / "workspace"
    original: list[bytes] = []
    cancel_requests: list[dict[str, object]] = []
    probe_type = type(seat_health.SeatHealthProbe())
    error: Exception = OSError("cleanup failed")
    if boundary == "preflight":
        error = runguard.RunGuardError("snapshot failed")
    elif boundary == "budget-compatibility":
        error = run_budget.BudgetCompatibilityError("unknown budget", code="unknown-schema")
    elif boundary in {"dispatch-budget-policy", "reserve-budget-policy"}:
        error = run_budget.BudgetPolicyError(
            "budget exhausted",
            code="budget-exhausted",
            dimension="worker_dispatches",
            exhausted=True,
            request_id="test-request",
        )

    def fail(*args, **kwargs):
        original.append((output_dir / "run.json").read_bytes())
        try:
            raise retained
        except runguard.RetainRunLockError:
            raise error  # noqa: B904

    def dispatch(*args, **kwargs):
        if boundary == "dispatch-budget-policy":
            fail()
        elif boundary == "reserve-budget-policy":
            kwargs["on_dispatch_requested"](roster.agents["coder"])
        elif boundary == "retry-probe":
            monkeypatch.setattr(probe_type, "probe", fail)
            kwargs["reprobe_seat"](roster.agents["coder"])
        elif boundary == "retry-receipt":
            monkeypatch.setattr(seat_health, "write_seat_health_receipt", fail)
            kwargs["reprobe_seat"](roster.agents["coder"])
        elif boundary == "attempt-receipt":
            monkeypatch.setattr(orchestrator.run_io, "write_sidecar_revision", fail)
            kwargs["on_failed_attempt_persisted"](result)
        return [result]

    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "codex", "plan and synthesize"),
            "coder": Agent("coder", "codex", "write code"),
        },
    )
    result = orchestrator.WorkerResult(worker="coder", task="implement", text="done", ok=True)
    monkeypatch.setattr(planning, "dispatch", dispatch)
    if boundary == "preflight":
        monkeypatch.setattr(runguard, "capture_pre_run_snapshot", fail)
    elif boundary == "budget-compatibility":
        monkeypatch.setattr(orchestrator.artifacts, "_build_budget_coordinator", fail)
    elif boundary == "reserve-budget-policy":
        monkeypatch.setattr(
            orchestrator.artifacts,
            "_build_budget_coordinator",
            lambda *args, **kwargs: SimpleNamespace(
                reserve_and_record_dispatch=fail, request_cancel=lambda **kwargs: cancel_requests.append(kwargs)
            ),
        )
    elif boundary == "dispatch-budget-policy":
        # Exercise the typed handler itself, before the registry boundary can
        # promote the chained retention error on its behalf.
        monkeypatch.setattr(
            planning, "_call_with_process_registry", lambda function, *args, **kwargs: function(*args, **kwargs)
        )
    elif boundary == "handoff":
        monkeypatch.setattr(orchestrator.run_io, "write_run_handoff", fail)
    elif boundary == "remote-transport":
        monkeypatch.setattr(fleet_t3_transport, "build_source_transport", fail)
    elif boundary == "health-probe":
        monkeypatch.setattr(probe_type, "probe_roster", fail)
    elif boundary == "health-receipt":
        monkeypatch.setattr(seat_health, "write_seat_health_receipt", fail)

    with pytest.raises(runguard.RetainRunLockError) as excinfo:
        run_aboyeur_guarded(
            "build feature",
            roster,
            worker="coder",
            output_dir=output_dir,
            lock_workspace=workspace,
            handoff_inbox=tmp_path / "inbox" if boundary == "handoff" else None,
            route_enabled=False,
        )

    assert excinfo.value is retained
    assert original
    assert cancel_requests == []
    assert (output_dir / "run.json").read_bytes() == original[0]
    assert runguard.lock_path(workspace).exists()


def _cleanup_error_over_retention(error: Exception):
    @orchestrator._terminalize_run_lifecycle
    def run(*, output_dir: Path, worker: str) -> int:
        try:
            raise runguard.RetainRunLockError("failed to write terminal run receipt: disk full")
        except runguard.RetainRunLockError:
            # Implicit __context__ chaining, as when a cleanup step raises mid-retention.
            raise error  # noqa: B904

    return run


@pytest.mark.parametrize(
    "error",
    [ValueError("cleanup failed"), TimeoutError("cleanup timed out")],
    ids=["exception", "timeout"],
)
def test_cleanup_error_over_retention_leaves_run_json_untouched(tmp_path, monkeypatch, error):
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    original = json.dumps({"status": "dispatching", "started_at": "2026-07-19T12:00:00Z"}) + "\n"
    (output_dir / "run.json").write_text(original)
    terminations: list[dict[str, object]] = []
    monkeypatch.setattr(
        orchestrator.artifacts,
        "record_run_termination",
        lambda *args, **kwargs: terminations.append(kwargs),
    )

    with pytest.raises(runguard.RetainRunLockError) as excinfo:
        _cleanup_error_over_retention(error)(output_dir=output_dir, worker="coder")

    assert runguard.retained_run_lock_error(excinfo.value) is not None
    assert terminations == []
    assert (output_dir / "run.json").read_text() == original


@pytest.mark.parametrize("error_type", [ValueError, TimeoutError], ids=["exception", "timeout"])
def test_dispatch_cleanup_error_over_retention_does_not_terminalize(tmp_path, monkeypatch, error_type):
    retained = runguard.RetainRunLockError("failed to write terminal run receipt: disk full")
    terminations: list[dict[str, object]] = []

    def dispatch(*args, **kwargs):
        try:
            raise retained
        except runguard.RetainRunLockError:
            raise error_type("dispatch cleanup failed")  # noqa: B904

    monkeypatch.setattr(planning, "dispatch", dispatch)
    # Deliver the cleanup error to run's dispatch handlers, without the inner
    # cancellation boundary promoting its retention context first.
    monkeypatch.setattr(
        planning, "_call_with_process_registry", lambda function, *args, **kwargs: function(*args, **kwargs)
    )
    monkeypatch.setattr(
        orchestrator.artifacts,
        "record_run_termination",
        lambda *args, **kwargs: terminations.append(kwargs),
    )
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "codex", "plan and synthesize"),
            "coder": Agent("coder", "codex", "write code"),
        },
    )

    with pytest.raises(runguard.RetainRunLockError) as excinfo:
        run_aboyeur_guarded(
            "build feature",
            roster,
            worker="coder",
            output_dir=tmp_path / "run",
            route_enabled=False,
        )

    assert excinfo.value is retained
    assert terminations == []


@pytest.mark.parametrize(
    ("error", "status"),
    [(ValueError("boom"), "failed"), (TimeoutError("slow"), "timeout")],
    ids=["exception", "timeout"],
)
def test_error_without_retention_still_terminalizes(tmp_path, monkeypatch, error, status):
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    (output_dir / "run.json").write_text(json.dumps({"status": "dispatching"}) + "\n")
    terminations: list[dict[str, object]] = []
    monkeypatch.setattr(
        orchestrator.artifacts,
        "record_run_termination",
        lambda *args, **kwargs: terminations.append(kwargs),
    )

    @orchestrator._terminalize_run_lifecycle
    def run(*, output_dir: Path, worker: str) -> int:
        raise error

    with pytest.raises(type(error)):
        run(output_dir=output_dir, worker="coder")

    assert [entry["status"] for entry in terminations] == [status]
