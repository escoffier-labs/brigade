"""Durable launch and correlation for a caller-mediated Orca worker."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from . import native_child_admission as admission
from . import native_child_capacity as capacity
from . import native_child_claim as claims
from . import native_child_journal as journal
from . import native_orca_dispatch as dispatch
from . import native_orca_enrollment as enrollment
from . import native_orca_observations as observations
from . import run_journal, run_tasks
from .native_child_custody import owned_run
from .native_child_host import NativeChildError
from .native_child_observations import RESOURCE_EVENT, START_UNCONFIRMED
from .native_orca_host import HOST

CORRELATION_EVENT = "task.native-correlated"
CORRELATION_SCHEMA = "brigade.orca_correlation.v1"
_INTENT_DOMAIN = "brigade.native-orca.intent.v1"


def _intent(
    handle: run_tasks.TaskHandle, attempt: int, batch_ref: str, template: str, orca_run: str, orca_task: str
) -> tuple[dispatch.LaunchBinding, str]:
    if handle.attempt_id is None:
        raise journal.refuse("no-claim", "the attempt has no identity")
    request = dispatch.request_key(handle.run_id, handle.spec.task_id, handle.attempt_id)
    binding = dispatch.LaunchBinding(
        handle.run_id, handle.spec.task_id, handle.attempt_id, orca_run, orca_task, request
    )
    binding.checked_key()
    intent_digest = journal.digest(
        {
            "run": handle.run_id,
            "task": handle.spec.task_id,
            "attempt": handle.attempt_id,
            "dispatch_attempt": attempt,
            "template": template,
            "batch_ref": batch_ref,
            "orca_run": orca_run,
            "orca_task": orca_task,
            "retry_request": request,
        },
        _INTENT_DOMAIN,
    )
    return binding, intent_digest


def claim(*, workspace: Path, run_id: str, task_id: str, orca_run: str, orca_task: str) -> dict[str, Any]:
    if not dispatch.valid_id(orca_run, "run") or not dispatch.valid_id(orca_task, "task"):
        raise journal.refuse("invalid-identity", "the existing Orca Run and Task identities are required")
    workspace = workspace.expanduser().resolve()
    run_dir = journal.context(workspace, run_id, task_id)
    hub = admission.read_hub_authority(workspace)

    def record(
        current: enrollment.VerifiedTask,
        attempt: int,
        batch_ref: str,
        evidence: admission.Admission,
        out: dict[str, Any],
    ) -> int | None:
        out["target"] = current.target
        return claims.record_claim(
            HOST,
            workspace=workspace,
            run_dir=run_dir,
            task_id=task_id,
            attempt=attempt,
            build_intent=lambda handle: _intent(
                handle, attempt, batch_ref, current.template_digest, orca_run, orca_task
            ),
            claim_payload=lambda intent_digest: {
                "parent_thread": orca_run,
                "orca_task_id": orca_task,
                "template_digest": current.template_digest,
                "intent_digest": intent_digest,
                "admission": evidence.code,
                "roster_digest": evidence.roster_digest,
                "policy_digest": evidence.policy_digest,
            },
            append=journal.append_fact,
            record_requested=journal.record_dispatch_requested,
            out=out,
        )

    ops = claims.ClaimOps(
        capacity_lock=capacity.capacity_lock,
        governed_occupancy=lambda ws: capacity.governed_occupancy(HOST, ws),
        assess=lambda roster, seat, live: capacity.assess(HOST, roster, seat, live),
        owned_run=owned_run,
        load_roster=admission.load_roster,
        run_meta=lambda ws, directory: journal.run_meta(HOST, ws, directory),
        read_events=journal.read_events,
        verify=enrollment.verify_for_launch,
        evaluate=lambda roster, seat, target, authority: admission.evaluate(HOST, roster, seat, target, hub=authority),
        batch_ref=journal.batch_ref,
        coordinator=journal.coordinator,
        record_claim=record,
    )
    grant = claims.run_claim(HOST, ops, workspace=workspace, run_dir=run_dir, task_id=task_id, hub=hub)
    binding, target = grant.recorded["intent"], grant.recorded["target"]
    return {
        "schema": "brigade.orca_claim.v1",
        "run_id": run_id,
        "task_id": task_id,
        "seat": grant.recorded["handle"].spec.seat,
        "attempt_id": binding.brigade_attempt_id,
        "dispatch_attempt": grant.attempt,
        "state": "claimed",
        "orca_version": target.orca_version,
        "retry_request": binding.retry_key,
        "argv": dispatch.launch_argv(binding, target),
    }


@dataclass(frozen=True)
class ClaimedTask:
    workspace: Path
    run_dir: Path
    events: list[run_journal.RunEvent]
    verified: enrollment.VerifiedTask
    claim: journal.ClaimBinding
    binding: dispatch.LaunchBinding

    @property
    def task_id(self) -> str:
        return self.claim.task_id

    def report(self, schema: str) -> dict[str, Any]:
        return {
            "schema": schema,
            "run_id": self.run_dir.name,
            "task_id": self.task_id,
            "attempt_id": self.claim.attempt_id,
            "orca_version": self.verified.target.orca_version,
        }

    def append(self, event_type: str, payload: dict[str, Any], *identity: object) -> None:
        journal.append_fact(
            self.workspace,
            self.run_dir,
            event_type,
            {"task_id": self.task_id, "attempt_id": self.claim.attempt_id, **payload},
            key=journal.idempotency_key(HOST, event_type, self.task_id, self.claim.attempt_id, *identity),
        )

    def correlated_worker(self) -> dispatch.WorkerReference | None:
        fact = journal.latest(self.events, CORRELATION_EVENT, self.task_id)
        if fact is None:
            return None
        dispatch_id = fact.get("provider_task_id")
        if not dispatch.valid_id(dispatch_id, "ctx"):
            raise journal.refuse("journal-refused", "the correlation record is not usable")
        worker = dispatch.WorkerReference(self.binding, dispatch_id, self.verified.target.orca_version)
        if fact.get("attempt_id") != self.claim.attempt_id or fact.get("handle_digest") != worker.handle_digest:
            raise journal.refuse("journal-refused", "the correlation record is not usable")
        return worker

    def require_unheld(self) -> None:
        held = journal.held(self.events, self.task_id)
        if held is not None:
            raise journal.refuse("task-held", f"the task is held ({held})")


@contextmanager
def claimed_task(workspace: Path, run_id: str, task_id: str, *, resolving: bool = False) -> Iterator[ClaimedTask]:
    workspace = workspace.expanduser().resolve()
    run_dir = journal.context(workspace, run_id, task_id)
    with owned_run(workspace, run_dir):
        journal.run_meta(HOST, workspace, run_dir)
        events = journal.read_events(run_dir)
        result = journal.find_task(journal.read_tasks(run_dir), task_id)
        verified = enrollment.verify_task(run_dir, result)
        claim = claims.require_durable_claim(
            events, result, require_launch=not resolving, require_requested=not resolving
        )
        fact = journal.latest(events, journal.CLAIM_EVENT, task_id)
        assert fact is not None
        orca_task = fact.get("orca_task_id")
        if not dispatch.valid_id(orca_task, "task"):
            raise journal.refuse("journal-refused", "the Orca Task claim is not usable")
        binding, digest = _intent(
            result.handle,
            claim.dispatch_attempt,
            journal.batch_ref(events, run_id, task_id),
            verified.template_digest,
            claim.parent_thread,
            orca_task,
        )
        if digest != claim.intent_digest or verified.template_digest != claim.template_digest:
            raise journal.refuse("intent-mismatch", "the retained intent no longer matches the durable claim")
        yield ClaimedTask(workspace, run_dir, events, verified, claim, binding)


def _correlate(task: ClaimedTask, worker: dispatch.WorkerReference, *, mode: str, held: str | None) -> dict[str, Any]:
    if journal.start_refusal(task.events, task.task_id) is not None:
        raise journal.refuse("task-terminal", "the task already has a definite Orca start refusal")
    prior = task.correlated_worker()
    if prior is not None and prior != worker:
        if mode == "parent-reported" and task.verified.result.result_id is None:
            journal.record_held(
                HOST,
                task.workspace,
                task.run_dir,
                task.task_id,
                task.claim.attempt_id,
                journal.ACCEPT_CONFLICT,
                worker.handle_digest,
            )
        raise journal.refuse("accept-conflict", "a different native handle already correlates this task")
    if held == journal.ACCEPT_CONFLICT and prior is None:
        raise journal.refuse("journal-refused", "the held task has no retained correlation")
    if mode == "operator-asserted":
        journal.close_lost_request(task.workspace, task.run_dir, task.events, task.verified.result)
    if held == journal.ACCEPT_CONFLICT:
        task.append(
            journal.HOLD_RESOLVED_EVENT,
            {"reason": held, "evidence_digest": worker.handle_digest},
            worker.handle_digest,
        )
        mode = "hold-resolved"
    elif prior is None:
        task.append(
            CORRELATION_EVENT,
            {"mode": mode, "handle_digest": worker.handle_digest, "provider_task_id": worker.dispatch_id},
            mode,
            worker.handle_digest,
        )
    return {
        **task.report(CORRELATION_SCHEMA),
        "mode": mode,
        "orca_run_id": worker.run_id,
        "orca_task_id": worker.task_id,
        "dispatch_id": worker.dispatch_id,
        "observe_argv": observations.observe_argv(worker),
        "replayed": prior is not None and held is None,
    }


def _record_start_refusal(task: ClaimedTask, reason: str, *, resolving: bool = False) -> dict[str, Any]:
    result = task.verified.result
    resource = journal.latest(task.events, RESOURCE_EVENT, task.task_id)
    if reason != "start-failed" and resource is not None and resource.get("reason") == START_UNCONFIRMED:
        raise journal.refuse("start-unknown", "the Task refusal cannot settle an earlier ambiguous worker start")
    prior = journal.start_refusal(task.events, task.task_id)
    if (
        journal.held(task.events, task.task_id) == journal.ACCEPT_CONFLICT
        or task.correlated_worker() is not None
        or (prior is not None and prior != reason)
    ):
        raise journal.refuse("accept-conflict", "the refusal contradicts the retained Orca start outcome")
    if prior is None and result.result_id is not None:
        raise journal.refuse("task-terminal", "the task already has a terminal result")
    if resolving:
        journal.close_lost_request(task.workspace, task.run_dir, task.events, result)
    if prior is None:
        # Seal the no-worker verdict first. If the next write is interrupted,
        # reporting the same receipt repairs the result without admitting a new launch.
        task.append(RESOURCE_EVENT, {"reason": reason}, reason)
    evidence = journal.digest(
        {"intent": task.claim.intent_digest, "reason": reason}, "brigade.native-orca.start-refusal.v1"
    )
    try:
        updated = run_tasks.observe_task(
            task.run_dir,
            workspace=task.workspace,
            handle=result.handle,
            observation=run_tasks.TaskObservation(
                status="failed", artifact_kind="receipt", artifact_digest=evidence, containment="quiescent"
            ),
            expected_version=result.version,
        )
    except run_tasks.TaskJournalError:
        raise journal.refuse("observation-conflict", "the journal refused the Orca start outcome") from None
    return {
        **task.report(CORRELATION_SCHEMA),
        "mode": "start-refusal",
        "reason": reason,
        "status": updated.status,
        "result_id": updated.result_id,
        "replayed": result.result_id is not None,
    }


def _parse_start(
    task: ClaimedTask, payload: object, *, exit_code: int, show: object, recover_unknown: bool = False
) -> dispatch.WorkerReference:
    target = task.verified.target
    try:
        return dispatch.parse_start(task.binding, target, payload, exit_code=exit_code)
    except NativeChildError as exc:
        if exc.code != "start-unknown" or show is None:
            raise
        failed = dispatch.failed_start_with_residuals(task.binding, target, payload, exit_code=exit_code)
        if failed is None and not recover_unknown:
            raise
        if failed is not None:
            worker, residuals = failed
            if not observations.failed_start_released(worker, show, residuals):
                raise journal.refuse(
                    "start-unknown", "the failed Orca worker has no confirmed release and exit"
                ) from None
            raise dispatch.DefiniteStartRefusal("start-failed", "Orca released the failed worker start") from None
    worker = dispatch.unconfirmed_start(task.binding, target, payload)
    if not observations.settled_start(worker, show):
        raise journal.refuse("start-unknown", "the exact Orca worker has no confirmed start or settlement")
    return worker


def accepted(
    *, workspace: Path, run_id: str, task_id: str, payload: object, exit_code: int, show: object = None
) -> dict[str, Any]:
    with claimed_task(workspace, run_id, task_id) as task:
        task.require_unheld()
        try:
            worker = _parse_start(task, payload, exit_code=exit_code, show=show)
        except dispatch.DefiniteStartRefusal as exc:
            return _record_start_refusal(task, exc.code)
        except NativeChildError as exc:
            if show is not None and dispatch.result_fields(payload).get("state") == "outcome_unknown":
                raise
            resource = journal.latest(task.events, RESOURCE_EVENT, task_id)
            if exc.code == "start-unknown" and resource is None and task.correlated_worker() is None:
                task.append(RESOURCE_EVENT, {"reason": START_UNCONFIRMED}, START_UNCONFIRMED)
            raise
        return _correlate(task, worker, mode="parent-reported", held=None)


def hold_resolve(
    *, workspace: Path, run_id: str, task_id: str, payload: object, abandon: bool, exit_code: int, show: object
) -> dict[str, Any]:
    with claimed_task(workspace, run_id, task_id, resolving=True) as task:
        result = task.verified.result
        held = journal.held(task.events, task_id)
        if abandon:
            if result.result_id is not None or journal.start_refusal(task.events, task.task_id) is not None:
                raise journal.refuse("task-terminal", "the recorded outcome must remain available for acknowledgement")
            journal.close_lost_request(task.workspace, task.run_dir, task.events, result)
            if held != journal.ABANDONED:
                journal.record_held(
                    HOST,
                    task.workspace,
                    task.run_dir,
                    task_id,
                    task.claim.attempt_id,
                    journal.ABANDONED,
                    task.claim.intent_digest,
                )
            return {**task.report(CORRELATION_SCHEMA), "mode": journal.ABANDONED, "replayed": held == journal.ABANDONED}
        if held == journal.ABANDONED:
            task.require_unheld()
        if result.launch_claim is None:
            raise journal.refuse("no-claim", "the task has no durable launch claim")
        try:
            worker = _parse_start(task, payload, exit_code=exit_code, show=show, recover_unknown=True)
        except dispatch.DefiniteStartRefusal as exc:
            return _record_start_refusal(task, exc.code, resolving=True)
        return _correlate(task, worker, mode="operator-asserted", held=held)
