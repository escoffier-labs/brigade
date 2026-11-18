"""Governed native T3 launch claims, reported correlation and hold resolution.

A T3 Code parent session drives its own ``delegate_task`` tool. These verbs only
authorize, correlate and hold. Brigade never calls T3, never proves native
acceptance and never authorizes resubmission.

A claim reserves one dispatch unit, prepares the attempt, binds the finalized
intent by digest, consumes the launch permission and records the durable
dispatch request. Only a granted claim prints the exact delegate arguments, and
only after every mutation committed. A claim that committed without its durable
request is a run-wide barrier: every new claim refuses until an operator
resolves it, and a lost reply always holds the task.

Task text lives only in the verified owner-only asset. It never enters a
refusal, a diagnostic, a journal payload, a command-line argument or an
exception message. ``accepted`` records caller-reported correlation. It is
never authenticated acceptance.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import fleet_t3_transport, run_budget, run_events, run_journal, run_lifecycle, run_tasks, runguard
from . import native_t3_observations as observations
from . import run_task_journal as journal
from . import native_t3_enrollment as enrollment
from .fleet_t3_transport import SourceRevisionError
from .native_t3_contract import NativeIntent, NativeIntentTemplate, NativeTarget

SCHEMA = "brigade.t3_claim.v1"
CORRELATION_SCHEMA = "brigade.t3_correlation.v1"
CLAIM_EVENT = "task.native-claimed"
CORRELATION_EVENT = "task.native-correlated"
HELD_EVENT = "task.native-held"
PARENT_REPORTED = "parent-reported"
OPERATOR_ASSERTED = "operator-asserted"
ABANDONED = "abandoned-unknown"
ACCEPT_CONFLICT = "accept-conflict"
HELD_RUN_STATUS = "held"
_MAX_DIAGNOSTIC = 240
_MAX_NATIVE_BYTES = 1_048_576
_INTENT_DOMAIN = "brigade.native-t3.intent.v1"
_KEY_DOMAIN = "brigade.native-t3.idempotency.v1"
_HANDLE_DOMAIN = "brigade.native-t3.handle.v1"
_EVIDENCE_DOMAIN = "brigade.native-t3.evidence.v1"
_STATUS_MAP = {
    "queued": "queued",
    "running": "running",
    "waiting": "waiting-for-input",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "interrupted": "interrupted",
}
_TERMINAL_NATIVE = frozenset({"completed", "failed", "cancelled", "interrupted"})
_WORK_STATES = frozenset({"working", "waiting_for_children", "result_available"})
_APPROVAL_KINDS = frozenset({"command", "file-read", "file-change", "mcp-elicitation", "permission"})
_RESULT_REQUIRED = frozenset(
    """taskId childThreadId childRunId childNodeId status workState hasPendingChildRuns latestTerminalRunId
    latestTerminalStatus latestTerminalSummary latestTerminalResultContextTransferId providerInstanceId model
    summary resultContextTransferId hasPendingQuestion pendingApproval waitTimedOut""".split()
)


class DispatchError(RuntimeError):
    """A bounded governed-verb refusal published before any output."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _bound(message: str) -> str:
    text = " ".join(str(message).split())
    return text if len(text) <= _MAX_DIAGNOSTIC else text[: _MAX_DIAGNOSTIC - 1] + "."


def _refuse(code: str, message: str) -> DispatchError:
    return DispatchError(code, _bound(message))


def _digest(material: Mapping[str, Any], domain: str) -> str:
    encoded = json.dumps(
        {"domain": domain, **material}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ClaimBinding:
    """The durable claim intent for one task attempt, without task text."""

    task_id: str
    attempt_id: str
    dispatch_attempt: int
    parent_thread: str
    template_digest: str
    intent_digest: str

    def __post_init__(self) -> None:
        if (
            not journal.task_id(self.task_id)
            or not journal.digest(self.attempt_id)
            or type(self.dispatch_attempt) is not int
            or self.dispatch_attempt < 1
            or not journal.provider_reference(self.parent_thread)
            or not journal.digest(self.template_digest)
            or not journal.digest(self.intent_digest)
        ):
            raise ValueError("invalid native claim binding")


@dataclass(frozen=True)
class NativeResult:
    """The validated ``delegate_task`` result projection, without raw payload."""

    task_id: str
    child_thread_id: str
    child_run_id: str
    child_node_id: str
    status: str
    work_state: str
    provider_instance: str
    model: str
    options: tuple[tuple[str, str | bool], ...]
    has_pending_child_runs: bool
    has_pending_question: bool
    pending_approval: Mapping[str, str] | None
    wait_timed_out: bool
    latest_terminal_run_id: str | None
    latest_terminal_status: str | None
    latest_terminal_result_context_transfer_id: str | None
    result_context_transfer_id: str | None

    @property
    def terminal_status(self) -> bool:
        return self.status in _TERMINAL_NATIVE


@dataclass(frozen=True)
class _Verified:
    result: run_tasks.TaskResult
    retained: enrollment.DelegationInput
    template: NativeIntentTemplate


def _run_dir(workspace: Path, run_id: str) -> Path:
    if not journal.token(run_id):
        raise _refuse("invalid-run", "the run identity is not valid")
    try:
        runs = runguard.lock_path(workspace).parent / "runs"
    except (OSError, ValueError):
        raise _refuse("run-missing", "no run matches that identity") from None
    run_dir = runs / run_id
    if not run_dir.is_dir():
        raise _refuse("run-missing", "no run matches that identity")
    return run_dir


def _run_meta(workspace: Path, run_dir: Path) -> dict[str, Any]:
    try:
        meta = json.loads(run_journal.bound_read_bytes(run_dir / "run.json"))
    except FileNotFoundError:
        raise _refuse("run-missing", "no run matches that identity") from None
    except (OSError, ValueError, UnicodeDecodeError):
        raise _refuse("run-state", "the run receipt is not readable") from None
    if not isinstance(meta, dict) or meta.get("kind") != enrollment.RUN_KIND:
        raise _refuse("not-native-run", "the run is not a governed native T3 run")
    if meta.get("lock_workspace") != str(workspace):
        raise _refuse("run-state", "the run belongs to a different workspace")
    status = meta.get("status")
    if status == HELD_RUN_STATUS:
        raise _refuse("run-held", "the run is explicitly held and accepts no native transition")
    if status != enrollment.AWAITING_NATIVE_PARENT:
        raise _refuse("run-state", "the run state does not permit this native transition")
    return meta


def _read(run_dir: Path) -> list[run_journal.RunEvent]:
    try:
        with journal.transaction(run_dir) as (_dir, events):
            return list(events)
    except journal.TaskJournalError as exc:
        raise _refuse("journal-refused", f"the task journal refused the read ({exc})") from None


def _head(workspace: Path) -> str:
    try:
        return fleet_t3_transport.clean_source_revision(workspace, ignore_prefixes=(".brigade/",))
    except SourceRevisionError as exc:
        code = "source-dirty" if exc.code == "source-dirty" else "source-unavailable"
        raise _refuse(code, "the workspace source revision is not usable") from None


def _verified(workspace: Path, run_dir: Path, only: str | None = None) -> dict[str, _Verified]:
    """Re-read retained assets and bind them to the journal enrollment facts.

    A claim verifies every task, so any digest, custody, source or target
    mismatch refuses every new claim in the run. Single-task verbs verify only
    their own task. Task text never enters the refusal.
    """
    head = _head(workspace)
    try:
        results = run_tasks.read_tasks(run_dir)
    except run_tasks.TaskJournalError as exc:
        raise _refuse("journal-refused", f"the task journal refused the read ({exc})") from None
    verified: dict[str, _Verified] = {}
    for result in results:
        task_id = result.handle.spec.task_id
        if only is not None and task_id != only:
            continue
        if result.enrollment is None:
            raise _refuse("not-enrolled", "a task has no native enrollment record")
        try:
            retained = enrollment.read_delegation_input(run_dir, task_id=task_id)
            template = NativeIntentTemplate(
                run_id=run_dir.name,
                task_id=task_id,
                seat=result.handle.spec.seat,
                target=retained.target,
                payload_digest=result.handle.spec.payload_digest,
                source_revision=retained.target.source_revision,
            )
        except enrollment.EnrollmentError as exc:
            raise _refuse(exc.code, str(exc)) from None
        except ValueError:
            raise _refuse("target-changed", "the retained target is not a valid intent") from None
        if retained.seat != result.handle.spec.seat or template.template_digest != result.enrollment.template_digest:
            raise _refuse("target-changed", "the retained asset no longer matches its enrollment binding")
        if retained.target.isolation != "worktree":
            raise _refuse("target-changed", "the retained target no longer declares worktree isolation")
        if retained.target.source_revision != head:
            raise _refuse("source-changed", "the workspace source revision changed since enrollment")
        verified[task_id] = _Verified(result, retained, template)
    return verified


def _barrier(events: Sequence[run_journal.RunEvent]) -> None:
    """Refuse all new claims while any durable claim is unresolved.

    Two states block the run. A launch claim without its dispatch request was
    authorized but never recorded. A claim binding without a launch claim died
    between the two facts and never consumed launch permission. Neither state
    may let a sibling reuse the dispatch identity.
    """
    requested = {
        (event.payload["seat"], event.payload["attempt"])
        for event in events
        if event.event_type == "run.dispatch.requested"
    }
    launched = {
        (event.payload["task_id"], event.payload["attempt_id"])
        for event in events
        if event.event_type == "task.launch-claimed"
    }
    abandoned = {
        event.payload["task_id"]
        for event in events
        if event.event_type == HELD_EVENT and event.payload.get("reason") == ABANDONED
    }
    for event in events:
        if event.event_type == "task.launch-claimed":
            if (event.payload["seat"], event.payload["dispatch_attempt"]) not in requested:
                raise _refuse(
                    "run-barrier",
                    "a durable launch claim has no matching dispatch request, so the run holds every new claim",
                )
        elif event.event_type == CLAIM_EVENT:
            identity = (event.payload["task_id"], event.payload["attempt_id"])
            if identity not in launched and identity[0] not in abandoned:
                raise _refuse(
                    "run-barrier",
                    "a retained claim binding has no launch claim, so the run holds every new claim",
                )


def _latest(events: Sequence[run_journal.RunEvent], event_type: str, task_id: str) -> Mapping[str, Any] | None:
    for event in reversed(events):
        if event.event_type == event_type and event.payload.get("task_id") == task_id:
            return event.payload
    return None


def _claim_binding(events: Sequence[run_journal.RunEvent], task_id: str) -> ClaimBinding | None:
    payload = _latest(events, CLAIM_EVENT, task_id)
    if payload is None:
        return None
    try:
        return ClaimBinding(
            task_id=payload["task_id"],
            attempt_id=payload["attempt_id"],
            dispatch_attempt=payload["dispatch_attempt"],
            parent_thread=payload["parent_thread"],
            template_digest=payload["template_digest"],
            intent_digest=payload["intent_digest"],
        )
    except (KeyError, ValueError):
        raise _refuse("journal-refused", "the claim binding is not usable") from None


def _correlation(events: Sequence[run_journal.RunEvent], task_id: str) -> tuple[str, str] | None:
    payload = _latest(events, CORRELATION_EVENT, task_id)
    if payload is None:
        return None
    mode, handle = payload.get("mode"), payload.get("handle_digest")
    if mode not in {PARENT_REPORTED, OPERATOR_ASSERTED} or not journal.digest(handle):
        raise _refuse("journal-refused", "the correlation record is not usable")
    return str(mode), str(handle)


def _held(events: Sequence[run_journal.RunEvent], task_id: str) -> str | None:
    payload = _latest(events, HELD_EVENT, task_id)
    if payload is None:
        return None
    reason = payload.get("reason")
    if reason not in {ACCEPT_CONFLICT, ABANDONED}:
        raise _refuse("journal-refused", "the task hold record is not usable")
    return str(reason)


def _batch_ref(events: Sequence[run_journal.RunEvent], run_id: str, task_id: str) -> str:
    for event in events:
        if event.event_type == "task.batch-accepted" and any(
            row.get("task_id") == task_id for row in event.payload["members"]
        ):
            return journal.batch_reference(
                run_id=run_id,
                request_id=event.payload["request_id"],
                membership_digest=event.payload["membership_digest"],
            )
    raise _refuse("not-batch", "the task has no retained batch binding")


def _intent(verified: _Verified, handle: run_tasks.TaskHandle, attempt: int, batch_ref: str) -> NativeIntent:
    enrollment_record = verified.result.enrollment
    if handle.attempt_id is None or enrollment_record is None:
        raise _refuse("claim-refused", "the attempt was not prepared")
    try:
        return NativeIntent(
            work=None,
            handle=handle,
            dispatch_attempt=attempt,
            target=verified.retained.target,
            payload_context_digest=enrollment_record.asset_digest,
            batch_binding_ref=batch_ref,
            native_client_request_key=enrollment.request_key(handle.run_id, handle.spec.task_id, handle.attempt_id),
            authority_refs=(),
        )
    except (ValueError, AttributeError, TypeError):
        raise _refuse("intent-invalid", "the finalized intent is not valid") from None


def _intent_digest(intent: NativeIntent) -> str:
    handle = intent.handle
    return _digest(
        {
            "work": None,
            "handle": {**asdict(handle.spec), "run_id": handle.run_id, "request_id": handle.request_id},
            "attempt_id": handle.attempt_id,
            "dispatch_attempt": intent.dispatch_attempt,
            "target": asdict(intent.target),
            "payload_context_digest": intent.payload_context_digest,
            "batch_binding_ref": intent.batch_binding_ref,
            "native_client_request_key": intent.native_client_request_key,
            "authority_refs": [],
        },
        _INTENT_DOMAIN,
    )


def _key(kind: str, *identity: object) -> str:
    """Derive a fixed-length idempotency key from an identity tuple.

    Task ids reach 128 characters and may not be embedded beside a 64-hex
    digest inside the 128-character key bound. The digest of the whole
    identity keeps the key both bounded and collision-safe.
    """
    digest = hashlib.sha256(
        run_events.canonical_bytes({"domain": _KEY_DOMAIN, "kind": kind, "identity": list(identity)})
    ).hexdigest()
    return f"native-{kind}:{digest}"


def _append(
    workspace: Path, run_dir: Path, event_type: str, payload: Mapping[str, Any], *, key: str
) -> run_journal.RunEvent:
    try:
        return run_lifecycle.record_lifecycle_event(
            run_dir, event_type=event_type, payload=payload, idempotency_key=key, workspace=workspace
        )
    except (run_lifecycle.LifecycleJournalError, run_journal.RunJournalError, OSError, ValueError):
        raise _refuse("journal-refused", "the run journal refused the native fact") from None


def _record_held(workspace: Path, run_dir: Path, task_id: str, attempt_id: str, reason: str, evidence: str) -> None:
    if not journal.digest(evidence):
        raise _refuse("journal-refused", "the hold evidence is not usable")
    _append(
        workspace,
        run_dir,
        HELD_EVENT,
        {"task_id": task_id, "attempt_id": attempt_id, "reason": reason, "evidence_digest": evidence},
        key=_key("held", task_id, attempt_id, reason, evidence),
    )


def _record_dispatch_requested(run_dir: Path, workspace: Path, seat: str, attempt: int) -> int:
    """The durable requested fact after the launch claim. A crash here holds the run."""
    try:
        event = run_lifecycle.record_dispatch_fact(
            run_dir, workspace=workspace, event_type="run.dispatch.requested", seat=seat, attempt=attempt
        )
    except (run_lifecycle.LifecycleJournalError, run_journal.RunJournalError, OSError):
        raise _refuse("journal-refused", "the dispatch request was not recorded") from None
    if event is None or event.payload.get("attempt") != attempt:
        raise _refuse("dispatch-binding", "the dispatch request did not keep the claimed attempt")
    return attempt


def _coordinator(workspace: Path, run_dir: Path, meta: Mapping[str, Any]) -> run_budget.BudgetCoordinator:
    """Build one per-invocation coordinator from the durable run state."""
    try:
        declaration = run_budget.parse_declaration(meta.get("run_budget"))
    except run_budget.BudgetCompatibilityError as exc:
        raise _refuse("budget-incompatible", exc.diagnostic) from None
    coordinator = run_budget.BudgetCoordinator(
        declaration=declaration,
        append_event=lambda event_type, payload, key: run_lifecycle.record_lifecycle_event(
            run_dir, event_type=event_type, payload=dict(payload), idempotency_key=key, workspace=workspace
        ),
    )
    started = meta.get("started_at")
    try:
        coordinator.set_started_at(
            datetime.fromisoformat(started.replace("Z", "+00:00")) if isinstance(started, str) else None
        )
    except ValueError:
        coordinator.set_started_at(None)
    try:
        report = run_journal.read_journal_bounded(run_lifecycle._journal_path(run_dir))
    except (OSError, run_journal.RunJournalError):
        raise _refuse("journal-refused", "the run journal is not readable") from None
    if report.partial_tail is not None or report.chain_errors:
        raise _refuse("journal-refused", "the run journal chain is not usable")
    coordinator.reload(report.events)
    return coordinator


def _task_version(run_dir: Path, task_id: str) -> str:
    try:
        version = next(item.version for item in run_tasks.read_tasks(run_dir) if item.handle.spec.task_id == task_id)
    except StopIteration:
        raise _refuse("not-enrolled", "the task has no native enrollment record") from None
    except run_tasks.TaskJournalError as exc:
        raise _refuse("journal-refused", f"the task journal refused the read ({exc})") from None
    if version is None:
        raise _refuse("journal-refused", "the task has no durable version")
    return version


def _claim_record(
    *,
    workspace: Path,
    run_dir: Path,
    verified: _Verified,
    parent_thread: str,
    attempt: int,
    batch_ref: str,
    out: dict[str, Any],
) -> int | None:
    """Prepare, bind, claim, then record the requested fact, in that order."""
    task_id = verified.result.handle.spec.task_id
    try:
        handle = run_tasks.prepare_attempt(run_dir, workspace=workspace, task_id=task_id, native_governed=True)
    except run_tasks.TaskJournalError as exc:
        raise _refuse("claim-refused", f"the attempt was not prepared ({exc})") from None
    if handle.attempt_id is None:
        raise _refuse("claim-refused", "the prepared attempt has no identity")
    intent = _intent(verified, handle, attempt, batch_ref)
    digest = _intent_digest(intent)
    _append(
        workspace,
        run_dir,
        CLAIM_EVENT,
        {
            "task_id": task_id,
            "attempt_id": handle.attempt_id,
            "dispatch_attempt": attempt,
            "parent_thread": parent_thread,
            "template_digest": verified.template.template_digest,
            "intent_digest": digest,
        },
        key=_key("claimed", task_id, handle.attempt_id),
    )
    try:
        decision = run_tasks.claim_task_launch(
            run_dir,
            workspace=workspace,
            handle=handle,
            expected_version=_task_version(run_dir, task_id),
            seat=handle.spec.seat,
            dispatch_attempt=attempt,
            native_governed=True,
        )
    except run_tasks.TaskJournalError as exc:
        raise _refuse("claim-refused", f"the launch claim was refused ({exc})") from None
    if decision.outcome != "granted":
        raise _refuse("claim-refused", f"the launch claim was {decision.outcome}")
    out.update({"handle": handle, "intent": intent})
    return _record_dispatch_requested(run_dir, workspace, handle.spec.seat, attempt)


def _native_options(raw: object) -> tuple[tuple[str, str | bool], ...]:
    if raw is None:
        return ()
    if isinstance(raw, Mapping):
        rows: list[tuple[Any, Any]] = list(raw.items())
    elif isinstance(raw, list):
        rows = []
        for row in raw:
            if not isinstance(row, Mapping) or set(row) != {"id", "value"}:
                raise _refuse("native-invalid", "a native option row is not usable")
            rows.append((row["id"], row["value"]))
    else:
        raise _refuse("native-invalid", "native options are not usable")
    pairs: list[tuple[str, str | bool]] = []
    for key, value in rows:
        if type(key) is not str or not journal.token(key) or type(value) not in (str, bool):
            raise _refuse("native-invalid", "a native option is not usable")
        pairs.append((key, value))
    if len({key for key, _value in pairs}) != len(pairs):
        raise _refuse("native-invalid", "native options repeat a key")
    return tuple(sorted(pairs))


def _reference(value: object) -> str:
    if not journal.provider_reference(value):
        raise _refuse("native-invalid", "a native reference is not usable")
    return str(value)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise _refuse("native-invalid", "a native text field is not usable")
    return value


def _optional_reference(value: object) -> str | None:
    if value is None:
        return None
    return _reference(value)


def _pending_approval(value: object) -> Mapping[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _refuse("native-invalid", "a native pending approval is not usable")
    kind = value.get("kind")
    if type(kind) is not str or kind not in _APPROVAL_KINDS:
        raise _refuse("native-invalid", "a native pending approval kind is not usable")
    return {
        "requestId": _reference(value.get("requestId")),
        "kind": kind,
        "runId": _reference(value.get("runId")),
    }


def _require_local_child(payload: Mapping[str, object]) -> None:
    """A linked-environment child is outside the enrolled local target."""
    remote = payload.get("remoteChild")
    if remote is None:
        return
    if not isinstance(remote, Mapping):
        raise _refuse("native-invalid", "a native remote child is not usable")
    if any(not journal.provider_reference(remote.get(field)) for field in ("environmentId", "threadId")):
        raise _refuse("native-invalid", "a native remote child reference is not usable")
    if type(remote.get("label")) is not str:
        raise _refuse("native-invalid", "a native remote child label is not usable")
    raise _refuse("native-mismatch", "the native result names a remote child outside the enrolled target")


def parse_native_result(payload: object, *, task_id: str) -> NativeResult:
    """Validate the full ``delegate_task`` projection; never retain raw fields.

    Every field the classifier reads is type checked here. The classifier then
    decides whether the projection is terminal, because a terminal status alone
    never proves that descendants, questions, approvals or waits are settled.
    """
    if not isinstance(payload, Mapping) or not _RESULT_REQUIRED.issubset(payload):
        raise _refuse("native-invalid", "the native result is missing required projection fields")
    if payload["taskId"] != task_id or payload["childNodeId"] != task_id:
        raise _refuse("native-mismatch", "the native result does not identify this task")
    status, work_state = payload["status"], payload["workState"]
    if type(status) is not str or status not in _STATUS_MAP:
        raise _refuse("native-invalid", "the native task status is not usable")
    if type(work_state) is not str or work_state not in _WORK_STATES:
        raise _refuse("native-invalid", "the native work state is not usable")
    flags = ("hasPendingChildRuns", "hasPendingQuestion", "waitTimedOut")
    if any(type(payload[field]) is not bool for field in flags):
        raise _refuse("native-invalid", "a native pending or wait flag is not usable")
    terminal_status = payload["latestTerminalStatus"]
    if terminal_status is not None and (type(terminal_status) is not str or terminal_status not in _TERMINAL_NATIVE):
        raise _refuse("native-invalid", "the native terminal status is not usable")
    for field in ("latestTerminalSummary", "summary", "link", "next"):
        _optional_text(payload.get(field))
    model = payload["model"]
    if type(model) is not str or not model:
        raise _refuse("native-invalid", "the native model is not usable")
    _require_local_child(payload)
    result = NativeResult(
        task_id=task_id,
        child_thread_id=_reference(payload["childThreadId"]),
        child_run_id=_reference(payload["childRunId"]),
        child_node_id=_reference(payload["childNodeId"]),
        status=status,
        work_state=work_state,
        provider_instance=_reference(payload["providerInstanceId"]),
        model=model,
        options=_native_options(payload.get("options")),
        has_pending_child_runs=payload["hasPendingChildRuns"],
        has_pending_question=payload["hasPendingQuestion"],
        pending_approval=_pending_approval(payload["pendingApproval"]),
        wait_timed_out=payload["waitTimedOut"],
        latest_terminal_run_id=_optional_reference(payload["latestTerminalRunId"]),
        latest_terminal_status=terminal_status,
        latest_terminal_result_context_transfer_id=_optional_reference(
            payload["latestTerminalResultContextTransferId"]
        ),
        result_context_transfer_id=_optional_reference(payload["resultContextTransferId"]),
    )
    if result.terminal_status and (
        result.latest_terminal_status != result.status or result.latest_terminal_run_id is None
    ):
        raise _refuse("native-invalid", "a terminal native result lacks matching terminal metadata")
    return result


def _handle_digest(result: NativeResult, parent_thread: str) -> str:
    return _digest(
        {
            "task_id": result.task_id,
            "child_thread_id": result.child_thread_id,
            "child_run_id": result.child_run_id,
            "child_node_id": result.child_node_id,
            "provider_instance": result.provider_instance,
            "model": result.model,
            "options": [list(pair) for pair in result.options],
            "parent_thread": parent_thread,
        },
        _HANDLE_DOMAIN,
    )


def _evidence_digest(handle_digest: str, result: NativeResult, observation: observations.NativeObservation) -> str:
    """Bind a terminal acknowledgement candidate to its exact projection.

    The digest covers the correlated handle, the terminal status and run id,
    the selected result-transfer id, the pending flags and work state, and the
    containment verdict. It becomes the terminal artifact digest, so two
    different terminal projections cannot share a result identity.
    """
    return _digest(
        {
            "handle_digest": handle_digest,
            "status": result.status,
            "terminal_run_id": observation.terminal_run_id,
            "result_transfer_id": observation.result_transfer_id,
            "work_state": result.work_state,
            "has_pending_child_runs": result.has_pending_child_runs,
            "has_pending_question": result.has_pending_question,
            "pending_approval": result.pending_approval is not None,
            "wait_timed_out": result.wait_timed_out,
            "containment": observation.containment,
        },
        _EVIDENCE_DOMAIN,
    )


def _require_target(result: NativeResult, target: NativeTarget) -> None:
    if result.provider_instance != target.provider_instance:
        raise _refuse("provider-mismatch", "the native provider instance does not match the finalized intent")
    if result.model != target.launch_model:
        raise _refuse("model-mismatch", "the native model does not match the finalized intent")
    if result.options != target.options:
        raise _refuse("options-mismatch", "the native options do not match the finalized intent")


def _classify(verified: _Verified, binding: ClaimBinding, result: NativeResult) -> observations.NativeObservation:
    """Route the projection through the shipped lifecycle classifier.

    Containment evidence is the projection's own pending-child statement: a
    pending child run is active, and no pending child run is the only quiescent
    evidence this verb can supply. Any pending child run, question, approval or
    wait timeout classifies as awaiting no matter what the status field says.
    The origin is the enrolled intent's declared origin, because the projection
    carries no origin of its own.
    """
    target = verified.retained.target
    try:
        reference = observations.NativeTaskReference(
            origin_environment_id=target.origin_environment,
            native_client_request_key=enrollment.request_key(
                verified.result.handle.run_id, result.task_id, binding.attempt_id
            ),
            task_id=result.task_id,
        )
    except ValueError:
        raise _refuse("native-invalid", "the native correlation reference is not usable") from None
    snapshot = {
        "taskId": result.task_id,
        "status": result.status,
        "workState": result.work_state,
        "hasPendingChildRuns": result.has_pending_child_runs,
        "hasPendingQuestion": result.has_pending_question,
        "pendingApproval": result.pending_approval,
        "waitTimedOut": result.wait_timed_out,
        "latestTerminalStatus": result.latest_terminal_status,
        "latestTerminalRunId": result.latest_terminal_run_id,
        "latestTerminalResultContextTransferId": result.latest_terminal_result_context_transfer_id,
        "resultContextTransferId": result.result_context_transfer_id,
    }
    try:
        return observations.classify_native_snapshot(
            reference,
            snapshot,
            observed_origin_environment_id=target.origin_environment,
            containment="active" if result.has_pending_child_runs else "quiescent",
        )
    except ValueError:
        raise _refuse("native-invalid", "the native projection is not classifiable") from None


def _recorded_status(result: NativeResult, observation: observations.NativeObservation) -> str:
    """Map a classification to a durable status, never terminal before its time."""
    if observation.family == "terminal-candidate":
        return _STATUS_MAP[result.status]
    if result.status not in _TERMINAL_NATIVE:
        return _STATUS_MAP[result.status]
    if result.has_pending_child_runs or result.work_state == "waiting_for_children":
        return "waiting-for-children"
    if result.has_pending_question or result.pending_approval is not None:
        return "waiting-for-input"
    return "unknown"


def _observe(
    workspace: Path,
    run_dir: Path,
    verified: _Verified,
    result: NativeResult,
    observation: observations.NativeObservation,
    *,
    evidence: str | None = None,
) -> run_tasks.TaskResult:
    terminal = observation.family == "terminal-candidate"
    containment = "quiescent" if terminal else ("active" if result.has_pending_child_runs else "unknown")
    try:
        return run_tasks.observe_task(
            run_dir,
            workspace=workspace,
            handle=verified.result.handle,
            observation=run_tasks.TaskObservation(
                status=_recorded_status(result, observation),
                provider_task_id=result.child_node_id,
                original_run_id=result.child_run_id,
                provider_result_id=observation.terminal_run_id if terminal else None,
                artifact_kind="receipt" if terminal else None,
                artifact_digest=evidence if terminal else None,
                containment=containment,
            ),
            expected_version=verified.result.version,
        )
    except run_tasks.TaskJournalError as exc:
        raise _refuse("observation-conflict", f"the native observation was refused ({exc})") from None


def _correlate(
    *,
    workspace: Path,
    run_dir: Path,
    verified: _Verified,
    result: NativeResult,
    binding: ClaimBinding,
    mode: str,
) -> dict[str, Any]:
    """Record a caller-reported handle. This is correlation, never acceptance proof."""
    handle_digest = _handle_digest(result, binding.parent_thread)
    prior = _correlation(_read(run_dir), result.task_id)
    if prior is not None and prior[1] != handle_digest:
        if mode == PARENT_REPORTED:
            _record_held(workspace, run_dir, result.task_id, binding.attempt_id, ACCEPT_CONFLICT, handle_digest)
            raise _refuse("accept-conflict", "a different native handle already correlates this task")
        raise _refuse("accept-conflict", "the operator assertion contradicts the recorded native handle")
    observation = _classify(verified, binding, result)
    if observation.family == "refused":
        raise _refuse("native-mismatch", "the native projection does not correlate with this task")
    evidence = (
        _evidence_digest(handle_digest, result, observation) if observation.family == "terminal-candidate" else None
    )
    updated = _observe(workspace, run_dir, verified, result, observation, evidence=evidence)
    _append(
        workspace,
        run_dir,
        CORRELATION_EVENT,
        {
            "task_id": result.task_id,
            "attempt_id": binding.attempt_id,
            "mode": mode,
            "handle_digest": handle_digest,
        },
        key=_key("correlated", result.task_id, binding.attempt_id, mode, handle_digest),
    )
    return {
        "schema": CORRELATION_SCHEMA,
        "run_id": run_dir.name,
        "task_id": result.task_id,
        "attempt_id": binding.attempt_id,
        "mode": mode,
        "status": updated.status,
        "result_id": updated.result_id,
        "child_thread_id": result.child_thread_id,
        "child_run_id": result.child_run_id,
        "child_node_id": result.child_node_id,
        "provider_instance": result.provider_instance,
        "model": result.model,
        "replayed": prior is not None,
    }


def _context(workspace: Path, run_id: str, task_id: str) -> Path:
    """Resolve the run directory. Run state is re-read inside the run lock."""
    if not journal.task_id(task_id):
        raise _refuse("invalid-identity", "the task identity is not valid")
    return _run_dir(workspace, run_id)


def claim(*, workspace: Path, run_id: str, task_id: str, parent_thread: str) -> dict[str, Any]:
    """Reserve, prepare, bind, claim and request exactly once for one task."""
    workspace = workspace.expanduser().resolve()
    if not journal.provider_reference(parent_thread):
        raise _refuse("invalid-identity", "the parent thread identity is not valid")
    run_dir = _context(workspace, run_id, task_id)
    with enrollment.owned_run(workspace, run_dir):
        meta = _run_meta(workspace, run_dir)
        events = _read(run_dir)
        _barrier(events)
        verified = _verified(workspace, run_dir)
        current = verified.get(task_id)
        if current is None:
            raise _refuse("not-enrolled", "the task has no native enrollment record")
        held = _held(events, task_id)
        if held is not None:
            raise _refuse("task-held", f"the task is held ({held})")
        if current.result.launch_claim is not None:
            raise _refuse("already-claimed", "the task already consumed its launch claim")
        batch_ref = _batch_ref(events, run_dir.name, task_id)
        coordinator = _coordinator(workspace, run_dir, meta)
        out: dict[str, Any] = {}
        try:
            attempt = coordinator.reserve_and_record_dispatch(
                seat=current.result.handle.spec.seat,
                allocate_attempt=lambda: run_lifecycle.allocate_next_dispatch_attempt(
                    run_dir, current.result.handle.spec.seat
                ),
                record_requested=lambda candidate: _claim_record(
                    workspace=workspace,
                    run_dir=run_dir,
                    verified=current,
                    parent_thread=parent_thread,
                    attempt=candidate,
                    batch_ref=batch_ref,
                    out=out,
                ),
                reclaim_pending=False,
            )
        except run_budget.BudgetPolicyError as exc:
            code = "capacity-denied" if not exc.exhausted else "budget-exhausted"
            raise _refuse(code, f"the run budget refused a dispatch unit ({exc.dimension})") from None
        except run_budget.BudgetError as exc:
            raise _refuse("budget-refused", exc.diagnostic) from None
        except (run_lifecycle.LifecycleJournalError, run_journal.RunJournalError, OSError):
            raise _refuse("journal-refused", "the dispatch transaction was refused") from None
        if type(attempt) is not int or "intent" not in out:
            raise _refuse("claim-refused", "the dispatch transaction did not complete")
        intent, handle = out["intent"], out["handle"]
        target = current.retained.target
        return {
            "schema": SCHEMA,
            "run_id": run_dir.name,
            "task_id": task_id,
            "seat": handle.spec.seat,
            "attempt_id": handle.attempt_id,
            "dispatch_attempt": attempt,
            "state": "claimed",
            "clientRequestId": intent.native_client_request_key,
            "delegate_task": {
                "task": current.retained.text,
                "target": {
                    "providerInstanceId": target.provider_instance,
                    "model": target.launch_model,
                    "options": [{"id": key, "value": value} for key, value in target.options],
                },
                "isolation": "worktree",
                "clientRequestId": intent.native_client_request_key,
            },
        }


def _durable_claim(
    workspace: Path,
    run_dir: Path,
    task_id: str,
    parent_thread: str | None,
    *,
    require_launch: bool = True,
    require_requested: bool = True,
) -> tuple[list[run_journal.RunEvent], _Verified, ClaimBinding]:
    """Load the task, its asset binding, its claim and (optionally) its parent thread."""
    events = _read(run_dir)
    verified = _verified(workspace, run_dir, only=task_id)
    current = verified.get(task_id)
    if current is None:
        raise _refuse("not-enrolled", "the task has no native enrollment record")
    binding = _claim_binding(events, task_id)
    if binding is None or current.result.handle.attempt_id != binding.attempt_id:
        raise _refuse("no-claim", "the task has no durable governed claim")
    if current.result.launch_claim is None and require_launch:
        raise _refuse("no-claim", "the task has no durable launch claim")
    requested = {
        (event.payload["seat"], event.payload["attempt"])
        for event in events
        if event.event_type == "run.dispatch.requested"
    }
    if require_requested and current.result.launch_claim is not None:
        binding_key = (current.result.launch_claim.seat, current.result.launch_claim.dispatch_attempt)
        if binding_key not in requested:
            raise _refuse("claim-incomplete", "the claim has no durable dispatch request")
    if parent_thread is not None and binding.parent_thread != parent_thread:
        raise _refuse("parent-mismatch", "the parent thread does not match the durable claim")
    batch_ref = _batch_ref(events, run_dir.name, task_id)
    if _intent_digest(_intent(current, current.result.handle, binding.dispatch_attempt, batch_ref)) != (
        binding.intent_digest
    ):
        raise _refuse("intent-mismatch", "the retained intent no longer matches the durable claim")
    return events, current, binding


def accepted(*, workspace: Path, run_id: str, task_id: str, parent_thread: str, payload: object) -> dict[str, Any]:
    """Record one caller-reported native handle; never authenticated acceptance."""
    workspace = workspace.expanduser().resolve()
    if not journal.provider_reference(parent_thread):
        raise _refuse("invalid-identity", "the parent thread identity is not valid")
    run_dir = _context(workspace, run_id, task_id)
    with enrollment.owned_run(workspace, run_dir):
        _run_meta(workspace, run_dir)
        events, verified, binding = _durable_claim(workspace, run_dir, task_id, parent_thread)
        held = _held(events, task_id)
        if held is not None:
            raise _refuse("task-held", f"the task is held ({held})")
        result = parse_native_result(payload, task_id=task_id)
        _require_target(result, verified.retained.target)
        return _correlate(
            workspace=workspace,
            run_dir=run_dir,
            verified=verified,
            result=result,
            binding=binding,
            mode=PARENT_REPORTED,
        )


def _close_lost_request(
    workspace: Path, run_dir: Path, events: Sequence[run_journal.RunEvent], verified: _Verified
) -> None:
    """Record a missing dispatch request so an abandoned lost reply frees the barrier.

    The claim already consumed launch permission, so the unit stays charged and
    the task stays held and non-relaunchable.
    """
    claim = verified.result.launch_claim
    if claim is None:
        return
    requested = {
        (event.payload["seat"], event.payload["attempt"])
        for event in events
        if event.event_type == "run.dispatch.requested"
    }
    if (claim.seat, claim.dispatch_attempt) not in requested:
        _record_dispatch_requested(run_dir, workspace, claim.seat, claim.dispatch_attempt)


def hold_resolve(
    *, workspace: Path, run_id: str, task_id: str, payload: object | None, abandon: bool
) -> dict[str, Any]:
    """Resolve a held task by operator assertion or an explicit abandon.

    Available to any principal allowed to mutate this run. Brigade cannot
    enforce operator-only on a shared local account, and this verb never
    re-grants launch for the task.
    """
    workspace = workspace.expanduser().resolve()
    if (payload is None) == (not abandon):
        raise _refuse("invalid-identity", "exactly one resolution mode is required")
    run_dir = _context(workspace, run_id, task_id)
    with enrollment.owned_run(workspace, run_dir):
        _run_meta(workspace, run_dir)
        events, verified, binding = _durable_claim(
            workspace, run_dir, task_id, None, require_launch=False, require_requested=False
        )
        held = _held(events, task_id)
        if not abandon:
            if held is not None:
                raise _refuse("task-held", f"the task is held ({held})")
            if verified.result.launch_claim is None:
                raise _refuse("no-claim", "the task has no durable launch claim")
        _close_lost_request(workspace, run_dir, events, verified)
        base = {
            "schema": CORRELATION_SCHEMA,
            "run_id": run_dir.name,
            "task_id": task_id,
            "attempt_id": binding.attempt_id,
        }
        if abandon:
            replay = held == ABANDONED
            if not replay:
                _record_held(workspace, run_dir, task_id, binding.attempt_id, ABANDONED, binding.intent_digest)
            return {**base, "mode": ABANDONED, "status": verified.result.status, "replayed": replay}
        result = parse_native_result(payload, task_id=task_id)
        _require_target(result, verified.retained.target)
        return _correlate(
            workspace=workspace,
            run_dir=run_dir,
            verified=verified,
            result=result,
            binding=binding,
            mode=OPERATOR_ASSERTED,
        )


def _read_native(path: str) -> object:
    """Read a bounded native projection from stdin or one regular file.

    A named pipe would block the CLI forever, so the path is lstat-checked
    first, then opened non-blocking and no-follow where the platform supports
    it, and the open handle is checked again before any read.
    """
    if path == "-":
        raw = sys.stdin.buffer.read(_MAX_NATIVE_BYTES + 1)
    else:
        target = Path(path).expanduser()
        try:
            before = os.lstat(target)
        except OSError:
            raise _refuse("native-unreadable", "the native result file is not readable") from None
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise _refuse("native-unreadable", "the native result is not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags)
        except OSError:
            raise _refuse("native-unreadable", "the native result file is not readable") from None
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise _refuse("native-unreadable", "the native result is not a regular file")
            chunks: list[bytes] = []
            remaining = _MAX_NATIVE_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        except OSError:
            raise _refuse("native-unreadable", "the native result file is not readable") from None
        finally:
            os.close(descriptor)
    if len(raw) > _MAX_NATIVE_BYTES:
        raise _refuse("native-too-large", "the native result exceeds the size bound")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _refuse("native-invalid", "the native result is not valid JSON") from None


def _emit(call: Any) -> int:
    try:
        report = call()
    except DispatchError as exc:
        print(f"error [{exc.code}]: {_bound(str(exc))}", file=sys.stderr)
        return 2
    except enrollment.EnrollmentError as exc:
        print(f"error [{exc.code}]: {_bound(str(exc))}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


def claim_cli(args: Any) -> int:
    return _emit(
        lambda: claim(workspace=args.target, run_id=args.run_id, task_id=args.task_id, parent_thread=args.parent_thread)
    )


def accepted_cli(args: Any) -> int:
    return _emit(
        lambda: accepted(
            workspace=args.target,
            run_id=args.run_id,
            task_id=args.task_id,
            parent_thread=args.parent_thread,
            payload=_read_native(args.native_json),
        )
    )


def hold_resolve_cli(args: Any) -> int:
    return _emit(
        lambda: hold_resolve(
            workspace=args.target,
            run_id=args.run_id,
            task_id=args.task_id,
            payload=None if args.abandon else _read_native(args.native_json),
            abandon=args.abandon,
        )
    )
