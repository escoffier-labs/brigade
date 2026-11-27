"""The deliberately small Stop policy retained after a hook timeout latch."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .. import localio, work_mode

VERIFY_COMMAND = 'brigade work verify run --target . --command "<check>" --capture brigade-work'


def _runtime() -> Any:
    from . import runtime

    return runtime


def _save(target: Path, session_id: str, state: dict[str, Any], *fields: str) -> bool:
    return bool(
        _runtime()._write_session_state_preserving_latch(
            target, session_id, state, log_target=target, patch_fields=fields
        )
    )


def handle(target: Path, session_id: str, payload: dict[str, Any]) -> dict[str, str] | None:
    """Check only the receipt gate, and never hold a latched session twice."""
    rt = _runtime()
    state = rt.read_session_state(target, session_id) or {}
    if not state.get("write_observed") or not work_mode.is_strict(target):
        return None

    already_blocked = isinstance(state.get("latched_stop_blocked_at"), str)
    if payload.get("stop_hook_active") is True or already_blocked:
        if already_blocked and not isinstance(state.get("ended_unverified_after_latch"), str):
            state["ended_unverified_after_latch"] = localio.utc_now_iso()
            _save(target, session_id, state, "ended_unverified_after_latch")
        return None

    fingerprint = state.get("session_fingerprint")
    if not isinstance(fingerprint, str):
        fingerprint = rt._session_fingerprint(session_id)
    threshold = state.get("last_verification_write_at") or state.get("last_write_at") or state.get("started_at")
    covered = False
    try:
        from ..work_cmd.verify_gate import STOP_POLICY_BUDGET_SECONDS, StopGate

        gate = StopGate(target, deadline=time.monotonic() + STOP_POLICY_BUDGET_SECONDS)
        covered = not gate.timed_out and rt._receipt_since(
            target, threshold, session_fingerprint=fingerprint, gate=gate
        )
        covered = covered and not gate.timed_out
    except Exception:  # noqa: BLE001 - an unconfirmed lookup is the blocking result
        covered = False
    if covered:
        return None

    state["latched_stop_blocked_at"] = localio.utc_now_iso()
    if not _save(target, session_id, state, "latched_stop_blocked_at"):
        return None
    reason = (
        f"{target}: Brigade hooks switched off after two timeouts in this session, so verification is not "
        f"confirmed. Run `{VERIFY_COMMAND}` and stop again."
    )
    return {"decision": "block", "reason": reason}
