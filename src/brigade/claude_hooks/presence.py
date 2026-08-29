"""Claude hook adapter for Fleet interactive session presence."""

from __future__ import annotations

from pathlib import Path


def emit_presence(event: str, target: Path, session_id: str) -> None:
    """Publish session presence without changing the hook envelope."""
    try:
        from ..fleet_session_presence import _publish_presence

        _publish_presence(event, target, session_id)
    except Exception:
        return
