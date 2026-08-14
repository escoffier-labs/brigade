# tests/test_run_seat.py
from __future__ import annotations

import json
from pathlib import Path

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
