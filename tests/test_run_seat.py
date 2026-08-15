# tests/test_run_seat.py
from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Thread

import pytest

from brigade.proc import ProcessRegistry
from brigade.roster import Agent, Roster
from brigade.run_seat import SeatInvoker
from brigade.run_transport import WorkerAttempt, WorkerResult


def _research_roster() -> Roster:
    return Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="orchestrator"),
            "luna": Agent(
                name="luna",
                cli="codex",
                role="researcher",
                model="gpt-5.6-luna",
                reasoning="medium",
                capabilities=("research.plan",),
            ),
        },
    )


def _oracle_research_roster() -> Roster:
    roster = _research_roster()
    return Roster(
        orchestrator=roster.orchestrator,
        agents={
            **roster.agents,
            "oracle": Agent(
                name="oracle",
                cli="oracle",
                role="researcher",
                capabilities=("research.plan",),
            ),
        },
    )


def _tree_text(root: Path) -> str:
    chunks: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def test_seat_invoker_forwards_controls(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_dispatch(assignments, roster, **kwargs):
        captured["assignments"] = assignments
        captured["roster"] = roster
        captured.update(kwargs)
        return [
            WorkerResult(
                worker="luna",
                task="research.plan",
                text='{"sub_questions": []}',
                ok=True,
                detail="",
            )
        ]

    monkeypatch.setattr("brigade.run_seat.run_transport.dispatch", fake_dispatch)
    invoker = SeatInvoker(
        roster=_research_roster(),
        cwd=tmp_path,
        run_id="research-1",
        process_registry=ProcessRegistry(),
        output_dir=tmp_path / ".brigade" / "runs" / "research-1",
    )

    result = invoker.invoke(
        seat="luna",
        phase="research.plan",
        prompt="Return strict JSON",
        timeout=91,
    )

    assert result.text == '{"sub_questions": []}'
    assert captured["read_only"] is True
    assert captured["direct"] is True
    assert captured["run_id"] == "research-1"
    assert captured["cwd"] == tmp_path
    selected = captured["roster"].agents["luna"]
    assert selected.model == "gpt-5.6-luna"
    assert selected.reasoning == "medium"
    assert selected.timeout_seconds == 91.0
    assignment = captured["assignments"][0]
    assert assignment.worker == "luna"
    assert assignment.task == "research.plan"


def test_seat_invoker_build_prompt_returns_exact_prompt(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_dispatch(assignments, roster, **kwargs):
        build_prompt = kwargs["build_prompt"]
        captured["prompt"] = build_prompt(
            roster.agents["luna"],
            assignments[0],
            prior_results=[],
            read_only=True,
            direct=True,
            code_graph=object(),
            drift_impact=object(),
            evidence=object(),
        )
        return [
            WorkerResult(
                worker="luna",
                task="research.plan",
                text="ok",
                ok=True,
                detail="",
            )
        ]

    monkeypatch.setattr("brigade.run_seat.run_transport.dispatch", fake_dispatch)
    invoker = SeatInvoker(
        roster=_research_roster(),
        cwd=tmp_path,
        run_id="research-1",
        process_registry=ProcessRegistry(),
        output_dir=tmp_path / ".brigade" / "runs" / "research-1",
    )

    invoker.invoke(seat="luna", phase="research.plan", prompt="exact-prompt-body", timeout=91)

    assert captured["prompt"] == "exact-prompt-body"


def test_seat_invoker_forwards_budget_callbacks(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def on_requested(agent: Agent) -> int | None:
        return 1

    def on_observed(agent: Agent, attempt: int) -> None:
        return None

    def on_completed(agent: Agent, attempt: int) -> None:
        return None

    def on_failed(agent: Agent, attempt: int) -> None:
        return None

    def fake_dispatch(assignments, roster, **kwargs):
        captured.update(kwargs)
        return [
            WorkerResult(
                worker="luna",
                task="research.plan",
                text="ok",
                ok=True,
                detail="",
            )
        ]

    monkeypatch.setattr("brigade.run_seat.run_transport.dispatch", fake_dispatch)
    invoker = SeatInvoker(
        roster=_research_roster(),
        cwd=tmp_path,
        run_id="research-1",
        process_registry=ProcessRegistry(),
        output_dir=tmp_path / ".brigade" / "runs" / "research-1",
        on_dispatch_requested=on_requested,
        on_dispatch_observed=on_observed,
        on_dispatch_completed=on_completed,
        on_dispatch_failed=on_failed,
    )

    invoker.invoke(seat="luna", phase="research.plan", prompt="plan", timeout=91)

    assert captured["on_dispatch_requested"] is on_requested
    assert captured["on_dispatch_observed"] is on_observed
    assert captured["on_dispatch_completed"] is on_completed
    assert captured["on_dispatch_failed"] is on_failed


def test_oracle_gate_wait_is_bounded_and_returns_timeout(monkeypatch, tmp_path: Path) -> None:
    class BusyGate:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def acquire(self, *, timeout: float) -> bool:
            self.timeouts.append(timeout)
            return False

        def release(self) -> None:
            raise AssertionError("a gate that was not acquired must not be released")

    gate = BusyGate()
    monkeypatch.setattr("brigade.run_seat._ORACLE_GATE", gate)
    monkeypatch.setattr(
        "brigade.run_seat.run_transport.dispatch",
        lambda *_args, **_kwargs: pytest.fail("dispatch must not run after the Oracle gate times out"),
    )
    invoker = SeatInvoker(
        roster=_oracle_research_roster(),
        cwd=tmp_path,
        run_id="research-1",
        process_registry=ProcessRegistry(),
        output_dir=tmp_path / ".brigade" / "runs" / "research-1",
    )

    result = invoker.invoke(seat="oracle", phase="research.plan", prompt="plan", timeout=7)

    assert gate.timeouts == [7.0]
    assert result.ok is False
    assert result.failure_phase == "dispatch"
    assert result.failure_kind == "timeout"
    assert "oracle" in result.detail.lower()
    assert "wait" in result.detail.lower()


def test_seat_invoker_keeps_per_invocation_log_paths(monkeypatch, tmp_path: Path) -> None:
    responses = [
        WorkerResult(
            worker="luna",
            task="research.plan",
            text="first",
            ok=True,
            detail="",
            stdout="first-stdout\n",
            stderr="first-stderr\n",
        ),
        WorkerResult(
            worker="luna",
            task="research.plan",
            text="second",
            ok=True,
            detail="",
            stdout="second-stdout\n",
            stderr="second-stderr\n",
        ),
    ]

    monkeypatch.setattr(
        "brigade.run_seat.run_transport.dispatch",
        lambda *_args, **_kwargs: [responses.pop(0)],
    )
    output_dir = tmp_path / ".brigade" / "runs" / "research-1"
    invoker = SeatInvoker(
        roster=_research_roster(),
        cwd=tmp_path,
        run_id="research-1",
        process_registry=ProcessRegistry(),
        output_dir=output_dir,
    )

    first = invoker.invoke(seat="luna", phase="research.plan", prompt="one", timeout=91)
    second = invoker.invoke(seat="luna", phase="research.plan", prompt="two", timeout=91)

    receipts = sorted((output_dir / "workers").glob("*.json"))
    assert len(receipts) == 2
    first_payload = json.loads(receipts[0].read_text(encoding="utf-8"))
    second_payload = json.loads(receipts[1].read_text(encoding="utf-8"))
    assert first.attempt_id == "research-1:0001"
    assert second.attempt_id == "research-1:0002"
    assert first_payload["stdout_log"] != second_payload["stdout_log"]
    assert first_payload["stderr_log"] != second_payload["stderr_log"]
    assert (output_dir / first_payload["stdout_log"]).read_text(encoding="utf-8") == "first-stdout\n"
    assert (output_dir / first_payload["stderr_log"]).read_text(encoding="utf-8") == "first-stderr\n"
    assert (output_dir / second_payload["stdout_log"]).read_text(encoding="utf-8") == "second-stdout\n"
    assert (output_dir / second_payload["stderr_log"]).read_text(encoding="utf-8") == "second-stderr\n"


def test_seat_invoker_drops_late_worker_receipt_after_close_holds_receipt_lock_boundary(
    monkeypatch, tmp_path: Path
) -> None:
    entered_dispatch = Event()
    release_dispatch = Event()

    def fake_dispatch(*_args, **_kwargs):
        entered_dispatch.set()
        assert release_dispatch.wait(timeout=1)
        return [WorkerResult(worker="luna", task="research.discover", text="[]", ok=True, detail="")]

    monkeypatch.setattr("brigade.run_seat.run_transport.dispatch", fake_dispatch)
    receipt_admission = Event()
    receipt_admission.set()
    output_dir = tmp_path / ".brigade" / "runs" / "research-1"
    invoker = SeatInvoker(
        roster=_research_roster(),
        cwd=tmp_path,
        run_id="research-1",
        process_registry=ProcessRegistry(),
        output_dir=output_dir,
        receipt_admission=receipt_admission,
    )
    result_box = {}
    worker = Thread(
        target=lambda: result_box.setdefault(
            "result",
            invoker.invoke(seat="luna", phase="research.discover", prompt="discover", timeout=91),
        )
    )
    worker.start()
    assert entered_dispatch.wait(timeout=1)

    invoker._receipt_lock.acquire()
    closer = Thread(target=invoker.close_receipts)
    closer.start()
    invoker._receipt_lock.release()
    closer.join(timeout=1)
    assert not closer.is_alive()

    release_dispatch.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert result_box["result"].attempt_id == "research-1:discarded"
    assert not (output_dir / "workers").exists()


def test_seat_invoker_empty_dispatch_returns_typed_failed_result(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("brigade.run_seat.run_transport.dispatch", lambda *_args, **_kwargs: [])
    output_dir = tmp_path / ".brigade" / "runs" / "research-1"
    invoker = SeatInvoker(
        roster=_research_roster(),
        cwd=tmp_path,
        run_id="research-1",
        process_registry=ProcessRegistry(),
        output_dir=output_dir,
    )

    result = invoker.invoke(seat="luna", phase="research.plan", prompt="plan", timeout=91)

    assert result.ok is False
    assert result.failure_phase == "dispatch"
    assert result.failure_kind == "unclassified"
    assert "no worker" in result.detail.lower() or "empty" in result.detail.lower()
    receipts = list((output_dir / "workers").glob("*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["ok"] is False
    assert receipt["failure_kind"] == "unclassified"
    assert receipt["attempt_id"] == "research-1:0001"


def test_seat_invoker_does_not_persist_raw_browser_auth_error(monkeypatch, tmp_path: Path) -> None:
    nested = WorkerAttempt(
        kind="initial",
        worker="luna",
        task="research.synthesize",
        transport="direct",
        model="gpt-5.6-luna",
        reasoning="medium",
        started_at="2026-08-13T00:00:00+00:00",
        finished_at="2026-08-13T00:00:01+00:00",
        exit_code=1,
        terminal_reason="browser-auth",
        failure_phase="provider-preflight",
        failure_kind="browser-auth",
        session_id=None,
        selected=True,
        stdout="browser session expired for /secret/profile",
        stderr="browser session expired for /secret/profile",
        detail="browser authentication failed; run research doctor",
        ok=False,
    )
    monkeypatch.setattr(
        "brigade.run_seat.run_transport.dispatch",
        lambda *_args, **_kwargs: [
            WorkerResult(
                worker="luna",
                task="research.synthesize",
                text="",
                ok=False,
                detail="browser authentication failed; run research doctor",
                failure_phase="provider-preflight",
                failure_kind="browser-auth",
                stdout="browser session expired for /secret/profile",
                stderr="browser session expired for /secret/profile",
                attempts=(nested,),
            )
        ],
    )
    output_dir = tmp_path / ".brigade" / "runs" / "research-1"
    invoker = SeatInvoker(
        roster=_research_roster(),
        cwd=tmp_path,
        run_id="research-1",
        process_registry=ProcessRegistry(),
        output_dir=output_dir,
    )

    invoker.invoke(seat="luna", phase="research.synthesize", prompt="write", timeout=91)

    tree = _tree_text(output_dir)
    assert "/secret/profile" not in tree
    assert "browser session expired" not in tree
    receipt = json.loads(next((output_dir / "workers").glob("*.json")).read_text(encoding="utf-8"))
    assert receipt["failure_kind"] == "browser-auth"
    assert receipt["detail"] == "browser authentication failed; run research doctor"
    assert receipt["attempts"][0]["failure_kind"] == "browser-auth"


def test_oracle_gate_wait_passes_remaining_timeout_to_dispatch(monkeypatch, tmp_path: Path) -> None:
    """Finding 6: gate wait must not leave dispatch with a full fresh deadline_ceiling."""
    import brigade.run_seat as run_seat_mod

    clock = {"now": 1000.0}

    def fake_monotonic() -> float:
        return clock["now"]

    class PartialWaitGate:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def acquire(self, *, timeout: float) -> bool:
            self.timeouts.append(timeout)
            # Deterministic clock model: gate wait consumes half the budget.
            clock["now"] += timeout / 2.0
            return True

        def release(self) -> None:
            return None

    captured: dict[str, object] = {}

    def fake_dispatch(_assignments, roster, **_kwargs):
        # Roster agent timeout is what SeatInvoker stamped after the gate wait.
        captured["dispatch_timeout"] = roster.agents["oracle"].timeout_seconds
        return [
            WorkerResult(
                worker="oracle",
                task="research.plan",
                text='{"sub_questions": []}',
                ok=True,
                detail="",
            )
        ]

    gate = PartialWaitGate()
    # raising=False so the RED pin still works before production imports time.
    monkeypatch.setattr(
        run_seat_mod,
        "time",
        type("T", (), {"monotonic": staticmethod(fake_monotonic)})(),
        raising=False,
    )
    monkeypatch.setattr("brigade.run_seat._ORACLE_GATE", gate)
    monkeypatch.setattr("brigade.run_seat.run_transport.dispatch", fake_dispatch)
    invoker = SeatInvoker(
        roster=_oracle_research_roster(),
        cwd=tmp_path,
        run_id="research-1",
        process_registry=ProcessRegistry(),
        output_dir=tmp_path / ".brigade" / "runs" / "research-1",
    )

    result = invoker.invoke(
        seat="oracle",
        phase="research.plan",
        prompt="plan",
        timeout=10,
        deadline_ceiling=True,
    )

    assert result.ok is True
    assert gate.timeouts == [10.0]
    # Desired: dispatch receives remaining time after gate wait, not another full 10s.
    assert captured["dispatch_timeout"] == pytest.approx(5.0)


def test_oracle_gate_exhaustion_does_not_dispatch(monkeypatch, tmp_path: Path) -> None:
    """Finding A: gate wait that consumes the whole deadline must not dispatch with 0s."""
    import brigade.run_seat as run_seat_mod

    clock = {"now": 1000.0}

    def fake_monotonic() -> float:
        return clock["now"]

    class ExhaustingGate:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def acquire(self, *, timeout: float) -> bool:
            self.timeouts.append(timeout)
            # Deterministic: waiting for the gate burns the entire remaining budget.
            clock["now"] += timeout
            return True

        def release(self) -> None:
            return None

    gate = ExhaustingGate()
    monkeypatch.setattr(
        run_seat_mod,
        "time",
        type("T", (), {"monotonic": staticmethod(fake_monotonic)})(),
        raising=False,
    )
    monkeypatch.setattr("brigade.run_seat._ORACLE_GATE", gate)
    monkeypatch.setattr(
        "brigade.run_seat.run_transport.dispatch",
        lambda *_args, **_kwargs: pytest.fail("dispatch must not run after deadline exhaustion"),
    )
    invoker = SeatInvoker(
        roster=_oracle_research_roster(),
        cwd=tmp_path,
        run_id="research-1",
        process_registry=ProcessRegistry(),
        output_dir=tmp_path / ".brigade" / "runs" / "research-1",
    )

    result = invoker.invoke(
        seat="oracle",
        phase="research.plan",
        prompt="plan",
        timeout=10,
        deadline_ceiling=True,
    )

    assert gate.timeouts == [10.0]
    assert result.ok is False
    assert result.failure_phase == "dispatch"
    assert result.failure_kind == "timeout"
