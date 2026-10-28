"""Programmatic durable assignment identity, observations and consumption.

This API cannot launch work. Callers retain immutable input bytes separately;
only their digest enters the lifecycle journal. There is no automatic retry.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from brigade import run_events, run_journal
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


@dataclass(frozen=True)
class TaskLaunchDecision:
    outcome: Literal["granted", "already-claimed", "terminal"]
    task: TaskResult


def _attempt_id(handle: TaskHandle) -> str:
    return _digest({"run_id": handle.run_id, "task_id": handle.spec.task_id, "spec_digest": handle.spec.spec_digest})


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


def _fold(run_id: str, events: list[run_journal.RunEvent]) -> dict[str, TaskResult]:
    results: dict[str, TaskResult] = {}
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
        prior_types[task_id] = event.event_type
        results[task_id] = replace(results[task_id], version=event.event_digest)
    return results


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


def prepare_attempt(run_dir: Path, *, workspace: Path, task_id: str) -> TaskHandle:
    """Create the first attempt once; never infer a retry from uncertain state."""
    with journal.transaction(run_dir, workspace=workspace, writing=True) as (run_dir, events):
        current = _find(_fold(run_dir.name, events), task_id)
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


def claim_task_launch(
    run_dir: Path,
    *,
    workspace: Path,
    handle: TaskHandle,
    expected_version: str,
    seat: str,
    dispatch_attempt: int,
) -> TaskLaunchDecision:
    """Consume launch permission once without reserving budget or launching.

    A future execution caller must first reserve budget for this exact candidate
    under the coordinator lock. A bare claim proves neither budget nor admission.
    A lost reply never reconstructs a grant. Durability ambiguity retains history
    and raises TaskJournalError; callers must retain recovery ownership.
    """
    with journal.transaction(run_dir, workspace=workspace, writing=True) as (run_dir, events):
        results = _fold(run_dir.name, events)
        current = _find(results, handle.spec.task_id)
        if handle != current.handle or handle.attempt_id is None:
            raise TaskJournalError("task launch handle mismatch")
        binding = TaskLaunchBinding(seat, dispatch_attempt)
        if seat != handle.spec.seat or (current.launch_claim is not None and current.launch_claim != binding):
            raise TaskJournalError("task launch binding identity conflict")
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
                    if e.event_type in journal.TASK_FIELDS and e.payload["task_id"] == handle.spec.task_id
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
