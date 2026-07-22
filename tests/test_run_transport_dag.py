"""DAG ready-queue scheduler for run_transport.dispatch (authoritative-router task 6).

Extends the Task-1 dispatch harness pattern: drives ``dispatch`` with a stubbed
``agents.run_agent`` while recording invocation start order thread-safely.
"""

import threading
import time

import pytest

from brigade import agents, run_transport
from brigade.roster import Agent, Roster
from brigade.run_transport import Assignment, WorkerResult


def _roster_for(assignments: list[Assignment]) -> Roster:
    seats = {"chef": Agent(name="chef", cli="codex", role="plan")}
    for assignment in assignments:
        seats[assignment.worker] = Agent(name=assignment.worker, cli=assignment.worker, role="worker")
    return Roster(orchestrator="chef", agents=seats, max_workers=max(2, len(assignments)))


def _a(worker, stage, covers):
    return Assignment(worker=worker, task=f"task-{worker}", stage=stage, covers=tuple(covers))


DEPS = {
    "plan": (),
    "implement": ("plan",),
    "docs": (),  # independent branch
    "review": ("implement",),
}


class _DagHarness:
    """Callable fixture: dispatches with scheduler="dag" and records start order."""

    def __init__(self, monkeypatch, tmp_path):
        self._monkeypatch = monkeypatch
        self._tmp_path = tmp_path
        self.invocations: list[str] = []
        self._lock = threading.Lock()

    def __call__(
        self,
        assignments,
        *,
        dependencies,
        outcomes=None,
        held=None,
        slow_workers=None,
    ):
        outcomes = outcomes or {}
        slow_workers = slow_workers or {}
        self.invocations = []
        roster = _roster_for(assignments)
        invocations = self.invocations
        lock = self._lock

        def fake_run_agent(cli_ref, prompt, **kwargs):  # noqa: ARG001
            with lock:
                invocations.append(cli_ref)
            if cli_ref in slow_workers:
                time.sleep(slow_workers[cli_ref])
            if cli_ref in outcomes:
                outcome = outcomes[cli_ref]
                return agents.AgentResult(text=outcome.text, ok=outcome.ok, detail=outcome.detail)
            return agents.AgentResult(text="ok", ok=True)

        self._monkeypatch.setattr(agents, "run_agent", fake_run_agent)
        return run_transport.dispatch(
            assignments,
            roster,
            build_prompt=lambda agent, assignment, **kw: assignment.task,
            run_appserver_worker=lambda *a, **kw: agents.AgentResult(text="", ok=False, detail="unused"),
            event_writer=lambda events_dir, worker, verbose=False: None,
            cwd=self._tmp_path,
            read_only=True,
            scheduler="dag",
            route_dependencies=dict(dependencies),
            route_held=held or {},
        )

    @property
    def invocations_set(self):
        return set(self.invocations)

    def start_order_for(self, assignments, *, dependencies, slow_workers=None):
        self(assignments, dependencies=dependencies, slow_workers=slow_workers)
        return list(self.invocations)


@pytest.fixture
def dag_harness(monkeypatch, tmp_path):
    return _DagHarness(monkeypatch, tmp_path)


def test_failure_skips_only_dependents(dag_harness):
    results = dag_harness(
        assignments=[
            _a("p", 1, ["plan"]),
            _a("i", 2, ["implement"]),
            _a("d", 2, ["docs"]),
            _a("r", 3, ["review"]),
        ],
        dependencies=DEPS,
        outcomes={"p": WorkerResult(worker="p", task="task-p", text="", ok=False, detail="boom")},
    )
    by_worker = {r.worker: r for r in results}
    assert by_worker["i"].status == "skipped"
    assert by_worker["r"].status == "skipped"
    assert by_worker["d"].ok is True  # independent branch still ran
    assert dag_harness.invocations_set == {"p", "d"}


def test_ready_dispatch_no_stage_barrier(dag_harness):
    # 'docs' (no deps) must start before 'implement' (waits on slow 'plan')
    order = dag_harness.start_order_for(
        assignments=[
            _a("p", 1, ["plan"]),
            _a("i", 2, ["implement"]),
            _a("d", 2, ["docs"]),
        ],
        dependencies=DEPS,
        slow_workers={"p": 0.3},
    )
    assert order.index("d") < order.index("i")


def test_held_stage_never_dispatched(dag_harness):
    results = dag_harness(
        assignments=[_a("p", 1, ["plan"]), _a("i", 2, ["implement"])],
        dependencies=DEPS,
        held={"plan": ["approve:plan"]},
    )
    by_worker = {r.worker: r for r in results}
    assert by_worker["p"].status == "held"
    assert by_worker["i"].status == "skipped"
    assert dag_harness.invocations_set == set()


def test_uncovered_assignment_falls_back_to_waves(dag_harness, capsys):
    results = dag_harness(
        assignments=[_a("p", 1, ["plan"]), Assignment(worker="x", task="t", stage=2)],
        dependencies=DEPS,
    )
    assert "falling back to wave scheduler" in capsys.readouterr().err
    assert len(results) == 2
