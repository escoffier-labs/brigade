"""Status probes for registered Codex Cloud tasks missing from a listed inventory."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Iterable

from . import codex_cloud

# The brief probes at most this many registered tasks missing from its one-page
# inventory, newest dispatch first.
BRIEF_STATUS_PROBE_LIMIT = 5
# The brief's whole Codex observation, the inventory page plus these probes, shares
# one budget that fits inside the 5 s SessionStart hook timeout.
BRIEF_CODEX_BUDGET_SECONDS = 3.0


def brief_deadline() -> float:
    """Return the monotonic deadline for one brief's Codex observation."""
    return time.monotonic() + BRIEF_CODEX_BUDGET_SECONDS


def observe_missing_tasks(
    entries: Iterable[dict[str, Any]],
    tasks: dict[str, Any],
    *,
    run_text: Callable[..., tuple[int, str, str]],
    cwd: Path,
    bounded: bool = False,
    deadline: float | None = None,
) -> None:
    """Add a state to ``tasks`` for each registered Codex task a status probe resolves."""
    # Failure or absence never implies a terminal state, so an unprobed task stays
    # unobserved rather than guessed.
    # The registry can hold several rows for one task, so keep each task's newest
    # registration and probe it once.
    by_task: dict[str, dict[str, Any]] = {}
    for entry in entries:
        task_id = entry.get("task_id")
        if not isinstance(task_id, str) or entry.get("provider") != "codex-cloud" or task_id in tasks:
            continue
        known = by_task.get(task_id)
        if known is None or str(entry.get("dispatched_at") or "") > str(known.get("dispatched_at") or ""):
            by_task[task_id] = entry
    missing = list(by_task.values())
    if bounded:
        missing.sort(key=lambda entry: str(entry.get("dispatched_at") or ""), reverse=True)
        del missing[BRIEF_STATUS_PROBE_LIMIT:]
    for entry in missing:
        task_id = entry["task_id"]
        if deadline is None:
            scode, sout, serr = run_text(["codex", "cloud", "status", task_id], cwd=cwd)
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            scode, sout, serr = run_text(["codex", "cloud", "status", task_id], cwd=cwd, timeout=remaining)
        blob = (sout + "\n" + serr).strip()
        if scode != 0 and not blob:
            continue
        # A failed probe still prints `[ERROR] <title>` for errored tasks, but a
        # diagnostic such as "request failed" is not a task state, so a nonzero
        # exit only counts with a structured bracket or status line.
        if scode == 0:
            state = codex_cloud._scan_status(blob)  # noqa: SLF001 - shared status scanner
        else:
            state = codex_cloud._verified_status(blob)  # noqa: SLF001 - shared status scanner
        if state is not None:
            tasks[task_id] = {"state": state, "ready_at": None}
