"""Bounded, advisory dependency observations for explicit Claude file tools."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import context_eval, graphtrail_delta, localio

MAX_SEEN_FILES = 2048
MAX_CHECKS = 32
GRAPH_TIMEOUT_SECONDS = 2.0
CONTENT_TIMEOUT_SECONDS = 0.5
_STATE_KEYS = ("read_files", "brief_files", "dependency_checks")


def _collection(source: dict[str, Any], key: str) -> list[Any]:
    value = source.get(key)
    return value if isinstance(value, list) else []


def _checks(source: dict[str, Any]) -> list[dict[str, Any]]:
    checks = []
    for item in _collection(source, "dependency_checks"):
        if not isinstance(item, dict) or not isinstance(item.get("checked_at"), str):
            continue
        unseen = item.get("unseen_dependents")
        if unseen is None or (isinstance(unseen, list) and all(isinstance(path, str) for path in unseen)):
            checks.append(item)
    return checks


def _check_key(check: dict[str, Any]) -> str:
    return str(check.get("check_id") or json.dumps(check, sort_keys=True))


def restore(state: dict[str, Any], persisted: dict[str, Any]) -> None:
    """Keep validated observations within the task epoch selected by the runtime."""
    for key in _STATE_KEYS:
        state.pop(key, None)
        if isinstance(persisted.get(key), list):
            state[key] = (
                _checks(persisted)[-MAX_CHECKS:]
                if key == "dependency_checks"
                else [item for item in _collection(persisted, key) if isinstance(item, str)][-MAX_SEEN_FILES:]
            )


def merge_observations(
    state: dict[str, Any], current: dict[str, Any], *, observed_fields: tuple[str, ...] = ()
) -> None:
    """Merge observations and finalize new checks under the session lock."""
    if state.get("task_epoch") != current.get("task_epoch"):
        return
    seen: set[str] = set()
    for key in ("read_files", "brief_files"):
        files = dict.fromkeys(item for item in _collection(current, key) if isinstance(item, str))
        if key in observed_fields:
            for item in _collection(state, key):
                if isinstance(item, str):
                    files.pop(item, None)
                    files[item] = None
        state[key] = list(files)[-MAX_SEEN_FILES:]
        seen.update(files)
    existing = {_check_key(check): check for check in _checks(current)}
    checks = dict(existing)
    for check in _checks(state):
        key = _check_key(check)
        if key in existing:
            continue
        updated = dict(check)
        unseen = updated.get("unseen_dependents")
        if isinstance(unseen, list):
            updated["unseen_dependents"] = sorted(set(unseen) - seen)
        checks[key] = updated
    state["dependency_checks"] = sorted(checks.values(), key=lambda check: check["checked_at"])[-MAX_CHECKS:]


def warning_for_check(state: dict[str, Any], check_id: str) -> str:
    """Render only the newly persisted check after its locked finalization."""
    for check in _checks(state):
        if check.get("check_id") == check_id:
            unseen = check.get("unseen_dependents")
            if isinstance(unseen, list) and unseen:
                return (
                    f"Dependency warning for {check['file_path']}: {len(unseen)} unseen dependent file(s): "
                    + ", ".join(unseen)
                )
    return ""


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
    files = dict.fromkeys(item for item in _collection(state, "read_files") if isinstance(item, str))
    files.pop(path, None)
    state["read_files"] = [*files, path][-MAX_SEEN_FILES:]
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


def _content_matches_index(target: Path, db: Path, stopped: threading.Event) -> bool:
    """Verify SHA-256 content independently of the engine's size/mtime shortcut."""
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)) as conn:
        count = 0
        for path, expected in conn.execute("SELECT path, content_hash FROM files"):
            if stopped.is_set() or not isinstance(path, str) or relative_path(target, path) != path:
                return False
            digest = hashlib.sha256()
            with (target / path).open("rb") as source:
                while not stopped.is_set():
                    chunk = source.read(65536)
                    if not chunk:
                        break
                    digest.update(chunk)
            if stopped.is_set() or digest.hexdigest() != expected:
                return False
            count += 1
        return count > 0


def _dependents(target: Path, path: str) -> tuple[list[str] | None, str | None]:
    from . import runtime

    db = target / ".graphtrail" / "graphtrail.db"
    if not db.is_file():
        return None, "no_index"
    binary = graphtrail_delta._graphtrail_bin()
    if binary is None:
        return None, "graph_unavailable"
    # Incremental sync handles ordinary edits. Only force extraction when the
    # independent content check finds edits the size/mtime shortcut missed.
    for extra in ((), ("--force",)):
        result = graphtrail_delta._run_graphtrail(binary, db, "sync", *extra, timeout=GRAPH_TIMEOUT_SECONDS)
        if result.get("timed_out"):
            return None, "sync_timed_out"
        if result.get("returncode") != 0:
            return None, "sync_failed"
        # Bound even a single stalled filesystem read. Stream rows and content,
        # and stop abandoned work when the filesystem becomes responsive again.
        stopped = threading.Event()
        matches = runtime._run_best_effort_bounded(
            partial(_content_matches_index, target, db, stopped),
            timeout=CONTENT_TIMEOUT_SECONDS,
            default=None,
            on_abandon=stopped.set,
        )
        if matches is None:
            return None, "content_check_incomplete"
        if matches:
            break
    else:
        return None, "stale_graph"
    result = graphtrail_delta._run_graphtrail(
        binary, db, "affected", path, timeout=GRAPH_TIMEOUT_SECONDS, json_output=True
    )
    if result.get("timed_out"):
        return None, "affected_timed_out"
    if result.get("returncode") != 0:
        return None, "affected_failed"
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
    """Prepare a write check; the locked state writer finalizes its unseen set."""
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
    return str(check["check_id"])
