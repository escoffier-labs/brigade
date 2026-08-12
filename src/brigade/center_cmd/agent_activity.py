"""Safe, read-only observations for the Center Agent Activity contract."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ACTIVITY_ENVELOPE_VERSION = 2
_STALE_AFTER_SECONDS = 300
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


def collect(target: Path, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Return bounded safe records without mutating local or provider state."""
    observed_at = now or datetime.now(timezone.utc)
    records = _brigade_records(target, observed_at)
    records.extend(_local_session_records(observed_at))
    records.extend(_configured_sources(target, observed_at))
    records.sort(
        key=lambda record: str(record.get("last_updated_at") or record.get("source", {}).get("observed_at") or ""),
        reverse=True,
    )
    return records[:100]


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


def _brigade_records(target: Path, now: datetime) -> list[dict[str, Any]]:
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
        state = _normalized_state(run.get("status"), updated_at, now, authoritative=True)
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
                host="local",
                label=_safe_label(run.get("orchestrator"), "Brigade orchestrator"),
                task_label=_safe_label(run.get("task_label"), "Brigade run"),
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
                host="local",
                label="Brigade run",
                task_label=_safe_label(run.get("task_label"), "Brigade run"),
                state=state,
                started_at=started_at,
                updated_at=updated_at,
                source=source,
                links={"run": f"brigade runs show {run_id}"},
                now=now,
            )
        )
        records.extend(_worker_records(run_dir, run, run_activity_id, source, now, started_at, updated_at, state))
    return records


def _local_session_records(now: datetime) -> list[dict[str, Any]]:
    """Observe file freshness only. Session files are never parsed as transcripts."""
    home = Path(os.environ.get("HOME", str(Path.home()))).expanduser()
    codex_home = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).expanduser()
    records = _session_file_records(
        codex_home / "sessions", "rollout-*.jsonl", "codex", "codex-cli", "Codex session", now
    )
    if not records:
        records.append(_missing_local_source("codex", "codex-cli", now))
    cursor_records = _session_file_records(
        home / ".cursor" / "projects",
        "agent-transcripts/*/*.jsonl",
        "cursor",
        "cursor-agent",
        "Cursor session",
        now,
    )
    records.extend(cursor_records or [_missing_local_source("cursor", "cursor-agent", now)])
    return records


def _session_file_records(
    root: Path, pattern: str, provider: str, harness: str, label: str, now: datetime
) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    try:
        paths = []
        for path in root.rglob(pattern):
            paths.append(path)
            if len(paths) >= 200:
                break
        paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        paths = paths[:25]
    except OSError:
        return []
    for path in paths:
        try:
            updated_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        except OSError:
            continue
        records.append(
            _record(
                activity_id=f"{provider}:session:{path.stem}",
                parent_activity_id=None,
                provider=provider,
                harness=harness,
                kind="agent",
                host="local",
                label=label,
                task_label="Task unavailable",
                state="running" if not _is_stale(updated_at, now) else "stale",
                started_at=None,
                updated_at=updated_at,
                source=_source(f"{provider}-local-session", "best-effort", now),
                links={},
                now=now,
            )
        )
    return records


def _missing_local_source(provider: str, harness: str, now: datetime) -> dict[str, Any]:
    return _record(
        activity_id=f"{provider}:source:local",
        parent_activity_id=None,
        provider=provider,
        harness=harness,
        kind="source",
        host="local",
        label=f"{provider} activity",
        task_label="activity unavailable",
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
        raw_state: object = result.get("status") if result else ("running" if worker in active else run_state)
        if result and result.get("ok") is True:
            raw_state = "succeeded"
        records.append(
            _record(
                activity_id=f"brigade:worker:{run_dir.name}:{worker}",
                parent_activity_id=parent_activity_id,
                provider="brigade",
                harness="brigade-run",
                kind="worker",
                host="local",
                label=_safe_label(worker, "Worker"),
                task_label=_safe_label(assignment.get("task_label"), "Worker assignment"),
                state=_normalized_state(raw_state, updated_at, now, authoritative=True),
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
            records.append(_unavailable_record(provider, host, index, now, "activity journal unavailable"))
            continue
        journal_path = (target / journal).resolve()
        try:
            journal_path.relative_to(target.resolve())
        except ValueError:
            records.append(_unavailable_record(provider, host, index, now, "activity journal unavailable"))
            continue
        if not journal_path.is_file():
            records.append(_unavailable_record(provider, host, index, now, "activity journal unavailable"))
            continue
        try:
            lines = _tail_lines(journal_path)
        except OSError:
            records.append(_unavailable_record(provider, host, index, now, "activity journal unavailable"))
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
                    task_label="Task unavailable",
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
            records.append(_unavailable_record(provider, host, index, now, "activity journal unavailable"))
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
        "state": state,
        "started_at": started_at,
        "last_updated_at": updated_at,
        "elapsed_seconds": _elapsed_seconds(started_at, now),
        "source": source,
        "links": links,
    }


def _source(name: str, authority: str, observed_at: datetime) -> dict[str, str]:
    return {"name": name, "authority": authority, "observed_at": observed_at.isoformat()}


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
