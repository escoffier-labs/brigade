"""Shared normalization for Memory Handoff content sections."""

from __future__ import annotations

import re

_OUTER_MARKDOWN_FENCE = re.compile(r"```[A-Za-z0-9_-]*")


def normalize_suggested_card_content(value: str) -> str:
    """Remove one complete outer Markdown fence from suggested card content."""
    lines = value.splitlines()
    if len(lines) < 2:
        return value

    opening = lines[0].strip()
    closing = lines[-1].strip()
    if _OUTER_MARKDOWN_FENCE.fullmatch(opening) and closing == "```":
        return "\n".join(lines[1:-1]).strip()
    return value
