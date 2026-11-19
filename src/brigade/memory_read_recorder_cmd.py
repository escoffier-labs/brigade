"""Operator command for the root read-recorder sentinel."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from . import memory_card_reads as reads


def clear(*, target: Path, confirm: bool = False, stdin: Any | None = None) -> int:
    """Clear the root sentinel after the operator confirms the recording gap."""
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if not confirm:
        print(
            "error: clearing the read recorder sentinel requires --confirm "
            "(and an interactive y/N when stdin is a TTY)",
            file=sys.stderr,
        )
        return 2
    stream = stdin if stdin is not None else sys.stdin
    if hasattr(stream, "isatty") and stream.isatty():
        print(f"Clear {reads.DISABLED_REL} for {target}? [y/N]: ", end="", flush=True)
        answer = stream.readline() if hasattr(stream, "readline") else ""
        if str(answer).strip().lower() not in {"y", "yes"}:
            print("error: read recorder sentinel not cleared", file=sys.stderr)
            return 2
    result = reads.clear_disabled(target)
    if not result["cleared"]:
        print(f"error: read recorder sentinel not cleared: {result['reason']}", file=sys.stderr)
        return 2
    print(f"read recorder sentinel: cleared {reads.DISABLED_REL}")
    if not result["recorded"]:
        print("error: the cleared record could not be written", file=sys.stderr)
        return 2
    print(f"read recorder sentinel: recorded in {reads.CLEARED_REL}; recording resumes on the next successful read")
    return 0
