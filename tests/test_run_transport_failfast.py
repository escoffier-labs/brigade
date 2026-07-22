"""fail_fast stage skipping for run_transport.dispatch (authoritative-router task 1)."""

import pytest

from brigade import agents, run_transport
from brigade.roster import Agent, Roster
from brigade.run_transport import Assignment, WorkerResult


def _roster_for(assignments: list[Assignment]) -> Roster:
    seats = {"chef": Agent(name="chef", cli="codex", role="plan")}
    for assignment in assignments:
        seats[assignment.worker] = Agent(name=assignment.worker, cli=assignment.worker, role="worker")
    return Roster(orchestrator="chef", agents=seats, max_workers=1)


@pytest.fixture
def dispatch_harness(monkeypatch, tmp_path):
    """Lift the recovery suite's Roster + stubbed run_agent pattern into a fixture."""

    def _run(
        assignments: list[Assignment],
        results_by_worker: dict[str, list[agents.AgentResult]],
        *,
        fail_fast: bool = True,
        **dispatch_kwargs,
    ) -> tuple[list[WorkerResult], list[str]]:
        direct_calls: list[str] = []
        iterators = {worker: iter(results) for worker, results in results_by_worker.items()}

        def fake_run_agent(cli_ref, prompt, **kwargs):  # noqa: ARG001
            direct_calls.append(cli_ref)
            return next(iterators[cli_ref])

        monkeypatch.setattr(agents, "run_agent", fake_run_agent)
        results = run_transport.dispatch(
            assignments,
            _roster_for(assignments),
            build_prompt=lambda agent, assignment, **kwargs: assignment.task,
            run_appserver_worker=lambda *a, **kw: agents.AgentResult(text="", ok=False, detail="unused"),
            event_writer=lambda events_dir, worker, verbose=False: None,
            cwd=tmp_path,
            read_only=True,
            fail_fast=fail_fast,
            **dispatch_kwargs,
        )
        return results, direct_calls

    return _run


def test_failed_stage_skips_later_stages_under_fail_fast(dispatch_harness):
    assignments = [
        Assignment(worker="a", task="stage one", stage=1),
        Assignment(worker="b", task="stage two", stage=2),
        Assignment(worker="c", task="stage three", stage=3),
    ]
    results, direct_calls = dispatch_harness(
        assignments,
        {
            "a": [agents.AgentResult(text="", ok=False, detail="boom")],
            "b": [agents.AgentResult(text="must not run", ok=True)],
            "c": [agents.AgentResult(text="must not run", ok=True)],
        },
        fail_fast=True,
    )

    assert [result.status for result in results] == ["", "skipped", "skipped"]
    assert [result.ok for result in results] == [False, False, False]
    assert [result.text for result in results] == ["", "", ""]
    assert direct_calls == ["a"]
    assert "stage 1" in results[1].detail
    assert "stage 1" in results[2].detail
    assert results[1].detail == results[2].detail


def test_keep_going_invokes_later_stages_after_failure(dispatch_harness):
    assignments = [
        Assignment(worker="a", task="stage one", stage=1),
        Assignment(worker="b", task="stage two", stage=2),
    ]
    results, direct_calls = dispatch_harness(
        assignments,
        {
            "a": [agents.AgentResult(text="", ok=False, detail="boom")],
            "b": [agents.AgentResult(text="ran anyway", ok=True)],
        },
        fail_fast=False,
    )

    assert direct_calls == ["a", "b"]
    assert [result.ok for result in results] == [False, True]
    assert results[1].text == "ran anyway"


def test_all_ok_stages_succeed_under_fail_fast(dispatch_harness):
    assignments = [
        Assignment(worker="a", task="stage one", stage=1),
        Assignment(worker="b", task="stage two", stage=2),
    ]
    results, direct_calls = dispatch_harness(
        assignments,
        {
            "a": [agents.AgentResult(text="a ok", ok=True)],
            "b": [agents.AgentResult(text="b ok", ok=True)],
        },
        fail_fast=True,
    )

    assert direct_calls == ["a", "b"]
    assert [result.ok for result in results] == [True, True]
    assert [result.text for result in results] == ["a ok", "b ok"]
