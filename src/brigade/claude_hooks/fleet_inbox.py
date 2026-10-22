"""Claude hook adapter for live Fleet Hub inbox delivery.

``UserPromptSubmit`` and ``PostToolUse`` accept ``additionalContext``. The
delivery text rides on the event's own result when that result has room for
it: no result at all, or an existing ``additionalContext`` envelope. A result
that carries a decision (a block or a deny) is left alone and nothing is
consumed, so the message arrives with the next event instead.
"""

from __future__ import annotations

from typing import Any, Callable

from .. import fleet_inbox_delivery


def _accepts_context(result: dict[str, Any] | None) -> bool:
    if result is None:
        return True
    output = result.get("hookSpecificOutput")
    return isinstance(output, dict) and isinstance(output.get("additionalContext"), str) and "decision" not in result


def attach(
    event: str,
    payload: dict[str, Any],
    result: dict[str, Any] | None,
    *,
    wired: Callable[[], bool],
) -> dict[str, Any] | None:
    """Return ``result`` with newly arrived inbox mail added. Never raises."""
    try:
        session_id = payload.get("session_id")
        if event not in fleet_inbox_delivery.DELIVERY_EVENTS or not isinstance(session_id, str) or not session_id:
            return result
        if not _accepts_context(result) or not wired():
            return result
        text = fleet_inbox_delivery.deliver("claude", session_id, event)
        if text is None:
            return result
        if result is None:
            return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
        output = result["hookSpecificOutput"]
        return {
            **result,
            "hookSpecificOutput": {**output, "additionalContext": f"{output['additionalContext']}\n\n{text}"},
        }
    except Exception:  # noqa: BLE001 - delivery must never break a hook
        return result
