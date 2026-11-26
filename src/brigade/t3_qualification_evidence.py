"""Local evidence adapter for the matched T3 qualification."""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    localio,
    native_child_journal,
    native_t3_capacity,
    native_t3_enrollment,
    native_t3_host,
    native_t3_lifecycle,
    roster_readiness,
    roster_readiness_t3,
    run_batch,
    run_batch_workspace,
    run_tasks,
)
from .fleet_t3_transport import clean_source_revision as _clean_revision
from .source_revision import SourceRevisionError

MODEL = "gpt-6.2-sol"
EFFORT = "low"
TIER = "default"
SLOTS = 3
MEASUREMENTS_SCHEMA = "brigade.t3_qualification_measurements.v1"
RECORD_SCHEMA = "brigade.orca_qualification.v1"
COUNTERS = ("input_tokens", "output_tokens", "cached_input_tokens")
READ_ONLY_ENFORCEMENT = {"t3": "prompt-only", "run-batch": "brigade-read-only-policy"}


def require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    require(isinstance(value, dict), f"expected JSON object in {path.name}")
    return value


def clock() -> dict[str, int]:
    return {"monotonic_ns": time.monotonic_ns(), "wall_ns": time.time_ns()}


def count(value: Any) -> int:
    require(type(value) is int and value >= 0, "expected a nonnegative integer")
    return value


def seconds(value: Any) -> float:
    require(type(value) in (int, float) and math.isfinite(value) and value >= 0, "invalid duration")
    return float(value)


def utc(value: str) -> int:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(timestamp.tzinfo is not None, "missing timezone in turn timestamp")
    delta = timestamp - datetime(1970, 1, 1, tzinfo=UTC)
    return ((delta.days * 86400 + delta.seconds) * 10**6 + delta.microseconds) * 1000


def clean_revision(path: Path) -> str:
    try:
        return _clean_revision(path)
    except SourceRevisionError as exc:
        raise ValueError("qualification checkout must exist and be clean") from exc


def check_native_idle(workspace: Path) -> None:
    occupancy = native_t3_capacity.governed_occupancy(workspace)
    require(occupancy.authoritative and occupancy.live == 0, "governed native capacity is held or unknown")


def timing(measurements: dict[str, Any], tasks: set[str]) -> tuple[float, dict[str, float | None]]:
    opened, verified = (measurements[key] for key in ("opened", "verified"))
    opened_mono, verified_mono = (count(mark["monotonic_ns"]) for mark in (opened, verified))
    opened_wall, verified_wall = (count(mark["wall_ns"]) for mark in (opened, verified))
    elapsed = verified_mono - opened_mono
    # The wall readings anchor timestamps from another process to the local
    # monotonic clock. Reject a clock step instead of reporting broken phases.
    wall_span = verified_wall - opened_wall
    require(
        elapsed > 0 and wall_span > 0 and abs(wall_span - elapsed) <= 50_000_000,
        "wall clock changed during measurement",
    )
    turns = measurements["turns"]
    require(turns.keys() == tasks, "turns do not cover the qualification tasks")
    starts, ends = [], []
    for turn in turns.values():
        started, ended = utc(turn["started_at"]), utc(turn["ended_at"])
        require(opened_wall <= started <= ended <= verified_wall, "turn is outside the batch clock marks")
        starts.append(started)
        ends.append(ended)
    # Project wall timestamps into the interval defined by the two paired
    # monotonic anchors. Each phase then uses the same scale and endpoint.
    queue_and_startup = (min(starts) - opened_wall) * elapsed // wall_span
    terminal = (max(ends) - opened_wall) * elapsed // wall_span
    execution = terminal - queue_and_startup
    return seconds(elapsed / 1e9), {
        "queue": seconds(queue_and_startup / 1e9),
        "startup": None,
        "planning": 0.0,
        "execution": seconds(execution / 1e9),
        "synthesis": 0.0,
        "delivery": seconds((elapsed - queue_and_startup - execution) / 1e9),
    }


def _worker_receipt(run_dir: Path, task: run_tasks.TaskResult) -> dict[str, Any]:
    key = hashlib.sha256(task.handle.spec.task_id.encode()).hexdigest()
    raw = (run_dir / f"batch-result-{key}.json").read_bytes()
    require(
        task.artifact_kind == "receipt" and hashlib.sha256(raw).hexdigest() == task.artifact_digest,
        "worker receipt does not match the terminal journal result",
    )
    require(task.containment == "quiescent", "worker is not confirmed quiescent")
    receipt = json.loads(raw)
    require(
        receipt.get("ok") is True
        and receipt.get("requested_model") == MODEL
        and receipt.get("effective_model", MODEL) == MODEL
        and receipt.get("reasoning") == EFFORT,
        "worker receipt does not match the model and effort",
    )
    return receipt


def _provider_turn(
    sessions: Path, tree: Path, opened: dict[str, int], base: str, *, retain: Path | None = None
) -> dict[str, Any]:
    """Find the unique fresh Codex turn for this specific clean worktree."""
    candidates = []
    for path in sessions.rglob("*.jsonl"):
        if path.stat().st_mtime_ns < opened["wall_ns"]:
            continue
        with path.open(encoding="utf-8") as stream:
            first = stream.readline()
            if not first:
                continue
            meta = json.loads(first)
            if meta.get("type") == "session_meta" and meta["payload"].get("cwd") == str(tree):
                candidates.append(path)
    require(len(candidates) == 1, "expected one fresh Codex session for the isolated worktree")
    raw = candidates[0].read_bytes()
    result = _session_turn(raw, tree, base)
    if retain is not None:
        localio.write_bytes_atomic(retain, raw)
    return result


def _session_turn(raw: bytes, tree: Path, base: str) -> dict[str, Any]:
    initial, *rows = [json.loads(line) for line in raw.splitlines()]
    meta = initial["payload"]
    require(initial["type"] == "session_meta" and meta.get("cwd") == str(tree), "Codex session worktree mismatch")
    require(meta.get("git", {}).get("commit_hash") == base, "Codex session used a different source revision")
    (turn,) = [row["payload"] for row in rows if row["type"] == "turn_context"]
    (settings_event,) = [row["payload"] for row in rows if row["payload"].get("type") == "thread_settings_applied"]
    settings = settings_event["thread_settings"]
    require(
        settings_event["thread_id"] == meta["id"]
        and settings["cwd"] == str(tree)
        and (turn.get("model"), turn.get("effort")) == (MODEL, EFFORT)
        and (settings["model"], settings["reasoning_effort"], settings["service_tier"]) == (MODEL, EFFORT, TIER),
        "Codex turn does not confirm standard service, model and effort",
    )
    (started,) = [row["timestamp"] for row in rows if row["payload"].get("type") == "task_started"]
    (ended,) = [row["timestamp"] for row in rows if row["payload"].get("type") == "task_complete"]
    return {
        "started_at": started,
        "ended_at": ended,
        "provider_errors": sum(row["payload"].get("type") in ("error", "stream_error") for row in rows),
    }


def _t3_turn(folder: Path, task: run_tasks.TaskResult, context: dict[str, Any]) -> tuple[Path, str, dict[str, str]]:
    name = task.handle.spec.task_id
    delegation = read(folder / f"delegate-{name}.json")
    status = read(folder / f"status-{name}.json")
    activity = read(folder / f"activity-{name}.json")
    observation = read(folder / f"observe-{name}.json")
    ack = read(folder / f"ack-{name}.json")
    original_run_id = delegation["childRunId"]
    terminal_run_id = status["latestTerminalRunId"]
    options = {row["id"]: row["value"] for row in status["options"]}
    require(
        all(delegation[key] == status[key] for key in ("taskId", "childThreadId", "childRunId", "providerInstanceId"))
        and original_run_id == task.original_run_id
        and terminal_run_id == task.provider_result_id
        and status["taskId"] == task.provider_task_id
        and status["status"] == "completed"
        and not status["hasPendingChildRuns"]
        and not status["hasPendingQuestion"]
        and status["pendingApproval"] is None
        and (status.get("latestTerminalResultContextTransferId") or status.get("resultContextTransferId"))
        and status["executedModel"] == MODEL
        and status["providerInstanceId"] == context["provider_instance"]
        and status["options"] == delegation["options"]
        and (options.get("reasoningEffort"), options.get("serviceTier")) == (EFFORT, TIER),
        "T3 status does not match the completed delegation",
    )
    thread = activity["thread"]
    require(
        activity["hasMore"] is False
        and thread["threadId"] == status["childThreadId"]
        and thread["parentThreadId"] == context["parent_thread"]
        and thread["projectId"] == context["project"]
        and thread["activeRunId"] is None,
        "T3 child activity is incomplete or mismatched",
    )
    (recent,) = [run for run in activity["recentRuns"] if run["runId"] == terminal_run_id]
    require(
        recent["status"] == "completed"
        and recent["executedModel"] == MODEL
        and recent["providerInstanceId"] == status["providerInstanceId"],
        "T3 activity does not match the terminal child run",
    )
    require(
        observation["run_id"] == ack["run_id"] == context["run_ids"]["t3"]
        and observation["task_id"] == ack["task_id"] == name
        and observation["attempt_id"] == task.handle.attempt_id
        and observation["status"] == "completed"
        and observation["result_id"] == ack["result_id"] == task.result_id
        and task.acknowledged,
        "T3 observation and acknowledgement do not match the journal",
    )
    turn = {"started_at": recent["startedAt"], "ended_at": recent["completedAt"]}
    if terminal_run_id != original_run_id:
        turn.update(original_run_id=original_run_id, terminal_run_id=terminal_run_id)
    return (
        Path(thread["worktreePath"]).resolve(),
        status.get("latestTerminalSummary") or status["summary"],
        turn,
    )


def log_count(path: Path) -> int:
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    require(all(isinstance(entry, str) and entry.strip() for entry in entries), f"invalid entry in {path.name}")
    return len(entries)


def parent_settings(folder: Path, context: dict[str, Any]) -> dict[str, str]:
    capabilities = read(folder / "capabilities.json")
    report = read(folder / "capabilities-report.json")
    require(
        report["schema"] == roster_readiness_t3.SCHEMA
        and report["capabilities"] == capabilities
        and report["origin_environment"] == context["environment"]
        and report["origin_project"] == context["project"]
        and capabilities["parentThreadId"] == context["parent_thread"],
        "T3 capabilities report does not match the parent",
    )
    options = {row["id"]: row["value"] for row in capabilities["inheritedOptions"]}
    settings = {
        "model": capabilities["inheritedModel"],
        "effort": options.get("reasoningEffort"),
        "service_tier": options.get("serviceTier"),
    }
    provider = capabilities["inheritedProviderInstanceId"]
    require(
        settings == {"model": MODEL, "effort": "medium", "service_tier": TIER}
        and any(
            item["providerInstanceId"] == provider and item["driverKind"] == "codex"
            for item in capabilities["providers"]
        ),
        "T3 parent must be Codex gpt-6.2-sol at medium effort and default tier",
    )
    readiness = read(folder / "readiness.json")
    request = readiness["request"]
    reported = utc(report["reported_at"]) / 1e9
    require(
        readiness["schema"] == roster_readiness.SCHEMA
        and readiness["status"] == "ready"
        and readiness["exit_code"] == 0
        and all(readiness.get(key) == value for key, value in roster_readiness.T3_QUALIFIERS.items())
        and readiness["t3_reported_at"] == reported
        and reported <= readiness["t3_received_at"] <= read(folder / "opened.json")["wall_ns"] / 1e9
        and readiness["t3_received_at"] - reported <= roster_readiness.OPERATION_TTL_SECONDS
        and request["route"] == "t3"
        and request["isolation"] == "worktree"
        and request["concurrency"] == SLOTS
        and request["workers"] == [context["seat"]],
        "T3 readiness is missing or mismatched",
    )
    return settings


def _validate_t3_marks(folder: Path, opened: dict[str, int], verified: dict[str, int], tasks: set[str]) -> None:
    names = ("delegate-start", "delegate-replies", *(f"wake-{task}" for task in sorted(tasks)))
    first, replied, *wakes = [count(read(folder / f"{name}.json")["monotonic_ns"]) for name in names]
    require(
        count(opened["monotonic_ns"]) <= first <= replied <= min(wakes)
        and max(wakes) <= count(verified["monotonic_ns"]),
        "T3 delegate and wake marks are out of order",
    )


def _verified_turns(
    arm: str,
    workspace: Path,
    run_dir: Path,
    folder: Path,
    context: dict[str, Any],
    members: tuple[run_tasks.TaskResult, ...],
    *,
    sessions: Path | None = None,
) -> dict[str, Any]:
    expected = context["answers"]
    require({task.handle.spec.task_id for task in members} == expected.keys(), "journal task set mismatch")
    trees = {workspace}
    turns = {}
    for task in members:
        name = task.handle.spec.task_id
        require(task.status == "completed" and task.result_id is not None, f"{name} has no completed terminal result")
        if arm == "t3":
            tree, output, turn = _t3_turn(folder, task, context)
        else:
            tree = run_batch_workspace.expected_path(workspace, run_dir.name, name).resolve()
            output = _worker_receipt(run_dir, task)["text"]
        require(tree not in trees and clean_revision(tree) == context["base_sha"], "worktree or base revision mismatch")
        require(json.loads(output) == expected[name], f"{name} answer did not match Git's pinned file digest")
        if arm == "run-batch":
            retained = folder / f"session-{name}.jsonl"
            if sessions is None:
                turn = _session_turn(retained.read_bytes(), tree, context["base_sha"])
            else:
                turn = _provider_turn(
                    sessions, tree, read(folder / "opened.json"), context["base_sha"], retain=retained
                )
        trees.add(tree)
        turns[name] = turn
    return turns


def measure(arm: str, workspace: Path, run_dir: Path, folder: Path, sessions: Path | None = None) -> dict[str, Any]:
    """Verify outputs independently and capture the monotonic endpoint."""
    context = read(folder / "expected.json")
    opened = read(folder / "opened.json")
    batch = run_tasks.read_task_batch(run_dir, request_id=context["request_id"])
    turns = _verified_turns(arm, workspace, run_dir, folder, context, batch.members, sessions=sessions)
    verified = clock()
    measurements = {
        "schema": MEASUREMENTS_SCHEMA,
        "run_id": run_dir.name,
        "opened": opened,
        "verified": verified,
        "turns": turns,
    }
    timing(measurements, set(context["answers"]))
    if arm == "t3":
        _validate_t3_marks(folder, opened, verified, set(turns))
    localio.write_json_exclusive(folder / "verified.json", verified)
    return measurements


def collect(
    arm: str, run_dir: Path, status: dict[str, Any], measurements: dict[str, Any], folder: Path
) -> dict[str, Any]:
    """Project the paired experiment into the existing closed record shape."""
    require(measurements.get("schema") == MEASUREMENTS_SCHEMA, "expected matched qualification measurements")
    context = read(folder / "expected.json")
    base = context["base_sha"]
    workspace = Path(context["workspace"])
    payload = read(folder.parent / "batch.json")
    canonical = run_batch.validate_batch(payload)
    plan = run_batch.plan_batch(payload, base_sha=base)
    expected_tasks = set(context["answers"])
    require(
        plan["schema"] == run_batch.SCHEMA
        and plan["request_id"] == context["request_id"]
        and plan["fingerprint"] == context["batch_fingerprint"]
        and plan["layers"] == [sorted(expected_tasks)]
        and plan["synthesis"] is None
        and len(expected_tasks) == SLOTS
        and all(row["read_only"] and row["isolation"] == "worktree" for row in plan["tasks"]),
        "shared batch differs from the qualification envelope",
    )
    require(
        arm in ("t3", "run-batch")
        and run_dir.name == context["run_ids"][arm] == status["run_id"] == measurements["run_id"]
        and measurements["opened"] == read(folder / "opened.json")
        and measurements["verified"] == read(folder / "verified.json"),
        "run identity or measurement mark mismatch",
    )
    batch = run_tasks.read_task_batch(run_dir, request_id=plan["request_id"])
    require({task.handle.spec.task_id for task in batch.members} == expected_tasks, "journal task set mismatch")
    require(all(task.status == "completed" and task.result_id for task in batch.members), "batch has unverified tasks")
    events = native_child_journal.read_events(run_dir)

    if arm == "t3":
        marker = read(run_dir / "t3-enrollment.json")
        require(
            marker.get("schema") == native_t3_enrollment.SCHEMA
            and marker.get("request_id") == plan["request_id"]
            and marker.get("fingerprint") == plan["fingerprint"]
            and marker.get("source_revision") == base,
            "T3 enrollment does not bind the shared batch",
        )
        require(
            status == native_t3_lifecycle.status(workspace=workspace, run_id=run_dir.name)
            and status["status"] == "completed"
            and all(
                row["terminal"] is True and row["held_reason"] is None and row["acked"] is True
                for row in status["tasks"]
            ),
            "T3 batch must be finalized, acknowledged and clear of holds",
        )
        for task in batch.members:
            retained = native_t3_enrollment.read_delegation_input(run_dir, task_id=task.handle.spec.task_id)
            requested = next(row for row in canonical["tasks"] if row["task_id"] == retained.task_id)
            require(
                retained.seat == requested["seat"] and retained.text == requested["text"],
                "T3 enrollment does not bind the requested task",
            )
            target = retained.target
            options = dict(target.options)
            require(
                target.source_revision == base
                and target.isolation == "worktree"
                and target.launch_model == MODEL
                and target.driver == "codex"
                and target.provider_instance == context["provider_instance"]
                and options.get("reasoningEffort") == EFFORT
                and options.get("serviceTier") == TIER,
                "retained T3 target differs from the matched envelope",
            )
        parent_settings(folder, context)
        _validate_t3_marks(folder, measurements["opened"], measurements["verified"], expected_tasks)
    else:
        batch_marker = run_batch.read_request_marker(run_dir)
        require(
            batch_marker is not None
            and all(batch_marker.get(key) == plan[key] for key in ("fingerprint", "base_sha"))
            and all(batch_marker.get(key) == canonical[key] for key in ("schema", "request_id", "tasks", "synthesis")),
            "batch reservation does not bind the shared batch",
        )
        require(
            status["schema"] == "brigade.run_batch_result.v1"
            and status["request_id"] == plan["request_id"]
            and status["run_status"] == "ok"
            and status["synthesis"] is None
            and set(status["tasks"]) == expected_tasks
            and read(run_dir / "run.json").get("status") == "ok",
            "direct batch has not settled",
        )
        for task in batch.members:
            require(task.workspace and task.workspace.base_sha == base, "missing pinned batch worktree")
            require(
                status["tasks"][task.handle.spec.task_id]["result_id"] == task.result_id,
                "batch status does not bind the journal result",
            )

    intervention_events = {"task.cancel-requested", native_t3_host.CANCEL_EVENT, native_child_journal.HELD_EVENT}
    turns = _verified_turns(arm, workspace, run_dir, folder, context, batch.members)
    require(turns == measurements["turns"], "turn measurements do not match retained evidence")
    elapsed, phases = timing({**measurements, "turns": turns}, expected_tasks)
    return {
        "schema": RECORD_SCHEMA,
        "arm": arm,
        "orca_version": None,
        "brigade_commit": context["brigade_commit"],
        "model": MODEL,
        "effort": EFFORT,
        "source_revision": base,
        "isolation": "worktree",
        "concurrency": SLOTS,
        "configuration_mismatch": False,
        "mismatched_fields": [],
        "read_only_enforcement": READ_ONLY_ENFORCEMENT[arm],
        "all_tasks_terminal": True,
        "held_capacity": 0,
        "slot_check_passed": True,
        "circuit_breaker_check_passed": True,
        "errors": {
            "failed_tasks": 0,
            "nonterminal_tasks": 0,
            "circuit_broken_tasks": 0,
            "other": log_count(folder / "errors.jsonl")
            + sum(count(turn.get("provider_errors", 0)) for turn in turns.values()),
        },
        "parent_interventions": sum(event.event_type in intervention_events for event in events)
        + log_count(folder / "interventions.jsonl"),
        "verified_completion_seconds": elapsed,
        "timings_seconds": phases,
        "usage": dict.fromkeys(COUNTERS),
    }
