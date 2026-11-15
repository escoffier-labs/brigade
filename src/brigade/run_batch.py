"""Versioned explicit batch contract and offline planning. No execution or storage."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Literal

from . import run_task_journal as journal

SCHEMA = "brigade.run_batch.v1"
MAX_TASKS = journal.MAX_BATCH_MEMBERS
MAX_TEXT_BYTES = 16 * 1024
MAX_INPUT_BYTES = 256 * 1024


class BatchError(ValueError):
    """Stable machine-readable refusal before any execution."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _fields(value: object, allowed: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.keys() - allowed:
        raise BatchError("invalid-payload", f"{label} must be an object with only documented fields")
    return value


def _selection(value: dict[str, Any], label: str) -> dict[str, str]:
    text = value.get("text")
    if not isinstance(text, str) or not text.strip():
        raise BatchError("empty-text", f"{label}.text must be non-empty text")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise BatchError("invalid-payload", f"{label}.text must be valid Unicode") from exc
    if size > MAX_TEXT_BYTES:
        raise BatchError("text-limit", f"{label}.text exceeds {MAX_TEXT_BYTES} UTF-8 bytes")
    seat = value.get("seat")
    if not isinstance(seat, str) or not journal.token(seat):
        raise BatchError("invalid-seat", f"{label}.seat must be a 1-512 character ASCII token")
    return {"text": text, "seat": seat}


def _layers(tasks: list[dict[str, Any]]) -> list[list[str]]:
    dependencies = {t["task_id"]: set(t["depends_on"]) for t in tasks}
    layers: list[list[str]] = []
    done: set[str] = set()
    while len(done) < len(tasks):
        ready = sorted(k for k, deps in dependencies.items() if k not in done and deps <= done)
        if not ready:
            # Follow remaining dependency edges until one repeats. At most eight nodes.
            path: list[str] = []
            node = min(set(dependencies) - done)
            while node not in path:
                path.append(node)
                node = min(dependencies[node] - done)
            cycle = path[path.index(node) :] + [node]
            raise BatchError("dependency-cycle", "dependency cycle: " + " -> ".join(cycle))
        layers.append(ready)
        done.update(ready)
    return layers


def validate_batch(payload: object) -> dict[str, Any]:
    """Validate and return canonical data. IDs map directly to TaskSpec.task_id.

    Task/dependency order is not semantic. Text bytes are preserved exactly.
    Seat names are syntactically checked, not admitted against a live roster.
    """
    obj = _fields(payload, {"schema", "request_id", "tasks", "synthesis"}, "batch")
    if obj.get("schema") != SCHEMA:
        raise BatchError("unsupported-schema", f"schema must be {SCHEMA}")
    if not journal.task_id(obj.get("request_id")):
        raise BatchError("invalid-request-id", "request_id must be a 1-128 character ASCII identifier")
    rows = obj.get("tasks")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_TASKS:
        raise BatchError("task-count", f"tasks must contain 1-{MAX_TASKS} members")
    tasks: list[dict[str, Any]] = []
    ids: set[str] = set()
    for row in rows:
        row = _fields(row, {"task_id", "text", "seat", "depends_on"}, "task")
        task_id = row.get("task_id")
        if not isinstance(task_id, str) or not journal.task_id(task_id):
            raise BatchError("invalid-task-id", "task_id must be a 1-128 character ASCII identifier")
        if task_id in ids:
            raise BatchError("duplicate-task-id", f"duplicate task_id: {task_id}")
        ids.add(task_id)
        selection = _selection(row, task_id)
        deps = row.get("depends_on", [])
        if (
            not isinstance(deps, list)
            or len(deps) > MAX_TASKS
            or any(not journal.task_id(dep) for dep in deps)
            or len(set(deps)) != len(deps)
        ):
            raise BatchError("invalid-dependencies", f"{task_id}.depends_on must contain unique task IDs")
        if task_id in deps:
            raise BatchError("self-dependency", f"task {task_id} depends on itself")
        tasks.append({"task_id": task_id, **selection, "depends_on": sorted(deps)})
    for task in tasks:
        missing = set(task["depends_on"]) - ids
        if missing:
            raise BatchError(
                "unknown-dependency", f"{task['task_id']} references unknown tasks: {', '.join(sorted(missing))}"
            )
    tasks.sort(key=lambda t: t["task_id"])
    _layers(tasks)
    synthesis = obj.get("synthesis")
    if synthesis is not None:
        if not isinstance(synthesis, dict):
            raise BatchError("invalid-synthesis", "synthesis must be null or an explicit text/seat object")
        synthesis = _selection(_fields(synthesis, {"text", "seat"}, "synthesis"), "synthesis")
    return {"schema": SCHEMA, "request_id": obj["request_id"], "tasks": tasks, "synthesis": synthesis}


def _fingerprint(canonical: dict[str, Any]) -> str:
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def request_fingerprint(payload: object) -> str:
    """Hash the validated canonical payload, including request identity."""
    return _fingerprint(validate_batch(payload))


def compare_request(stored_fingerprint: str | None, payload: object) -> Literal["new", "same", "conflict"]:
    """Compare within one request ID's storage scope; this helper stores nothing."""
    fingerprint = request_fingerprint(payload)
    if stored_fingerprint is None:
        return "new"
    return "same" if stored_fingerprint == fingerprint else "conflict"


def plan_batch(payload: object) -> dict[str, Any]:
    """Return deterministic layers and selections without prompts or model calls."""
    canonical = validate_batch(payload)
    return {
        "schema": SCHEMA,
        "request_id": canonical["request_id"],
        "fingerprint": _fingerprint(canonical),
        "layers": _layers(canonical["tasks"]),
        "tasks": [{k: v for k, v in task.items() if k != "text"} for task in canonical["tasks"]],
        "synthesis": {"seat": canonical["synthesis"]["seat"]} if canonical["synthesis"] else None,
    }


def load_batch(path: Path) -> object:
    """Read a bounded UTF-8 JSON file, rejecting ambiguous duplicate keys."""

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BatchError("invalid-json", "duplicate JSON object key")
            result[key] = value
        return result

    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise BatchError("input-read", "cannot read batch input") from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise BatchError("input-limit", f"batch input exceeds {MAX_INPUT_BYTES} bytes")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, BatchError):
            raise
        raise BatchError("invalid-json", "batch input must be valid UTF-8 JSON") from exc


def dry_run_cli(args) -> int:
    """Fail closed before entering any single-worker run machinery."""
    try:
        plan = plan_batch(load_batch(args.batch_file))
        if not args.dry_run:
            raise BatchError("execution-not-implemented", "batch execution not yet implemented (#2047)")
    except BatchError as exc:
        if args.json:
            print(json.dumps({"error": {"code": exc.code, "message": str(exc)}}))
        else:
            print(f"error [{exc.code}]: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(plan, sort_keys=True))
    else:
        print(f"batch {plan['request_id']} ({SCHEMA})")
        print(f"fingerprint: {plan['fingerprint']}")
        seats = {task["task_id"]: task["seat"] for task in plan["tasks"]}
        for index, layer in enumerate(plan["layers"], 1):
            print(f"layer {index}: " + ", ".join(f"{task_id} ({seats[task_id]})" for task_id in layer))
        print("synthesis: " + (plan["synthesis"]["seat"] if plan["synthesis"] else "off"))
    return 0
