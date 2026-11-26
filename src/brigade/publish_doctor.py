"""Doctor diagnostics for publication hooks and their scanner."""

from __future__ import annotations

import subprocess
from pathlib import Path


def push_gate_checks(target: Path) -> list[tuple[str, str, str]]:
    from .work_cmd import push_gate

    results: list[tuple[str, str, str]] = []

    try:
        repo = push_gate.Repository.locate(target)
        if repo is not None:
            info = push_gate.status(repo)
            needs_attention = info["mode"] == "warn" and (not info["will_run"] or not info["current"])
            results.append(("WARN" if needs_attention else "INFO", "verify push gate", push_gate.describe(info)))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        results.append(("WARN", "verify push gate", "could not inspect the hook or its log"))
    return results
