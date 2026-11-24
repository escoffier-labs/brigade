"""Passive reporting for Claude sessions whose hooks have timed out."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import anchored_io, session_state


def check(target: Path) -> tuple[str, str, str] | None:
    label = "Claude hook timeout latch"
    try:
        recent = datetime.now(UTC) - timedelta(days=1)
        count = sum(
            session_state._read_hook_latch(target, session_id)[0]
            for state in anchored_io.iter_states(target, modified_since=recent, limit=16)
            if isinstance(session_id := state.get("session_id"), str) and session_id
        )
    except Exception:
        return "WARN", label, "could not read recent Claude hook session state"
    if count:
        return "WARN", label, f"{count} recent session(s) bypass Claude hooks after repeated timeouts"
    return None
