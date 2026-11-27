"""The deliberately small Stop policy retained after a hook timeout latch."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .. import localio, work_mode
from . import session_state, write_attribution


def _runtime() -> Any:
    from . import runtime

    return runtime


def _save(target: Path, session_id: str, state: dict[str, Any], *fields: str) -> bool:
    return bool(
        _runtime()._write_session_state_preserving_latch(
            target, session_id, state, log_target=target, patch_fields=fields
        )
    )


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
    write_attribution.include_pending_at_stop(state)
    if not state.get("write_observed") or not work_mode.is_strict(target):
        return None

    from ..work_cmd.verify_gate import STOP_POLICY_BUDGET_SECONDS

    deadline = time.monotonic() + STOP_POLICY_BUDGET_SECONDS
    already_blocked = isinstance(state.get("latched_stop_blocked_at"), str)
    if payload.get("stop_hook_active") is True:
        return None
    covered, gate = _receipt_look(target, session_id, state, deadline)
    if already_blocked:
        if not covered and not isinstance(state.get("ended_unverified_after_latch"), str):
            state["ended_unverified_after_latch"] = localio.utc_now_iso()
            _save(target, session_id, state, "ended_unverified_after_latch")
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
