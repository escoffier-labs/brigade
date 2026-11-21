"""Project Orca 1.4.268 worker observations into the native-child families."""

from __future__ import annotations

import json
from typing import Mapping

from .native_child_journal import digest
from .native_child_observations import NativeObservation
from .native_orca_dispatch import CLI, WorkerReference, mutation_key, result_fields, valid_id

_STOP_DOMAIN = "brigade.native-orca.worker-stop.v1"
_RESULT_DOMAIN = "brigade.native-orca.result.v1"
_WAIT_SOURCES = ("hook", "prompt-text", "title")
_TERMINAL_WORKER = {"completed": "succeeded", "failed": "failed"}


def _dispatch_argv(command: str, worker: WorkerReference) -> tuple[str, ...]:
    return (*CLI, command, "--environment", "local", "--dispatch", worker.dispatch_id)


def observe_argv(worker: WorkerReference) -> tuple[str, ...]:
    # The concise view omits the Dispatch status and changes agentWait's representation.
    return (*_dispatch_argv("worker-show", worker), "--json", "--full")


def _cancel_key(worker: WorkerReference) -> str:
    return mutation_key({"handle": worker.handle_digest}, _STOP_DOMAIN)


def cancel_argv(worker: WorkerReference) -> tuple[str, ...]:
    return (*_dispatch_argv("worker-stop", worker), "--retry-request", _cancel_key(worker), "--json")


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
    if (dispatch.get("runId"), dispatch.get("taskId"), dispatch.get("id")) != (
        worker.run_id,
        worker.task_id,
        worker.dispatch_id,
    ):
        return {}, NativeObservation("refused", "task-mismatch", worker, None, None, None, "unknown")
    host_scope = _fields(dispatch.get("hostScope"))
    if (
        reply.get("server") is not None
        or reply.get("coordinatedFrom") is not None
        or (host_scope and host_scope.get("kind") not in ("local", "wsl"))
    ):
        return {}, NativeObservation("refused", "remote-mismatch", worker, None, None, None, "unknown")
    if dispatch.get("status") == "circuit_broken":
        return {}, NativeObservation("refused", "circuit-broken", worker, None, None, None, "unknown")
    shown_worker = _fields(reply.get("worker"))
    if shown_worker.get("dispatchId") != worker.dispatch_id:
        return {}, _awaiting(worker, "missing-evidence")
    if shown_worker.get("state") in ("start_unknown", "stop_unknown", "unsupervised"):
        return {}, _awaiting(worker, "acceptance-uncertain")
    return reply, None


def _liveness(reply: Mapping[str, object], verdict: str) -> bool:
    observation = _fields(reply.get("observation"))
    projection = _fields(reply.get("projection"))
    agent_liveness = _fields(projection.get("liveness"))
    return (
        observation.get("exactWorker") is True
        and observation.get("status") == verdict
        and agent_liveness.get("verdict") == verdict
    )


def _report_id(worker: WorkerReference, worker_done: object, status: str) -> str | None:
    reply = result_fields(worker_done)
    message = _fields(reply.get("message"))
    lifecycle = _fields(reply.get("lifecycle"))
    message_id = message.get("id")
    raw = message.get("payload")
    if (
        message.get("type") != "worker_done"
        or message.get("run_id") != worker.run_id
        or not valid_id(message_id, "msg")
        or not isinstance(raw, str)
        or len(raw) > 65_536
    ):
        return None
    try:
        report = _fields(json.loads(raw))
    except (ValueError, RecursionError):
        return None
    outcome = _TERMINAL_WORKER[status]
    if (report.get("dispatchId"), report.get("taskId"), report.get("outcome")) != (
        worker.dispatch_id,
        worker.task_id,
        outcome,
    ):
        return None
    if lifecycle.get("action") == "settled":
        return message_id if lifecycle.get("outcome") == outcome else None
    if (lifecycle.get("action"), lifecycle.get("dispatchId"), lifecycle.get("taskId")) != (
        status,
        worker.dispatch_id,
        worker.task_id,
    ):
        return None
    return message_id


def _transcript_digest(worker: WorkerReference, payload: object, worker_state: str) -> str | None:
    reply = result_fields(payload)
    page = _fields(reply.get("transcript"))
    status = _fields(reply.get("status"))
    source = reply.get("sourceIdentity")
    messages = page.get("messages")
    if (
        reply.get("dispatchId") != worker.dispatch_id
        or reply.get("source") != "transcript"
        or reply.get("sourceExact") is not True
        or reply.get("contentComplete") is not True
        or reply.get("fallbackReason") is not None
        or reply.get("clipping") not in (None, [])
        or status.get("worker") != worker_state
        or not isinstance(source, str)
        or not source
        or not isinstance(messages, list)
        or not messages
        or page.get("limited") is not False
        or type(page.get("returnedMessageCount")) is not int
        or page.get("returnedMessageCount") != len(messages)
    ):
        return None
    try:
        return digest({"source": source, "messages": messages}, _RESULT_DOMAIN)
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
    status = _fields(reply.get("dispatch")).get("status")
    observation = _fields(reply.get("observation"))
    if "agentWait" not in observation:
        return _awaiting(worker, "missing-evidence")
    wait = observation["agentWait"]
    if wait is not None:
        if _fields(wait).get("source") not in _WAIT_SOURCES:
            return _awaiting(worker, "malformed-snapshot")
        if not _liveness(reply, "live"):
            return _awaiting(worker, "containment-unconfirmed")
        return NativeObservation("active", "native-active", worker, "waiting", None, None, "active")
    if status == "pending":
        return _awaiting(worker, "pending-work", "queued")
    if status == "dispatched" and _liveness(reply, "live"):
        return NativeObservation("active", "native-active", worker, "running", None, None, "active")
    if not isinstance(status, str) or status not in _TERMINAL_WORKER:
        return _awaiting(worker, "work-state-inconsistent")
    if not _liveness(reply, "exited"):
        return _awaiting(worker, "containment-unconfirmed")
    worker_state = _TERMINAL_WORKER[status]
    if _fields(reply.get("worker")).get("state") != worker_state:
        return _awaiting(worker, "terminal-status-mismatch")
    report_id = _report_id(worker, worker_done, status)
    artifact = _transcript_digest(worker, transcript, worker_state)
    if report_id is None or artifact is None:
        return _awaiting(worker, "result-transfer-missing")
    transfer = digest(
        {"handle": worker.handle_digest, "status": status, "report": report_id, "artifact": artifact}, _RESULT_DOMAIN
    )
    return NativeObservation(
        "terminal-candidate", "terminal-evidence-complete", worker, status, worker.run_id, transfer, "quiescent"
    )


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
        or _fields(stop.get("mutation")).get("requestId") != _cancel_key(worker)
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
        or not _liveness(reply, "exited")
        or _fields(reply.get("observation")).get("agentWait", False) is not None
    ):
        return _awaiting(worker, "containment-unconfirmed")
    transfer = digest({"handle": worker.handle_digest, "outcome": "cancelled"}, _RESULT_DOMAIN)
    return NativeObservation(
        "terminal-candidate", "terminal-evidence-complete", worker, "cancelled", worker.run_id, transfer, "quiescent"
    )
