"""Low-level git/path/time/session-file utilities for the work command family."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any
from .. import dogfood_cmd, localio
from ..selection import Selection
from . import constants

# Re-exported for the work_cmd package and its tests, which import these
# private aliases from here rather than from localio directly.
from ..localio import (  # noqa: F401
    read_json_dict as _read_json,
    stable_hash as _stable_hash,
    utc_now as _now,
    write_json as _write_json,
)


def _git(target: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(target), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _git_value(target: Path, *args: str) -> str | None:
    result = _git(target, *args)
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _short(text: str, limit: int = 96) -> str:
    rendered = " ".join(text.split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


def _count_status(count: object, label: str = "issue") -> str:
    return "ok" if count == 0 else f"{count} {label}(s)"


def _slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return value[:48].strip("-") or "work-session"


def _work_root(target: Path) -> Path:
    return target / ".brigade" / "work"


def _current_path(target: Path) -> Path:
    return _work_root(target) / "current"


def _tasks_path(target: Path) -> Path:
    return _work_root(target) / "tasks.json"


def _plans_dir(target: Path) -> Path:
    return _work_root(target) / "plans"


def _plan_paths(target: Path, task_id: str, kind: str = "plan") -> tuple[Path, Path]:
    plans = _plans_dir(target)
    if kind == "meta":
        return plans / f"{task_id}.meta.json", plans / f"{task_id}.meta.plan.md"
    return plans / f"{task_id}.json", plans / f"{task_id}.plan.md"


def _imports_path(target: Path) -> Path:
    return _work_root(target) / "imports" / "inbox.jsonl"


def _imports_archive_path(target: Path) -> Path:
    return _work_root(target) / "imports" / "archive.jsonl"


def _backup_config_path(target: Path) -> Path:
    return target / constants.BACKUP_CONFIG_REL_PATH


def _scanner_config_path(target: Path) -> Path:
    return target / constants.SCANNER_CONFIG_REL_PATH


def _scanner_runs_root(target: Path) -> Path:
    return target / ".brigade" / "scanners" / "runs"


def _scanner_sweeps_root(target: Path) -> Path:
    return target / ".brigade" / "scanners" / "sweeps"


def _review_config_path(target: Path) -> Path:
    return target / constants.REVIEW_CONFIG_REL_PATH


def _review_runs_root(target: Path) -> Path:
    return target / ".brigade" / "reviews" / "runs"


def _verify_runs_root(target: Path) -> Path:
    return _work_root(target) / "verify-runs"


def _work_closeouts_root(target: Path) -> Path:
    return _work_root(target) / "closeouts"


def _git_snapshot(target: Path) -> dict[str, Any]:
    repo_root = _git_value(target, "rev-parse", "--show-toplevel")
    if repo_root is None:
        return {"available": False, "dirty_files": []}
    branch = _git_value(target, "branch", "--show-current")
    if branch is None:
        branch = _git_value(target, "rev-parse", "--short", "HEAD") or "unknown"
        branch = f"detached:{branch}"
    status_out = _git_value(target, "status", "--short") or ""
    return {
        "available": True,
        "repo": repo_root,
        "branch": branch,
        "dirty_files": status_out.splitlines(),
    }


def _dogfood_snapshot(target: Path) -> dict[str, Any]:
    try:
        effective_target, artifacts_dir, cfg = dogfood_cmd._load_effective_paths(target)
    except (FileNotFoundError, ValueError) as exc:
        return {"ready": False, "error": str(exc)}
    latest = dogfood_cmd._latest_run(artifacts_dir)
    snapshot: dict[str, Any] = {
        "ready": dogfood_cmd.config_path(target).exists() and shutil.which("codex") is not None,
        "config": str(dogfood_cmd.config_path(target)),
        "target": str(effective_target),
        "artifacts_dir": str(artifacts_dir),
        "handoff_inbox": str(
            cfg.handoff_inbox
            if cfg and cfg.handoff_inbox is not None
            else dogfood_cmd.default_handoff_inbox(effective_target)
        ),
    }
    if latest is None:
        snapshot["latest_run"] = None
        snapshot["next"] = None
        return snapshot
    latest_path, latest_meta = latest
    next_step, next_source = dogfood_cmd.extract_next_step_from_run(latest_path)
    snapshot["latest_run"] = {
        "path": str(latest_path),
        "started_at": latest_meta.get("started_at"),
        "status": latest_meta.get("status"),
        "task": latest_meta.get("task"),
    }
    snapshot["next"] = next_step
    snapshot["next_source"] = next_source
    return snapshot


def _session_snapshot(target: Path) -> dict[str, Any]:
    return {
        "git": _git_snapshot(target),
        "dogfood": _dogfood_snapshot(target),
    }


def _read_session(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads((path / "session.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _session_sort_key(item: tuple[Path, dict[str, Any]]) -> str:
    path, payload = item
    return str(payload.get("ended_at") or payload.get("started_at") or path.name)


# Facade contract: re-exported by work_cmd for cross-station callers.
_parse_iso_datetime = localio.parse_iso_datetime


def _parse_since(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed_date = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("--since must use YYYY-MM-DD") from exc
    return datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)


def _collect_sessions(root: Path) -> tuple[list[tuple[Path, dict[str, Any]]], int]:
    sessions: list[tuple[Path, dict[str, Any]]] = []
    skipped = 0
    if not root.is_dir():
        return sessions, skipped
    for child in root.iterdir():
        if not child.is_dir():
            continue
        payload = _read_session(child)
        if payload is None:
            skipped += 1
            continue
        sessions.append((child, payload))
    sessions.sort(key=_session_sort_key, reverse=True)
    return sessions, skipped


def _resolve_session(target: Path, session: str | Path) -> Path:
    candidate = Path(session).expanduser()
    if candidate.is_dir():
        return candidate
    return _work_root(target) / str(session)


def _dirty_count(snapshot: dict[str, Any]) -> int:
    git = snapshot.get("git")
    if not isinstance(git, dict):
        return 0
    dirty = git.get("dirty_files")
    return len(dirty) if isinstance(dirty, list) else 0


def _snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("end"), dict):
        return payload["end"]
    if isinstance(payload.get("start"), dict):
        return payload["start"]
    return {}


def _branch(snapshot: dict[str, Any]) -> str | None:
    git = snapshot.get("git")
    if isinstance(git, dict) and isinstance(git.get("branch"), str):
        return git["branch"]
    return None


def _next_step(snapshot: dict[str, Any]) -> str | None:
    dogfood = snapshot.get("dogfood")
    if isinstance(dogfood, dict) and isinstance(dogfood.get("next"), str):
        return dogfood["next"]
    return None


def _session_info(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    snapshot = _snapshot(payload)
    notes = payload.get("notes")
    latest_note = None
    if isinstance(notes, list) and notes:
        latest = notes[-1]
        if isinstance(latest, dict) and latest.get("text"):
            latest_note = latest["text"]
    return {
        "path": str(path),
        "id": payload.get("id", path.name),
        "status": payload.get("status", "unknown"),
        "title": payload.get("title"),
        "started_at": payload.get("started_at"),
        "ended_at": payload.get("ended_at"),
        "note": payload.get("note"),
        "latest_note": latest_note,
        "handoff": payload.get("handoff"),
        "branch": _branch(snapshot),
        "dirty_files": _dirty_count(snapshot),
        "next": _next_step(snapshot),
    }


def _handoff_inbox(target: Path, payload: dict[str, Any], override: Path | None) -> Path:
    if override is not None:
        return override.expanduser()
    dogfood = payload.get("end", {}).get("dogfood", {})
    configured = dogfood.get("handoff_inbox")
    if isinstance(configured, str) and configured:
        return Path(configured).expanduser()
    return dogfood_cmd.default_handoff_inbox(target)


def _doctor_line(level: str, name: str, detail: object) -> None:
    print(f"[{level}] {name}: {detail}")


def _active_session_info(target: Path) -> dict[str, Any] | None:
    current = _current_path(target)
    if not current.exists():
        return None
    active_dir = _work_root(target) / current.read_text().strip()
    payload = _read_session(active_dir)
    if payload is None:
        return {
            "path": str(active_dir),
            "valid": False,
        }
    return {
        "path": str(active_dir),
        "valid": True,
        "status": payload.get("status", "unknown"),
        "title": payload.get("title"),
        "started_at": payload.get("started_at"),
    }


def _active_session_dir(target: Path) -> Path | None:
    current = _current_path(target)
    if not current.exists():
        return None
    session_id = current.read_text().strip()
    if not session_id:
        return None
    return _work_root(target) / session_id


def _work_selection(target: Path, handoff_inbox: Path | None) -> Selection:
    harnesses = ["codex"]
    if handoff_inbox is not None:
        try:
            relative = handoff_inbox.expanduser().resolve().relative_to(target)
        except ValueError:
            relative = None
        if relative is not None:
            parts = relative.parts
            if len(parts) >= 2 and parts[:2] == (".claude", "memory-handoffs"):
                harnesses = ["claude"]
            elif len(parts) >= 2 and parts[:2] == (".codex", "memory-handoffs"):
                harnesses = ["codex"]
    owner = harnesses[0] if harnesses else "this-repo"
    return Selection(depth="repo", harnesses=harnesses, owner=owner, includes=[])
