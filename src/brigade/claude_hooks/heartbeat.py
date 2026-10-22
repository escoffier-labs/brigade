"""Throttled Hub presence refresh for live Claude Code sessions.

The Hub expires an interactive session ``ttl_seconds`` after its last write
(``fleet_session_presence.DEFAULT_TTL_SECONDS``). ``SessionStart`` publishes the
row once and ``SessionEnd`` ends it, so without a cadence in between a thread
that stays open past the TTL disappears from ``brigade fleet sessions`` and the
Command Deck while it is still alive.

``UserPromptSubmit`` and ``PreToolUse`` therefore refresh presence, but only
once per ``HEARTBEAT_INTERVAL_SECONDS``. The throttle stamp is one small JSON
file per session under the existing hook state directory; it never reaches the
Hub. The stamp is written before the Hub call, so an unreachable Hub cannot
turn every prompt or tool call into a fresh network attempt, and no call path
here raises: a hook must never be blocked by fleet presence.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .. import localio
from . import envelope, presence

# fleet_session_presence.DEFAULT_TTL_SECONDS // 3. Pinned as a literal so the
# hook path does not import the presence module just to read a constant;
# tests/test_claude_hooks_heartbeat.py holds the two in sync.
HEARTBEAT_INTERVAL_SECONDS = 300
PRESENCE_DIRNAME = "presence"
REFRESH_EVENTS = frozenset({"UserPromptSubmit", "PreToolUse"})
MAX_STATE_BYTES = 4096
_STAMP_KEY = "last_refresh_at"


def state_path(target: Path, session_id: str) -> Path:
    """Return the per-session throttle stamp path inside the hook state dir."""
    slug = localio.slugify(session_id, fallback="session")[:48]
    suffix = localio.stable_hash(session_id)[:8]
    return envelope.hooks_state_root(target) / PRESENCE_DIRNAME / f"{slug}-{suffix}.json"


def last_refresh(target: Path, session_id: str) -> float | None:
    """Read the last recorded refresh epoch, or None when there is no usable stamp."""
    try:
        raw = state_path(target, session_id).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    if len(raw) > MAX_STATE_BYTES:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    value = payload.get(_STAMP_KEY) if isinstance(payload, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def record_refresh(target: Path, session_id: str, *, now: float | None = None) -> None:
    """Stamp a presence write that already happened. Never raises."""
    stamp = time.time() if now is None else now
    try:
        path = state_path(target, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        envelope.write_private_text(path, json.dumps({_STAMP_KEY: stamp}, sort_keys=True) + "\n")
    except (OSError, ValueError):
        return


def due(target: Path, session_id: str, *, now: float | None = None) -> bool:
    """True when this session has no stamp, or its stamp is older than the interval."""
    stamp = time.time() if now is None else now
    previous = last_refresh(target, session_id)
    if previous is None:
        return True
    delta = stamp - previous
    # A backwards clock (< 0) counts as due so a bad stamp cannot pin a live
    # session off the Hub until the session restarts.
    return delta >= HEARTBEAT_INTERVAL_SECONDS or delta < 0


def clear(target: Path, session_id: str) -> None:
    """Drop this session's throttle stamp. Never raises."""
    try:
        state_path(target, session_id).unlink()
    except (OSError, ValueError):
        return


def on_event(event: str, target: Path, session_id: str, *, now: float | None = None) -> None:
    """Apply this hook event's presence effect. Always returns None, never raises.

    ``SessionStart`` only stamps: ``handle_payload`` already published that row.
    ``UserPromptSubmit`` and ``PreToolUse`` refresh at most once per interval.
    ``SessionEnd`` ends the Hub row and drops the local stamp.
    """
    try:
        if event == "SessionStart":
            record_refresh(target, session_id, now=now)
            return
        if event == "SessionEnd":
            _note(target, event, presence.emit_presence(event, target, session_id))
            clear(target, session_id)
            return
        if event not in REFRESH_EVENTS:
            return
        stamp = time.time() if now is None else now
        if not due(target, session_id, now=stamp):
            return
        # Stamp first: a failed or unreachable Hub still consumes the interval,
        # so the bound stays "at most one Hub call per interval per session".
        record_refresh(target, session_id, now=stamp)
        _note(target, event, presence.emit_presence(event, target, session_id))
    except Exception:  # noqa: BLE001 - presence must never break a hook
        return


def _note(target: Path, event: str, reason: str | None) -> None:
    """Record a bounded presence failure in the hook log, never on the session stream."""
    if not reason:
        return
    try:
        envelope.append_log(target, f"{event}: fleet presence refresh failed: {reason}")
    except OSError:
        return
