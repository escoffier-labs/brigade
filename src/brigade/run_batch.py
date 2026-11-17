"""Versioned explicit batch contract and offline planning. No execution or storage."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Literal

from . import run_task_journal as journal

SCHEMA = "brigade.run_batch.v1"
MAX_TASKS = journal.MAX_BATCH_MEMBERS
MAX_TEXT_BYTES = 16 * 1024
MAX_INPUT_BYTES = 256 * 1024
ISOLATIONS = ("shared", "worktree")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_BASE_SHA = re.compile(r"[0-9a-f]{40}")


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


def _shared_writable(tasks: list[dict[str, Any]], layers: list[list[str]]) -> None:
    """Refuse two same-layer tasks that would write the same checkout at once.

    One writable task per layer keeps today's sequential behavior. Callers who
    need parallel writers declare ``worktree`` isolation, mark work
    ``read_only``, or add a dependency that separates the tasks into layers.
    """
    owners = {t["task_id"]: t for t in tasks}
    for layer in layers:
        shared = [
            task_id
            for task_id in layer
            if owners[task_id]["isolation"] == "shared" and not owners[task_id]["read_only"]
        ]
        if len(shared) > 1:
            raise BatchError(
                "shared-writable-concurrency",
                "shared writable tasks cannot run in the same layer: "
                + ", ".join(shared)
                + "; use isolation worktree, read_only true, or depends_on",
            )


def validate_batch(payload: object) -> dict[str, Any]:
    """Validate and return canonical data. IDs map directly to TaskSpec.task_id.

    Task/dependency order is not semantic. Text bytes are preserved exactly.
    Seat names are syntactically checked, not admitted against a live roster.
    Isolation and read-only defaults are filled into the canonical row, but the
    fingerprint omits those defaults so existing request fingerprints are
    unchanged.
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
    for index, row in enumerate(rows):
        row = _fields(row, {"task_id", "text", "seat", "depends_on", "isolation", "read_only"}, f"tasks[{index}]")
        task_id = row.get("task_id")
        if not isinstance(task_id, str) or not journal.task_id(task_id):
            raise BatchError("invalid-task-id", f"tasks[{index}].task_id must be a 1-128 character ASCII identifier")
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
        isolation = row.get("isolation", "shared")
        if isolation not in ISOLATIONS:
            raise BatchError("invalid-isolation", f"{task_id}.isolation must be shared or worktree")
        read_only = row.get("read_only", False)
        if not isinstance(read_only, bool):
            raise BatchError("invalid-read-only", f"{task_id}.read_only must be a boolean")
        tasks.append(
            {
                "task_id": task_id,
                **selection,
                "depends_on": sorted(deps),
                "isolation": isolation,
                "read_only": read_only,
            }
        )
    for task in tasks:
        missing = set(task["depends_on"]) - ids
        if missing:
            raise BatchError(
                "unknown-dependency", f"{task['task_id']} references unknown tasks: {', '.join(sorted(missing))}"
            )
    tasks.sort(key=lambda t: t["task_id"])
    _shared_writable(tasks, _layers(tasks))
    synthesis = obj.get("synthesis")
    if synthesis is not None:
        if not isinstance(synthesis, dict):
            raise BatchError("invalid-synthesis", "synthesis must be null or an explicit text/seat object")
        synthesis = _selection(_fields(synthesis, {"text", "seat"}, "synthesis"), "synthesis")
    return {"schema": SCHEMA, "request_id": obj["request_id"], "tasks": tasks, "synthesis": synthesis}


def _needs_base_pin(canonical: dict[str, Any]) -> bool:
    return any(task["isolation"] == "worktree" for task in canonical["tasks"])


def _fingerprint(canonical: dict[str, Any], base_sha: str | None = None) -> str:
    # Defaults are omitted so a batch written before these fields existed keeps
    # its fingerprint. A non-default value, or a pinned base, does change it.
    material = {
        **canonical,
        "tasks": [
            {
                key: value
                for key, value in task.items()
                if not (key == "isolation" and value == "shared") and not (key == "read_only" and value is False)
            }
            for task in canonical["tasks"]
        ],
    }
    if base_sha is not None:
        material["base_sha"] = base_sha
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _base_revision(canonical: dict[str, Any], base_sha: str | None) -> str | None:
    """Return the base to record, or refuse a missing, malformed, or unneeded one."""
    if _needs_base_pin(canonical):
        if base_sha is None:
            raise BatchError("base-unpinned", "worktree isolation requires a pinned base revision")
        if not isinstance(base_sha, str) or _BASE_SHA.fullmatch(base_sha) is None:
            raise BatchError("invalid-base-sha", "base_sha must be a 40-character lowercase git revision")
        return base_sha
    if base_sha is not None:
        raise BatchError("unexpected-base-sha", "base_sha is only recorded when a task requests worktree isolation")
    return None


def request_fingerprint(payload: object, *, base_sha: str | None = None) -> str:
    """Hash the validated canonical payload, including request identity and any pinned base."""
    canonical = validate_batch(payload)
    return _fingerprint(canonical, _base_revision(canonical, base_sha))


def compare_request(
    stored_fingerprint: str | None, payload: object, *, base_sha: str | None = None
) -> Literal["new", "same", "conflict"]:
    """Compare within one request ID's storage scope; this helper stores nothing."""
    fingerprint = request_fingerprint(payload, base_sha=base_sha)
    if stored_fingerprint is None:
        return "new"
    if not isinstance(stored_fingerprint, str) or not _FINGERPRINT.fullmatch(stored_fingerprint):
        raise BatchError("invalid-fingerprint", "stored fingerprint must be 64 lowercase hex characters")
    return "same" if stored_fingerprint == fingerprint else "conflict"


def plan_batch(payload: object, *, base_sha: str | None = None) -> dict[str, Any]:
    """Return deterministic layers and selections without prompts or model calls."""
    canonical = validate_batch(payload)
    base_sha = _base_revision(canonical, base_sha)
    return {
        "schema": SCHEMA,
        "request_id": canonical["request_id"],
        "base_sha": base_sha,
        "fingerprint": _fingerprint(canonical, base_sha),
        "layers": _layers(canonical["tasks"]),
        "tasks": [{k: v for k, v in task.items() if k != "text"} for task in canonical["tasks"]],
        "synthesis": {"seat": canonical["synthesis"]["seat"]} if canonical["synthesis"] else None,
    }


def _pinned_base_sha(canonical: dict[str, Any], cwd: Path | None) -> str | None:
    """Pin the workspace HEAD when any task asks for a worktree.

    Reuses the transport's clean-checkout rule, so an isolated child is never
    implied to receive uncommitted parent edits.
    """
    if not _needs_base_pin(canonical):
        return None
    from .fleet_t3_transport import SourceRevisionError, clean_source_revision

    workspace = (cwd or Path.cwd()).expanduser().resolve()
    try:
        return clean_source_revision(workspace)
    except SourceRevisionError as exc:
        code = "base-dirty" if exc.code == "source-dirty" else "base-unavailable"
        raise BatchError(code, f"worktree isolation needs a pinned base revision: {exc}") from exc


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
        raise BatchError("input-read", f"cannot read batch input: {exc.strerror or type(exc).__name__}") from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise BatchError("input-limit", f"batch input exceeds {MAX_INPUT_BYTES} bytes")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, BatchError):
            raise
        raise BatchError("invalid-json", "batch input must be valid UTF-8 JSON") from exc


def dry_run_cli(args) -> int:
    """Validate before dry-run output or explicit-batch execution."""
    try:
        payload = validate_batch(load_batch(args.batch_file))
        plan = plan_batch(payload, base_sha=_pinned_base_sha(payload, getattr(args, "cwd", None)))
        if not args.dry_run:
            from .run_batch_execute import execute_cli

            return execute_cli(args, {**plan, "tasks": payload["tasks"], "synthesis": payload["synthesis"]})
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
        if plan["base_sha"] is not None:
            print(f"base: {plan['base_sha']}")
        seats = {task["task_id"]: task["seat"] for task in plan["tasks"]}
        for index, layer in enumerate(plan["layers"], 1):
            print(f"layer {index}: " + ", ".join(f"{task_id} ({seats[task_id]})" for task_id in layer))
        print("synthesis: " + (plan["synthesis"]["seat"] if plan["synthesis"] else "off"))
    return 0
