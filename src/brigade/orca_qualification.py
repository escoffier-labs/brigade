"""Project local qualification evidence into one exportable batch record."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

from . import native_orca_enrollment, native_orca_host, run_batch, run_journal, run_task_journal, run_tasks
from .native_child_host import NativeChildCustodyError, NativeChildError

TASKS = frozenset({"alpha", "beta", "gamma"})
MODELS = ("gpt-6-luna", "gpt-6.2-sol", "deepseek-v4.1-flash")
EFFORTS = ("default", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
ARMS = ("orca", "run-batch")
PHASES = ("queue", "startup", "planning", "execution", "synthesis", "delivery")
COUNTERS = ("input_tokens", "output_tokens", "cached_input_tokens")
REVISION = r"[0-9a-f]{40}|[0-9a-f]{64}"
ISOLATIONS = ("worktree", "shared")


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


def _batch_evidence(run_dir: Path, tasks: list[run_tasks.TaskResult]) -> dict[str, list[Any]]:
    evidence: dict[str, list[Any]] = {"model": [], "effort": [], "source_revision": [], "isolation": []}
    for task in tasks:
        task_digest = hashlib.sha256(task.handle.spec.task_id.encode("utf-8")).hexdigest()
        path = run_dir / f"batch-result-{task_digest}.json"
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raw = None
        worker = {}
        if raw is not None and (
            task.artifact_kind != "receipt" or hashlib.sha256(raw).hexdigest() == task.artifact_digest
        ):
            worker = _object(json.loads(raw))
        evidence["model"].append(worker.get("requested_model"))
        evidence["effort"].append(worker.get("reasoning"))
        evidence["source_revision"].append(task.workspace.base_sha if task.workspace else None)
        evidence["isolation"].append("worktree" if task.workspace else "shared")
    return evidence


def _orca_evidence(run_dir: Path, tasks: list[run_tasks.TaskResult]) -> dict[str, list[Any]]:
    marker = _read(run_dir / "orca-enrollment.json")
    if marker.get("schema") != native_orca_enrollment.SCHEMA:
        raise ValueError("invalid Orca enrollment marker")
    evidence: dict[str, list[Any]] = {
        "model": [],
        "effort": [],
        "source_revision": [marker.get("source_revision")],
        "isolation": [],
        "orca_version": [],
    }
    for task in tasks:
        target = native_orca_enrollment.verify_task(run_dir, task).target
        evidence["model"].append(target.launch_model)
        evidence["effort"].append(target.effort)
        evidence["source_revision"].append(target.source_revision)
        evidence["isolation"].append("worktree" if target.worktree in native_orca_host.NEW_WORKTREES else "shared")
        evidence["orca_version"].append(target.orca_version)
    return evidence


def _matched_configuration(
    arm: str,
    run_dir: Path,
    tasks: list[run_tasks.TaskResult],
    measurements: dict[str, Any],
    capacity: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    evidence = _orca_evidence(run_dir, tasks) if arm == "orca" else _batch_evidence(run_dir, tasks)
    evidence["concurrency"] = [capacity.get("max_workers")]
    allowed = {
        "model": lambda value: value in MODELS,
        "effort": lambda value: value in EFFORTS,
        "source_revision": lambda value: isinstance(value, str) and re.fullmatch(REVISION, value) is not None,
        "isolation": lambda value: value in ISOLATIONS,
        "concurrency": lambda value: type(value) is int and value > 0,
        "orca_version": lambda value: (
            isinstance(value, str) and re.fullmatch(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}", value) is not None
        ),
    }
    observed = {}
    mismatches = []
    for key, values in evidence.items():
        first = values[0]
        agreed = allowed[key](first) and all(type(value) is type(first) and value == first for value in values)
        observed[key] = first if agreed else None
        if not agreed or first != measurements[key] or (key == "isolation" and first != "worktree"):
            mismatches.append(key)
    return observed, mismatches


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
    capacity = _object(receipt["capacity"])
    live = capacity.get("live")
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
    if arm == "run-batch":
        settled = _read(run_dir / "run.json").get("status") == "ok"
        request = _read(run_dir / "batch-request.json")
        hard_read_only = (
            request.get("schema") == run_batch.SCHEMA
            and {row["task_id"] for row in map(_object, request["tasks"]) if row.get("read_only") is True} == TASKS
        )
        enforcement = "brigade-read-only-policy" if hard_read_only else "unverified"
    else:
        enforcement = "prompt-only"
        settled = receipt.get("status") == "completed" and all(row.get("acked") is True for row in rows.values())
    configuration, mismatches = _matched_configuration(arm, run_dir, list(results.values()), measurements, capacity)
    safe = measurements["circuit_breaker_routed_around"] is False
    verified_time = _seconds(measurements["verified_completion_seconds"])
    timings = _object(measurements["timings_seconds"])
    usage = _object(measurements["usage"])
    return {
        "schema": "brigade.orca_qualification.v1",
        "arm": arm,
        "orca_version": _pattern(measurements["orca_version"], r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}"),
        "brigade_commit": _pattern(measurements["brigade_commit"], REVISION),
        **configuration,
        "configuration_mismatch": bool(mismatches),
        "mismatched_fields": mismatches,
        "read_only_enforcement": enforcement,
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
            and not mismatches
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
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        run_tasks.TaskJournalError,
        run_journal.RunJournalError,
        NativeChildError,
        NativeChildCustodyError,
    ):
        parser.exit(2, "Qualification evidence is missing or invalid. Check the local inputs.\n")
    if record["orca_version"] not in native_orca_host.SUPPORTED_VERSIONS:
        print("Warning: Orca version is outside Brigade's supported set.", file=sys.stderr)
    print(json.dumps(record, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
