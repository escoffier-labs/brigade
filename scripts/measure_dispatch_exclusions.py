#!/usr/bin/env python3
"""Issue #1946 dispatch exclusion index measurement harness.

Injects a synthetic intent history into a temporary target and compares the
pre-index full scan with the persisted incremental index. Nothing here touches
the operator workspace: the target is a fresh temporary directory.

Cells reported per planning call (``exclusions()``, what ``_plan`` calls):

* ``full_scan`` reproduces the pre-#1946 implementation, which parsed every
  receipt twice: once for failure counts and once for exclusions;
* ``cold`` is the first indexed call, which rebuilds from the receipts;
* ``warm`` is a tick with no journal change;
* ``tick`` appends one receipt and measures the following planning call.

Standard library plus the in-repo ``brigade`` package only.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path
from typing import Callable

from brigade import localio, work_cmd, worker_failure
from brigade.work_cmd import dispatch_index, dispatch_state


def _receipt_count(path: Path) -> bool:
    return path.parent.name == "intents" and path.suffix == ".json"


def _inject(target: Path, count: int, *, tasks: int = 200, sessions: int = 40) -> None:
    ledger = [
        {
            "id": f"task-{index}",
            "text": f"Change module {index}",
            "status": "pending",
            "created_at": "2026-01-01T00:00:00Z",
            "item_revision": 2,
        }
        for index in range(tasks)
    ]
    work_cmd._write_task_ledger(target, {"version": 2, "tasks": ledger, "edges": []})
    for index in range(count):
        session = f"dispatch-{index % sessions:08x}"
        task_id = f"task-{index % tasks}"
        attempt = index // tasks + 1
        intent_id = f"{index:016x}"
        if index % 7 == 0:
            state, terminal = "planned", None
        elif index % 5 == 0:
            state, terminal = "completed", None
        else:
            state = "failed"
            terminal = {"failure_kind": "worker-exhausted", "infrastructure": False}
        localio.write_json(
            dispatch_state.root(target) / session / "intents" / f"{intent_id}.json",
            {
                "intent_id": intent_id,
                "task_id": task_id,
                "task_item_revision": 2,
                "attempt_no": attempt,
                "claim_id": "dispatch-" + intent_id,
                "state": state,
                "state_seq": 1,
                "created_at": f"2026-01-01T00:00:00.{index:06d}+00:00",
                "updated_at": f"2026-01-01T00:00:00.{index:06d}+00:00",
                "terminal": terminal,
            },
        )


def _full_scan_exclusions(target: Path) -> dict[str, str]:
    """The pre-#1946 implementation, kept here as the comparison arm."""
    from brigade.work_cmd import claiming
    from brigade.work_cmd.ledger import locking

    resets: dict[str, str] = {}
    for path in (dispatch_state.root(target) / "resets").glob("*.json"):
        row = dispatch_state.read(path)
        resets[row["task_id"]] = max(resets.get(row["task_id"], ""), row["created_at"])
    counts: dict[str, int] = {}
    for _, row in dispatch_state.intents(target):
        task_id = row["task_id"]
        if row["created_at"] <= resets.get(task_id, ""):
            continue
        if row["state"] == "completed":
            counts[task_id] = 0
        elif row["state"] == "failed":
            terminal = row.get("terminal") or {}
            if (
                terminal.get("infrastructure") is not True
                and terminal.get("failure_kind") not in worker_failure.RUN_POLICY_TERMINAL_FAILURE_KINDS
            ):
                counts[task_id] = counts.get(task_id, 0) + 1
    result = {task_id: "circuit-breaker" for task_id, count in counts.items() if count >= 3}
    tasks = {task["id"]: task for task in locking._read_task_ledger(target)["tasks"]}
    for _, row in dispatch_state.intents(target):
        task_id = row["task_id"]
        if row["state"] in dispatch_state.ACTIVE or row["state"] == "unknown":
            result[task_id] = "active-intent"
        elif row["state"] == "completed" and row["created_at"] > resets.get(task_id, ""):
            task = tasks.get(task_id, {})
            claim = claiming.claim_record(task)
            revision = row.get("claimed_item_revision", row["task_item_revision"] + 1)
            if (
                task.get("status") == "in_progress"
                and claim is not None
                and claim["claim_id"] == row["claim_id"]
                and task.get("item_revision") == revision
            ):
                result[task_id] = "completed-unlanded"
    return result


def _instrumented(attribute: str, module: object, call: Callable[[], object]) -> tuple[object, int]:
    original = getattr(module, attribute)
    reads = 0

    def counting(path: Path, *args: object, **kwargs: object) -> object:
        nonlocal reads
        if _receipt_count(path):
            reads += 1
        return original(path, *args, **kwargs)

    setattr(module, attribute, counting)
    try:
        result = call()
    finally:
        setattr(module, attribute, original)
    return result, reads


def _inject_extra(target: Path, index: int) -> None:
    session = f"dispatch-{index % 40:08x}"
    task_id = f"task-{index % 200}"
    intent_id = f"{index:016x}"
    localio.write_json(
        dispatch_state.root(target) / session / "intents" / f"{intent_id}.json",
        {
            "intent_id": intent_id,
            "task_id": task_id,
            "task_item_revision": 2,
            "attempt_no": index // 200 + 1,
            "claim_id": "dispatch-" + intent_id,
            "state": "completed",
            "state_seq": 1,
            "created_at": f"2026-01-01T00:00:00.{index:06d}+00:00",
            "updated_at": f"2026-01-01T00:00:00.{index:06d}+00:00",
            "terminal": None,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=10_000)
    parser.add_argument("--trials", type=int, default=5)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory)
        _inject(target, args.records)

        def full_scan() -> object:
            return _full_scan_exclusions(target)

        def indexed() -> object:
            return dispatch_state.exclusions(target)

        # One warmup so page cache and imports are not the measurement.
        _instrumented("read", dispatch_state, full_scan)

        full_times: list[float] = []
        full_reads = 0
        for _ in range(args.trials):
            started = time.perf_counter()
            _, reads = _instrumented("read", dispatch_state, full_scan)
            full_times.append(time.perf_counter() - started)
            full_reads = max(full_reads, reads)

        dispatch_index.state_path(target).unlink(missing_ok=True)
        started = time.perf_counter()
        _, cold_reads = _instrumented("_read_record", dispatch_index, indexed)
        cold_time = time.perf_counter() - started

        warm_times: list[float] = []
        warm_reads = 0
        for _ in range(args.trials):
            started = time.perf_counter()
            _, reads = _instrumented("_read_record", dispatch_index, indexed)
            warm_times.append(time.perf_counter() - started)
            warm_reads = max(warm_reads, reads)

        tick_times: list[float] = []
        tick_reads = 0
        for step in range(args.trials):
            _inject_extra(target, args.records + step)
            started = time.perf_counter()
            _, reads = _instrumented("_read_record", dispatch_index, indexed)
            tick_times.append(time.perf_counter() - started)
            tick_reads = max(tick_reads, reads)

        receipts = len(dispatch_state.intents(target))
        rows = [
            ("full_scan (pre-index)", full_reads, statistics.median(full_times)),
            ("cold (rebuild)", cold_reads, cold_time),
            ("warm (no change)", warm_reads, statistics.median(warm_times)),
            ("tick (+1 receipt)", tick_reads, statistics.median(tick_times)),
        ]
        print(f"receipts injected: {receipts}")
        print(f"trials: {args.trials} (median for repeated cells)")
        print()
        print("| cell | receipts read | ms per planning call |")
        print("| --- | ---: | ---: |")
        for name, reads, seconds in rows:
            print(f"| {name} | {reads} | {seconds * 1000:.1f} |")
        print()
        print(
            json.dumps(
                {"records": receipts, "cells": [{"cell": row[0], "reads": row[1], "seconds": row[2]} for row in rows]}
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
