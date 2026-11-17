"""Per-task launch claims for explicit batches.

The run budget reserves one dispatch attempt for an exact seat. A batch task
claims launch permission inside that reservation, before the requested fact,
because ``claim_task_launch`` refuses any binding a ``run.dispatch.*`` event
already used. A refusal keeps the reservation: a claim may already be durable.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import run_tasks
from .run_transport import Assignment, LaunchRefused


def launch_recorder(
    *,
    run_dir: Path,
    workspace: Path,
    assignment: Assignment | None,
    seat: str,
    prepared: tuple[run_tasks.TaskHandle, str] | None,
    record_requested: Callable[[int], int | None],
    on_refused: Callable[[str], None],
) -> Callable[[int], int | None]:
    """Build the ``record_requested`` callback for one task's reservation."""
    task_id = assignment.task_id if assignment is not None else None

    def refuse(reason: str) -> LaunchRefused:
        # An explicit check, not an assert: it must hold under python -O too.
        if task_id is None:
            return LaunchRefused(f"batch launch refused: no task identity ({reason})")
        on_refused(task_id)
        return LaunchRefused(f"task {task_id} launch refused: {reason}")

    def record(attempt: int) -> int | None:
        if (
            task_id is None
            or prepared is None
            or prepared[0].attempt_id != (assignment.attempt_id if assignment else None)
        ):
            raise refuse("no prepared attempt for this assignment")
        handle, version = prepared
        try:
            decision = run_tasks.claim_task_launch(
                run_dir,
                workspace=workspace,
                handle=handle,
                expected_version=version,
                seat=seat,
                dispatch_attempt=attempt,
            )
        except run_tasks.TaskJournalError as exc:
            raise refuse(f"journal rejected the claim ({exc})") from exc
        if decision.outcome != "granted":
            raise refuse(decision.outcome)
        # A crash here leaves the claim durable without a requested fact. A retry
        # then sees "already-claimed" and never relaunches.
        return record_requested(attempt)

    return record


def refuse_unbudgeted(assignment: Assignment | None, on_refused: Callable[[str], None]) -> Any:
    """A batch launch without a budget coordinator cannot be claimed, so it never starts."""
    if assignment is not None and assignment.task_id is not None:
        on_refused(assignment.task_id)
    raise LaunchRefused("batch launch requires the run budget coordinator and lifecycle journal")


def unbudgeted(batch: Any, agent: Any, assignment: Assignment | None, dispatch_fact: Callable[..., int | None]) -> Any:
    """Ordinary runs without a budget coordinator record the requested fact; batches refuse."""
    if batch is None:
        return dispatch_fact("run.dispatch.requested", agent)
    return batch.refuse_unbudgeted(assignment)


def recorder_for(
    batch: Any, agent: Any, assignment: Assignment | None, dispatch_fact: Callable[..., int | None]
) -> Callable[[int], int | None]:
    """The ``record_requested`` callback: plain for ordinary runs, claim-first for a batch task."""
    if batch is None:
        return lambda attempt: dispatch_fact("run.dispatch.requested", agent, attempt)
    return batch.launch_recorder(agent, assignment, dispatch_fact)
