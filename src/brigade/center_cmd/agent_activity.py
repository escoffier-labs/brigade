"""Safe, read-only observations for the Center Agent Activity contract."""

from __future__ import annotations

import json
import os
import re
import socket
from collections import deque
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from brigade import runguard
from brigade.center_cmd.dashboard import timing as center_timing

ACTIVITY_ENVELOPE_VERSION = 2
_STALE_AFTER_SECONDS = 300
_HEARTBEAT_STALE_SECONDS = 2 * 3600
_DEFAULT_COMPLETED_WINDOW_SECONDS = 3600
_SESSION_MTIME_WINDOW_SECONDS = 7 * 24 * 60 * 60
_SESSION_READ_SIZE_CAP = 1_048_576
_UNKNOWN_TASK = "Unknown task"
_DEFAULT_HOSTS = ("rocinante", "shadowfax", "gandalf")
_RUNNING_STATUSES = {
    "started",
    "planning",
    "dispatching",
    "running",
    "result-processing",
    "synthesizing",
    "handoff",
    "artifact-collection",
}
_DATE_FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-(.+)$")


def collect(target: Path, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Return bounded safe records without mutating local or provider state."""
    observed_at = now or datetime.now(timezone.utc)
    local_host = local_host_alias(target)
    with center_timing.phase("activity:brigade-runs"):
        records = _brigade_records(target, observed_at, local_host)
    with center_timing.phase("activity:local-sessions"):
        records.extend(_local_session_records(observed_at, local_host))
    with center_timing.phase("activity:configured-sources"):
        records.extend(_configured_sources(target, observed_at))
    with center_timing.phase("activity:cloud-tracker"):
        records.extend(_cloud_tracker_records(target, observed_at))
    records.sort(
        key=lambda record: str(record.get("last_updated_at") or record.get("source", {}).get("observed_at") or ""),
        reverse=True,
    )
    return records[:100]


def _cloud_tracker_records(target: Path, now: datetime) -> list[dict[str, Any]]:
    """Feed the Cloud machine card from the local cloud dispatch registry (#890).

    Live ``gh`` / ``codex cloud`` probes stay off the request path. Those can
    hang for seconds; Center reads the local registry snapshot instead.
    """
    try:
        from .. import cloud_tracker

        return cloud_tracker.center_activity_records(
            target,
            now=now,
            provider_tasks={},
            github={"branches": [], "prs": []},
            cursor_wired=cloud_tracker.cursor_cloud_wired(),
        )
    except Exception:
        return []


def summary(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, dict[str, int]] = {}
    for record in records:
        provider = str(record.get("provider") or "unknown")
        state = str(record.get("state") or "unknown")
        counts.setdefault(provider, {})[state] = counts.setdefault(provider, {}).get(state, 0) + 1
    return [
        {"provider": provider, "state_counts": dict(sorted(state_counts.items())), "count": sum(state_counts.values())}
        for provider, state_counts in sorted(counts.items())
    ]


def local_host_alias(target: Path) -> str:
    config = _read_json(target / ".brigade" / "center" / "agent-activity-sources.json") or {}
    configured = config.get("local_host")
    if isinstance(configured, str) and configured.strip():
        return _safe_alias(configured)
    return _safe_alias(socket.gethostname().split(".")[0] or "local")


def completed_window_seconds(target: Path) -> int:
    config = _read_json(target / ".brigade" / "center" / "agent-activity-sources.json") or {}
    value = config.get("completed_window_seconds")
    if isinstance(value, int) and 60 <= value <= 86400:
        return value
    return _DEFAULT_COMPLETED_WINDOW_SECONDS


def default_hosts() -> tuple[str, ...]:
    return _DEFAULT_HOSTS


def _brigade_records(target: Path, now: datetime, local_host: str) -> list[dict[str, Any]]:
    root = target / ".brigade" / "runs"
    if not root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for run_dir in sorted((path for path in root.iterdir() if path.is_dir()), reverse=True)[:50]:
        run = _read_json(run_dir / "run.json")
        if run is None:
            continue
        run_id = run_dir.name
        started_at = _timestamp(run.get("started_at"))
        updated_at = _timestamp(run.get("finished_at")) or _timestamp(run.get("status_started_at")) or started_at
        lock_state = runguard.run_lock_state(target, run_dir)
        state = _reconcile_claimed_live(
            _normalized_state(run.get("status"), updated_at, now, authoritative=True),
            updated_at,
            now,
            lock_state,
        )
        orchestrator_id = f"brigade:orchestrator:{run_id}"
        run_activity_id = f"brigade:run:{run_id}"
        source = _source("brigade-run-journal", "authoritative", now)
        records.append(
            _record(
                activity_id=orchestrator_id,
                parent_activity_id=None,
                provider="brigade",
                harness="brigade-run",
                kind="orchestrator",
                host=local_host,
                label=_safe_label(run.get("orchestrator"), "Brigade orchestrator"),
                task_label=_safe_label(run.get("task_label"), "Brigade run"),
                model=None,
                state=state,
                started_at=started_at,
                updated_at=updated_at,
                source=source,
                links={"run": f"brigade runs show {run_id}"},
                now=now,
            )
        )
        records.append(
            _record(
                activity_id=run_activity_id,
                parent_activity_id=orchestrator_id,
                provider="brigade",
                harness="brigade-run",
                kind="run",
                host=local_host,
                label="Brigade run",
                task_label=_safe_label(run.get("task_label"), "Brigade run"),
                model=None,
                state=state,
                started_at=started_at,
                updated_at=updated_at,
                source=source,
                links={"run": f"brigade runs show {run_id}"},
                now=now,
            )
        )
        records.extend(
            _worker_records(
                run_dir, run, run_activity_id, source, now, started_at, updated_at, state, local_host, lock_state
            )
        )
    return records


def _local_session_records(now: datetime, local_host: str) -> list[dict[str, Any]]:
    """Observe local sessions. Task labels come from cwd folder names or first user line only."""
    home = Path(os.environ.get("HOME", str(Path.home()))).expanduser()
    codex_home = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).expanduser()
    records = _session_file_records(codex_home / "sessions", "codex", "codex-cli", "Codex session", now, local_host)
    if not records:
        records.append(_missing_local_source("codex", "codex-cli", now, local_host))
    cursor_records = _session_file_records(
        home / ".cursor" / "projects", "cursor", "cursor-agent", "Cursor session", now, local_host
    )
    records.extend(cursor_records or [_missing_local_source("cursor", "cursor-agent", now, local_host)])
    return records


def _session_file_records(
    root: Path, provider: str, harness: str, label: str, now: datetime, local_host: str
) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    try:
        paths = _bounded_session_paths(root, provider, now=now)
    except OSError:
        return []
    cutoff = now.timestamp() - _SESSION_MTIME_WINDOW_SECONDS
    ranked: list[tuple[float, int, Path]] = []
    for path in paths:
        try:
            stat_result = path.stat()
        except OSError:
            continue
        if stat_result.st_mtime < cutoff:
            continue
        ranked.append((stat_result.st_mtime, stat_result.st_size, path))
    ranked.sort(key=lambda item: item[0], reverse=True)
    for mtime, size, path in ranked[:25]:
        updated_at = datetime.fromtimestamp(mtime, timezone.utc).isoformat()
        if size > _SESSION_READ_SIZE_CAP:
            task_label, model = _UNKNOWN_TASK, None
        else:
            task_label, model = _session_task_hints(path, provider)
        records.append(
            _record(
                activity_id=f"{provider}:session:{path.stem}",
                parent_activity_id=None,
                provider=provider,
                harness=harness,
                kind="agent",
                host=local_host,
                label=label,
                task_label=task_label,
                model=model,
                state="running" if not _is_stale(updated_at, now) else "stale",
                started_at=None,
                updated_at=updated_at,
                source=_source(f"{provider}-local-session", "best-effort", now),
                links={},
                now=now,
            )
        )
    return records


def _session_task_hints(path: Path, provider: str) -> tuple[str, str | None]:
    cwd_folder = ""
    user_prompt = ""
    model: str | None = None
    if provider == "cursor":
        # projects/<project>/agent-transcripts/...
        parts = path.parts
        if "projects" in parts:
            index = parts.index("projects")
            if index + 1 < len(parts):
                cwd_folder = parts[index + 1]
    for item in _head_json_objects(path):
        item_type = item.get("type")
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if item_type == "session_meta" and not cwd_folder:
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd:
                cwd_folder = Path(cwd).name
        elif item_type == "turn_context":
            if not cwd_folder:
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd:
                    cwd_folder = Path(cwd).name
            if model is None:
                model = _safe_model(payload.get("model"))
        elif item_type == "event_msg" and not user_prompt:
            if payload.get("type") == "user_message" and isinstance(payload.get("message"), str):
                user_prompt = payload["message"]
        elif item_type == "response_item" and not user_prompt:
            if payload.get("role") == "user":
                user_prompt = _text_from_content(payload.get("content"))
        if cwd_folder and user_prompt and model:
            break
    return _task_label_from_hints(cwd_folder, user_prompt), model


def _task_label_from_hints(cwd_folder: str, user_prompt: str) -> str:
    if cwd_folder and _DATE_FOLDER_RE.match(cwd_folder):
        return _safe_label(_humanize_folder_name(cwd_folder), _UNKNOWN_TASK)
    prompt = _safe_label(user_prompt, "")
    if prompt:
        return prompt
    if cwd_folder:
        return _safe_label(_humanize_folder_name(cwd_folder), _UNKNOWN_TASK)
    return _UNKNOWN_TASK


def _humanize_folder_name(name: str) -> str:
    text = str(name or "").strip()
    match = _DATE_FOLDER_RE.match(text)
    if match:
        text = match.group(1)
    return " ".join(part for part in text.replace("_", "-").split("-") if part)


def _text_from_content(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    chunks: list[str] = []
    for item in value:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            chunks.append(item["text"])
    return "\n".join(chunks)


def _head_json_objects(path: Path, *, max_lines: int = 40, max_bytes: int = 65_536) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes)
    except OSError:
        return []
    objects: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines()[:max_lines]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            objects.append(item)
    return objects


def _bounded_session_paths(root: Path, provider: str, *, now: datetime | None = None) -> list[Path]:
    """Find a bounded number of session files without scanning an entire home tree."""
    paths: list[Path] = []
    pending = deque([root])
    directories_seen = 0
    cutoff = (now or datetime.now(timezone.utc)).timestamp() - _SESSION_MTIME_WINDOW_SECONDS
    while pending and directories_seen < 200 and len(paths) < 200:
        directory_path = pending.popleft()
        directories_seen += 1
        entries = sorted(os.scandir(directory_path), key=lambda entry: entry.name)
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                child = Path(entry.path)
                if provider == "cursor" and (
                    entry.name == "agent-transcripts" or "agent-transcripts" in directory_path.parts
                ):
                    pending.appendleft(child)
                else:
                    pending.append(child)
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            filename = entry.name
            is_codex_rollout = provider == "codex" and fnmatch(filename, "rollout-*.jsonl")
            is_cursor_transcript = (
                provider == "cursor" and "agent-transcripts" in directory_path.parts and filename.endswith(".jsonl")
            )
            if not (is_codex_rollout or is_cursor_transcript):
                continue
            try:
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    continue
            except OSError:
                continue
            paths.append(directory_path / filename)
            if len(paths) >= 200:
                return paths
    return paths


def _missing_local_source(provider: str, harness: str, now: datetime, local_host: str) -> dict[str, Any]:
    return _record(
        activity_id=f"{provider}:source:local",
        parent_activity_id=None,
        provider=provider,
        harness=harness,
        kind="source",
        host=local_host,
        label=f"{provider} activity",
        task_label=_UNKNOWN_TASK,
        model=None,
        state="stale",
        started_at=None,
        updated_at=None,
        source=_source(f"{provider}-local-session", "best-effort", now),
        links={},
        now=now,
    )


def _worker_records(
    run_dir: Path,
    run: dict[str, Any],
    parent_activity_id: str,
    source: dict[str, str],
    now: datetime,
    started_at: str | None,
    updated_at: str | None,
    run_state: str,
    local_host: str,
    lock_state: str,
) -> list[dict[str, Any]]:
    plan = _read_json(run_dir / "plan.json") or {}
    assignments = plan.get("assignments") if isinstance(plan.get("assignments"), list) else []
    results = _read_json(run_dir / "worker-results.json") or {}
    result_by_worker = {
        str(item.get("worker")): item
        for item in results.get("results", [])
        if isinstance(item, dict) and isinstance(item.get("worker"), str)
    }
    active = {seat for seat in run.get("active_seats", []) if isinstance(seat, str)}
    records: list[dict[str, Any]] = []
    for assignment in assignments[:50]:
        if not isinstance(assignment, dict) or not isinstance(assignment.get("worker"), str):
            continue
        worker = assignment["worker"]
        result = result_by_worker.get(worker, {})
        raw_state: object = (
            result.get("status") if result else ("running" if worker in active else run.get("status") or run_state)
        )
        if result and result.get("ok") is True:
            raw_state = "succeeded"
        records.append(
            _record(
                activity_id=f"brigade:worker:{run_dir.name}:{worker}",
                parent_activity_id=parent_activity_id,
                provider="brigade",
                harness="brigade-run",
                kind="worker",
                host=local_host,
                label=_safe_label(worker, "Worker"),
                task_label=_safe_label(assignment.get("task_label"), "Worker assignment"),
                model=_safe_model(assignment.get("model")),
                state=_reconcile_claimed_live(
                    _normalized_state(raw_state, updated_at, now, authoritative=True),
                    updated_at,
                    now,
                    lock_state,
                ),
                started_at=started_at,
                updated_at=updated_at,
                source=source,
                links={"run": f"brigade runs show {run_dir.name}"},
                now=now,
            )
        )
    return records


def _configured_sources(target: Path, now: datetime) -> list[dict[str, Any]]:
    config = _read_json(target / ".brigade" / "center" / "agent-activity-sources.json") or {}
    sources = config.get("sources") if isinstance(config.get("sources"), list) else []
    records: list[dict[str, Any]] = []
    for index, source_config in enumerate(sources[:50]):
        if not isinstance(source_config, dict):
            continue
        provider = _safe_provider(source_config.get("provider"))
        host = _safe_alias(source_config.get("host"))
        journal = source_config.get("journal")
        if not isinstance(journal, str) or not journal:
            records.append(_unavailable_record(provider, host, index, now, _UNKNOWN_TASK))
            continue
        journal_path = (target / journal).resolve()
        try:
            journal_path.relative_to(target.resolve())
        except ValueError:
            records.append(_unavailable_record(provider, host, index, now, _UNKNOWN_TASK))
            continue
        if not journal_path.is_file():
            records.append(_unavailable_record(provider, host, index, now, _UNKNOWN_TASK))
            continue
        try:
            lines = _tail_lines(journal_path)
        except OSError:
            records.append(_unavailable_record(provider, host, index, now, _UNKNOWN_TASK))
            continue
        observed = False
        for line_number, line in enumerate(lines):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            observed = True
            updated_at = _timestamp(item.get("last_updated_at") or item.get("updated_at") or item.get("observed_at"))
            records.append(
                _record(
                    activity_id=f"{provider}:journal:{index}:{line_number}",
                    parent_activity_id=None,
                    provider=provider,
                    harness=f"{provider} observation",
                    kind="agent",
                    host=host,
                    label=f"{provider} agent",
                    task_label=_UNKNOWN_TASK,
                    model=None,
                    state=_normalized_state(
                        item.get("state") or item.get("status"), updated_at, now, authoritative=False
                    ),
                    started_at=_timestamp(item.get("started_at")),
                    updated_at=updated_at,
                    source=_source(f"{provider}-controller-journal", "best-effort", now),
                    links={},
                    now=now,
                )
            )
        if not observed:
            records.append(_unavailable_record(provider, host, index, now, _UNKNOWN_TASK))
    return records


def _unavailable_record(provider: str, host: str, index: int, now: datetime, label: str) -> dict[str, Any]:
    return _record(
        activity_id=f"{provider}:source:{index}",
        parent_activity_id=None,
        provider=provider,
        harness=f"{provider} observation",
        kind="source",
        host=host,
        label=f"{provider} activity",
        task_label=label,
        model=None,
        state="stale",
        started_at=None,
        updated_at=None,
        source=_source(f"{provider}-controller-journal", "best-effort", now),
        links={},
        now=now,
    )


def _record(
    *,
    activity_id: str,
    parent_activity_id: str | None,
    provider: str,
    harness: str,
    kind: str,
    host: str,
    label: str,
    task_label: str,
    model: str | None,
    state: str,
    started_at: str | None,
    updated_at: str | None,
    source: dict[str, str],
    links: dict[str, str],
    now: datetime,
) -> dict[str, Any]:
    return {
        "activity_id": activity_id,
        "parent_activity_id": parent_activity_id,
        "provider": provider,
        "harness": harness,
        "kind": kind,
        "host": host,
        "label": label,
        "task_label": task_label,
        "model": model,
        "state": state,
        "started_at": started_at,
        "last_updated_at": updated_at,
        "elapsed_seconds": _elapsed_seconds(started_at, now),
        "source": source,
        "links": links,
    }


def _source(name: str, authority: str, observed_at: datetime) -> dict[str, str]:
    return {"name": name, "authority": authority, "observed_at": observed_at.isoformat()}


def _reconcile_claimed_live(state: str, updated_at: str | None, now: datetime, lock_state: str) -> str:
    """Journal running/blocked is live only with a live lock and a fresh heartbeat."""
    if state not in {"running", "blocked"}:
        return state
    parsed = _parse_time(updated_at)
    heartbeat_stale = parsed is None or (now - parsed).total_seconds() > _HEARTBEAT_STALE_SECONDS
    if lock_state != "live" or heartbeat_stale:
        return "stale"
    return state


def _normalized_state(value: object, updated_at: str | None, now: datetime, *, authoritative: bool) -> str:
    raw = str(value or "unknown").lower().replace("_", "-")
    if raw in {"queued", "pending", "planned"}:
        return "queued"
    if raw in _RUNNING_STATUSES or raw in {"active", "in-progress"}:
        return "running" if authoritative or not _is_stale(updated_at, now) else "stale"
    if raw in {"paused", "awaiting-approval", "waiting"}:
        return "awaiting approval"
    if raw in {"blocked", "canceled", "interrupted"}:
        return "blocked"
    if raw in {"succeeded", "success", "completed", "ok", "dry-run"}:
        return "succeeded"
    if raw in {"failed", "timeout", "incomplete", "error"}:
        return "failed"
    return "unknown" if updated_at is not None else "stale"


def _is_stale(updated_at: str | None, now: datetime) -> bool:
    parsed = _parse_time(updated_at)
    return parsed is None or (now - parsed).total_seconds() > _STALE_AFTER_SECONDS


def _elapsed_seconds(started_at: str | None, now: datetime) -> int | None:
    parsed = _parse_time(started_at)
    return max(0, int((now - parsed).total_seconds())) if parsed else None


def _timestamp(value: object) -> str | None:
    return str(value) if _parse_time(value) else None


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _safe_alias(value: object) -> str:
    if not isinstance(value, str):
        return "local"
    text = value.lower().strip()
    return text if text and len(text) <= 64 and text.replace("-", "").isalnum() else "local"


def _safe_provider(value: object) -> str:
    provider = str(value or "unknown").lower()
    return provider if provider in {"brigade", "codex", "cursor", "t3"} else "unknown"


def _safe_label(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    text = " ".join(value.split())[:160]
    if not text or "/home/" in text or "\\Users\\" in text or text.startswith("/"):
        return fallback
    return text


def _safe_model(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())[:80]
    if not text or "/" in text or " " in text:
        return None
    return text


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _tail_lines(path: Path, limit: int = 65536) -> list[str]:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - limit))
        text = handle.read().decode("utf-8", errors="replace")
    return text.splitlines()[-100:]
