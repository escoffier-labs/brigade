"""Stop-hook handoff note and the ``Nothing durable:`` close rule."""

from __future__ import annotations

from pathlib import Path
from typing import Any

HANDOFF_STOP_NOTE = (
    "Write a Memory Handoff in `.claude/memory-handoffs/` with `brigade handoff draft` only if this work produced "
    "knowledge the repo, git history or an existing card does not hold. Otherwise end with one line, "
    "`Nothing durable: <reason>`."
)
_NOTHING_DURABLE_PREFIX = "nothing durable:"
_OWN_INBOX = "`.claude/memory-handoffs/`"


def handoff_stop_note(target: Path) -> str:
    """The Stop note for `target`, naming the inbox that outlives a linked worktree.

    Outside a linked worktree this is `HANDOFF_STOP_NOTE` unchanged.
    """
    from .. import handoff_worktrees

    return HANDOFF_STOP_NOTE.replace(_OWN_INBOX, handoff_worktrees.stop_hint(target), 1)


def closes_nothing_durable(payload: dict[str, Any]) -> bool:
    """True when the final assistant line is a ``Nothing durable: <reason>`` close.

    Only ``last_assistant_message`` counts. User prompts, tool input and tool
    output are never read, so quoted text from them cannot waive the note.
    """
    message = payload.get("last_assistant_message")
    if not isinstance(message, str):
        return False
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    if not lines:
        return False
    last = lines[-1]
    return last.casefold().startswith(_NOTHING_DURABLE_PREFIX) and bool(last[len(_NOTHING_DURABLE_PREFIX) :].strip())
