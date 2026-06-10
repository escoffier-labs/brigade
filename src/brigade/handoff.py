"""`brigade handoff-template` - print the handoff TEMPLATE.md to stdout."""

from __future__ import annotations

import sys
from pathlib import Path

from .templates import template_root


def run(target: Path | None = None) -> int:
    """Print the handoff template. If `target` is given and has its own
    TEMPLATE.md, that copy is preferred (matches the user's installed version)."""
    if target is not None:
        local = target.expanduser().resolve() / ".claude" / "memory-handoffs" / "TEMPLATE.md"
        if local.is_file():
            sys.stdout.write(local.read_text())
            return 0
    packaged = template_root() / "claude" / "memory-handoffs" / "TEMPLATE.md"
    if not packaged.is_file():
        print("error: packaged TEMPLATE.md missing", file=sys.stderr)
        return 1
    sys.stdout.write(packaged.read_text())
    return 0
