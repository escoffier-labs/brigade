"""Programmatic durable assignment identity, observations and consumption.

This API cannot launch work. Callers retain immutable input bytes separately;
only their digest enters the lifecycle journal. There is no automatic retry.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from brigade import run_events, run_journal, run_lifecycle
from brigade import run_task_journal as journal
from brigade.run_task_journal import TaskJournalError


def _digest(value: Any) -> str:
    return hashlib.sha256(run_events.canonical_bytes(value)).hexdigest()


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    seat: str
    payload_digest: str

    def __post_init__(self) -> None:
        if not journal.task_id(self.task_id) or not journal.token(self.seat) or not journal.digest(self.payload_digest):
            raise TaskJournalError("invalid immutable task specification")

    @property
    def spec_digest(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True)
class TaskHandle:
    run_id: str
    request_id: str
    spec: TaskSpec
    attempt_id: str | None = None


@dataclass(frozen=True)
class TaskObservation:
    status: str
    provider_task_id: str | None = None
    original_run_id: str | None = None
    provider_result_id: str | None = None
    artifact_kind: str | None = None
    artifact_digest: str | None = None
    containment: str = "unknown"
    wait_timed_out: bool = False

    def __post_init__(self) -> None:
        if (
            self.status not in journal.STATES
            or self.containment not in journal.CONTAINMENT
            or not isinstance(self.wait_timed_out, bool)
        ):
            raise TaskJournalError("invalid task observation")
        for value in (self.provider_task_id, self.original_run_id, self.provider_result_id):
            if value is not None and not journal.provider_reference(value):
                raise TaskJournalError("invalid provider correlation reference")
        if self.artifact_kind is not None and self.artifact_kind not in journal.ARTIFACT_KINDS:
            raise TaskJournalError("invalid artifact classification")
        if self.artifact_digest is not None and not journal.digest(self.artifact_digest):
            raise TaskJournalError("invalid artifact digest")


@dataclass(frozen=True)
class TaskLaunchBinding:
    """Durable consumption of launch permission, never a replayable grant."""

    seat: str
    dispatch_attempt: int

    def __post_init__(self) -> None:
        if not journal.token(self.seat) or type(self.dispatch_attempt) is not int or self.dispatch_attempt <= 0:
            raise TaskJournalError("invalid task launch binding")


@dataclass(frozen=True)
class TaskEnrollment:
    """Retained custody references for one enrolled native delegation input.

    The asset holds the exact delegation input. This record binds it by
    relative name and digest and carries no prompt text or native payload.
    """

    descriptor_version: int
    asset_path: str
    asset_digest: str
    template_digest: str

    def __post_init__(self) -> None:
        if (
            type(self.descriptor_version) is not int
            or self.descriptor_version != journal.NATIVE_ENROLLMENT_DESCRIPTOR_VERSION
            or not journal.native_asset_path(self.asset_path)
            or not journal.digest(self.asset_digest)
            or not journal.digest(self.template_digest)
        ):
            raise TaskJournalError("invalid native enrollment reference")


@dataclass(frozen=True)
class TaskWorkspaceBinding:
    """One validated per-task worktree, retained as digests only.

    The checkout path, branch and device/inode never enter the journal, so this
    record cannot be used to reconstruct or label another workspace.
    """

    base_sha: str
    branch_digest: str
    path_digest: str
    dev_ino_digest: str

    def __post_init__(self) -> None:
        if not journal.base_sha(self.base_sha):
            raise TaskJournalError("invalid task workspace base revision")
        if not all(journal.digest(value) for value in (self.branch_digest, self.path_digest, self.dev_ino_digest)):
            raise TaskJournalError("invalid task workspace binding digest")


@dataclass(frozen=True)
class TaskIntegration:
    """One recorded integration of this task's candidate commit.

    The integration identity covers the run, the pinned base and the ordered
    selection, so the same fact cannot be replayed under a different result.
    """

    integration_id: str
    candidate_commit: str
    integration_commit: str

    def __post_init__(self) -> None:
        if not journal.integration_id(self.integration_id):
            raise TaskJournalError("invalid task integration identity")
        if not journal.base_sha(self.candidate_commit) or not journal.base_sha(self.integration_commit):
            raise TaskJournalError("invalid task integration revision")


@dataclass(frozen=True)
class TaskDisposal:
    """One recorded disposal of this task's worktree.

    The mode names the retained-copy proof that allowed the removal, or
    ``discarded`` when the caller decided to dispose of the checkout without
    one. The branch verdict is a token, never a boolean, because the canonical
    payload contract refuses booleans.
    """

    mode: str
    branch_kept: str

    def __post_init__(self) -> None:
        if self.mode not in journal.DISPOSAL_MODES:
            raise TaskJournalError("invalid task disposal mode")
        if self.branch_kept not in journal.BRANCH_STATES:
            raise TaskJournalError("invalid task disposal branch state")


@dataclass(frozen=True)
class TaskCollectedArtifact:
    """One recovered artifact collection recorded after the terminal observation.

    A worker's receipt observation says only that the worker returned. When
    collection was interrupted, this fact carries the sidecar digest, the
    candidate revision and the delivery a later ``runs tasks collect`` wrote,
    so integration and disposal can read a commit artifact from either source.
    """

    attempt_id: str
    artifact_digest: str
    candidate_commit: str | None
    delivery: str

    def __post_init__(self) -> None:
        if not journal.digest(self.attempt_id) or not journal.digest(self.artifact_digest):
            raise TaskJournalError("invalid task artifact collection reference")
        if self.candidate_commit is not None and not journal.base_sha(self.candidate_commit):
            raise TaskJournalError("invalid task artifact collection revision")
        if not journal.token(self.delivery):
            raise TaskJournalError("invalid task artifact collection delivery")


@dataclass(frozen=True)
class TaskResult:
    handle: TaskHandle
    status: str
    provider_task_id: str | None = None
    original_run_id: str | None = None
    provider_result_id: str | None = None
    result_id: str | None = None
    artifact_kind: str | None = None
    artifact_digest: str | None = None
    containment: str = "unknown"
    acknowledged: bool = False
    version: str | None = None
    launch_claim: TaskLaunchBinding | None = None
    enrollment: TaskEnrollment | None = None
    workspace: TaskWorkspaceBinding | None = None
    integrations: tuple[TaskIntegration, ...] = ()
    disposal: TaskDisposal | None = None
    collected: TaskCollectedArtifact | None = None
    # Cancellation intent is recorded separately from the task's state. It
    # blocks a launch that has not happened yet and never rewrites a result.
    cancel_requested: bool = False
    cancel_reason: str | None = None

    @property
    def cancel_too_late(self) -> bool:
        return self.cancel_requested and self.status in journal.TERMINAL and self.status != "cancelled"

    @property
    def native_enrolled(self) -> bool:
        """True when the fold saw a ``task.native-enrolled`` fact for this task."""
        return self.enrollment is not None

    @property
    def attempted(self) -> bool:
        """True once an attempt exists. A workspace binding is never an attempt."""
        return self.handle.attempt_id is not None


@dataclass(frozen=True)
class TaskBatchResult:
    run_id: str
    request_id: str
    descriptor_version: int
    membership_digest: str
    batch_ref: str
    members: tuple[TaskResult, ...]

    @property
    def specs(self) -> tuple[TaskSpec, ...]:
        return tuple(member.handle.spec for member in self.members)


@dataclass(frozen=True)
class _BatchMembership:
    descriptor_version: int
    membership_digest: str
    specs: tuple[TaskSpec, ...]


@dataclass
class _TaskHistory:
    tasks: dict[str, TaskResult]
    batches: dict[str, _BatchMembership]


@dataclass(frozen=True)
class TaskLaunchDecision:
    outcome: Literal["granted", "already-claimed", "terminal", "cancelled-before-launch"]
    task: TaskResult


def _attempt_id(handle: TaskHandle) -> str:
    return _digest({"run_id": handle.run_id, "task_id": handle.spec.task_id, "spec_digest": handle.spec.spec_digest})


def planned_attempt_id(handle: TaskHandle) -> str:
    """The attempt identity ``prepare_attempt`` will mint, known before any journal write."""
    return _attempt_id(handle)


def _result_id(result: TaskResult) -> str:
    return _digest(
        {
            "run_id": result.handle.run_id,
            "task_id": result.handle.spec.task_id,
            "attempt_id": result.handle.attempt_id,
            "status": result.status,
            "provider_task_id": result.provider_task_id,
            "original_run_id": result.original_run_id,
            "provider_result_id": result.provider_result_id,
            "artifact_kind": result.artifact_kind,
            "artifact_digest": result.artifact_digest,
        }
    )


def _observation_payload(result: TaskResult) -> dict[str, Any]:
    return {
        "task_id": result.handle.spec.task_id,
        "attempt_id": result.handle.attempt_id,
        **{
            field: getattr(result, field)
            for field in (
                "status",
                "provider_task_id",
                "original_run_id",
                "provider_result_id",
                "result_id",
                "artifact_kind",
                "artifact_digest",
                "containment",
            )
        },
    }


def _observe(current: TaskResult, observation: TaskObservation) -> TaskResult:
    if observation.wait_timed_out:
        return current
    refs = {}
    for field in ("provider_task_id", "original_run_id", "provider_result_id"):
        old, new = getattr(current, field), getattr(observation, field)
        if old is not None and new is not None and old != new:
            raise TaskJournalError("immutable provider identity conflict")
        refs[field] = old if new is None else new
    status = observation.status
    if status == "completed" and observation.containment == "active" and current.result_id is None:
        status = "waiting-for-children"
    terminal = status in journal.TERMINAL
    kind, artifact = observation.artifact_kind, observation.artifact_digest
    if terminal:
        if kind is None and artifact is None:
            kind, artifact = current.artifact_kind or "unavailable", current.artifact_digest
        if kind is None or (kind == "unavailable") != (artifact is None):
            raise TaskJournalError("terminal artifact requires classification and digest")
    elif kind is not None or artifact is not None:
        raise TaskJournalError("nonterminal task cannot publish result artifact")
    result = replace(
        current,
        status=status,
        artifact_kind=kind,
        artifact_digest=artifact,
        containment=observation.containment,
        **refs,
    )
    result = replace(result, result_id=_result_id(result) if terminal else None)
    if current.result_id is not None and result.result_id != current.result_id:
        raise TaskJournalError("contradictory terminal task replacement")
    return result


def _dispatch_binding(event: run_journal.RunEvent) -> tuple[str, int] | None:
    p = event.payload
    if (
        event.event_type.startswith("run.dispatch.")
        and isinstance(p.get("seat"), str)
        and bool(p["seat"])
        and type(p.get("attempt")) is int
        and p["attempt"] > 0
    ):
        return p["seat"], p["attempt"]
    return None


def _validate_launch_candidate(
    current: TaskResult,
    binding: TaskLaunchBinding,
    *,
    prior_event_type: str | None,
    used_bindings: set[tuple[str, int]],
) -> None:
    """Apply the same ordered-history eligibility rule online and on replay."""
    if (
        current.handle.attempt_id is None
        or current.launch_claim is not None
        or current.status in journal.TERMINAL
        or prior_event_type != "task.attempt-prepared"
        or binding.seat != current.handle.spec.seat
    ):
        raise TaskJournalError("task launch requires pristine prepared history")
    if (binding.seat, binding.dispatch_attempt) in used_bindings:
        raise TaskJournalError("task launch dispatch binding already used")


def _fold_history(run_id: str, events: list[run_journal.RunEvent]) -> _TaskHistory:
    results: dict[str, TaskResult] = {}
    batches: dict[str, _BatchMembership] = {}
    requests: set[str] = set()
    prior_types: dict[str, str] = {}
    used_bindings: set[tuple[str, int]] = set()
    for event in events:
        dispatch_binding = _dispatch_binding(event)
        if dispatch_binding is not None:
            used_bindings.add(dispatch_binding)
        if event.event_type not in journal.TASK_FIELDS:
            continue
        p = event.payload
        try:
            journal.validate_payload(event.event_type, p)
        except run_events.CanonicalizationError as exc:
            raise TaskJournalError("invalid task payload history") from exc
        if event.event_type == "task.batch-accepted":
            request_id = p["request_id"]
            specs = tuple(TaskSpec(row["task_id"], row["seat"], row["payload_digest"]) for row in p["members"])
            if request_id in batches:
                raise TaskJournalError("duplicate task batch acceptance history")
            children = _batch_children(run_id, request_id, specs)
            _validate_batch_collisions(results, requests, specs, children)
            batches[request_id] = _BatchMembership(p["descriptor_version"], p["membership_digest"], specs)
            for spec, child in zip(specs, children, strict=True):
                requests.add(child)
                prior_types[spec.task_id] = event.event_type
                results[spec.task_id] = TaskResult(
                    TaskHandle(run_id, child, spec), "queued", version=event.event_digest
                )
            continue
        task_id = p["task_id"]
        if event.event_type == "task.accepted":
            spec = TaskSpec(task_id, p["seat"], p["payload_digest"])
            if p["spec_digest"] != spec.spec_digest or task_id in results or p["request_id"] in requests:
                raise TaskJournalError("conflicting task acceptance history")
            requests.add(p["request_id"])
            prior_types[task_id] = event.event_type
            results[task_id] = TaskResult(
                TaskHandle(run_id, p["request_id"], spec), "queued", version=event.event_digest
            )
            continue
        current = _find(results, task_id)
        if event.event_type == "task.attempt-prepared":
            if current.handle.attempt_id is not None or p["attempt_id"] != _attempt_id(current.handle):
                raise TaskJournalError("invalid task attempt history")
            results[task_id] = replace(current, handle=replace(current.handle, attempt_id=p["attempt_id"]))
        elif event.event_type == "task.launch-claimed":
            binding = TaskLaunchBinding(p["seat"], p["dispatch_attempt"])
            if p["attempt_id"] != current.handle.attempt_id:
                raise TaskJournalError("task launch attempt mismatch")
            _validate_launch_candidate(
                current, binding, prior_event_type=prior_types.get(task_id), used_bindings=used_bindings
            )
            used_bindings.add((binding.seat, binding.dispatch_attempt))
            results[task_id] = replace(current, launch_claim=binding, status="unknown")
        elif event.event_type == "task.observed":
            if current.handle.attempt_id is None or p["attempt_id"] != current.handle.attempt_id:
                raise TaskJournalError("task observation attempt mismatch")
            observation = TaskObservation(
                **{k: v for k, v in p.items() if k not in {"task_id", "attempt_id", "result_id"}}
            )
            updated = _observe(current, observation)
            if updated.result_id != p["result_id"]:
                raise TaskJournalError("invalid terminal result identity")
            results[task_id] = updated
        elif event.event_type == "task.acknowledged":
            if current.result_id is None or p["result_id"] != current.result_id:
                raise TaskJournalError("task acknowledgement result mismatch")
            results[task_id] = replace(current, acknowledged=True)
        elif event.event_type == "task.native-enrolled":
            if current.enrollment is not None or current.status != "queued" or current.handle.attempt_id is not None:
                raise TaskJournalError("native enrollment requires pristine queued task history")
            results[task_id] = replace(
                current,
                enrollment=TaskEnrollment(
                    p["descriptor_version"], p["asset_path"], p["asset_digest"], p["template_digest"]
                ),
            )
        elif event.event_type == "task.workspace-bound":
            # A binding never grants launch authority: the task stays queued,
            # attempt-free, and cannot be re-bound differently. An identical
            # duplicate is a no-op so a repaired journal stays readable.
            bound = TaskWorkspaceBinding(p["base_sha"], p["branch_digest"], p["path_digest"], p["dev_ino_digest"])
            if (
                (current.workspace is not None and current.workspace != bound)
                or current.status != "queued"
                or current.handle.attempt_id is not None
                or current.launch_claim is not None
            ):
                raise TaskJournalError("workspace binding requires pristine queued task history")
            results[task_id] = replace(current, workspace=bound)
        elif event.event_type == "task.integrated":
            # Integration consumes a terminal result, never grants or changes
            # execution state. One task may be part of several explicit
            # integrations; an identical fact is a no-op on replay.
            integrated = TaskIntegration(p["integration_id"], p["candidate_commit"], p["integration_commit"])
            if current.status not in journal.TERMINAL or current.result_id is None or current.handle.attempt_id is None:
                raise TaskJournalError("task integration requires a terminal task result")
            if integrated not in current.integrations:
                results[task_id] = replace(current, integrations=(*current.integrations, integrated))
        elif event.event_type == "task.disposed":
            # Disposal records that a worktree is gone and why that was safe. It
            # consumes a terminal result and never changes its status.
            disposed = TaskDisposal(p["mode"], p["branch_kept"])
            if current.status not in journal.TERMINAL or current.result_id is None:
                raise TaskJournalError("task disposal requires a terminal task result")
            if current.disposal is not None and current.disposal != disposed:
                raise TaskJournalError("task disposal conflicts with recorded disposal")
            results[task_id] = replace(current, disposal=disposed)
        elif event.event_type == "task.cancel-requested":
            # A queued request can be drained after the terminal observation.
            # Intent never changes the worker's status or result identity.
            if current.cancel_requested and current.cancel_reason != p["reason"]:
                raise TaskJournalError("task cancel intent conflicts with recorded intent")
            results[task_id] = replace(current, cancel_requested=True, cancel_reason=p["reason"])
        elif event.event_type == "task.artifact-collected":
            # A recovered collection is a late, closed record of an artifact the
            # interrupted collection would have observed. It never rewrites the
            # observation and never changes the terminal status.
            collected = TaskCollectedArtifact(
                p["attempt_id"], p["artifact_digest"], p["candidate_commit"], p["delivery"]
            )
            if current.status not in journal.TERMINAL or current.result_id is None:
                raise TaskJournalError("task artifact collection requires a terminal task result")
            if collected.attempt_id != current.handle.attempt_id:
                raise TaskJournalError("task artifact collection attempt mismatch")
            if current.collected is not None and current.collected != collected:
                raise TaskJournalError("task artifact collection conflicts with recorded collection")
            results[task_id] = replace(current, collected=collected)
        prior_types[task_id] = event.event_type
        results[task_id] = replace(results[task_id], version=event.event_digest)
    return _TaskHistory(results, batches)


def _fold(run_id: str, events: list[run_journal.RunEvent]) -> dict[str, TaskResult]:
    """Compatibility view over the single validated task/batch history fold."""
    return _fold_history(run_id, events).tasks


def _batch_children(run_id: str, request_id: str, specs: tuple[TaskSpec, ...]) -> tuple[str, ...]:
    return tuple(
        journal.batch_child_request_id(run_id=run_id, request_id=request_id, task_id=spec.task_id) for spec in specs
    )


def _validate_batch_collisions(
    results: dict[str, TaskResult], requests: set[str], specs: tuple[TaskSpec, ...], children: tuple[str, ...]
) -> None:
    if (
        len({spec.task_id for spec in specs}) != len(specs)
        or any(spec.task_id in results for spec in specs)
        or len(set(children)) != len(children)
        or requests.intersection(children)
    ):
        raise TaskJournalError("task batch acceptance identity conflict")


def _batch_result(run_id: str, request_id: str, history: _TaskHistory) -> TaskBatchResult:
    batch = history.batches.get(request_id)
    if batch is None:
        raise TaskJournalError("task batch identity not accepted")
    return TaskBatchResult(
        run_id,
        request_id,
        batch.descriptor_version,
        batch.membership_digest,
        journal.batch_reference(run_id=run_id, request_id=request_id, membership_digest=batch.membership_digest),
        tuple(history.tasks[spec.task_id] for spec in batch.specs),
    )


def _find(results: dict[str, TaskResult], task_id: str) -> TaskResult:
    if not journal.task_id(task_id) or task_id not in results:
        raise TaskJournalError("task identity not accepted")
    return results[task_id]


def accept_task(run_dir: Path, *, workspace: Path, request_id: str, spec: TaskSpec) -> TaskHandle:
    """Accept exactly one immutable task, replaying only the same request/spec."""
    if not journal.token(request_id) or not isinstance(spec, TaskSpec):
        raise TaskJournalError("invalid task acceptance")
    with journal.transaction(run_dir, workspace=workspace, writing=True) as (run_dir, events):
        results = _fold(run_dir.name, events)
        for result in results.values():
            if result.handle.request_id == request_id or result.handle.spec.task_id == spec.task_id:
                if result.handle.request_id == request_id and result.handle.spec == spec:
                    return result.handle
                raise TaskJournalError("task acceptance identity conflict")
        journal.append(
            run_dir,
            workspace=workspace,
            event_type="task.accepted",
            payload={**asdict(spec), "spec_digest": spec.spec_digest, "request_id": request_id},
        )
        return TaskHandle(run_dir.name, request_id, spec)


def accept_task_batch(
    run_dir: Path,
    *,
    workspace: Path,
    request_id: str,
    specs: tuple[TaskSpec, ...],
    descriptor_version: int = 1,
) -> TaskBatchResult:
    """Commit bounded ordered descriptors, without granting execution authority."""
    if (
        not journal.token(request_id)
        or type(descriptor_version) is not int
        or descriptor_version != journal.BATCH_DESCRIPTOR_VERSION
        or type(specs) is not tuple
        or not 1 <= len(specs) <= journal.MAX_BATCH_MEMBERS
        or any(type(spec) is not TaskSpec or set(vars(spec)) != {"task_id", "seat", "payload_digest"} for spec in specs)
    ):
        raise TaskJournalError("invalid task batch acceptance")
    # Revalidate and snapshot every scalar before locking or publishing. Never
    # project richer execution requests into descriptor-only acceptance.
    specs = tuple(TaskSpec(spec.task_id, spec.seat, spec.payload_digest) for spec in specs)
    rows = [asdict(spec) for spec in specs]
    payload = {
        "descriptor_version": descriptor_version,
        "request_id": request_id,
        "members": rows,
        "membership_digest": journal.batch_membership_digest(descriptor_version=descriptor_version, members=rows),
    }
    with journal.transaction(run_dir, workspace=workspace, writing=True) as (run_dir, events):
        history = _fold_history(run_dir.name, events)
        prior = history.batches.get(request_id)
        if prior is not None:
            if prior.descriptor_version != descriptor_version or prior.specs != specs:
                raise TaskJournalError("task batch acceptance identity conflict")
            return _batch_result(run_dir.name, request_id, history)
        children = _batch_children(run_dir.name, request_id, specs)
        _validate_batch_collisions(
            history.tasks, {result.handle.request_id for result in history.tasks.values()}, specs, children
        )
        event = journal.append(run_dir, workspace=workspace, event_type="task.batch-accepted", payload=payload)
        # Fold the committed fact through the same validator as every reader.
        return _batch_result(run_dir.name, request_id, _fold_history(run_dir.name, [*events, event]))


def read_task_batch(run_dir: Path, *, request_id: str) -> TaskBatchResult:
    """Enumerate one retained batch's current members after a fresh barrier."""
    if not journal.token(request_id):
        raise TaskJournalError("invalid task batch request identity")
    with journal.transaction(run_dir) as (run_dir, events):
        return _batch_result(run_dir.name, request_id, _fold_history(run_dir.name, events))


def prepare_attempt(run_dir: Path, *, workspace: Path, task_id: str, native_governed: bool = False) -> TaskHandle:
    """Create the first attempt once; never infer a retry from uncertain state.

    A native-enrolled task is refused unless the caller is the governed native
    path. Enrollment alone grants no generic launch authority.
    """
    with journal.transaction(run_dir, workspace=workspace, writing=True) as (run_dir, events):
        current = _find(_fold(run_dir.name, events), task_id)
        if current.native_enrolled and not native_governed:
            raise TaskJournalError("native-enrolled task requires the governed launch path")
        if current.handle.attempt_id is not None:
            return current.handle
        handle = replace(current.handle, attempt_id=_attempt_id(current.handle))
        journal.append(
            run_dir,
            workspace=workspace,
            event_type="task.attempt-prepared",
            payload={"task_id": task_id, "attempt_id": handle.attempt_id},
        )
        return handle


def record_native_enrollment(
    run_dir: Path,
    *,
    workspace: Path,
    task_id: str,
    enrollment: TaskEnrollment,
) -> TaskResult:
    """Bind retained delegation input references once, before any attempt.

    Replay of the identical record is idempotent. A different record for the
    same task refuses, and enrollment never rewrites an existing fact.
    """
    if type(enrollment) is not TaskEnrollment:
        raise TaskJournalError("invalid native enrollment reference")
    with journal.transaction(run_dir, workspace=workspace, writing=True) as (run_dir, events):
        current = _find(_fold(run_dir.name, events), task_id)
        if current.enrollment is not None:
            if current.enrollment == enrollment:
                return current
            raise TaskJournalError("native enrollment identity conflict")
        if current.status != "queued" or current.handle.attempt_id is not None:
            raise TaskJournalError("native enrollment requires pristine queued task history")
        event = journal.append(
            run_dir,
            workspace=workspace,
            event_type="task.native-enrolled",
            payload={"task_id": task_id, **asdict(enrollment)},
        )
        return replace(current, enrollment=enrollment, version=event.event_digest)


def record_workspace_binding(
    run_dir: Path,
    *,
    workspace: Path,
    task_id: str,
    binding: TaskWorkspaceBinding,
) -> TaskResult:
    """Bind one per-task worktree before any attempt exists.

    Requires a queued, attempt-free task. Replaying the identical binding is a
    no-op that writes nothing; a different binding for the same task refuses.
    A binding is custody of a checkout, never launch permission, so the task
    stays queued and the relaunch fence still treats it as never attempted.
    """
    if type(binding) is not TaskWorkspaceBinding:
        raise TaskJournalError("invalid task workspace binding")
    with journal.transaction(run_dir, workspace=workspace, writing=True) as (run_dir, events):
        current = _find(_fold(run_dir.name, events), task_id)
        if current.workspace is not None:
            if current.workspace == binding:
                return current
            raise TaskJournalError("task workspace binding conflict")
        if current.status != "queued" or current.handle.attempt_id is not None or current.launch_claim is not None:
            raise TaskJournalError("workspace binding requires pristine queued task history")
        event = journal.append(
            run_dir,
            workspace=workspace,
            event_type="task.workspace-bound",
            payload={"task_id": task_id, **asdict(binding)},
        )
        return replace(current, workspace=binding, version=event.event_digest)


def claim_task_launch(
    run_dir: Path,
    *,
    workspace: Path,
    handle: TaskHandle,
    expected_version: str,
    seat: str,
    dispatch_attempt: int,
    native_governed: bool = False,
) -> TaskLaunchDecision:
    """Consume launch permission once without reserving budget or launching.

    A future execution caller must first reserve budget for this exact candidate
    under the coordinator lock. A bare claim proves neither budget nor admission.
    A lost reply never reconstructs a grant. Durability ambiguity retains history
    and raises TaskJournalError; callers must retain recovery ownership.

    A native-enrolled task is refused unless the caller is the governed native
    path. Enrollment alone grants no generic launch authority.
    """
    with journal.transaction(run_dir, workspace=workspace, writing=True) as (run_dir, events):
        results = _fold(run_dir.name, events)
        current = _find(results, handle.spec.task_id)
        if current.native_enrolled and not native_governed:
            raise TaskJournalError("native-enrolled task requires the governed launch path")
        if handle != current.handle or handle.attempt_id is None:
            raise TaskJournalError("task launch handle mismatch")
        binding = TaskLaunchBinding(seat, dispatch_attempt)
        if seat != handle.spec.seat or (current.launch_claim is not None and current.launch_claim != binding):
            raise TaskJournalError("task launch binding identity conflict")
        if current.launch_claim is None and current.cancel_requested:
            # A recorded cancel intent owns the decision, even when it landed
            # after ``task.attempt-prepared`` and the caller's captured version
            # is now stale. It is never reported as a generic history error, and
            # nothing is written: the batch records the terminal cancellation.
            return TaskLaunchDecision("cancelled-before-launch", current)
        if not journal.digest(expected_version) or expected_version != current.version:
            raise TaskJournalError("stale task launch: expected version does not match")
        used = {
            (r.launch_claim.seat, r.launch_claim.dispatch_attempt)
            for r in results.values()
            if r.launch_claim is not None
        }
        used.update(b for e in events if (b := _dispatch_binding(e)) is not None)
        if current.launch_claim is None and (seat, dispatch_attempt) in used:
            raise TaskJournalError("task launch dispatch binding already used")
        if current.status in journal.TERMINAL:
            decision = TaskLaunchDecision("terminal", current)
        elif current.launch_claim is not None:
            decision = TaskLaunchDecision("already-claimed", current)
        else:
            prior_type = next(
                (
                    e.event_type
                    for e in reversed(events)
                    if e.event_type in journal.TASK_FIELDS and e.payload.get("task_id") == handle.spec.task_id
                ),
                None,
            )
            _validate_launch_candidate(current, binding, prior_event_type=prior_type, used_bindings=used)
            event = journal.append(
                run_dir,
                workspace=workspace,
                event_type="task.launch-claimed",
                payload={
                    "task_id": handle.spec.task_id,
                    "attempt_id": handle.attempt_id,
                    "seat": seat,
                    "dispatch_attempt": dispatch_attempt,
                },
            )
            decision = TaskLaunchDecision(
                "granted", replace(current, launch_claim=binding, status="unknown", version=event.event_digest)
            )
    # A grant is returned only after all mutation/barrier contexts have exited.
    return decision


def observe_task(
    run_dir: Path,
    *,
    workspace: Path,
    handle: TaskHandle,
    observation: TaskObservation,
    expected_version: str | None = None,
) -> TaskResult:
    """Record an observation, optionally comparing the last durable task version.

    Retryable or out-of-order callers must retain and pass the version against
    which they collected the observation. Omission trusts caller ordering.
    """
    with journal.transaction(run_dir, workspace=workspace, writing=True) as (run_dir, events):
        current = _find(_fold(run_dir.name, events), handle.spec.task_id)
        if handle != current.handle or handle.attempt_id is None:
            raise TaskJournalError("task observation handle mismatch")
        if expected_version is not None and (
            not journal.digest(expected_version) or expected_version != current.version
        ):
            raise TaskJournalError("stale task observation: expected version does not match")
        if observation.containment == "active" and (
            observation.artifact_kind is not None or observation.artifact_digest is not None
        ):
            raise TaskJournalError("active containment cannot accept an artifact; retain and resubmit it")
        result = _observe(current, observation)
        if result == current:
            return current
        payload = _observation_payload(result)
        event = journal.append(run_dir, workspace=workspace, event_type="task.observed", payload=payload)
        return replace(result, version=event.event_digest)


def read_tasks(run_dir: Path) -> tuple[TaskResult, ...]:
    """Read durable results, including consumed results, with a fresh barrier."""
    with journal.transaction(run_dir) as (run_dir, events):
        return tuple(_fold(run_dir.name, events).values())


def batch_accepted(run_dir: Path) -> bool:
    """True when any journal line records an explicit batch acceptance.

    A raw scan, not a verified fold: resume uses it as a fence that does not
    depend on the request marker or sentinel, and leaves journal trust checks
    to its own later diagnostics. Unparseable lines are skipped.
    """
    try:
        raw = run_journal.bound_read_bytes(run_lifecycle._journal_path(run_dir))
    except (OSError, run_journal.RunJournalError):
        return False
    for line in raw.splitlines():
        if b"task.batch-accepted" not in line:
            continue
        try:
            env = json.loads(line)
        except ValueError:
            continue
        if isinstance(env, dict) and env.get("event_type") == "task.batch-accepted":
            return True
    return False


def acknowledge_result(run_dir: Path, *, workspace: Path, task_id: str, result_id: str) -> TaskResult:
    """Idempotently acknowledge consumption without deleting the result."""
    with journal.transaction(run_dir, workspace=workspace, writing=True) as (run_dir, events):
        current = _find(_fold(run_dir.name, events), task_id)
        if current.result_id is None or result_id != current.result_id:
            raise TaskJournalError("task acknowledgement result mismatch")
        if not current.acknowledged:
            event = journal.append(
                run_dir,
                workspace=workspace,
                event_type="task.acknowledged",
                payload={"task_id": task_id, "result_id": result_id},
            )
            current = replace(current, version=event.event_digest)
        return replace(current, acknowledged=True)
