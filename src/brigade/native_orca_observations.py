"""Project Orca worker observations into the native-child families."""

from __future__ import annotations

import json
from typing import Mapping

from .native_child_host import NativeChildError
from .native_child_journal import digest
from .native_child_observations import NativeObservation
from .native_orca_dispatch import CLI, WorkerReference, mutation_key, result_fields, valid_id

_STOP_DOMAIN = "brigade.native-orca.worker-stop.v1"
_RESULT_DOMAIN = "brigade.native-orca.result.v1"
_WAIT_SOURCES = ("hook", "prompt-text", "title")
_TERMINAL_WORKER = {"completed": "succeeded", "failed": "failed"}
_MAX_TRANSCRIPT_PAGES = 8


def _dispatch_argv(command: str, worker: WorkerReference) -> tuple[str, ...]:
    return (*CLI, command, "--environment", "local", "--dispatch", worker.dispatch_id)


def observe_argv(worker: WorkerReference) -> tuple[str, ...]:
    # The concise view omits the Dispatch status and changes agentWait's representation.
    return (*_dispatch_argv("worker-show", worker), "--json", "--full")


def cancel_key(worker: WorkerReference) -> str:
    return mutation_key({"handle": worker.handle_digest}, _STOP_DOMAIN)


def cancel_argv(worker: WorkerReference) -> tuple[str, ...]:
    return (*_dispatch_argv("worker-stop", worker), "--retry-request", cancel_key(worker), "--json")


def inbox_argv(worker: WorkerReference) -> tuple[str, ...]:
    return (*CLI, "inbox", "--full", "--environment", "local", "--run", worker.run_id, "--limit", "100", "--json")


def result_argv(worker: WorkerReference) -> tuple[str, ...]:
    return (*_dispatch_argv("worker-read", worker), "--source", "transcript", "--json")


def _fields(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _awaiting(worker: WorkerReference, reason: str, status: str | None = None) -> NativeObservation:
    return NativeObservation("awaiting", reason, worker, status, None, None, "unknown")


def _show(worker: WorkerReference, payload: object) -> tuple[Mapping[str, object], NativeObservation | None]:
    reply = result_fields(payload)
    dispatch = _fields(reply.get("dispatch"))
    if not dispatch:
        return {}, _awaiting(worker, "snapshot-unavailable")
    host_scope = _fields(dispatch.get("hostScope"))
    if (
        reply.get("server") is not None
        or reply.get("coordinatedFrom") is not None
        or (host_scope and host_scope.get("kind") not in ("local", "wsl"))
    ):
        return {}, NativeObservation("refused", "remote-mismatch", worker, None, None, None, "unknown")
    if dispatch.get("status") == "circuit_broken":
        return {}, NativeObservation("refused", "circuit-broken", worker, None, None, None, "unknown")
    if (dispatch.get("runId"), dispatch.get("taskId"), dispatch.get("id")) != (
        worker.run_id,
        worker.task_id,
        worker.dispatch_id,
    ):
        return reply, NativeObservation("refused", "task-mismatch", worker, None, None, None, "unknown")
    shown_worker = _fields(reply.get("worker"))
    if shown_worker.get("dispatchId") != worker.dispatch_id:
        return reply, _awaiting(worker, "missing-evidence")
    return reply, None


def _agent_liveness(reply: Mapping[str, object]) -> Mapping[str, object]:
    return _fields(_fields(reply.get("projection")).get("liveness"))


def _exited(worker: WorkerReference, reply: Mapping[str, object]) -> bool:
    observation = _fields(reply.get("observation"))
    liveness = _agent_liveness(reply)
    if liveness.get("verdict") != "exited" or observation.get("status") == "live":
        return False
    # A structured session can have no process but still restart on mail.
    if (
        observation.get("exactWorker") is True
        and observation.get("status") == "exited"
        and observation.get("addressable") is False
    ):
        return True

    # A released PTY disappears from terminal inspection. The fleet projection
    # and the Dispatch's own resource row retain the proof after the pane closes.
    resource = _fields(reply.get("terminalResource"))
    return (
        resource.get("ownerDispatchId") == worker.dispatch_id
        and resource.get("releaseState") == "released"
        and liveness.get("source") in ("resource_release", "worker_stop")
    )


def _active(worker: WorkerReference, reply: Mapping[str, object]) -> NativeObservation:
    observation = _fields(reply.get("observation"))
    if "agentWait" not in observation:
        return _awaiting(worker, "missing-evidence")
    wait = observation["agentWait"]
    if wait is not None and _fields(wait).get("source") not in _WAIT_SOURCES:
        return _awaiting(worker, "malformed-snapshot")
    if (
        observation.get("exactWorker") is not True
        or observation.get("status") != "live"
        or _agent_liveness(reply).get("verdict") != "live"
    ):
        return _awaiting(worker, "containment-unconfirmed")
    status = "running" if wait is None else "waiting"
    return NativeObservation("active", "native-active", worker, status, None, None, "active")


def _matching_report_id(worker: WorkerReference, message: Mapping[str, object], outcome: str) -> str | None:
    message_id = message.get("id")
    raw = message.get("payload")
    if (
        message.get("type") != "worker_done"
        or message.get("run_id") != worker.run_id
        or message.get("delivery_contract") == "audit_only"
        or not valid_id(message_id, "msg")
        or not isinstance(raw, str)
        or len(raw) > 65_536
    ):
        return None
    try:
        report = _fields(json.loads(raw))
    except (ValueError, RecursionError):
        return None
    # Orca keeps rejected worker_done rows in the mailbox with their original type.
    if "_orcaLifecycleRejection" in report or (
        report.get("dispatchId"),
        report.get("taskId"),
        report.get("outcome"),
    ) != (worker.dispatch_id, worker.task_id, outcome):
        return None
    return message_id


def _report_id(worker: WorkerReference, worker_done: object, status: str) -> str | None:
    # Use the coordinator's non-consuming inbox --full output. The full view keeps
    # each message's Run identity alongside the worker-provided payload.
    messages = result_fields(worker_done).get("messages")
    if not isinstance(messages, list):
        return None
    outcome = _TERMINAL_WORKER[status]
    matches = {
        report_id
        for message in messages
        if (report_id := _matching_report_id(worker, _fields(message), outcome)) is not None
    }
    # Every match already agrees on Dispatch, Task, Run and outcome. Orca accepts a
    # repeated worker_done with the same outcome as a duplicate without marking it
    # rejected (worker-report-settlement.ts at v1.4.268), so two matches are one
    # report sent twice. Take the smallest id so the choice does not depend on
    # mailbox order.
    return min(matches) if matches else None


def _ends_in_assistant_text(message: object) -> bool:
    row = _fields(message)
    blocks = row.get("blocks")
    if row.get("role") != "assistant" or not isinstance(blocks, list) or not blocks:
        return False
    block = _fields(blocks[-1])
    text = block.get("text")
    return block.get("type") == "text" and isinstance(text, str) and bool(text.strip())


def _transcript_digest(worker: WorkerReference, payload: object, worker_state: str) -> tuple[str, bool] | None:
    pages = payload if isinstance(payload, list) else [payload]
    if not 1 <= len(pages) <= _MAX_TRANSCRIPT_PAGES:
        return None
    messages: list[object] = []
    cursors: set[str] = set()
    source_identity: str | None = None
    clipped = False
    for index, envelope in enumerate(pages, 1):
        reply = result_fields(envelope)
        page = _fields(reply.get("transcript"))
        source = reply.get("sourceIdentity")
        page_messages = page.get("messages")
        more = index < len(pages)
        complete = reply.get("contentComplete")
        clipping = reply.get("clipping")
        if (
            _fields(envelope).get("error") is not None
            or reply.get("dispatchId") != worker.dispatch_id
            or reply.get("source") != "transcript"
            or reply.get("sourceExact") is not True
            or reply.get("fallbackReason") is not None
            or _fields(reply.get("status")).get("worker") != worker_state
            or not isinstance(source, str)
            or not source
            or (source_identity is not None and source_identity != source)
            or not isinstance(page_messages, list)
            or page.get("limited") is not more
            or type(complete) is not bool
            or (not more and not complete)
            or type(page.get("returnedMessageCount")) is not int
            or page.get("returnedMessageCount") != len(page_messages)
        ):
            return None
        if clipping == ["transcript_payload"] and "fallbackReason" in reply:
            clipped = True
        elif clipping not in (None, []):
            return None
        if more:
            cursor = page.get("nextCursor")
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                return None
            cursors.add(cursor)
        source_identity = source
        messages.extend(page_messages)
    if not messages or (clipped and not _ends_in_assistant_text(messages[-1])):
        return None
    try:
        return digest({"source": source_identity, "messages": messages}, _RESULT_DOMAIN), clipped
    except (ValueError, TypeError, RecursionError):
        return None


def classify_show(
    worker: WorkerReference,
    payload: object,
    *,
    worker_done: object = None,
    transcript: object = None,
) -> NativeObservation:
    """Use a correlated show reply and optional completion evidence without retaining raw content."""
    reply, refusal = _show(worker, payload)
    if refusal is not None:
        return refusal
    if _fields(reply.get("worker")).get("state") in (
        "start_unknown",
        "outcome_unknown",
        "stop_unknown",
        "unsupervised",
        "abandoned",
    ):
        return _awaiting(worker, "acceptance-uncertain")
    status = _fields(reply.get("dispatch")).get("status")
    if status == "pending":
        return _awaiting(worker, "pending-work", "queued")
    if status == "dispatched":
        return _active(worker, reply)
    if not isinstance(status, str) or status not in _TERMINAL_WORKER:
        return _awaiting(worker, "work-state-inconsistent")
    if not _exited(worker, reply):
        return _awaiting(worker, "containment-unconfirmed")
    worker_state = _TERMINAL_WORKER[status]
    if _fields(reply.get("worker")).get("state") != worker_state:
        return _awaiting(worker, "terminal-status-mismatch")
    report_id = _report_id(worker, worker_done, status)
    evidence = _transcript_digest(worker, transcript, worker_state)
    if report_id is None or evidence is None:
        return _awaiting(worker, "result-transfer-missing")
    artifact, clipped = evidence
    transfer = digest(
        {"handle": worker.handle_digest, "status": status, "report": report_id, "artifact": artifact}, _RESULT_DOMAIN
    )
    return NativeObservation(
        "terminal-candidate",
        "terminal-evidence-complete",
        worker,
        status,
        worker.run_id,
        transfer,
        "quiescent",
        transcript_clipped=clipped,
    )


def settled_start(worker: WorkerReference, payload: object) -> bool:
    """A later exact show can confirm that the original unknown start recovered."""
    reply, refusal = _show(worker, payload)
    if refusal is not None:
        return False
    state = _fields(reply.get("worker")).get("state")
    dispatch = _fields(reply.get("dispatch")).get("status")
    if dispatch == "dispatched" and state == "ready":
        return _active(worker, reply).family == "active"
    return (
        isinstance(dispatch, str)
        and dispatch in _TERMINAL_WORKER
        and state == _TERMINAL_WORKER[dispatch]
        and _exited(worker, reply)
    )


def resource_state(worker: WorkerReference, payload: object, *, retained_status: str | None) -> str | None:
    """Project only bounded resource tokens from a correlated full show reply."""
    reply, refusal = _show(worker, payload)
    if refusal is not None:
        return None
    shown_worker = _fields(reply.get("worker"))
    if shown_worker.get("state") == "abandoned":
        return "abandoned-unknown"
    resource = _fields(reply.get("terminalResource"))
    if resource.get("ownerDispatchId") != worker.dispatch_id:
        return "release-unverified"
    if resource.get("releaseState") == "released":
        expected = "stopped" if retained_status == "cancelled" else _TERMINAL_WORKER.get(retained_status or "")
        expected_dispatch = "completed" if retained_status == "completed" else "failed"
        if (
            expected is not None
            and shown_worker.get("state") == expected
            and _fields(reply.get("dispatch")).get("status") == expected_dispatch
            and resource.get("ownershipState") == "released"
            and _exited(worker, reply)
        ):
            return "released"
        return "release-unverified"
    if resource.get("retainedReason") in ("user_kept", "user_requested"):
        return "kept-open"
    handle = resource.get("terminalHandle")
    if (
        isinstance(handle, str)
        and handle.startswith("structworker_")
        and handle == shown_worker.get("agentTerminalHandle")
    ):
        return "structured-tab-open"
    return "worker-not-released"


def failed_start_released(worker: WorkerReference, payload: object, residuals: list[object]) -> set[str]:
    """Return matched residual kinds when the worker's release and exit are proven."""
    reply, refusal = _show(worker, payload)
    if refusal is not None and refusal.reason in ("remote-mismatch", "circuit-broken"):
        raise NativeChildError(refusal.reason, "Orca refused the failed-start worker snapshot")
    dispatch = _fields(reply.get("dispatch"))
    shown_worker = _fields(reply.get("worker"))
    resource = _fields(reply.get("terminalResource"))
    identities = (
        (dispatch.get("runId"), worker.run_id),
        (dispatch.get("taskId"), worker.task_id),
        (dispatch.get("id"), worker.dispatch_id),
        (shown_worker.get("dispatchId"), worker.dispatch_id),
        (resource.get("ownerDispatchId"), worker.dispatch_id),
    )
    if any(actual is not None and actual != expected for actual, expected in identities):
        raise NativeChildError("native-mismatch", "the Orca worker-show does not match the start receipt")
    if refusal is not None or resource_state(worker, payload, retained_status="failed") != "released":
        return set()

    # The released terminal's row also identifies the worktree it ran in.
    handle = resource.get("terminalHandle")
    if not isinstance(handle, str) or not handle:
        return set()
    resource_ids = {"terminal": handle, "worktree": resource.get("worktreeId")}
    matched: set[str] = set()
    for entry in residuals:
        residual = _fields(entry)
        kind, residual_id = residual.get("kind"), residual.get("id")
        if (
            not isinstance(kind, str)
            or not isinstance(residual_id, str)
            or not residual_id
            or resource_ids.get(kind) != residual_id
        ):
            return set()
        matched.add(kind)
    return matched


def parse_stop(
    worker: WorkerReference,
    payload: object,
    *,
    exit_code: int,
    show: object = None,
) -> NativeObservation:
    """Treat a close request as unconfirmed until the correlated worker is proven stopped."""
    stop = result_fields(payload)
    if stop.get("dispatchId") not in (None, worker.dispatch_id):
        return NativeObservation("refused", "task-mismatch", worker, None, None, None, "unknown")
    if (
        exit_code != 0
        or stop.get("dispatchId") != worker.dispatch_id
        or _fields(stop.get("mutation")).get("requestId") != cancel_key(worker)
        or stop.get("state") != "stopped"
        or stop.get("processAction") not in ("none", "closed_agent_terminal")
        or type(stop.get("alreadySettled")) is not bool
        or stop.get("helperStopError")
    ):
        return _awaiting(worker, "containment-unconfirmed")
    helpers = stop.get("helpers", [])
    if not isinstance(helpers, list) or any(
        _fields(helper).get("outcome") not in ("stopped", "already_finished") for helper in helpers
    ):
        return _awaiting(worker, "containment-unconfirmed")
    reply, refusal = _show(worker, show)
    if refusal is not None:
        return refusal
    if (
        _fields(reply.get("worker")).get("state") != "stopped"
        or _fields(reply.get("dispatch")).get("status") != "failed"
        or not _exited(worker, reply)
    ):
        return _awaiting(worker, "containment-unconfirmed")
    transfer = digest({"handle": worker.handle_digest, "outcome": "cancelled"}, _RESULT_DOMAIN)
    return NativeObservation(
        "terminal-candidate", "terminal-evidence-complete", worker, "cancelled", worker.run_id, transfer, "quiescent"
    )
