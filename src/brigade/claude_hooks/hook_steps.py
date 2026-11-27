"""Bounded, best-effort progress markers for timed Claude hook workers."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

STEPS = frozenset(
    {
        "session-brief",
        "state-read",
        "classify",
        "write-attribution",
        "tree-fingerprint",
        "stop-receipt-gate",
        "stop-manifest-gate",
        "code-graph",
        "state-write",
        "unknown",
    }
)


def _path(target: Path, session_id: str) -> Path:
    from . import runtime

    return Path(f"{runtime._state_path(target, session_id)}.step")


def mark(target: Path, session_id: str, event: str, step: str) -> None:
    """Replace a tiny marker without syncing; marker failure never affects a hook."""
    if step not in STEPS:
        step = "unknown"
    path = _path(target, session_id)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps({"event": event, "step": step}), encoding="ascii")
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def read(target: Path, session_id: str, event: str) -> str:
    try:
        payload: Any = json.loads(_path(target, session_id).read_text(encoding="ascii")[:128])
    except (OSError, ValueError, UnicodeError):
        return "unknown"
    step = payload.get("step") if isinstance(payload, dict) and payload.get("event") == event else None
    return step if isinstance(step, str) and step in STEPS else "unknown"
