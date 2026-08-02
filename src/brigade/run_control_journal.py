"""Idempotent live-control requests backed by the lifecycle journal (issue #604).

Wraps app-server steer/interrupt with a caller request identity:

1. Append and fsync ``control.requested`` before contacting the transport.
2. Append ``control.observed`` or ``control.failed`` after the transport returns.
3. Reuse of the same request id with the same fingerprint returns the committed
   terminal result without a second transport call.
4. Reuse with a different fingerprint is a conflict.
5. ``control.requested`` without a terminal observation is ``indeterminate``;
   the transport is not reissued automatically.

Steering text never enters the journal — only a SHA-256 digest. External CLI
callers do not hold the run lock, so this module uses ``append_event`` directly
(status-neutral; no checkpoint pairing) and retries on ``StaleSequenceError``.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from brigade import runguard, run_events, run_journal, run_lifecycle, run_shadow
from brigade.run_events import MAX_DIAGNOSTIC_LEN, MAX_PAYLOAD_STR_LEN

CONTROL_REQUESTED = "control.requested"
CONTROL_OBSERVED = "control.observed"
CONTROL_FAILED = "control.failed"
_TERMINAL_CONTROL_TYPES = frozenset({CONTROL_OBSERVED, CONTROL_FAILED})
_MAX_STALE_RETRIES = 32
_REQUEST_ID_HEX_BYTES = 8


class ControlJournalError(RuntimeError):
    """Bounded control-journal failure with a stable ``code``."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code
        self.diagnostic = (
            message if len(message) <= MAX_DIAGNOSTIC_LEN else message[: MAX_DIAGNOSTIC_LEN - 1] + "\u2026"
        )


class ControlFailureClass(str, Enum):
    TRANSPORT_EXCEPTION = "transport_exception"
    TRANSPORT_REJECTED = "transport_rejected"
    TRANSPORT_PROTOCOL_ERROR = "transport_protocol_error"


@dataclass(frozen=True)
class ControlResult:
    """Outcome of an idempotent control request."""

    request_id: str
    response: dict[str, Any]
    replayed: bool
    requested_event: run_journal.RunEvent | None
    terminal_event: run_journal.RunEvent | None


def generate_request_id() -> str:
    """Return a new opaque request id suitable for ``--request-id``."""
    return secrets.token_hex(_REQUEST_ID_HEX_BYTES)


def text_digest(text: str) -> str:
    """SHA-256 hex digest of steering text (never store the text itself)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def control_fingerprint_payload(
    *,
    op: str,
    request_id: str,
    worker: str | None = None,
    text: str | None = None,
    turn_id: str | None = None,
) -> dict[str, Any]:
    """Build the closed ``control.requested`` payload (reference fields only)."""
    if op not in ("steer", "interrupt"):
        raise ControlJournalError("unsupported control op", code="control_op_invalid")
    if not isinstance(request_id, str) or not request_id:
        raise ControlJournalError("request_id must be a non-empty string", code="request_id_invalid")
    if len(request_id) > MAX_PAYLOAD_STR_LEN:
        raise ControlJournalError("request_id exceeds payload string bound", code="request_id_invalid")
    payload: dict[str, Any] = {
        "op": op,
        "request_id": request_id,
        "text_digest": text_digest(text) if text is not None else "",
    }
    if worker is not None:
        payload["worker"] = worker
    if turn_id is not None:
        payload["turn_id"] = turn_id
    return payload


def requested_idempotency_key(request_id: str) -> str:
    return f"control.requested:{request_id}"


def observed_idempotency_key(request_id: str) -> str:
    return f"control.observed:{request_id}"


def failed_idempotency_key(request_id: str) -> str:
    return f"control.failed:{request_id}"


def _bound(msg: str) -> str:
    if len(msg) <= MAX_DIAGNOSTIC_LEN:
        return msg
    return msg[: MAX_DIAGNOSTIC_LEN - 1] + "\u2026"


def _journal_path(run_dir: Path) -> Path:
    return run_lifecycle._journal_path(run_dir)


def _run_id(run_dir: Path) -> str:
    return run_lifecycle._run_id_from_dir(run_dir)


def journal_present(run_dir: Path) -> bool:
    """True when the run has a regular lifecycle journal file."""
    path = _journal_path(run_dir)
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _read_verified(journal_path: Path) -> list[run_journal.RunEvent]:
    report = run_journal.read_journal_bounded(journal_path)
    if report.partial_tail is not None:
        raise ControlJournalError(
            _bound("lifecycle journal ends in a partial line"),
            code="journal_partial_tail",
        )
    if report.chain_errors:
        raise ControlJournalError(
            _bound(f"lifecycle journal chain error: {report.chain_errors[0]}"),
            code="journal_chain_error",
        )
    return list(report.events)


def _find_control_pair(
    events: list[run_journal.RunEvent],
    *,
    request_id: str,
) -> tuple[run_journal.RunEvent | None, run_journal.RunEvent | None]:
    requested: run_journal.RunEvent | None = None
    terminal: run_journal.RunEvent | None = None
    for event in events:
        payload = event.payload
        if not isinstance(payload, Mapping):
            continue
        if payload.get("request_id") != request_id:
            continue
        if event.event_type == CONTROL_REQUESTED:
            requested = event
        elif event.event_type in _TERMINAL_CONTROL_TYPES:
            terminal = event
    return requested, terminal


def _response_from_terminal(event: run_journal.RunEvent) -> dict[str, Any]:
    payload = event.payload
    if event.event_type == CONTROL_OBSERVED:
        response: dict[str, Any] = {"ok": True, "op": payload.get("op")}
        if isinstance(payload.get("worker"), str):
            response["worker"] = payload["worker"]
        if isinstance(payload.get("turn_id"), str):
            response["turn_id"] = payload["turn_id"]
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.startswith("interrupted="):
            try:
                response["interrupted"] = int(detail.split("=", 1)[1])
            except ValueError:
                response["detail"] = detail
        return response
    response = {"ok": False, "op": payload.get("op"), "error": "control request failed"}
    if isinstance(payload.get("error_class"), str):
        response["error_class"] = payload["error_class"]
    if isinstance(payload.get("detail_digest"), str):
        response["detail_digest"] = payload["detail_digest"]
    if isinstance(payload.get("worker"), str):
        response["worker"] = payload["worker"]
    return response


def _load_run_snapshot(run_dir: Path) -> dict[str, Any] | None:
    try:
        raw = (run_dir / "run.json").read_bytes()
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _preflight_live_control(run_dir: Path) -> None:
    """Reject persisted terminal or unlocked runs before any control mutation."""
    snapshot = _load_run_snapshot(run_dir)
    # Journal-only runs predate persisted control metadata and remain readable
    # for compatibility. A persisted receipt, however, is authoritative.
    if snapshot is None:
        return
    status = snapshot.get("status")
    if (
        not isinstance(status, str)
        or status not in runguard._NONTERMINAL_RUN_STATUSES
        or snapshot.get("finished_at") is not None
    ):
        raise ControlJournalError("control run is not live", code="control_run_not_live")
    if "lock_workspace" not in snapshot:
        return
    workspace = runguard.resolve_run_lock_workspace(snapshot, run_dir)
    if workspace is None or not runguard.has_active_run_owner(workspace, run_dir):
        raise ControlJournalError("control run owner is not active", code="control_owner_not_active")


def _sync_shadow_after_control(run_dir: Path) -> None:
    """Advance shadow evidence to the new journal tail after a control append.

    Authoritative runs gate on shadow ``last_compared_*`` matching the journal
    tail. Status-neutral control events must not leave ``journal-ahead-of-evidence``
    for the run owner. Mirrors the post-append ``record_shadow_comparison`` call
    used by dispatch facts.
    """
    snapshot = _load_run_snapshot(run_dir)
    if snapshot is None:
        return
    run_shadow.record_shadow_comparison(run_dir, snapshot)


def _append_control_event(
    journal_path: Path,
    *,
    run_dir: Path,
    run_id: str,
    event_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
) -> run_journal.RunEvent:
    """Append a control event, retrying on concurrent tail races.

    Holds ``checkpoint_event_pair`` around the append + shadow sync so a control
    line cannot land between an owner's checkpoint and its paired event.
    """
    last_error: Exception | None = None
    for _ in range(_MAX_STALE_RETRIES):
        try:
            with run_lifecycle.checkpoint_event_pair():
                events = _read_verified(journal_path)
                expected = events[-1].sequence if events else 0
                try:
                    event = run_journal.append_event(
                        journal_path,
                        run_id=run_id,
                        event_type=event_type,
                        payload=payload,
                        idempotency_key=idempotency_key,
                        expected_previous_sequence=expected,
                    )
                except run_journal.StaleSequenceError as exc:
                    last_error = exc
                    continue
                except run_journal.IdempotencyConflict as exc:
                    raise ControlJournalError(
                        _bound(exc.diagnostic),
                        code="control_fingerprint_conflict",
                    ) from exc
                except run_events.CanonicalizationError as exc:
                    raise ControlJournalError(_bound(str(exc)), code="control_payload_invalid") from exc
                _sync_shadow_after_control(run_dir)
                return event
        except run_lifecycle.LifecycleJournalError as exc:
            raise ControlJournalError(_bound(str(exc)), code="control_append_failed") from exc
    raise ControlJournalError(
        _bound(f"control append raced the journal tail: {last_error}"),
        code="control_append_race",
    )


def _claim_control_request(
    journal_path: Path,
    *,
    run_dir: Path,
    run_id: str,
    request_id: str,
    fingerprint: Mapping[str, Any],
) -> tuple[str, run_journal.RunEvent | None, run_journal.RunEvent | None]:
    """Inspect and claim ``control.requested`` under one journal lock."""
    with run_lifecycle.checkpoint_event_pair():
        with run_journal.journal_mutation(journal_path):
            state, requested, terminal = _inspect_control_request(
                run_dir,
                request_id=request_id,
                fingerprint=fingerprint,
            )
            if state != "absent":
                return state, requested, terminal
            events = _read_verified(journal_path)
            try:
                requested = run_journal.append_event(
                    journal_path,
                    run_id=run_id,
                    event_type=CONTROL_REQUESTED,
                    payload=dict(fingerprint),
                    idempotency_key=requested_idempotency_key(request_id),
                    expected_previous_sequence=events[-1].sequence if events else 0,
                )
            except run_journal.IdempotencyConflict as exc:
                raise ControlJournalError(_bound(exc.diagnostic), code="control_fingerprint_conflict") from exc
            except run_events.CanonicalizationError as exc:
                raise ControlJournalError(_bound(str(exc)), code="control_payload_invalid") from exc
            _sync_shadow_after_control(run_dir)
            return "claimed", requested, None


def inspect_control_request(
    run_dir: Path,
    *,
    request_id: str,
    fingerprint: Mapping[str, Any],
) -> tuple[str, run_journal.RunEvent | None, run_journal.RunEvent | None]:
    """Classify a prior control request: ``absent``, ``replay``, ``indeterminate``, or raise conflict."""
    return _inspect_control_request(run_dir, request_id=request_id, fingerprint=fingerprint)


def _inspect_control_request(
    run_dir: Path,
    *,
    request_id: str,
    fingerprint: Mapping[str, Any],
) -> tuple[str, run_journal.RunEvent | None, run_journal.RunEvent | None]:
    journal_path = _journal_path(run_dir)
    events = _read_verified(journal_path)
    requested, terminal = _find_control_pair(events, request_id=request_id)
    if requested is None:
        return "absent", None, None
    expected_digest = run_events.request_digest(
        event_type=CONTROL_REQUESTED,
        payload=dict(fingerprint),
        idempotency_key=requested_idempotency_key(request_id),
    )
    if requested.request_digest != expected_digest:
        raise ControlJournalError(
            _bound("request_id reused with a different control fingerprint"),
            code="control_fingerprint_conflict",
        )
    if terminal is None:
        return "indeterminate", requested, None
    return "replay", requested, terminal


def execute_control_request(
    run_dir: Path,
    *,
    op: str,
    request_id: str | None = None,
    worker: str | None = None,
    text: str | None = None,
    turn_id: str | None = None,
    send: Callable[[dict[str, object]], dict[str, Any]],
) -> ControlResult:
    """Execute an idempotent control request against a journal-backed run.

    ``send`` is the transport callable (typically a lambda around
    ``send_request_with_retry``). It is not invoked on replay or indeterminate.
    """
    run_dir = Path(run_dir)
    if not journal_present(run_dir):
        raise ControlJournalError("legacy-no-journal", code="legacy-no-journal")
    _preflight_live_control(run_dir)

    resolved_id = request_id if request_id is not None else generate_request_id()
    fingerprint = control_fingerprint_payload(
        op=op,
        request_id=resolved_id,
        worker=worker,
        text=text,
        turn_id=turn_id,
    )
    journal_path = _journal_path(run_dir)
    run_id = _run_id(run_dir)

    state, prior_requested, prior_terminal = inspect_control_request(
        run_dir,
        request_id=resolved_id,
        fingerprint=fingerprint,
    )
    if state == "indeterminate":
        raise ControlJournalError(
            _bound(f"control request {resolved_id!r} is indeterminate"),
            code="indeterminate",
        )
    if state == "replay":
        assert prior_terminal is not None
        return ControlResult(
            request_id=resolved_id,
            response=_response_from_terminal(prior_terminal),
            replayed=True,
            requested_event=prior_requested,
            terminal_event=prior_terminal,
        )

    state, prior_requested, prior_terminal = _claim_control_request(
        journal_path,
        run_dir=run_dir,
        run_id=run_id,
        request_id=resolved_id,
        fingerprint=fingerprint,
    )
    if state == "indeterminate":
        raise ControlJournalError(
            _bound(f"control request {resolved_id!r} is indeterminate"),
            code="indeterminate",
        )
    if state == "replay":
        assert prior_terminal is not None
        return ControlResult(
            request_id=resolved_id,
            response=_response_from_terminal(prior_terminal),
            replayed=True,
            requested_event=prior_requested,
            terminal_event=prior_terminal,
        )
    assert prior_requested is not None
    requested_event = prior_requested

    transport_payload: dict[str, object] = {"op": op}
    if worker is not None:
        transport_payload["worker"] = worker
    if text is not None:
        transport_payload["text"] = text

    try:
        response = send(transport_payload)
    except Exception as exc:
        response = {"_transport_exception": exc}

    if isinstance(response, Mapping) and response.get("ok") is True:
        observed_payload: dict[str, Any] = {
            "op": op,
            "request_id": resolved_id,
            "detail": "ok",
        }
        if worker is not None:
            observed_payload["worker"] = worker
        elif isinstance(response.get("worker"), str):
            observed_payload["worker"] = response["worker"]
        resp_turn = response.get("turn_id")
        if isinstance(resp_turn, str) and resp_turn:
            observed_payload["turn_id"] = resp_turn
        elif turn_id is not None:
            observed_payload["turn_id"] = turn_id
        interrupted = response.get("interrupted")
        if isinstance(interrupted, int) and not isinstance(interrupted, bool):
            observed_payload["detail"] = f"interrupted={interrupted}"
        terminal = _append_control_event(
            journal_path,
            run_dir=run_dir,
            run_id=run_id,
            event_type=CONTROL_OBSERVED,
            payload=observed_payload,
            idempotency_key=observed_idempotency_key(resolved_id),
        )
        return ControlResult(
            request_id=resolved_id,
            response=dict(response),
            replayed=False,
            requested_event=requested_event,
            terminal_event=terminal,
        )

    if isinstance(response, Mapping) and "_transport_exception" in response:
        failure_class = ControlFailureClass.TRANSPORT_EXCEPTION
        detail = str(response["_transport_exception"])
    elif isinstance(response, Mapping) and response.get("ok") is False:
        failure_class = ControlFailureClass.TRANSPORT_REJECTED
        detail = str(response.get("error") or "control request failed")
    else:
        failure_class = ControlFailureClass.TRANSPORT_PROTOCOL_ERROR
        detail = str(response.get("error") if isinstance(response, Mapping) else response)
    failed_payload: dict[str, Any] = {
        "op": op,
        "request_id": resolved_id,
        "error_class": failure_class.value,
        "detail_digest": hashlib.sha256(detail.encode("utf-8")).hexdigest(),
    }
    if worker is not None:
        failed_payload["worker"] = worker
    terminal = _append_control_event(
        journal_path,
        run_dir=run_dir,
        run_id=run_id,
        event_type=CONTROL_FAILED,
        payload=failed_payload,
        idempotency_key=failed_idempotency_key(resolved_id),
    )
    return ControlResult(
        request_id=resolved_id,
        response={
            "ok": False,
            "error": "control request failed",
            "error_class": failure_class.value,
            "detail_digest": failed_payload["detail_digest"],
        },
        replayed=False,
        requested_event=requested_event,
        terminal_event=terminal,
    )


__all__ = [
    "CONTROL_FAILED",
    "CONTROL_OBSERVED",
    "CONTROL_REQUESTED",
    "ControlJournalError",
    "ControlFailureClass",
    "ControlResult",
    "control_fingerprint_payload",
    "execute_control_request",
    "failed_idempotency_key",
    "generate_request_id",
    "inspect_control_request",
    "journal_present",
    "observed_idempotency_key",
    "requested_idempotency_key",
    "text_digest",
]
