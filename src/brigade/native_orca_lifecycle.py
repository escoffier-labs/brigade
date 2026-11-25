"""Observe and cancel an already-correlated Orca worker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import native_child_admission, native_child_capacity, native_child_lifecycle, run_tasks
from . import native_child_journal as journal
from . import native_orca_observations as observations
from .native_child_host import NativeChildCustodyError
from .native_child_observations import RESOURCE_EVENT, NativeObservation, observation_diagnostic
from .native_orca_dispatch import WorkerReference
from .native_orca_host import HOST
from .native_orca_verbs import CORRELATION_EVENT, ClaimedTask, claimed_task
from .native_child_custody import owned_run

_OBSERVE_SCHEMA = "brigade.orca_observe.v1"
_CANCEL_SCHEMA = "brigade.orca_cancel.v1"


def _worker(task: ClaimedTask) -> WorkerReference:
    task.require_unheld()
    worker = task.correlated_worker()
    if worker is None:
        raise journal.refuse("no-correlation", "the task has no recorded Orca correlation")
    return worker


def _record_resource(task: ClaimedTask, reason: str | None) -> None:
    prior = next(
        (
            event
            for event in reversed(task.events)
            if event.event_type == RESOURCE_EVENT and event.payload.get("task_id") == task.task_id
        ),
        None,
    )
    previous = prior.payload["reason"] if prior is not None else None
    if reason is None or previous in (reason, "released"):
        return
    task.append(RESOURCE_EVENT, {"reason": reason}, reason, prior.event_digest if prior is not None else None)


def _record_observation(task: ClaimedTask, observation: NativeObservation, show: object) -> run_tasks.TaskResult:
    if observation.family == "refused":
        raise journal.refuse(observation.reason, "Orca refused or did not match the retained dispatch")
    result = task.verified.result
    terminal = observation.family == "terminal-candidate"
    if terminal and result.result_id is not None and observation.status != result.status:
        raise journal.refuse("observation-conflict", "the Orca observation contradicts the retained result")
    retained_status = observation.status if terminal else result.status if result.result_id is not None else None
    worker = observation.reference
    reason = observations.resource_state(worker, show, retained_status=retained_status)
    # Publish resource evidence before making a new result available to ack.
    _record_resource(task, reason or ("release-unverified" if terminal else None))
    if terminal or (result.result_id is None and observation.family == "active"):
        status = "waiting-for-input" if observation.status == "waiting" else observation.status
        assert status is not None
        try:
            result = run_tasks.observe_task(
                task.run_dir,
                workspace=task.workspace,
                handle=result.handle,
                observation=run_tasks.TaskObservation(
                    status=status,
                    provider_task_id=worker.dispatch_id,
                    original_run_id=worker.run_id,
                    artifact_kind="receipt" if terminal else None,
                    artifact_digest=observation.result_transfer_id if terminal else None,
                    containment=observation.containment,
                ),
                expected_version=result.version,
            )
        except run_tasks.TaskJournalError:
            raise journal.refuse("observation-conflict", "the journal refused the Orca observation") from None
    return result


def observe(
    *, workspace: Path, run_id: str, task_id: str, payload: object, worker_done: object, transcript: object
) -> dict[str, Any]:
    with claimed_task(workspace, run_id, task_id) as task:
        worker = _worker(task)
        observation = observations.classify_show(worker, payload, worker_done=worker_done, transcript=transcript)
        result = _record_observation(task, observation, payload)
        report = {
            **task.report(_OBSERVE_SCHEMA),
            **observation_diagnostic(observation),
            "status": result.status,
            "result_id": result.result_id,
            "inbox_argv": observations.inbox_argv(worker),
            "transcript_argv": observations.result_argv(worker),
            "observe_argv": observations.observe_argv(worker),
        }
        if observation.transcript_clipped:
            report["transcript_clipped"] = True
        return report


def cancel(
    *, workspace: Path, run_id: str, task_id: str, payload: object, reporting: bool, exit_code: int, show: object
) -> dict[str, Any]:
    with claimed_task(workspace, run_id, task_id) as task:
        worker = _worker(task)
        result = task.verified.result
        prior = journal.latest(task.events, HOST.cancel_event, task_id)
        fields = task.report(_CANCEL_SCHEMA)
        if not reporting:
            if result.result_id is not None:
                raise journal.refuse("task-terminal", "the task already has a terminal result")
            if prior is None:
                cancel_key = observations.cancel_key(worker)
                task.append(
                    HOST.cancel_event,
                    {
                        "handle_digest": worker.handle_digest,
                        "cancel_key": journal.digest({"key": cancel_key}, _CANCEL_SCHEMA),
                    },
                    cancel_key,
                )
            return {**fields, "argv": observations.cancel_argv(worker), "replayed": prior is not None}
        if prior is None:
            raise journal.refuse("cancel-unrequested", "record a cancel request before supplying its Orca result")
        observation = observations.parse_stop(worker, payload, exit_code=exit_code, show=show)
        result = _record_observation(task, observation, show)
        return {
            **fields,
            **observation_diagnostic(observation),
            "status": result.status,
            "result_id": result.result_id,
            "observe_argv": observations.observe_argv(worker),
        }


def ack(*, workspace: Path, run_id: str, task_id: str, result_id: str) -> dict[str, Any]:
    return native_child_lifecycle.ack(
        HOST, workspace=workspace, run_id=run_id, task_id=task_id, result_id=result_id, owned_run=owned_run
    )


def finalize(*, workspace: Path, run_id: str) -> dict[str, Any]:
    return native_child_lifecycle.finalize(
        HOST,
        workspace=workspace,
        run_id=run_id,
        owned_run=owned_run,
        require_released=True,
    )


def status(*, workspace: Path, run_id: str) -> dict[str, Any]:
    report = native_child_lifecycle.status(
        HOST,
        workspace=workspace,
        run_id=run_id,
        correlated=lambda events, task: journal.latest(events, CORRELATION_EVENT, task) is not None,
        include_capacity=True,
    )
    occupancy = native_child_capacity.governed_occupancy(HOST, workspace)
    try:
        limit = native_child_admission.load_roster(workspace).max_workers
    except NativeChildCustodyError:
        limit = None
    return {
        **report,
        "capacity": {
            "live": occupancy.live if occupancy.authoritative else None,
            "max_workers": limit,
            "diagnostic": occupancy.diagnostic,
        },
    }
