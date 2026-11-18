"""Skill-tool invocations recorded in Claude hook session state.

A skill loaded through the ``Skill`` tool is never a ``Read`` of its ``SKILL.md``,
so the read list cannot see it. The PostToolUse ``Skill`` hook records the
invoked name here, and ``--capture auto`` resolves it next to the reads
(``outcome_attribution``). The names are agent-chosen, so like reads they only
feed veto-only, ``session_observed`` attribution.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

STATE_KEY = "invoked_skills"
MAX_INVOKED = 128
LOCAL_PLUGIN_PREFIXES = frozenset({"brigade"})
"""Plugin names whose skills are this install's own (plugins/brigade). Another plugin's
``<plugin>:brigade-work`` is a different skill and must not credit the local one."""


def invoked_names(state: dict[str, Any]) -> list[str]:
    """Bare recorded names plus the id of a local-plugin ``brigade:skill`` name, ordered and unique."""
    names: dict[str, None] = {}
    value = state.get(STATE_KEY)
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, str) or not item:
            continue
        prefix, sep, bare = item.partition(":")
        if not sep:
            names.setdefault(item, None)
        elif prefix.casefold() in LOCAL_PLUGIN_PREFIXES and bare:
            names.setdefault(bare, None)
    return list(names)


def record(state: dict[str, Any], tool_input: dict[str, Any], tool_response: object = None) -> bool:
    """Record one successful ``Skill`` tool call; return whether the state changed."""
    if isinstance(tool_response, dict) and tool_response.get("is_error"):
        return False
    name = tool_input.get("skill")
    if not isinstance(name, str):
        return False
    name = name.strip()
    if not name or len(name) > 256 or any(ch in name for ch in "/\\\0\n"):
        return False
    current = state.get(STATE_KEY)
    names = dict.fromkeys(item for item in current if isinstance(item, str)) if isinstance(current, list) else {}
    names.pop(name, None)
    state[STATE_KEY] = [*names, name][-MAX_INVOKED:]
    return True


def handle(
    target: Path, session_id: str, state: dict[str, Any], tool_input: dict[str, Any], tool_response: object
) -> None:
    """PostToolUse ``Skill``: persist the invoked name through the locked patch merge."""
    if record(state, tool_input, tool_response):
        from .session_state import _write_session_state_preserving_latch

        _write_session_state_preserving_latch(target, session_id, state, log_target=target, patch_fields=(STATE_KEY,))
