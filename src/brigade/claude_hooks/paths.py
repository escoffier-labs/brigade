"""Claude Code config directory resolution."""

from __future__ import annotations

import os
from pathlib import Path


def resolve_claude_home() -> Path:
    """Return the Claude Code user config root.

    Honors ``CLAUDE_CONFIG_DIR`` when set; otherwise ``~/.claude``.
    """
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home().expanduser().resolve() / ".claude"
