"""The deliberately small Stop policy retained after a hook timeout latch."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .. import localio, work_mode
from . import anchored_io, session_state, write_attribution


def _runtime() -> Any:
    from . import runtime

    return runtime


def _save(target: Path, session_id: str, state: dict[str, Any], *fields: str) -> bool:
    return bool(
        _runtime()._write_session_state_preserving_latch(
            target, session_id, state, log_target=target, patch_fields=fields
        )
    )


def _record_unverified(target: Path, session_id: str, state: dict[str, Any]) -> None:
    """Record the latched end once, when no receipt covers the session."""
    if isinstance(state.get("ended_unverified_after_latch"), str):
        return
    state["ended_unverified_after_latch"] = localio.utc_now_iso()
    _save(target, session_id, state, "ended_unverified_after_latch")


def _include_pending_write(state: dict[str, Any]) -> None:
    """Treat an unfinished Write/Edit as this session's write at Stop."""
    pending = localio.parse_iso_datetime(state.get("pending_write_at"))
    if pending is None:
        return
    state["write_observed"] = True
    threshold = localio.parse_iso_datetime(
        state.get("last_verification_write_at") or state.get("last_write_at") or state.get("started_at")
    )
    if threshold is None or pending > threshold:
        state["last_verification_write_at"] = pending.isoformat()


def _receipt_look(target: Path, session_id: str, state: dict[str, Any], deadline: float) -> tuple[bool, Any | None]:
    """Perform the single fail-closed receipt look within the Stop budget."""
    rt = _runtime()
    fingerprint = state.get("session_fingerprint")
    if not isinstance(fingerprint, str):
        fingerprint = rt._session_fingerprint(session_id)
    threshold = state.get("last_verification_write_at") or state.get("last_write_at") or state.get("started_at")
    if time.monotonic() >= deadline:
        return False, None

    def lookup() -> tuple[bool, Any | None]:
        from ..work_cmd.verify_gate import StopGate

        gate = StopGate(target, deadline=deadline)
        # Enumerate receipts before asking the gate for policy/tree work. An
        # empty receipt set therefore never launches git just to find no match.
        covered = rt._receipt_since(target, threshold, session_fingerprint=fingerprint, gate=gate)
        # Reading the gate's timeout flag loads policy, so only a covering result asks for it.
        if not covered or time.monotonic() >= deadline or gate.timed_out:
            return False, gate
        return True, gate

    try:
        return rt._run_best_effort_bounded(lookup, timeout=max(deadline - time.monotonic(), 0.0), default=(False, None))
    except Exception:  # noqa: BLE001 - an unconfirmed lookup is the blocking result
        return False, None


def handle_task_boundary(target: Path, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Advance a latched session's task epoch with one bounded state write."""
    rt = _runtime()
    task_epoch = rt._task_epoch_for_event("SessionStart", payload, session_id)
    persisted = rt.read_session_state(target, session_id)
    state = rt._normalize_state(target, session_id, persisted, task_epoch=task_epoch, fingerprint=lambda _path: None)
    state["hook_latched"] = True
    history = persisted.get("hook_timeout_history") if isinstance(persisted, dict) else None
    if isinstance(history, list):
        state["hook_timeout_history"] = list(history)
    rt._write_session_state_preserving_latch(target, session_id, state, log_target=target)
    return {
        "kind": "latched_silent",
        "result": rt.envelope.empty_envelope("SessionStart"),
        "log_target": str(target),
        "claim_path": None,
        "session_id": session_id,
    }


def handle(target: Path, session_id: str, payload: dict[str, Any]) -> dict[str, str] | None:
    """Check only the receipt gate, and never hold a latched task twice."""
    rt = _runtime()
    persisted = rt.read_session_state(target, session_id) or {}
    task_epoch = rt._current_task_epoch(session_id)
    state = rt._normalize_state(
        target,
        session_id,
        persisted,
        task_epoch=task_epoch,
        fingerprint=lambda _target: persisted.get("repo_fingerprint"),
    )
    state["hook_latched"] = True
    _include_pending_write(state)
    write_attribution.include_pending_at_stop(state)
    if not state.get("write_observed") or not work_mode.is_strict(target):
        return None

    from ..work_cmd.verify_gate import STOP_POLICY_BUDGET_SECONDS

    deadline = time.monotonic() + STOP_POLICY_BUDGET_SECONDS
    covered, gate = _receipt_look(target, session_id, state, deadline)
    if payload.get("stop_hook_active") is True:
        if not covered:
            _record_unverified(target, session_id, state)
        return None
    already_blocked = isinstance(state.get("latched_stop_blocked_at"), str) or session_state.has_latched_stop_claim(
        target, session_id, state.get("task_epoch")
    )
    if already_blocked:
        if not covered:
            _record_unverified(target, session_id, state)
        return None
    if covered:
        return None
    if time.monotonic() < deadline and gate is not None:
        try:
            foreign = rt._run_best_effort_bounded(
                lambda: rt._shared_tree_block_is_foreign(target, session_id, state, gate),
                timeout=max(deadline - time.monotonic(), 0.0),
                default=False,
            )
            if foreign:
                return None
        except Exception:  # noqa: BLE001 - inability to prove the waiver keeps the fail-closed look
            pass

    try:
        claimed = session_state.claim_latched_stop(target, session_id, state.get("task_epoch"))
    except anchored_io.SessionIntegrityError:
        raise
    except (OSError, ValueError):
        return None
    if not claimed:
        return None

    state["latched_stop_blocked_at"] = localio.utc_now_iso()
    _save(target, session_id, state, "latched_stop_blocked_at")
    fingerprint = state.get("session_fingerprint")
    if not isinstance(fingerprint, str):
        fingerprint = rt._session_fingerprint(session_id)
    command = rt._verify_replacement(target, "<check>", fingerprint, hook_target=target)
    reason = (
        f"{target}: Brigade hooks switched off after two timeouts in this session, so verification is not "
        f"confirmed. Run `{command}` and stop again."
    )
    return {"decision": "block", "reason": reason}
