"""Bounded, advisory dependency observations for explicit Claude file tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import context_eval, graphtrail_delta, localio

MAX_SEEN_FILES = 2048
MAX_CHECKS = 32
GRAPH_TIMEOUT_SECONDS = 2.0
_STATE_KEYS = ("read_files", "brief_files", "dependency_checks")


def restore(state: dict[str, Any], persisted: dict[str, Any]) -> None:
    """Keep observations within the task epoch selected by the runtime."""
    for key in _STATE_KEYS:
        value = persisted.get(key)
        if isinstance(value, list):
            state[key] = (
                value[-MAX_CHECKS:]
                if key == "dependency_checks"
                else [item for item in value if isinstance(item, str)][:MAX_SEEN_FILES]
            )


def merge_observations(state: dict[str, Any], current: dict[str, Any]) -> None:
    """Merge monotonic observations while the caller holds the session lock."""
    if state.get("task_epoch") != current.get("task_epoch"):
        return
    for key in ("read_files", "brief_files"):
        files = {item for source in (current, state) for item in source.get(key, []) if isinstance(item, str)}
        if files:
            state[key] = sorted(files)[:MAX_SEEN_FILES]
    checks = {}
    for source in (current, state):
        for check in source.get("dependency_checks", []):
            if isinstance(check, dict):
                key = str(check.get("check_id") or json.dumps(check, sort_keys=True))
                checks[key] = check
    if checks:
        state["dependency_checks"] = sorted(checks.values(), key=lambda check: str(check.get("checked_at", "")))[
            -MAX_CHECKS:
        ]


def relative_path(target: Path, raw: object, cwd: object = None) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw).expanduser()
    base = Path(cwd) if isinstance(cwd, str) and Path(cwd).is_absolute() else target
    try:
        return (path if path.is_absolute() else base / path).resolve().relative_to(target.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def record_read(target: Path, state: dict[str, Any], payload: dict[str, Any]) -> bool:
    tool_input = payload.get("tool_input")
    response = payload.get("tool_response")
    if not isinstance(tool_input, dict) or (isinstance(response, dict) and response.get("is_error")):
        return False
    path = relative_path(target, tool_input.get("file_path"), payload.get("cwd"))
    if path is None:
        return False
    state["read_files"] = sorted(set(state.get("read_files", [])) | {path})[:MAX_SEEN_FILES]
    return True


def injected_brief_files(text: str) -> list[str]:
    """Count only graph records actually injected, excluding the echoed task."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("_schema v") and " entry points - " in line]
    if not starts:
        return []
    graph_lines = []
    for line in lines[starts[-1] + 1 :]:
        if line.startswith("## ") and line.lower() not in {
            "## entry points",
            "## callers",
            "## callees",
            "## related files",
        }:
            break
        graph_lines.append(line)
    return context_eval.extract_brief_files("\n".join(graph_lines))[:MAX_SEEN_FILES]


def _dependents(target: Path, path: str) -> tuple[list[str] | None, str | None]:
    db = target / ".graphtrail" / "graphtrail.db"
    if not db.is_file():
        return None, "no_index"
    binary = graphtrail_delta._graphtrail_bin()
    if binary is None:
        return None, "graph_unavailable"
    # Force content extraction: incremental sync can miss same-size edits within
    # one mtime second. A failed refresh must never certify an old graph.
    for command, extra in (("sync", ("--force",)), ("affected", (path,))):
        result = graphtrail_delta._run_graphtrail(
            binary, db, command, *extra, timeout=GRAPH_TIMEOUT_SECONDS, json_output=command == "affected"
        )
        if result.get("timed_out"):
            return None, f"{command}_timed_out"
        if result.get("returncode") != 0:
            return None, f"{command}_failed"
    report = json.loads(result["stdout"])
    if not isinstance(report, dict):
        return None, "invalid_graph_report"
    if report.get("truncated") is not False:
        return None, "incomplete_graph_report"
    if report.get("missing_files") != [] or path not in report.get("changed_files", []):
        return None, "file_outside_coverage"
    files: set[str] = set()
    for key in ("impacted_files", "affected_tests"):
        rows = report.get(key)
        if not isinstance(rows, list):
            return None, "invalid_graph_report"
        for row in rows:
            raw = row.get("file_path") if isinstance(row, dict) else None
            relative = relative_path(target, raw)
            if relative is None:
                return None, "invalid_graph_report"
            if relative != path:
                files.add(relative)
    return sorted(files), None


def record_write(target: Path, state: dict[str, Any], payload: dict[str, Any]) -> str:
    """Persist an attempted write check, returning advisory context, never denial."""
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    path = relative_path(target, tool_input.get("file_path") or tool_input.get("notebook_path"), payload.get("cwd"))
    dependents: list[str] | None = None
    reason: str | None = "write_path_outside_repo"
    if path is not None:
        try:
            dependents, reason = _dependents(target, path)
        except Exception:
            # Graph errors are skipped observations, not a reason to stop an edit.
            reason = "graph_error"
    seen = set(state.get("read_files", [])) | set(state.get("brief_files", []))
    unseen = sorted(set(dependents) - seen) if dependents is not None else None
    check = {
        "check_id": uuid4().hex,
        "file_path": path,
        "tool_name": payload.get("tool_name"),
        "phase": "pre_write",
        "checked_at": localio.utc_now_iso(),
        "unseen_dependents": unseen,
        "reason": reason,
    }
    state["dependency_checks"] = [*state.get("dependency_checks", []), check][-MAX_CHECKS:]
    if unseen is None:
        return ""
    if unseen:
        return f"Dependency warning for {path}: {len(unseen)} unseen dependent file(s): " + ", ".join(unseen)
    return ""
