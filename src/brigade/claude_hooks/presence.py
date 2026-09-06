"""Claude hook adapter for Fleet interactive session presence."""

from __future__ import annotations

from pathlib import Path


def emit_presence(event: str, target: Path, session_id: str) -> str | None:
    """Publish session presence without changing the hook envelope.

    Returns a bounded failure reason when the Hub write did not land, or None
    when it succeeded, was skipped, or the Hub is unconfigured. Callers stay
    free to ignore it: this never raises.
    """
    try:
        from ..fleet_session_presence import _publish_presence

        return _publish_presence(event, target, session_id)
    except Exception:
        return None
