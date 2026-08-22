"""Claude Code config directory resolution."""

from __future__ import annotations

import os
from pathlib import Path


def resolved_path(value: Path) -> Path | None:
    try:
        return value.expanduser().resolve()
    except OSError:
        return None


def is_operator_home(path: Path) -> bool:
    """True when ``path`` is the current user's home directory."""
    resolved = resolved_path(path)
    home = resolved_path(Path.home())
    return resolved is not None and home is not None and resolved == home


def resolve_claude_home() -> Path:
    """Return the Claude Code user config root.

    Honors ``CLAUDE_CONFIG_DIR`` when set; otherwise ``~/.claude``.
    """
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home().expanduser().resolve() / ".claude"
