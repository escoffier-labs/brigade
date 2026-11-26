"""Acknowledgement, finalize and status for governed native children.

``ack`` consumes one exact terminal result. ``finalize`` closes the run
explicitly and is never automatic. ``status`` reads fixed tokens for every task
without native details. Task text and raw native payloads never enter a report,
a refusal, a journal payload or a command-line argument.

The host adapter supplies the persisted schemas through ``NativeChildHost`` and
the run lock through ``owned_run``, so a replaced lock takes effect.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable, Sequence

from . import native_child_capacity, run_journal, run_lifecycle, run_tasks
from . import native_child_journal as journal_core
from . import run_task_journal as journal
from .native_child_host import NativeChildHost
from .native_child_journal import refuse
from .native_child_observations import RESOURCE_EVENT

OwnedRun = Callable[[Path, Path], AbstractContextManager[Any]]


def ack(
    host: NativeChildHost, *, workspace: Path, run_id: str, task_id: str, result_id: str, owned_run: OwnedRun
) -> dict[str, Any]:
    """Acknowledge exactly one retained terminal result, idempotently."""
    workspace = workspace.expanduser().resolve()
    if not journal.digest(result_id):
        raise refuse("invalid-identity", "the result identity is not valid")
    run_dir = journal_core.context(workspace, run_id, task_id)
    with owned_run(workspace, run_dir):
        journal_core.read_meta(host, workspace, run_dir)
        events = journal_core.read_events(run_dir)
        held = journal_core.held(events, task_id)
        if held is not None:
            raise refuse("task-held", f"the task is held ({held})")
        task = journal_core.find_task(journal_core.read_tasks(run_dir), task_id)
        if task.result_id is None:
            raise refuse("task-nonterminal", "the task has no terminal result to acknowledge")
        if task.result_id != result_id:
            raise refuse("result-mismatch", "the result identity does not match the retained result")
        try:
            updated = run_tasks.acknowledge_result(run_dir, workspace=workspace, task_id=task_id, result_id=result_id)
        except run_tasks.TaskJournalError as exc:
            raise refuse("ack-refused", f"the acknowledgement was refused ({exc})") from None
        return {
            "schema": host.ack_schema,
            "run_id": run_dir.name,
            "task_id": task_id,
            "result_id": result_id,
            "acknowledged": updated.acknowledged,
            "replayed": task.acknowledged,
        }


def finalize(
    host: NativeChildHost, *, workspace: Path, run_id: str, owned_run: OwnedRun, require_released: bool = False
) -> dict[str, Any]:
    """Close the run explicitly: completed only when every task is acked.

    An abandoned-unknown task refuses finalization. Releasing live-work
    capacity cannot be separated from the terminal run status here, and an
    abandoned task must never imply quiescence or refund its dispatch unit.
    """
    workspace = workspace.expanduser().resolve()
    run_dir = journal_core.run_dir(workspace, run_id)
    with owned_run(workspace, run_dir):
        journal_core.run_meta(host, workspace, run_dir)
        events = journal_core.read_events(run_dir)
        results = journal_core.read_tasks(run_dir)
        if not results:
            raise refuse("finalize-refused", "the run has no enrolled tasks")
        abandoned = [
            result.handle.spec.task_id
            for result in results
            if journal_core.held(events, result.handle.spec.task_id) == journal_core.ABANDONED
        ]
        if abandoned:
            raise refuse(
                "finalize-abandoned",
                "an abandoned task keeps live-work capacity, so the run cannot be finalized",
            )
        held = [
            result.handle.spec.task_id
            for result in results
            if journal_core.held(events, result.handle.spec.task_id) is not None
        ]
        if held:
            raise refuse(
                "finalize-held",
                "an unresolved task hold prevents finalization until an operator resolves it",
            )
        if require_released and any(
            reason != "awaiting-ack" for reason in native_child_capacity.capacity_reasons(events).values()
        ):
            raise refuse("finalize-live", "a claimed worker still needs terminal or release evidence")
        completed = all(
            result.status == "completed" and result.result_id is not None and result.acknowledged for result in results
        )
        decision = "completed" if completed else "failed"
        try:
            from . import aboyeur, localio

            aboyeur.update_run_receipt(
                run_dir,
                status=decision,
                finished_at=localio.utc_now_iso(),
                native_finalize={
                    "schema": host.finalize_schema,
                    "decision": decision,
                    "tasks": len(results),
                    "acknowledged": sum(1 for result in results if result.acknowledged),
                },
            )
        except (OSError, ValueError, run_journal.RunJournalError, run_lifecycle.LifecycleJournalError) as exc:
            raise refuse("journal-refused", f"the run receipt refused finalization ({exc})") from None
        return {
            "schema": host.finalize_schema,
            "run_id": run_dir.name,
            "status": decision,
            "tasks": len(results),
            "acknowledged": sum(1 for result in results if result.acknowledged),
            "terminals": sum(1 for result in results if result.result_id is not None),
        }


def status(
    host: NativeChildHost,
    *,
    workspace: Path,
    run_id: str,
    correlated: Callable[[Sequence[run_journal.RunEvent], str], bool],
    include_capacity: bool = False,
) -> dict[str, Any]:
    """Read-only fixed tokens for every task, without native details.

    ``correlated`` is the adapter's answer to whether a task has a recorded
    native correlation, because the correlation record is host specific.
    """
    workspace = workspace.expanduser().resolve()
    run_dir = journal_core.run_dir(workspace, run_id)
    meta = journal_core.read_meta(host, workspace, run_dir)
    events = journal_core.read_events(run_dir)
    reasons = native_child_capacity.capacity_reasons(events) if include_capacity else {}
    rows = []
    for result in sorted(journal_core.read_tasks(run_dir), key=lambda item: item.handle.spec.task_id):
        task_id = result.handle.spec.task_id
        binding = journal_core.claim_binding(events, task_id)
        rows.append(
            {
                "task_id": task_id,
                "state": result.status,
                "held_reason": journal_core.held(events, task_id),
                "correlated": correlated(events, task_id),
                "terminal": result.result_id is not None,
                "acked": result.acknowledged,
                "cancel_requested": journal_core.latest(events, host.cancel_event, task_id) is not None,
                "attempt": binding.dispatch_attempt if binding is not None else None,
                "seat_digest": journal_core.digest({"seat": result.handle.spec.seat}, host.seat_digest_domain),
            }
        )
        if include_capacity:
            reason = reasons.get(task_id)
            refusal = journal_core.start_refusal(events, task_id)
            if reason == "awaiting-terminal" and refusal is None and not rows[-1]["correlated"]:
                reason = "start-unconfirmed"
            rows[-1].update(holds_capacity=reason is not None, capacity_reason=reason)
            if refusal is not None:
                rows[-1].update(start_refusal=refusal, result_id=result.result_id)
            if refusal == "start-failed":
                resource = journal_core.latest(events, RESOURCE_EVENT, task_id)
                retained = resource.get("worktree_retained") if resource is not None else None
                if type(retained) is bool:
                    rows[-1]["worktree_retained"] = retained
    return {
        "schema": host.status_schema,
        "run_id": run_dir.name,
        "status": meta.get("status"),
        "tasks": rows,
    }
