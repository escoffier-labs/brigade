"""Optional batch synthesis: one budgeted, receipted call over settled task results.

Synthesis is a separate fact from task execution. It never changes a task's
status or result. One accepted batch runs at most one synthesis call: the
reservation is recorded before the call, and a retry that finds the reservation
without a sidecar reports ``indeterminate`` instead of calling again.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from . import agents, localio, run_budget, run_journal, run_lifecycle, run_tasks
from .run_transport import WorkerResult

SCHEMA = "brigade.batch_synthesis.v1"
SIDECAR = "batch-synthesis.json"
DISPATCH_MARKER = "batch-synthesis-dispatch.json"

COMPLETED = "completed"
FAILED = "failed"
REFUSED_BUDGET = "refused-budget"
INDETERMINATE = "indeterminate"

# Retry classification for the synthesis-only path.
PENDING_NONE = "none"
PENDING_DONE = "done"
PENDING_INDETERMINATE = "indeterminate"
PENDING_RUN = "run"
PENDING_BLOCKED = "blocked"

# A task that reached one of these states can no longer change, so a retry may
# synthesize from the stored results. A queued task means the batch never
# settled every member, so a retry does not synthesize.
_TERMINAL = frozenset({"completed", "failed", "cancelled", "unknown"})


@dataclass(frozen=True)
class Context:
    """Everything one synthesis call needs, owned by the enclosing run."""

    roster: Any
    budget: run_budget.BudgetCoordinator | None
    record_dispatch: Callable[[str, Any, int | None], int | None]
    ground_truth: Mapping[str, Any]
    cwd: Path | None
    codex_transport: str | None
    process_registry: Any
    model_lease: Any


@dataclass(frozen=True)
class Outcome:
    status: str
    seat: str | None
    output_ref: str | None = None
    output_sha256: str | None = None
    output_bytes: int | None = None
    text: str | None = None


def _read_states(run_dir: Path, request_id: str) -> dict[str, run_tasks.TaskResult]:
    try:
        return {
            state.handle.spec.task_id: state
            for state in run_tasks.read_task_batch(run_dir, request_id=request_id).members
        }
    except run_tasks.TaskJournalError:
        return {}


def _inputs(plan: Mapping[str, Any], run_dir: Path) -> list[dict[str, Any]]:
    states = _read_states(run_dir, str(plan["request_id"]))
    return [
        {
            "task_id": task["task_id"],
            "result_id": states[task["task_id"]].result_id if task["task_id"] in states else None,
        }
        for task in plan["tasks"]
    ]


def _all_terminal(plan: Mapping[str, Any], run_dir: Path) -> bool:
    states = _read_states(run_dir, str(plan["request_id"]))
    if len(states) != len(plan["tasks"]):
        return False
    return all(
        (state := states.get(task["task_id"])) is not None and state.status in _TERMINAL for task in plan["tasks"]
    )


def read(run_dir: Path) -> dict[str, Any] | None:
    """The batch synthesis sidecar, or None when absent or unreadable."""
    payload = localio.read_json_dict(run_dir / SIDECAR)
    if payload is None or payload.get("schema") != SCHEMA:
        return None
    return payload


def summary(run_dir: Path) -> dict[str, Any] | None:
    """The additive batch-result projection of the sidecar, with its own digest."""
    payload = read(run_dir)
    if payload is None:
        return None
    try:
        digest = hashlib.sha256((run_dir / SIDECAR).read_bytes()).hexdigest()
    except OSError:
        return None
    return {
        "status": payload.get("status"),
        "seat": payload.get("seat"),
        "ref": SIDECAR,
        "digest": digest,
    }


def marker_present(run_dir: Path) -> bool:
    return (run_dir / DISPATCH_MARKER).exists()


def pending(plan: Mapping[str, Any], run_dir: Path) -> str:
    """Classify what a retry owes an accepted batch's synthesis request."""
    if plan.get("synthesis") is None:
        return PENDING_NONE
    if read(run_dir) is not None:
        return PENDING_DONE
    if marker_present(run_dir):
        return PENDING_INDETERMINATE
    return PENDING_RUN if _all_terminal(plan, run_dir) else PENDING_BLOCKED


def _write_exclusive(path: Path, payload: Mapping[str, Any]) -> bool:
    """Create one receipt exactly once; never overwrite an existing fact."""
    try:
        localio.write_json_exclusive(path, dict(payload))
    except FileExistsError:
        return False
    run_journal._fsync_directory(path.parent)
    return True


def _write_sidecar(
    plan: Mapping[str, Any], run_dir: Path, seat: str, status: str, *, text: str | None = None, **output: Any
) -> Outcome:
    payload = {
        "schema": SCHEMA,
        "request_id": plan["request_id"],
        "seat": seat,
        "status": status,
        "inputs": _inputs(plan, run_dir),
        "output_ref": output.get("output_ref"),
        "output_sha256": output.get("output_sha256"),
        "output_bytes": output.get("output_bytes"),
    }
    _write_exclusive(run_dir / SIDECAR, payload)
    return Outcome(
        status=status,
        seat=seat,
        output_ref=payload["output_ref"],
        output_sha256=payload["output_sha256"],
        output_bytes=payload["output_bytes"],
        text=text,
    )


def write_indeterminate(plan: Mapping[str, Any], run_dir: Path) -> Outcome:
    """Record that a reserved call may have run; never call again."""
    synthesis = plan.get("synthesis") or {}
    seat = str(synthesis.get("seat") or "")
    return _write_sidecar(plan, run_dir, seat, INDETERMINATE)


def _write_marker(run_dir: Path, seat: str, attempt: int) -> None:
    # The reservation identity is allocated once; an existing marker is the
    # fact a retry reads, so leave it exactly as written.
    _write_exclusive(run_dir / DISPATCH_MARKER, {"seat": seat, "attempt": attempt})


def _call(
    plan: Mapping[str, Any], run_dir: Path, results: list[WorkerResult], context: Context, seat: str
) -> tuple[Any, bool]:
    from . import message_envelope
    from .aboyeur import planning, prompts

    agent = context.roster.agents[seat]
    prompt = prompts.build_synth_prompt(
        plan["synthesis"]["text"],
        results,
        read_only=True,
        ground_truth=dict(context.ground_truth),
        run_id=run_dir.name,
        to_seat=seat,
    )
    harness = agent.cli
    request = message_envelope.emit(
        prompt,
        kind="synthesis-request",
        producer="aboyeur.build_synth_prompt",
        from_seat=message_envelope.BRIGADE_SEAT,
        to_seat=seat,
        run_dir=run_dir,
        run_id=run_dir.name,
        assignment_id=message_envelope.default_assignment_id("synthesis-request"),
        session_harness=harness,
    )
    if not request.delivered:
        return (
            agents.AgentResult(
                text="",
                ok=False,
                detail=request.reason or "synthesis-request rejected by provenance gate",
                failure_phase="synthesis",
                failure_kind="unclassified",
            ),
            False,
        )
    result = planning._call_with_process_registry(
        planning._run_orchestrator,
        context.roster,
        prompt,
        cwd=context.cwd,
        read_only=True,
        codex_transport=context.codex_transport,
        process_registry=context.process_registry,
        model_lease=context.model_lease,
        seat=seat,
    )
    delivered = message_envelope.emit(
        result.text,
        kind="synthesis-result",
        producer="aboyeur.run",
        from_seat=seat,
        to_seat=message_envelope.BRIGADE_SEAT,
        run_dir=run_dir,
        run_id=run_dir.name,
        assignment_id=message_envelope.default_assignment_id("synthesis-result"),
        session_harness=harness,
    )
    if delivered.delivered:
        return result, True
    return (
        replace(
            result,
            text="",
            ok=False,
            detail=delivered.reason or "synthesis-result rejected by provenance gate",
            failure_phase=result.failure_phase or "synthesis",
            failure_kind=result.failure_kind or "unclassified",
        ),
        False,
    )


def perform(
    *,
    run_dir: Path,
    plan: Mapping[str, Any],
    results: list[WorkerResult],
    context: Context,
) -> Outcome:
    """Reserve once, call once, and record the separate synthesis fact."""
    synthesis = plan.get("synthesis")
    if synthesis is None:
        return Outcome(status=PENDING_NONE, seat=None)
    seat = str(synthesis["seat"])
    existing = read(run_dir)
    if existing is not None:
        return Outcome(
            status=str(existing.get("status") or FAILED),
            seat=str(existing.get("seat") or seat),
            output_ref=existing.get("output_ref"),
            output_sha256=existing.get("output_sha256"),
            output_bytes=existing.get("output_bytes"),
        )
    if marker_present(run_dir):
        # A reserved dispatch without a sidecar means the call may have run.
        return write_indeterminate(plan, run_dir)
    if context.budget is None:
        # A batch without a coordinator cannot reserve its synthesis unit.
        return _write_sidecar(plan, run_dir, seat, REFUSED_BUDGET)

    agent = context.roster.agents.get(seat)
    if agent is None:
        # The seat vanished from the roster after acceptance; no call is made.
        return _write_sidecar(plan, run_dir, seat, FAILED)

    def record(attempt: int) -> int | None:
        # The marker lands before the requested fact, so a crash between the
        # reservation and the call is still visible to a later retry.
        _write_marker(run_dir, seat, attempt)
        return context.record_dispatch("run.dispatch.requested", agent, attempt)

    try:
        context.budget.reserve_and_record_dispatch(
            seat=seat,
            allocate_attempt=lambda: run_lifecycle.allocate_next_dispatch_attempt(run_dir, seat),
            record_requested=record,
            reclaim_pending=False,
        )
    except run_budget.BudgetPolicyError:
        return _write_sidecar(plan, run_dir, seat, REFUSED_BUDGET)

    final, delivered = _call(plan, run_dir, results, context, seat)
    if not final.ok or not delivered:
        return _write_sidecar(plan, run_dir, seat, FAILED)

    path = run_dir / "final.txt"
    data = (final.text + "\n").encode("utf-8")
    path.write_bytes(data)
    run_journal._fsync_directory(run_dir)
    return _write_sidecar(
        plan,
        run_dir,
        seat,
        COMPLETED,
        text=final.text,
        output_ref="final.txt",
        output_sha256=hashlib.sha256(data).hexdigest(),
        output_bytes=len(data),
    )
