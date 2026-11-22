"""Optional batch synthesis: one budgeted, receipted call over settled task results.

Synthesis is a separate fact from task execution. It never changes a task's
status or result. One accepted batch runs at most one synthesis call: the
reservation is recorded before the call, and a retry that finds the reservation
without a sidecar reports ``indeterminate`` instead of calling again. Synthesis
also waits until every accepted task is terminal: a fail-fast run that leaves
tasks queued records ``not-run`` with reason ``tasks-unfinished``, never reserves
a dispatch unit, and never calls.

Both receipts are read and written as untrusted local state: a symlinked, malformed,
oversized, or otherwise non-regular ``batch-synthesis.json`` or
``batch-synthesis-dispatch.json`` fails closed. It never reserves budget and never
reaches the model, because a path an attacker controls must not be able to turn a
retry into a second paid call.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, TypeGuard

from . import agents, localio, run_budget, run_journal, run_lifecycle, run_tasks
from .run_transport import WorkerResult

SCHEMA = "brigade.batch_synthesis.v1"
SIDECAR = "batch-synthesis.json"
DISPATCH_MARKER = "batch-synthesis-dispatch.json"
OUTPUT_REF = "final.txt"

COMPLETED = "completed"
FAILED = "failed"
REFUSED_BUDGET = "refused-budget"
INDETERMINATE = "indeterminate"
# The one non-final status: synthesis waited because a task was not terminal yet.
NOT_RUN = "not-run"

STATUSES = frozenset({COMPLETED, FAILED, REFUSED_BUDGET, INDETERMINATE, NOT_RUN})

# Closed reasons for a non-completed synthesis fact. Nothing free-form is stored.
REASON_SIDECAR_INVALID = "sidecar-invalid"
REASON_MARKER_INVALID = "marker-invalid"
REASON_RESERVATION_HELD = "reservation-held"
REASON_BUDGET_UNAVAILABLE = "budget-unavailable"
REASON_SEAT_UNAVAILABLE = "seat-unavailable"
REASON_CALL_FAILED = "call-failed"
REASON_OUTPUT_CONFLICT = "output-conflict"
REASON_OUTPUT_UNWRITTEN = "output-write-failed"
REASON_SYNTHESIS_ERROR = "synthesis-error"
REASON_TASKS_UNFINISHED = "tasks-unfinished"

REASONS = frozenset(
    {
        REASON_SIDECAR_INVALID,
        REASON_MARKER_INVALID,
        REASON_RESERVATION_HELD,
        REASON_BUDGET_UNAVAILABLE,
        REASON_SEAT_UNAVAILABLE,
        REASON_CALL_FAILED,
        REASON_OUTPUT_CONFLICT,
        REASON_OUTPUT_UNWRITTEN,
        REASON_SYNTHESIS_ERROR,
        REASON_TASKS_UNFINISHED,
    }
)

# A bounded error class, never the message text: an exception's type is closed.
ERROR_CLASSES = frozenset({"os-error", "budget-error", "journal-error", "value-error", "unexpected-error"})
ERROR_OS = "os-error"
ERROR_BUDGET = "budget-error"
ERROR_JOURNAL = "journal-error"
ERROR_VALUE = "value-error"
ERROR_UNEXPECTED = "unexpected-error"

# Retry classification for the synthesis-only path.
PENDING_NONE = "none"
PENDING_DONE = "done"
PENDING_INDETERMINATE = "indeterminate"
PENDING_RUN = "run"
PENDING_BLOCKED = "blocked"

# A task that reached one of these states can no longer change, so a retry may
# synthesize from the stored results. A queued task means the batch never
# settled every member, so a retry does not synthesize.
_TERMINAL = frozenset({"completed", "failed", "cancelled", "unknown"})

_SIDECAR_MAX_BYTES = 64 * 1024
_MARKER_MAX_BYTES = 4 * 1024

# Read outcomes for the two receipts: absent, a valid closed document, or a path
# that exists but must never be trusted.
_ABSENT = "absent"
_OK = "ok"
_INVALID = "invalid"

_SIDECAR_KEYS = frozenset(
    {
        "schema",
        "request_id",
        "seat",
        "status",
        "inputs",
        "output_ref",
        "output_sha256",
        "output_bytes",
        "reason",
        "error_class",
    }
)
_MARKER_KEYS = frozenset({"seat", "attempt"})
_INPUT_KEYS = frozenset({"task_id", "result_id"})
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Context:
    """Everything one synthesis call needs, owned by the enclosing run."""

    roster: Any
    budget: run_budget.BudgetCoordinator | None
    record_dispatch: Callable[[str, Any, int | None], int | None]
    ground_truth: Mapping[str, Any]
    cwd: Path | None
    codex_transport: str | None
    process_registry: Any
    model_lease: Any


@dataclass(frozen=True)
class Outcome:
    status: str
    seat: str | None
    output_ref: str | None = None
    output_sha256: str | None = None
    output_bytes: int | None = None
    text: str | None = None
    reason: str | None = None


class _MarkerConflict(Exception):
    """A second reservation tried to publish over the existing dispatch marker."""


def error_class(exc: BaseException) -> str:
    """The closed class of an unexpected synthesis failure, never its message."""
    if isinstance(exc, run_budget.BudgetError):
        return ERROR_BUDGET
    if isinstance(exc, (run_journal.RunJournalError, run_tasks.TaskJournalError)):
        return ERROR_JOURNAL
    if isinstance(exc, OSError):
        return ERROR_OS
    if isinstance(exc, ValueError):
        return ERROR_VALUE
    return ERROR_UNEXPECTED


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _read_bounded(path: Path, limit: int) -> tuple[str, bytes | None]:
    """Read one regular, bounded file without following a symlinked final component."""
    try:
        fd = run_journal._open_nofollow(path, os.O_RDONLY)
    except FileNotFoundError:
        return _ABSENT, None
    except (OSError, run_journal.RunJournalError):
        return _INVALID, None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return _INVALID, None
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining > 0:
            chunk = os.read(fd, min(limit, remaining))
            if not chunk:
                return _INVALID, None
            chunks.append(chunk)
            remaining -= len(chunk)
        return _OK, b"".join(chunks)
    except OSError:
        return _INVALID, None
    finally:
        os.close(fd)


def _parse_object(raw: bytes) -> dict[str, Any] | None:
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _valid_inputs(value: Any) -> bool:
    if not isinstance(value, list) or len(value) > 64:
        return False
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != _INPUT_KEYS:
            return False
        if not isinstance(entry.get("task_id"), str) or not entry["task_id"]:
            return False
        result_id = entry.get("result_id")
        if result_id is not None and not isinstance(result_id, str):
            return False
    return True


def _valid_sidecar(document: Mapping[str, Any]) -> bool:
    """True only for the exact closed sidecar this module writes."""
    if set(document) != _SIDECAR_KEYS:
        return False
    if document.get("schema") != SCHEMA:
        return False
    if not isinstance(document.get("request_id"), str) or not document["request_id"]:
        return False
    if not isinstance(document.get("seat"), str):
        return False
    status = document.get("status")
    if status not in STATUSES:
        return False
    if not _valid_inputs(document.get("inputs")):
        return False
    reason = document.get("reason")
    if reason is not None and reason not in REASONS:
        return False
    failure_class = document.get("error_class")
    if failure_class is not None:
        if failure_class not in ERROR_CLASSES:
            return False
        if status != INDETERMINATE or reason != REASON_SYNTHESIS_ERROR:
            return False
    ref = document.get("output_ref")
    digest = document.get("output_sha256")
    size = document.get("output_bytes")
    if ref is not None and ref != OUTPUT_REF:
        return False
    if digest is not None and (not isinstance(digest, str) or _SHA256.fullmatch(digest) is None):
        return False
    if size is not None and (not _is_int(size) or size < 0):
        return False
    if status == COMPLETED:
        # A completed fact owns its output reference and digest together.
        return ref == OUTPUT_REF and digest is not None and size is not None and reason is None
    return ref is None and digest is None and size is None and reason is not None


def _read_sidecar(run_dir: Path) -> tuple[str, dict[str, Any] | None, bytes | None]:
    state, raw = _read_bounded(run_dir / SIDECAR, _SIDECAR_MAX_BYTES)
    if state != _OK or raw is None:
        return state, None, None
    document = _parse_object(raw)
    if document is None or not _valid_sidecar(document):
        return _INVALID, None, raw
    return _OK, document, raw


def _read_marker(run_dir: Path) -> tuple[str, dict[str, Any] | None]:
    state, raw = _read_bounded(run_dir / DISPATCH_MARKER, _MARKER_MAX_BYTES)
    if state != _OK:
        return state, None
    document = _parse_object(raw) if raw is not None else None
    if document is None or set(document) != _MARKER_KEYS:
        return _INVALID, None
    seat = document.get("seat")
    attempt = document.get("attempt")
    if not isinstance(seat, str) or not seat or not _is_int(attempt) or attempt < 1:
        return _INVALID, None
    return _OK, document


def _read_states(run_dir: Path, request_id: str) -> dict[str, run_tasks.TaskResult]:
    try:
        return {
            state.handle.spec.task_id: state
            for state in run_tasks.read_task_batch(run_dir, request_id=request_id).members
        }
    except run_tasks.TaskJournalError:
        return {}


def _inputs(plan: Mapping[str, Any], run_dir: Path) -> list[dict[str, Any]]:
    states = _read_states(run_dir, str(plan["request_id"]))
    return [
        {
            "task_id": task["task_id"],
            "result_id": states[task["task_id"]].result_id if task["task_id"] in states else None,
        }
        for task in plan["tasks"]
    ]


def _all_terminal(plan: Mapping[str, Any], run_dir: Path) -> bool:
    states = _read_states(run_dir, str(plan["request_id"]))
    if len(states) != len(plan["tasks"]):
        return False
    return all(
        (state := states.get(task["task_id"])) is not None and state.status in _TERMINAL for task in plan["tasks"]
    )


def _unfinished(payload: Mapping[str, Any] | None) -> bool:
    """True for the one non-final fact: synthesis waited on a task that was not terminal."""
    return payload is not None and payload.get("status") == NOT_RUN and payload.get("reason") == REASON_TASKS_UNFINISHED


def read(run_dir: Path) -> dict[str, Any] | None:
    """The valid batch synthesis sidecar, or None when absent or not closed."""
    state, payload, _raw = _read_sidecar(run_dir)
    return payload if state == _OK else None


def _summary_reason(run_dir: Path) -> tuple[str, str]:
    """The receipt that poisons this retry and its closed reason."""
    state, _payload, _raw = _read_sidecar(run_dir)
    if state == _INVALID:
        return SIDECAR, REASON_SIDECAR_INVALID
    if _read_marker(run_dir)[0] == _INVALID:
        return DISPATCH_MARKER, REASON_MARKER_INVALID
    return DISPATCH_MARKER, REASON_RESERVATION_HELD


def summary(run_dir: Path, *, seat: str | None = None) -> dict[str, Any] | None:
    """The additive batch-result projection of the sidecar, with its own digest."""
    state, payload, raw = _read_sidecar(run_dir)
    if state == _OK and payload is not None and raw is not None:
        return {
            "status": payload["status"],
            "seat": payload["seat"],
            "ref": SIDECAR,
            "digest": hashlib.sha256(raw).hexdigest(),
            "reason": payload["reason"],
        }
    if state == _INVALID:
        # The sidecar path exists but is untrusted: fail closed, so a retry reads
        # this as a call that may already have run.
        return {"status": INDETERMINATE, "seat": seat, "ref": SIDECAR, "digest": None, "reason": REASON_SIDECAR_INVALID}
    marker_state, marker = _read_marker(run_dir)
    if marker_state == _INVALID:
        return {
            "status": INDETERMINATE,
            "seat": seat,
            "ref": DISPATCH_MARKER,
            "digest": None,
            "reason": REASON_MARKER_INVALID,
        }
    if marker_state == _OK and marker is not None:
        # A durable reservation without a sidecar means the call may have run.
        return {
            "status": INDETERMINATE,
            "seat": marker.get("seat") or seat,
            "ref": DISPATCH_MARKER,
            "digest": None,
            "reason": REASON_RESERVATION_HELD,
        }
    return None


def marker_present(run_dir: Path) -> bool:
    """True when anything occupies the dispatch-marker path, valid or not."""
    return _read_marker(run_dir)[0] != _ABSENT


def pending(plan: Mapping[str, Any], run_dir: Path) -> str:
    """Classify what a retry owes an accepted batch's synthesis request."""
    if plan.get("synthesis") is None:
        return PENDING_NONE
    state, payload, _raw = _read_sidecar(run_dir)
    if state == _OK and not _unfinished(payload):
        return PENDING_DONE
    if state == _INVALID:
        # An untrusted sidecar may hide a completed call; never reserve again.
        return PENDING_INDETERMINATE
    if marker_present(run_dir):
        return PENDING_INDETERMINATE
    # No sidecar, or the non-final not-run fact that reserved nothing: a retry
    # whose journal shows every accepted task terminal still owes the one call.
    return PENDING_RUN if _all_terminal(plan, run_dir) else PENDING_BLOCKED


def _write_exclusive(path: Path, payload: Mapping[str, Any]) -> bool:
    """Create one receipt exactly once; never overwrite an existing fact."""
    try:
        localio.write_json_exclusive(path, dict(payload))
    except FileExistsError:
        return False
    run_journal._fsync_directory(path.parent)
    return True


def _write_output(path: Path, data: bytes) -> bool:
    """Publish final bytes once, as bytes, without following an existing entry."""
    try:
        localio.write_bytes_exclusive(path, data)
    except FileExistsError:
        return False
    run_journal._fsync_directory(path.parent)
    return True


def _outcome(payload: Mapping[str, Any], *, text: str | None = None) -> Outcome:
    status = str(payload.get("status") or FAILED)
    return Outcome(
        status=status,
        seat=payload.get("seat") if isinstance(payload.get("seat"), str) else None,
        output_ref=payload.get("output_ref") if isinstance(payload.get("output_ref"), str) else None,
        output_sha256=payload.get("output_sha256") if isinstance(payload.get("output_sha256"), str) else None,
        output_bytes=payload.get("output_bytes") if _is_int(payload.get("output_bytes")) else None,
        text=text,
        reason=payload.get("reason") if isinstance(payload.get("reason"), str) else None,
    )


def _write_sidecar(
    plan: Mapping[str, Any],
    run_dir: Path,
    seat: str,
    status: str,
    *,
    reason: str | None = None,
    error: str | None = None,
    text: str | None = None,
    **output: Any,
) -> Outcome:
    payload = {
        "schema": SCHEMA,
        "request_id": plan["request_id"],
        "seat": seat,
        "status": status,
        "inputs": _inputs(plan, run_dir),
        "output_ref": output.get("output_ref"),
        "output_sha256": output.get("output_sha256"),
        "output_bytes": output.get("output_bytes"),
        "reason": reason,
        "error_class": error,
    }
    if _write_exclusive(run_dir / SIDECAR, payload):
        return _outcome(payload, text=text)
    # The existing fact wins. It is re-read and validated, so a sidecar this
    # process did not write can never be reported as the outcome it wanted.
    state, existing, _raw = _read_sidecar(run_dir)
    if state == _OK and existing is not None:
        return _outcome(existing)
    return Outcome(status=INDETERMINATE, seat=seat, reason=REASON_SIDECAR_INVALID)


def write_indeterminate(plan: Mapping[str, Any], run_dir: Path, *, reason: str | None = None) -> Outcome:
    """Record that a reserved call may have run; never call again."""
    synthesis = plan.get("synthesis") or {}
    seat = str(synthesis.get("seat") or "")
    if reason is None:
        reason = _summary_reason(run_dir)[1]
    return _write_sidecar(plan, run_dir, seat, INDETERMINATE, reason=reason)


def write_error_indeterminate(plan: Mapping[str, Any], run_dir: Path, exc: BaseException) -> Outcome:
    """Record an unexpected failure as a closed class, never the exception text."""
    synthesis = plan.get("synthesis") or {}
    seat = str(synthesis.get("seat") or "")
    return _write_sidecar(
        plan,
        run_dir,
        seat,
        INDETERMINATE,
        reason=REASON_SYNTHESIS_ERROR,
        error=error_class(exc),
    )


def _write_marker(run_dir: Path, seat: str, attempt: int) -> bool:
    # The reservation identity is allocated once; an existing marker is the
    # fact a retry reads, so leave it exactly as written.
    return _write_exclusive(run_dir / DISPATCH_MARKER, {"seat": seat, "attempt": attempt})


def finish_batch(
    batch: Any,
    results: list[WorkerResult],
    *,
    output_dir: Path | None,
    roster: Any,
    budget: run_budget.BudgetCoordinator | None,
    record_dispatch: Callable[[str, Any, int | None], int | None],
    ground_truth: Mapping[str, Any],
    cwd: Path | None,
    codex_transport: str | None,
    process_registry: Any,
    model_lease: Any,
) -> agents.AgentResult:
    """Finish a batch run: its task summary plus its one optional synthesis call."""
    context = (
        batch.synthesis_context(
            roster=roster,
            budget=budget,
            record_dispatch=record_dispatch,
            ground_truth=ground_truth,
            cwd=cwd,
            codex_transport=codex_transport,
            process_registry=process_registry,
            model_lease=model_lease,
        )
        if output_dir is not None
        else None
    )
    return batch.finish(results, synthesis=context)


def published_final(batch: Any) -> bool:
    """True when a batch's completed synthesis already published final.txt."""
    return batch is not None and batch.synthesis_text is not None


def synthesis_retry(batch: Any) -> bool:
    """True on a retry that owes only its accepted batch's synthesis call."""
    return batch is not None and bool(batch.synthesis_only)


def _clear_unfinished(run_dir: Path) -> bool:
    """Remove the non-final not-run fact so the owed call can record its own.

    Only a sidecar this module validated as ``not-run``/``tasks-unfinished``
    reaches here. It reserved no budget and made no call, so clearing it cannot
    allow a second call. Unlinking never follows a symlink, and a crash before
    the replacement sidecar is written leaves the batch with no final fact, which
    a retry reads as the still-owed call.
    """
    try:
        os.unlink(run_dir / SIDECAR)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    run_journal._fsync_directory(run_dir)
    return True


def _call(
    plan: Mapping[str, Any], run_dir: Path, results: list[WorkerResult], context: Context, seat: str
) -> tuple[Any, bool]:
    from . import message_envelope
    from .aboyeur import planning, prompts

    agent = context.roster.agents[seat]
    prompt = prompts.build_synth_prompt(
        plan["synthesis"]["text"],
        results,
        read_only=True,
        ground_truth=dict(context.ground_truth),
        run_id=run_dir.name,
        to_seat=seat,
    )
    harness = agent.cli
    request = message_envelope.emit(
        prompt,
        kind="synthesis-request",
        producer="aboyeur.build_synth_prompt",
        from_seat=message_envelope.BRIGADE_SEAT,
        to_seat=seat,
        run_dir=run_dir,
        run_id=run_dir.name,
        assignment_id=message_envelope.default_assignment_id("synthesis-request"),
        session_harness=harness,
    )
    if not request.delivered:
        return (
            agents.AgentResult(
                text="",
                ok=False,
                detail=request.reason or "synthesis-request rejected by provenance gate",
                failure_phase="synthesis",
                failure_kind="unclassified",
            ),
            False,
        )
    result = planning._call_with_process_registry(
        planning._run_orchestrator,
        context.roster,
        prompt,
        cwd=context.cwd,
        read_only=True,
        codex_transport=context.codex_transport,
        process_registry=context.process_registry,
        model_lease=context.model_lease,
        seat=seat,
    )
    delivered = message_envelope.emit(
        result.text,
        kind="synthesis-result",
        producer="aboyeur.run",
        from_seat=seat,
        to_seat=message_envelope.BRIGADE_SEAT,
        run_dir=run_dir,
        run_id=run_dir.name,
        assignment_id=message_envelope.default_assignment_id("synthesis-result"),
        session_harness=harness,
    )
    if delivered.delivered:
        return result, True
    return (
        replace(
            result,
            text="",
            ok=False,
            detail=delivered.reason or "synthesis-result rejected by provenance gate",
            failure_phase=result.failure_phase or "synthesis",
            failure_kind=result.failure_kind or "unclassified",
        ),
        False,
    )


def _final_bytes(text: str) -> bytes:
    return (text + "\n").encode("utf-8")


def _publish_output(run_dir: Path, text: str) -> Outcome | None:
    """Write final.txt once and return an outcome only when it cannot stand."""
    data = _final_bytes(text)
    try:
        created = _write_output(run_dir / OUTPUT_REF, data)
    except OSError:
        return Outcome(status=FAILED, seat=None, reason=REASON_OUTPUT_UNWRITTEN)
    if created:
        return None
    # An existing final.txt is only acceptable when it already holds exactly
    # these bytes; otherwise the recorded digest would describe a different file.
    state, existing = _read_bounded(run_dir / OUTPUT_REF, max(len(data), 1))
    if state == _OK and existing == data:
        return None
    return Outcome(status=FAILED, seat=None, reason=REASON_OUTPUT_CONFLICT)


def perform(
    *,
    run_dir: Path,
    plan: Mapping[str, Any],
    results: list[WorkerResult],
    context: Context,
) -> Outcome:
    """Reserve once, call once, and record the separate synthesis fact."""
    synthesis = plan.get("synthesis")
    if synthesis is None:
        return Outcome(status=PENDING_NONE, seat=None)
    seat = str(synthesis["seat"])

    state, existing, _raw = _read_sidecar(run_dir)
    if state == _INVALID:
        # The sidecar path is occupied by something untrusted. Do not reserve
        # budget and do not call: the run cannot prove the call never ran.
        return Outcome(status=INDETERMINATE, seat=seat, reason=REASON_SIDECAR_INVALID)
    if state == _OK and existing is not None and not _unfinished(existing):
        return _outcome(existing)

    marker_state, marker = _read_marker(run_dir)
    if marker_state == _INVALID:
        return write_indeterminate(plan, run_dir, reason=REASON_MARKER_INVALID)
    if marker_state == _OK and marker is not None:
        # A reserved dispatch without a sidecar means the call may have run.
        return write_indeterminate(plan, run_dir, reason=REASON_RESERVATION_HELD)

    if not _all_terminal(plan, run_dir):
        # Fail-fast can leave later tasks queued and never launched. Synthesis
        # runs only after every accepted task is terminal, so record why, and
        # reserve no dispatch unit and make no call. This fact is non-final: it
        # reserved nothing, so a retry that settles the remaining tasks still
        # owes the one call.
        return _write_sidecar(plan, run_dir, seat, NOT_RUN, reason=REASON_TASKS_UNFINISHED)

    if state == _OK and existing is not None:
        # The recorded not-run fact is non-final. Clear it before the owed call
        # so the sidecar this call writes is its own outcome, never the stale one.
        if not _clear_unfinished(run_dir):
            return _outcome(existing)

    if context.budget is None:
        # A batch without a coordinator cannot reserve its synthesis unit.
        return _write_sidecar(plan, run_dir, seat, REFUSED_BUDGET, reason=REASON_BUDGET_UNAVAILABLE)

    agent = context.roster.agents.get(seat)
    if agent is None:
        # The seat vanished from the roster after acceptance; no call is made.
        return _write_sidecar(plan, run_dir, seat, FAILED, reason=REASON_SEAT_UNAVAILABLE)

    def record(attempt: int) -> int | None:
        # The marker lands before the requested fact, so a crash between the
        # reservation and the call is still visible to a later retry.
        if not _write_marker(run_dir, seat, attempt):
            raise _MarkerConflict
        return context.record_dispatch("run.dispatch.requested", agent, attempt)

    try:
        context.budget.reserve_and_record_dispatch(
            seat=seat,
            allocate_attempt=lambda: run_lifecycle.allocate_next_dispatch_attempt(run_dir, seat),
            record_requested=record,
            reclaim_pending=False,
        )
    except run_budget.BudgetPolicyError:
        return _write_sidecar(plan, run_dir, seat, REFUSED_BUDGET, reason=REASON_BUDGET_UNAVAILABLE)
    except _MarkerConflict:
        # Another reservation owns this seat's marker; never call over it.
        return write_indeterminate(plan, run_dir, reason=REASON_RESERVATION_HELD)

    final, delivered = _call(plan, run_dir, results, context, seat)
    if not final.ok or not delivered:
        return _write_sidecar(plan, run_dir, seat, FAILED, reason=REASON_CALL_FAILED)

    conflict = _publish_output(run_dir, final.text)
    if conflict is not None:
        return _write_sidecar(plan, run_dir, seat, conflict.status, reason=conflict.reason)
    data = _final_bytes(final.text)
    return _write_sidecar(
        plan,
        run_dir,
        seat,
        COMPLETED,
        text=final.text,
        output_ref=OUTPUT_REF,
        output_sha256=hashlib.sha256(data).hexdigest(),
        output_bytes=len(data),
    )


__all__ = [
    "COMPLETED",
    "DISPATCH_MARKER",
    "ERROR_CLASSES",
    "FAILED",
    "INDETERMINATE",
    "NOT_RUN",
    "Outcome",
    "PENDING_BLOCKED",
    "PENDING_DONE",
    "PENDING_INDETERMINATE",
    "PENDING_NONE",
    "PENDING_RUN",
    "REASONS",
    "REFUSED_BUDGET",
    "SCHEMA",
    "SIDECAR",
    "STATUSES",
    "Context",
    "error_class",
    "finish_batch",
    "marker_present",
    "pending",
    "perform",
    "published_final",
    "read",
    "summary",
    "synthesis_retry",
    "write_error_indeterminate",
    "write_indeterminate",
]
