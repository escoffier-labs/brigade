"""Read-only dispatch planning with receipts isolated under .brigade/dispatch."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from .. import __version__, localio, roster
from . import claiming, constants, edges, footprint, partition
from .ledger import locking, queries_handoffs


def live_children(target: Path) -> int:
    """Planning has no intents yet. The apply slice will inspect live children here."""
    return 0


def _seat_map(values: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        seat_class, separator, seat = value.partition("=")
        seat_class, seat = seat_class.strip(), seat.strip()
        if not separator or seat_class not in constants.TASK_SEAT_CLASSES or not seat:
            raise ValueError("--seat-map requires CLASS=SEAT with class mechanical, judgment, or review")
        if seat_class in result:
            raise ValueError(f"duplicate --seat-map class: {seat_class}")
        result[seat_class] = seat
    return result


def _plan(
    target: Path,
    ledger: dict[str, Any],
    *,
    cap: int,
    batch_size: int,
    wave: int | None,
    worker: str | None,
    seat_map: dict[str, str],
) -> dict[str, Any]:
    resolution = edges.resolve_readiness(ledger)
    tasks = {str(task["id"]): task for task in ledger["tasks"] if isinstance(task, dict) and "id" in task}
    skipped = [{"task_id": item["id"], "reason": item["reason"]} for item in resolution.blocked]
    startable = []
    for item in sorted(resolution.ready, key=claiming.ready_sort_key):
        task_id = item["id"]
        try:
            queries_handoffs._evaluate_task_start_gates(target, ledger, tasks[task_id], require_pending=True)
        except claiming.ClaimError as exc:
            skipped.append({"task_id": task_id, "reason": exc.reason})
            continue
        if seat_map and item.get("seat_class") not in seat_map:
            skipped.append({"task_id": task_id, "reason": "unmapped_seat_class"})
            continue
        startable.append(item)
    occupied = [
        footprint.task_footprint(task)
        for task in tasks.values()
        if task.get("status") == "in_progress" and claiming.claim_record(task) is not None
    ]
    waves = partition.partition_ready(startable, tasks, target, occupied=occupied)
    skipped.extend(waves.get("skipped", []))
    wave_list = waves["waves"]
    if wave is not None and wave >= len(wave_list):
        raise ValueError(f"wave {wave} does not exist ({len(wave_list)} dispatchable waves)")
    wave_index = wave if wave is not None else (0 if wave_list else None)
    candidates = []
    for entry in wave_list:
        if entry["index"] != wave_index:
            skipped.extend({"task_id": task_id, "reason": "other_wave"} for task_id in entry["task_ids"])
            continue
        for rank, item in enumerate(entry["tasks"]):
            task_id = item["id"]
            candidates.append(
                {
                    "rank": rank,
                    "task_id": task_id,
                    "task_item_revision": tasks[task_id].get("item_revision", 0),
                    "seat": worker or seat_map.get(item.get("seat_class", "")),
                }
            )
    children = live_children(target)
    capacity = max(0, cap - children)
    count = min(capacity, batch_size, len(candidates))
    limit_reason = "capacity" if capacity <= batch_size else "batch_size"
    skipped.extend({"task_id": item["task_id"], "reason": limit_reason} for item in candidates[count:])
    plan = {
        "schema": "brigade.work_dispatch_plan.v1",
        "ledger_digest": localio.canonical_json_digest(ledger),
        "wave": wave_index,
        "cap": cap,
        "live_children": children,
        "capacity": capacity,
        "batch_size": batch_size,
        "ready_in_wave": len(candidates),
        "dispatch_count": count,
        "worker": worker,
        "seat_map": seat_map,
        "candidates": candidates,
        "selected": candidates[:count],
        "skipped": sorted(skipped, key=lambda item: (item["task_id"], item["reason"])),
        "partition_mode": waves["partition_mode"],
        "partition_degraded": waves["partition_degraded"],
    }
    # Apply/resume must reload this saved plan by dispatch id, never re-plan:
    # claims mutate the ledger and would change the digest and future intent ids.
    plan["plan_digest"] = localio.canonical_json_digest(plan)
    return plan


def _base_sha(target: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=target, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def dispatch(
    *,
    target: Path,
    wave: int | None = None,
    max_workers: int | None = None,
    batch_size: int | None = None,
    worker: str | None = None,
    seat_map: Sequence[str] = (),
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    if not dry_run:
        message = "work dispatch currently plans only. Use --dry-run. Launching lands in a follow-up slice of #1849."
        if json_output:
            print(json.dumps({"error": message, "reason": "launch_not_implemented"}))
        else:
            print(f"error: {message}", file=sys.stderr)
        return 2
    try:
        target = target.expanduser().resolve()
        if not target.is_dir():
            raise ValueError(f"target is not a directory: {target}")
        for name, number in (("--wave", wave), ("--max", max_workers), ("--batch-size", batch_size)):
            if number is not None and number < 0:
                raise ValueError(f"{name} must be nonnegative")
        if worker is not None:
            worker = worker.strip()
            if not worker or seat_map:
                raise ValueError("use a nonempty --worker or --seat-map, exclusively")
        mapping = _seat_map(seat_map)
        roster_path = None
        cap = max_workers
        try:
            roster_path = roster.resolve_roster_path(target)
        except FileNotFoundError:
            if cap is None:
                raise
        if roster_path is not None:
            configured = roster.load_roster(roster_path)
            seats = set(mapping.values()) | ({worker} if worker is not None else set())
            unknown = seats - configured.agents.keys()
            if unknown:
                raise ValueError(f"unknown roster seats: {', '.join(sorted(unknown))}")
            if cap is None:
                cap = configured.max_workers
        assert cap is not None
        plan = _plan(
            target,
            locking._read_task_ledger(target),
            cap=cap,
            batch_size=batch_size if batch_size is not None else cap,
            wave=wave,
            worker=worker,
            seat_map=mapping,
        )
        dispatch_id = f"dispatch-{uuid4().hex}"
        directory = target / ".brigade" / "dispatch" / dispatch_id
        session = {
            "schema": "brigade.work_dispatch_session.v1",
            "dispatch_id": dispatch_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "target": str(target),
            "base_sha": _base_sha(target),
            "tool_version": __version__,
            "roster_path": str(roster_path) if roster_path is not None else None,
            "requested_wave": wave,
            "cap": cap,
            "batch_size": plan["batch_size"],
            "worker": worker,
            "seat_map": mapping,
            "ledger_digest": plan["ledger_digest"],
            "plan_digest": plan["plan_digest"],
            "dry_run": True,
        }
        directory.mkdir(parents=True, exist_ok=False)
        localio.write_json_exclusive(directory / "plan.json", plan)
        localio.write_json_exclusive(directory / "session.json", session)
    except locking.TaskLedgerError:
        raise
    except (OSError, ValueError) as exc:
        if json_output:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps({"dispatch_id": dispatch_id, "plan": plan}, indent=2, sort_keys=True))
    else:
        print(f"dispatch: {dispatch_id}")
        print(f"wave: {plan['wave']}, planned: {plan['dispatch_count']}, capacity: {plan['capacity']}")
        print(f"plan: {directory / 'plan.json'}")
    return 0
