"""Bounded, best-effort progress markers for timed Claude hook workers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import anchored_io

STEPS = frozenset(
    {
        "state-read",
        "classify",
        "stop-receipt-gate",
        "stop-manifest-gate",
        "unknown",
    }
)


def _path(target: Path, session_id: str) -> Path:
    from . import runtime

    return Path(f"{runtime._state_path(target, session_id)}.step")


def mark(target: Path, session_id: str, event: str, step: str) -> None:
    """Replace a tiny marker without syncing; marker failure never affects a hook."""
    if event not in _EVENTS:
        event = "unknown"
    if step not in STEPS:
        step = "unknown"
    path = _path(target, session_id)
    try:
        with anchored_io.session_directory(target, create=True) as anchor:
            anchor.replace_bytes_unsynced(path.name, json.dumps({"event": event, "step": step}).encode("ascii"))
    except (anchored_io.SessionIntegrityError, OSError):
        pass


def read(target: Path, session_id: str, event: str) -> str:
    try:
        with anchored_io.session_directory(target) as anchor:
            raw = anchor.read_bytes(_path(target, session_id).name, 129)
        if len(raw) > 128:
            return "unknown"
        payload: Any = json.loads(raw.decode("ascii"))
    except (anchored_io.SessionIntegrityError, OSError, ValueError, UnicodeError):
        return "unknown"
    step = payload.get("step") if isinstance(payload, dict) and payload.get("event") == event else None
    return step if isinstance(step, str) and step in STEPS else "unknown"


_EVENTS = frozenset(
    {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "Stop",
        "SessionStart",
        "UserPromptSubmit",
        "PreCompact",
        "SessionEnd",
    }
)
