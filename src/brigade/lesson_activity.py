"""Bounded, local work-hook observations for automatic lesson review."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import guard, localio, memory_proposal_io as io
from .guard.types import ScanOptions
from .inbox_lock import held_file_lock
from .untrusted import scan_untrusted

ROOT = ".brigade/lesson-review"
ORIGIN = "brigade.lesson-review.v1"
MAX_EVENTS = 32
MAX_TEXT = 2048


def root(target: Path) -> Path:
    path = target / ROOT
    with io.directory(path, create=True, private=True):
        pass
    return path


def config(target: Path) -> dict[str, Any]:
    raw = io.text(target / ".brigade/lesson-review.json", absent=True, limit=4096)
    value = json.loads(raw) if raw is not None else {}
    if not isinstance(value, dict):
        raise ValueError("lesson review config must be an object")
    result = {"enabled": True, "threshold": 20, "idle_seconds": 300, "seat": "researcher", **value}
    if type(result["enabled"]) is not bool or not isinstance(result["seat"], str) or not result["seat"]:
        raise ValueError("invalid lesson review config")
    for key, maximum in (("threshold", 10000), ("idle_seconds", 86400)):
        if type(result[key]) is not int or not 1 <= result[key] <= maximum:
            raise ValueError(f"invalid lesson review {key}")
    return result


def load(target: Path) -> dict[str, Any]:
    raw = io.text(target / ROOT / "state.json", absent=True, limit=128 * 1024, private=True)
    if raw is None:
        return {"sequence": 0, "reviewed": 0, "last_activity": 0, "events": []}
    state = json.loads(raw)
    if (
        not isinstance(state, dict)
        or type(state.get("sequence")) is not int
        or type(state.get("reviewed")) is not int
        or not 0 <= state["reviewed"] <= state["sequence"]
        or not isinstance(state.get("events"), list)
        or len(state["events"]) > MAX_EVENTS
        or type(state.get("last_activity")) not in (int, float)
    ):
        raise ValueError("invalid lesson activity state")
    return state


def save(target: Path, state: dict[str, Any]) -> None:
    localio.write_bytes_atomic(root(target) / "state.json", io.json_bytes(state))
    io.protect_file(target / ROOT / "state.json")


def scanned(text: str) -> str:
    if scan_untrusted(text).flagged:
        raise ValueError("lesson text contains untrusted instructions")
    result = guard.scan_text(text, options=ScanOptions(honor_allow_comments=False, scan_frontmatter=True))
    if result.blocked or result.changed:
        raise ValueError("lesson text failed content scan")
    return text


def record_event(target: Path, event: str, payload: dict[str, Any], *, now: float | None = None) -> None:
    if event not in {"PostToolUse", "PostToolUseFailure", "UserPromptSubmit", "PreToolUse"}:
        return
    if not config(target)["enabled"]:
        return
    # Review workers do not feed their own activity back into the trigger.
    import os

    if os.environ.get("BRIGADE_RUN_ID", "").startswith("lesson-"):
        return
    fields = ("prompt",) if event == "UserPromptSubmit" else ("tool_input", "tool_response", "tool_output", "error")
    values = [str(payload[key]) for key in fields if key in payload]
    budget = (MAX_TEXT - len(values) * 6) // max(1, len(values))
    text = "\n".join(
        value
        if len(value.encode()) <= budget
        else value.encode()[: budget // 2].decode(errors="ignore")
        + "\n...\n"
        + value.encode()[-budget // 2 :].decode(errors="ignore")
        for value in values
    )
    text = "".join(c for c in text if c.isprintable() or c in "\n\t")
    text = text.encode()[:MAX_TEXT].decode(errors="ignore")
    try:
        text = scanned(text)
    except ValueError:
        text = "[observation withheld by scanner]"
    with held_file_lock(root(target) / "state.lock", deadline_seconds=0.1):
        state = load(target)
        # Pre-tool activity invalidates an in-flight review without counting twice.
        state["sequence"] += 1
        count = state.get("calls", 0) + int(event in {"PostToolUse", "PostToolUseFailure"})
        state["calls"] = count
        state["last_activity"] = time.time() if now is None else now
        state["events"] = (state["events"] + [{"id": state["sequence"], "kind": event, "text": text}])[-MAX_EVENTS:]
        while len(io.json_bytes(state)) > 96 * 1024:
            state["events"].pop(0)
        save(target, state)


def on_event(target: Path, event: str, payload: dict[str, Any]) -> None:
    """Optional hook work must never affect the primary hook response."""
    try:
        record_event(target, event, payload)
    except Exception:
        pass
