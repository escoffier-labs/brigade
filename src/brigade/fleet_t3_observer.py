"""Opt-in native T3 Code thread presence, independent of managed fleet receipts.

The explicitly configured local interpreter must already contain t3-fleet.
The subprocess reads only the native shell and returns operational metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Sequence, cast

from . import proc
from .fleet_openclaw_observer import (
    OBSERVATION_BUDGET_SECONDS,
    TTL_SECONDS,
    ObservationError,
    RecentSession,
    _absolute_workspace,
    _bounded_json,
    _opaque_id,
    _publish_recent,
    _remaining,
    _require_node_auth,
)

NATIVE_TIMEOUT_SECONDS = 8
MAX_THREADS = 10_000
MAX_PROJECTS = 2_000
MAX_METADATA_BYTES = 262_144
_ACTIVE = frozenset({"queued", "preparing", "starting", "running", "waiting"})
_TERMINAL = frozenset({"completed", "interrupted", "failed", "cancelled", "rolled_back"})
# Constant program, never interpolated with user input. The installed read client
# bounds shell responses to 16 MiB. No thread details, messages, provider turns,
# target store or raw shell data are serialized here. Native auth may issue its
# own local grant; no grant values are exposed by this helper.
_NATIVE_PROGRAM = r"""
import json, os
try:
    from t3_fleet.agent import _build_agent
    shell = _build_agent(os.environ).t3.shell_snapshot()
    if not isinstance(shell, dict) or type(shell.get("schemaVersion")) is not int or shell["schemaVersion"] != 2:
        raise ValueError()
    projects, threads = (shell.get(k) for k in ("projects", "threads"))
    archived = shell.get("archivedThreads", [])
    if not isinstance(projects, list) or len(projects) > 2000 or not isinstance(threads, list) or not isinstance(archived, list) or len(threads) + len(archived) > 10000:
        raise ValueError()
    if any(not isinstance(row, dict) for row in projects + threads + archived):
        raise ValueError()
    active_states = {"queued", "preparing", "starting", "running", "waiting"}
    terminal_states = {"completed", "interrupted", "failed", "cancelled", "rolled_back"}
    active = []
    for row in threads:
        status = row.get("status")
        if not isinstance(status, str) or status not in active_states | terminal_states | {"idle"}:
            raise ValueError()
        if "activeRunId" not in row:
            raise ValueError()
        run_id, activity = row["activeRunId"], row.get("activityRunStatus")
        if activity is not None and (not isinstance(activity, str) or activity not in active_states):
            raise ValueError()
        pending = row.get("pendingRuntimeRequest")
        background = row.get("pendingBackgroundTasks", [])
        if pending is not None and (not isinstance(pending, dict) or any(not isinstance(pending.get(k), str) or not pending[k] for k in ("id", "kind", "createdAt"))):
            raise ValueError()
        if pending is not None and pending["kind"] not in {"command", "file-read", "file-change", "mcp-elicitation", "permission", "dynamic_tool_call", "user_input", "auth_refresh"}:
            raise ValueError()
        if not isinstance(background, list) or any(not isinstance(t, dict) or not isinstance(t.get("taskId"), str) or not t["taskId"] for t in background):
            raise ValueError()
        if row.get("archivedAt") is not None or row.get("deletedAt") is not None or status in terminal_states:
            continue
        if status in active_states or run_id is not None or activity is not None or bool(pending) or bool(background):
            active.append(row)
    needed = {row.get("projectId") for row in active if isinstance(row.get("projectId"), str)}
    result = {
        "schemaVersion": 2,
        "total_threads": len(threads) + len(archived),
        "projects": [{k: row.get(k) for k in ("id", "workspaceRoot")} for row in projects if isinstance(row.get("id"), str) and row.get("id") in needed],
        "threads": [{**{k: row.get(k) for k in ("id", "projectId", "status", "activeRunId", "activityRunStatus", "worktreePath", "archivedAt", "deletedAt") if k in row}, "hasPendingRuntimeRequest": bool(row.get("pendingRuntimeRequest")), "hasPendingBackgroundTasks": bool(row.get("pendingBackgroundTasks"))} for row in active],
        "archived_count": len(archived),
    }
    output = json.dumps(result)
    if len(output.encode("utf-8")) > 262144:
        raise ValueError()
    print(output)
except Exception:
    print('{"error":"native_metadata_failed"}')
"""


def _native_metadata(interpreter: str, deadline: float) -> object:
    path = Path(interpreter)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise ObservationError("t3_python_unavailable")
    result = proc.run(
        [str(path), "-c", _NATIVE_PROGRAM],
        cwd=path.parent,
        timeout=_remaining(deadline, cap=NATIVE_TIMEOUT_SECONDS),
        supervise_group=True,
    )
    _remaining(deadline, cap=1)
    payload = _bounded_json(result, limit=MAX_METADATA_BYTES)
    if isinstance(payload, dict) and payload.get("error"):
        raise ObservationError("native_metadata_failed")
    return payload


def _native_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or any(c.isspace() or not c.isprintable() for c in value)
    ):
        raise ObservationError("native_metadata_invalid")
    return value


def _presence_id(native_id: str) -> str:
    native_id = _native_id(native_id)
    try:
        return _opaque_id(native_id)
    except ObservationError:
        return "t3-" + hashlib.sha256(native_id.encode("utf-8")).hexdigest()


def _active_threads(payload: object) -> tuple[list[RecentSession], int, int]:
    if not isinstance(payload, dict) or type(payload.get("schemaVersion")) is not int or payload["schemaVersion"] != 2:
        raise ObservationError("native_metadata_invalid")
    projects, threads, archived = (payload.get(k) for k in ("projects", "threads", "archived_count"))
    total = payload.get("total_threads")
    if (
        not isinstance(projects, list)
        or len(projects) > MAX_PROJECTS
        or not isinstance(threads, list)
        or len(threads) > MAX_THREADS
        or type(archived) is not int
        or archived < 0
        or type(total) is not int
        or total < len(threads) + archived
        or total > MAX_THREADS
    ):
        raise ObservationError("native_metadata_invalid")
    roots: dict[str, object] = {}
    for project in projects:
        if not isinstance(project, dict):
            raise ObservationError("native_metadata_invalid")
        try:
            project_id = _native_id(project.get("id"))
        except ObservationError:
            continue
        if project_id in roots:
            raise ObservationError("native_identity_collision")
        # Only referenced active projects need a usable path.
        roots[project_id] = project.get("workspaceRoot")
    invalid = 0
    active: list[RecentSession] = []
    seen: set[str] = set()
    now_ms = time.time_ns() // 1_000_000
    for row in threads:
        if not isinstance(row, dict):
            raise ObservationError("native_metadata_invalid")
        status, run_id, activity = (row.get(k) for k in ("status", "activeRunId", "activityRunStatus"))
        if not isinstance(status, str) or status not in _ACTIVE | _TERMINAL | {"idle"}:
            raise ObservationError("native_state_unknown")
        if "activeRunId" not in row:
            raise ObservationError("native_metadata_invalid")
        if activity is not None and (not isinstance(activity, str) or activity not in _ACTIVE):
            raise ObservationError("native_state_unknown")
        if row.get("archivedAt") is not None or row.get("deletedAt") is not None or status in _TERMINAL:
            continue
        pending = row.get("hasPendingRuntimeRequest", False)
        background = row.get("hasPendingBackgroundTasks", False)
        if type(pending) is not bool or type(background) is not bool:
            raise ObservationError("native_metadata_invalid")
        try:
            session_id = _presence_id(_native_id(row.get("id")))
        except ObservationError:
            invalid += 1
            continue
        if session_id in seen:
            raise ObservationError("native_identity_collision")
        seen.add(session_id)
        if status not in _ACTIVE and run_id is None and activity is None and not pending and not background:
            continue
        try:
            if run_id is not None:
                _native_id(run_id)
            raw = row.get("worktreePath")
            if raw is None:
                project_id = _native_id(row.get("projectId"))
                if project_id not in roots:
                    raise ObservationError("native_metadata_invalid")
                raw = roots[project_id]
            active.append(RecentSession(session_id, _absolute_workspace(raw), now_ms))
        except ObservationError:
            invalid += 1
    return active, total, invalid


def observe(*, t3_python: str, publish: bool = False) -> dict[str, object]:
    deadline = time.monotonic() + OBSERVATION_BUDGET_SECONDS
    if publish:
        _require_node_auth()
    recent, total, invalid = _active_threads(_native_metadata(t3_python, deadline))
    _remaining(deadline, cap=1)
    report: dict[str, object] = {
        "ok": not invalid,
        "mode": "publish" if publish else "inspect",
        "activity_scope": "native_t3_presence",
        "active_threads": len(recent),
        "observed": len(recent),
        "total_threads": total,
        "published": 0,
        "skipped": invalid,
        "invalid_rows": invalid,
        "truncated": False,
        "ttl_seconds": TTL_SECONDS,
        "budget_seconds": OBSERVATION_BUDGET_SECONDS,
    }
    if publish:
        report.update(_publish_recent(recent, harness="t3", deadline=deadline))
        report["skipped"] = cast(int, report["skipped"]) + invalid
        report["ok"] = not (report["truncated"] or invalid or report.get("error"))
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--t3-python", required=True, help="Absolute path to the existing trusted T3 fleet Python")
    parser.add_argument("--publish", action="store_true", help="Publish through this enrolled node")
    args = parser.parse_args(argv)
    try:
        report = observe(t3_python=args.t3_python, publish=args.publish)
    except ObservationError as exc:
        report = {"ok": False, "error": str(exc), "observed": 0, "published": 0, "skipped": 0}
    except Exception:
        report = {"ok": False, "error": "observation_failed", "observed": 0, "published": 0, "skipped": 0}
    print(json.dumps(report, sort_keys=True))
    if report["ok"]:
        return 0
    return (
        2
        if report.get("truncated")
        and not report.get("invalid_rows")
        and report.get("error") in (None, "observation_timeout")
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
