"""Project local qualification evidence into one exportable batch record."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any

from . import run_task_journal, run_tasks

TASKS = frozenset({"alpha", "beta", "gamma"})
MODELS = ("gpt-6-luna", "gpt-6.2-sol", "deepseek-v4.1-flash")
EFFORTS = ("default", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
ARMS = ("orca", "run-batch")
PHASES = ("queue", "startup", "planning", "execution", "synthesis", "delivery")
COUNTERS = ("input_tokens", "output_tokens", "cached_input_tokens")


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def _read(path: Path) -> dict[str, Any]:
    return _object(json.loads(path.read_bytes()))


def _pattern(value: object, pattern: str) -> str:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError("invalid version or revision")
    return value


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("invalid nonnegative count")
    return value


def _seconds(value: object) -> float | int | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0 or not math.isfinite(value):
        raise ValueError("invalid duration")
    return value


def collect(arm: str, run_dir: Path, status_path: Path, measurements_path: Path) -> dict[str, Any]:
    """Read the durable task ledger and the saved post-batch observations."""
    measurements = _read(measurements_path)
    model, effort = measurements["model"], measurements["effort"]
    concurrency = _count(measurements["concurrency"])
    if arm not in ARMS or model not in MODELS or effort not in EFFORTS or not 1 <= concurrency <= len(TASKS):
        raise ValueError("invalid qualification configuration")

    results = {result.handle.spec.task_id: result for result in run_tasks.read_tasks(run_dir)}
    if results.keys() != TASKS:
        raise ValueError("journal does not contain the three qualification tasks")
    receipt = _read(status_path)
    if receipt.get("schema") != "brigade.orca_status.v1":
        raise ValueError("expected a Brigade Orca status receipt")
    if arm == "orca" and receipt.get("run_id") != run_dir.name:
        raise ValueError("Orca status does not match the journal run")
    live = _object(receipt["capacity"]).get("live")
    held = None if live is None else _count(live)
    rows = {}
    for entry in receipt["tasks"]:
        row = _object(entry)
        if type(row.get("holds_capacity")) is not bool:
            raise ValueError("missing task capacity evidence")
        task_id = row["task_id"]
        if task_id in rows:
            raise ValueError("duplicate Orca status task")
        rows[task_id] = row
    if arm == "orca" and rows.keys() != TASKS:
        raise ValueError("Orca status does not cover the qualification tasks")

    slots_clear = held == 0 and not any(row["holds_capacity"] for row in rows.values())
    terminals = {
        task_id
        for task_id, task in results.items()
        if task.status in run_task_journal.TERMINAL
        and task.result_id is not None
        and (arm == "run-batch" or (rows[task_id].get("terminal") is True and rows[task_id].get("held_reason") is None))
    }
    failed = sum(results[task_id].status != "completed" for task_id in terminals)
    settled = arm == "run-batch" or (
        receipt.get("status") == "completed" and all(row.get("acked") is True for row in rows.values())
    )
    safe = measurements["circuit_breaker_routed_around"] is False
    verified_time = _seconds(measurements["verified_completion_seconds"])
    timings = _object(measurements["timings_seconds"])
    usage = _object(measurements["usage"])
    return {
        "schema": "brigade.orca_qualification.v1",
        "arm": arm,
        "orca_version": _pattern(measurements["orca_version"], r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}"),
        "brigade_commit": _pattern(measurements["brigade_commit"], r"[0-9a-f]{40}|[0-9a-f]{64}"),
        "source_revision": _pattern(measurements["source_revision"], r"[0-9a-f]{40}|[0-9a-f]{64}"),
        "model": model,
        "effort": effort,
        "concurrency": concurrency,
        "isolation": "worktree",
        "all_tasks_terminal": terminals == TASKS,
        "held_capacity": held,
        "slot_check_passed": slots_clear,
        "circuit_breaker_check_passed": safe,
        "errors": {
            "failed_tasks": failed,
            "nonterminal_tasks": len(TASKS - terminals),
            "circuit_broken_tasks": sum(row.get("start_refusal") == "circuit-broken" for row in rows.values())
            if arm == "orca"
            else 0,
            "other": _count(measurements["other_errors"]),
        },
        "parent_interventions": _count(measurements["parent_interventions"]),
        "verified_completion_seconds": (
            verified_time
            if measurements["verification_passed"] is True
            and settled
            and terminals == TASKS
            and failed == 0
            and slots_clear
            and safe
            else None
        ),
        "timings_seconds": {phase: _seconds(timings.get(phase)) for phase in PHASES},
        "usage": {key: None if usage.get(key) is None else _count(usage[key]) for key in COUNTERS},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arm", choices=ARMS)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("orca_status", type=Path)
    parser.add_argument("measurements", type=Path)
    args = parser.parse_args()
    try:
        record = collect(args.arm, args.run_dir, args.orca_status, args.measurements)
    except (OSError, ValueError, TypeError, KeyError, OverflowError, run_tasks.TaskJournalError):
        parser.exit(2, "Qualification evidence is missing or invalid. Check the local inputs.\n")
    print(json.dumps(record, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
