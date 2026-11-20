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

import json
import os
import re
import stat
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Sequence
from urllib.parse import quote, unquote

from . import fleet_t3_transport, run_events, run_journal, run_tasks
from . import native_child_claim as claim_core
from . import native_child_journal as journal_core
from . import native_t3_admission as admission
from . import native_t3_capacity as capacity
from . import native_t3_observations as observations
from . import run_task_journal as journal
from . import native_t3_enrollment as enrollment
from .fleet_t3_transport import SourceRevisionError
from .native_child_host import NativeChildError
from .native_t3_contract import INHERIT_MODE, NativeIntent, NativeIntentTemplate, NativeTarget
from .native_t3_host import HOST

SCHEMA = "brigade.t3_claim.v1"
CORRELATION_SCHEMA = "brigade.t3_correlation.v1"
CLAIM_EVENT = journal_core.CLAIM_EVENT
CORRELATION_EVENT = "task.native-correlated"
HELD_EVENT = journal_core.HELD_EVENT
HOLD_RESOLVED_EVENT = journal_core.HOLD_RESOLVED_EVENT
PARENT_REPORTED = "parent-reported"
OPERATOR_ASSERTED = "operator-asserted"
ABANDONED = journal_core.ABANDONED
ACCEPT_CONFLICT = journal_core.ACCEPT_CONFLICT
HELD_RUN_STATUS = journal_core.HELD_RUN_STATUS
_MAX_DIAGNOSTIC = journal_core.MAX_DIAGNOSTIC
_MAX_NATIVE_BYTES = 1_048_576
_INTENT_DOMAIN = "brigade.native-t3.intent.v1"
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
# T3 Code revision c52df24d285a072ad9c3f7ab5b3656c0be1cb0f5 builds the
# `delegate_task` task node identity in three steps.
#   1. The request key is the caller's `clientRequestId` verbatim
#      (apps/server/src/mcp/OrchestratorMcpService.ts:1476-1477).
#   2. The command id is "command:mcp:" + enc(requestNamespace) +
#      ":delegate-task:" + enc(requestKey), where enc is encodeURIComponent
#      (OrchestratorMcpService.ts:679-695 and the delegate handler at :2435-2439).
#   3. The task node id is "node:delegated-task:" + enc(commandId)
#      (packages/provider-core/src/server/IdAllocator.ts:191-194, :259, called
#      from apps/server/src/orchestration-v2/Orchestrator.ts:7973-7975).
# Because the request key is encoded twice, ":" appears as "%253A". A future T3
# revision that changes this encoding makes `accepted` refuse with
# native-mismatch, so the task holds instead of correlating to a guess.
_T3_NODE_PREFIX = "node:delegated-task:"
_T3_COMMAND_PREFIX = "command:mcp:"
_T3_OPERATION = "delegate-task"
# Request namespaces are T3 provider-session UUIDs or prefixed client, provider
# and remote-task tokens. They are opaque, bounded and ASCII in the reviewed
# revision, and never contain whitespace.
_T3_NAMESPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:~+=@/-]{0,255}", re.ASCII)

_TERMINAL_NATIVE = frozenset({"completed", "failed", "cancelled", "interrupted"})
_WORK_STATES = frozenset({"working", "waiting_for_children", "result_available"})
_APPROVAL_KINDS = frozenset({"command", "file-read", "file-change", "mcp-elicitation", "permission"})
_RESULT_REQUIRED = frozenset(
    """taskId childThreadId childRunId childNodeId status workState hasPendingChildRuns latestTerminalRunId
    latestTerminalStatus latestTerminalSummary latestTerminalResultContextTransferId providerInstanceId model
    summary resultContextTransferId hasPendingQuestion pendingApproval waitTimedOut""".split()
)


# encodeURIComponent leaves exactly these ASCII punctuation characters alone.
_URI_COMPONENT_SAFE = "-_.!~*'()"


def _encode_t3_component(value: str) -> str:
    """Mirror JavaScript ``encodeURIComponent`` for the pinned T3 revision."""
    return quote(value, safe=_URI_COMPONENT_SAFE)


def _decode_t3_component(value: str) -> str | None:
    """Decode one T3 component, refusing anything that is not its canonical encoding."""
    try:
        decoded = unquote(value, encoding="utf-8", errors="strict")
        # A lone surrogate survives unquote and then fails to re-encode.
        canonical = _encode_t3_component(decoded)
    except UnicodeError:
        return None
    if not decoded or canonical != value:
        return None
    return decoded


def _is_t3_task_node(value: object, request_key: str) -> bool:
    """Accept exactly the node id T3 builds for our ``delegate_task`` request key.

    The check is structural and canonical. It decodes the single encoded command
    id, requires the fixed command prefix and operation, requires a plausible
    namespace, and requires the decoded request key to equal ours exactly with
    nothing trailing. Any other shape refuses so the caller can fail closed.
    """
    if (
        type(value) is not str
        or len(value) > run_events.MAX_PAYLOAD_STR_LEN
        or not value.startswith(_T3_NODE_PREFIX)
        or not request_key
    ):
        return False
    command = _decode_t3_component(value[len(_T3_NODE_PREFIX) :])
    if command is None or not command.startswith(_T3_COMMAND_PREFIX):
        return False
    parts = command[len(_T3_COMMAND_PREFIX) :].split(":")
    if len(parts) != 3:
        return False
    namespace, operation, encoded_key = parts
    if operation != _T3_OPERATION:
        return False
    decoded_namespace = _decode_t3_component(namespace)
    if decoded_namespace is None or _T3_NAMESPACE.fullmatch(decoded_namespace) is None:
        return False
    return _decode_t3_component(encoded_key) == request_key


@dataclass(frozen=True)
class NativeResult:
    """The validated ``delegate_task`` result projection, without raw payload."""

    task_id: str
    provider_task_id: str
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

    @property
    def target(self) -> NativeTarget:
        """The retained target the host admits for this task."""
        return self.retained.target


class _Correlation(NamedTuple):
    mode: str
    handle_digest: str
    # Optional legacy input. Correlations recorded before the native task node
    # identity was pinned carry only the digest.
    provider_task_id: str | None = None


# The journal, barrier, hold and claim machinery is host neutral and lives in
# ``native_child_journal`` and ``native_child_claim``. These names keep the
# surface this module has always exported. The two that tests replace
# (``_append`` and ``_record_dispatch_requested``) are real functions, so the
# core receives the replaced step at call time.
DispatchError = NativeChildError
ClaimBinding = journal_core.ClaimBinding
_bound = journal_core.bound
_refuse = journal_core.refuse
_digest = journal_core.digest
_run_dir = journal_core.run_dir
_read = journal_core.read_events
_barrier = journal_core.barrier
_latest = journal_core.latest
_claim_binding = journal_core.claim_binding
_held = journal_core.held
_batch_ref = journal_core.batch_ref
_context = journal_core.context
_coordinator = journal_core.coordinator
_task_version = journal_core.task_version


def _run_meta(workspace: Path, run_dir: Path) -> dict[str, Any]:
    return journal_core.run_meta(HOST, workspace, run_dir)


def _key(kind: str, *identity: object) -> str:
    return journal_core.idempotency_key(HOST, kind, *identity)


def _append(
    workspace: Path, run_dir: Path, event_type: str, payload: Mapping[str, Any], *, key: str
) -> run_journal.RunEvent:
    return journal_core.append_fact(workspace, run_dir, event_type, payload, key=key)


def _record_held(workspace: Path, run_dir: Path, task_id: str, attempt_id: str, reason: str, evidence: str) -> None:
    journal_core.record_held(HOST, workspace, run_dir, task_id, attempt_id, reason, evidence, append=_append)


def _record_dispatch_requested(run_dir: Path, workspace: Path, seat: str, attempt: int) -> int:
    """The durable requested fact after the launch claim. A crash here holds the run."""
    return journal_core.record_dispatch_requested(run_dir, workspace, seat, attempt)


def _close_lost_request(
    workspace: Path, run_dir: Path, events: Sequence[run_journal.RunEvent], verified: _Verified
) -> None:
    journal_core.close_lost_request(
        workspace, run_dir, events, verified.result, record_requested=_record_dispatch_requested
    )


def _claim_record(
    *,
    workspace: Path,
    run_dir: Path,
    verified: _Verified,
    parent_thread: str,
    attempt: int,
    batch_ref: str,
    admission_evidence: admission.Admission,
    out: dict[str, Any],
) -> int | None:
    """Prepare, bind, claim, then record the requested fact, in that order."""

    def build_intent(handle: run_tasks.TaskHandle) -> tuple[NativeIntent, str]:
        intent = _intent(verified, handle, attempt, batch_ref)
        return intent, _intent_digest(intent)

    return claim_core.record_claim(
        HOST,
        workspace=workspace,
        run_dir=run_dir,
        task_id=verified.result.handle.spec.task_id,
        attempt=attempt,
        build_intent=build_intent,
        claim_payload=lambda intent_digest: {
            "parent_thread": parent_thread,
            "template_digest": verified.template.template_digest,
            "intent_digest": intent_digest,
            "admission": admission_evidence.code,
            "roster_digest": admission_evidence.roster_digest,
            "policy_digest": admission_evidence.policy_digest,
        },
        append=_append,
        record_requested=_record_dispatch_requested,
        out=out,
    )


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


def _correlation(events: Sequence[run_journal.RunEvent], task_id: str) -> _Correlation | None:
    payload = _latest(events, CORRELATION_EVENT, task_id)
    if payload is None:
        return None
    mode, handle = payload.get("mode"), payload.get("handle_digest")
    provider_task_id = payload.get("provider_task_id")
    if (
        mode not in {PARENT_REPORTED, OPERATOR_ASSERTED}
        or not journal.digest(handle)
        or (provider_task_id is not None and not journal.provider_reference(provider_task_id))
    ):
        raise _refuse("journal-refused", "the correlation record is not usable")
    return _Correlation(str(mode), str(handle), provider_task_id)


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


def parse_native_result(payload: object, *, task_id: str, request_key: str) -> NativeResult:
    """Validate the full ``delegate_task`` projection; never retain raw fields.

    The reply must name the exact T3 task node built for the retained claim's
    ``clientRequestId``. Brigade's own batch task id never appears in a real
    reply, so it is not the correlation identity. ``childNodeId`` must repeat
    that same node id. Every other field the classifier reads is type checked
    here. The classifier then decides whether the projection is terminal,
    because a terminal status alone never proves that descendants, questions,
    approvals or waits are settled.
    """
    if not journal.provider_reference(request_key):
        raise _refuse("native-invalid", "the native request key is not usable")
    if not isinstance(payload, Mapping) or not _RESULT_REQUIRED.issubset(payload):
        raise _refuse("native-invalid", "the native result is missing required projection fields")
    provider_task_id = payload["taskId"]
    if not _is_t3_task_node(provider_task_id, request_key) or payload["childNodeId"] != provider_task_id:
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
        provider_task_id=str(provider_task_id),
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


def _client_request_id(verified: _Verified, binding: ClaimBinding) -> str:
    """Return the retained claim's ``clientRequestId`` for this run, task and attempt."""
    try:
        return enrollment.request_key(
            verified.result.handle.run_id, verified.result.handle.spec.task_id, binding.attempt_id
        )
    except ValueError:
        raise _refuse("native-invalid", "the native correlation reference is not usable") from None


def parse_claimed_result(payload: object, *, verified: _Verified, binding: ClaimBinding) -> NativeResult:
    """Parse a reply against the client request id the durable claim printed."""
    return parse_native_result(
        payload,
        task_id=verified.result.handle.spec.task_id,
        request_key=_client_request_id(verified, binding),
    )


def _classify(verified: _Verified, binding: ClaimBinding, result: NativeResult) -> observations.NativeObservation:
    """Route the projection through the shipped lifecycle classifier.

    Containment evidence is the projection's own pending-child statement: a
    pending child run is active, and no pending child run is the only quiescent
    evidence this verb can supply. Any pending child run, question, approval or
    wait timeout classifies as awaiting no matter what the status field says.
    The origin is the enrolled intent's declared origin, because the projection
    carries no origin of its own. The accepted task identity is T3's task node
    id, not Brigade's batch task id.
    """
    target = verified.retained.target
    try:
        reference = observations.NativeTaskReference(
            origin_environment_id=target.origin_environment,
            native_client_request_key=_client_request_id(verified, binding),
            task_id=result.provider_task_id,
        )
    except ValueError:
        raise _refuse("native-invalid", "the native correlation reference is not usable") from None
    snapshot = {
        "taskId": result.provider_task_id,
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
    if prior is not None and prior.handle_digest != handle_digest:
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
            "provider_task_id": result.provider_task_id,
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


def delegate_target_arguments(target: NativeTarget) -> dict[str, Any]:
    """The target-derived ``delegate_task`` fields, without attempt-scoped values.

    Readiness binds these exact fields so a target change invalidates evidence.
    The task text and client request id arrive only with a claimed attempt.
    """
    arguments: dict[str, Any] = {
        "target": {
            "providerInstanceId": target.provider_instance,
            "driverKind": target.driver,
            "model": target.launch_model,
            "projectId": target.target_project,
            "options": [{"id": key, "value": value} for key, value in target.options],
        },
        "isolation": "worktree",
    }
    if target.runtime_mode != INHERIT_MODE:
        arguments["runtimeMode"] = target.runtime_mode
    if target.interaction_mode != INHERIT_MODE:
        arguments["interactionMode"] = target.interaction_mode
    return arguments


def _delegate_arguments(text: str, target: NativeTarget, client_request_id: str) -> dict[str, Any]:
    """Build exactly the reviewed ``delegate_task`` input for one claim.

    Every emitted name is a field of the pinned T3 Code delegate schema. The
    environment id is deliberately absent, because naming a linked environment
    selects T3's remote path, which rejects worktree isolation. Modes are sent
    only when the retained target declares one instead of ``inherit``.
    """
    arguments: dict[str, Any] = {
        "task": text,
        **delegate_target_arguments(target),
        "clientRequestId": client_request_id,
    }
    return arguments


def claim(*, workspace: Path, run_id: str, task_id: str, parent_thread: str) -> dict[str, Any]:
    """Reserve, prepare, bind, claim and request exactly once for one task."""
    workspace = workspace.expanduser().resolve()
    if not journal.provider_reference(parent_thread):
        raise _refuse("invalid-identity", "the parent thread identity is not valid")
    run_dir = _context(workspace, run_id, task_id)
    # The Hub read is network I/O, so it stays outside the capacity lock and
    # never stretches the bounded wait.
    hub = admission.read_hub_authority(workspace)

    def record(
        current: _Verified, candidate: int, batch_ref: str, evidence: admission.Admission, recorded: dict[str, Any]
    ) -> int | None:
        recorded["verified"] = current
        return _claim_record(
            workspace=workspace,
            run_dir=run_dir,
            verified=current,
            parent_thread=parent_thread,
            attempt=candidate,
            batch_ref=batch_ref,
            admission_evidence=evidence,
            out=recorded,
        )

    # Every step is looked up here, at call time, so a replaced step applies.
    ops = claim_core.ClaimOps(
        capacity_lock=capacity.capacity_lock,
        governed_occupancy=capacity.governed_occupancy,
        assess=capacity.assess,
        owned_run=enrollment.owned_run,
        load_roster=admission.load_roster,
        run_meta=_run_meta,
        read_events=_read,
        verify=_verified,
        evaluate=lambda roster, seat, target, authority: admission.evaluate(roster, seat, target, hub=authority),
        batch_ref=_batch_ref,
        coordinator=_coordinator,
        record_claim=record,
    )
    grant = claim_core.run_claim(HOST, ops, workspace=workspace, run_dir=run_dir, task_id=task_id, hub=hub)
    current = grant.recorded["verified"]
    intent, handle = grant.recorded["intent"], grant.recorded["handle"]
    return {
        "schema": SCHEMA,
        "run_id": run_dir.name,
        "task_id": task_id,
        "seat": handle.spec.seat,
        "attempt_id": handle.attempt_id,
        "dispatch_attempt": grant.attempt,
        "state": "claimed",
        "clientRequestId": intent.native_client_request_key,
        "delegate_task": _delegate_arguments(current.retained.text, current.target, intent.native_client_request_key),
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
    requested = journal_core.requested_attempts(events)
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
        result = parse_claimed_result(payload, verified=verified, binding=binding)
        _require_target(result, verified.retained.target)
        return _correlate(
            workspace=workspace,
            run_dir=run_dir,
            verified=verified,
            result=result,
            binding=binding,
            mode=PARENT_REPORTED,
        )


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
            if held == ABANDONED:
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
        result = parse_claimed_result(payload, verified=verified, binding=binding)
        _require_target(result, verified.retained.target)
        if held == ACCEPT_CONFLICT:
            return _resolve_hold(workspace, run_dir, verified, result, binding, base)
        return _correlate(
            workspace=workspace,
            run_dir=run_dir,
            verified=verified,
            result=result,
            binding=binding,
            mode=OPERATOR_ASSERTED,
        )


def _resolve_hold(
    workspace: Path,
    run_dir: Path,
    verified: _Verified,
    result: NativeResult,
    binding: ClaimBinding,
    base: Mapping[str, Any],
) -> dict[str, Any]:
    """Clear an accept-conflict hold only for the already-recorded handle.

    The operator assertion is an assertion, not acceptance. It records a closed
    resolution fact and re-observes the matching projection. A different handle
    keeps the hold, and nothing here re-grants launch.
    """
    prior = _correlation(_read(run_dir), result.task_id)
    handle_digest = _handle_digest(result, binding.parent_thread)
    if (
        prior is None
        or prior.handle_digest != handle_digest
        or (prior.provider_task_id is not None and prior.provider_task_id != result.provider_task_id)
    ):
        raise _refuse("accept-conflict", "the operator assertion contradicts the recorded native handle")
    observation = _classify(verified, binding, result)
    if observation.family == "refused":
        raise _refuse("native-mismatch", "the native projection does not correlate with this task")
    evidence = (
        _evidence_digest(handle_digest, result, observation) if observation.family == "terminal-candidate" else None
    )
    updated = _observe(workspace, run_dir, verified, result, observation, evidence=evidence)
    # Clear the hold only after the projection is accepted. A refused or
    # contradictory projection must leave the task held.
    _append(
        workspace,
        run_dir,
        HOLD_RESOLVED_EVENT,
        {
            "task_id": result.task_id,
            "attempt_id": binding.attempt_id,
            "reason": ACCEPT_CONFLICT,
            "evidence_digest": handle_digest,
        },
        key=_key("hold-resolved", result.task_id, binding.attempt_id, ACCEPT_CONFLICT, handle_digest),
    )
    return {
        **base,
        "mode": "hold-resolved",
        "status": updated.status,
        "result_id": updated.result_id,
        "replayed": False,
    }


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
