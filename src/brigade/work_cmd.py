"""Daily work session helpers."""
from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import dogfood_cmd
from .install import apply_gitignore
from .selection import Selection

OK = "ok"
WARN = "warn"
FAIL = "fail"
IMPORT_KINDS = ("task", "finding", "decision", "preference", "incident", "link", "command")


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return value[:48].strip("-") or "work-session"


def _work_root(target: Path) -> Path:
    return target / ".brigade" / "work"


def _current_path(target: Path) -> Path:
    return _work_root(target) / "current"


def _tasks_path(target: Path) -> Path:
    return _work_root(target) / "tasks.json"


def _imports_path(target: Path) -> Path:
    return _work_root(target) / "imports" / "inbox.jsonl"


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


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _read_task_ledger(target: Path) -> dict[str, Any]:
    path = _tasks_path(target)
    if not path.exists():
        return {"version": 1, "tasks": []}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "tasks": []}
    if not isinstance(payload, dict):
        return {"version": 1, "tasks": []}
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        payload["tasks"] = []
    payload["version"] = 1
    return payload


def _write_task_ledger(target: Path, payload: dict[str, Any]) -> None:
    payload["version"] = 1
    if not isinstance(payload.get("tasks"), list):
        payload["tasks"] = []
    _write_json(_tasks_path(target), payload)


def _read_imports(target: Path) -> list[dict[str, Any]]:
    path = _imports_path(target)
    if not path.exists():
        return []
    imports: list[dict[str, Any]] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            imports.append(item)
    return imports


def _write_imports(target: Path, imports: list[dict[str, Any]]) -> None:
    path = _imports_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = "".join(json.dumps(item, sort_keys=True) + "\n" for item in imports)
    path.write_text(rendered)


def _task_sort_key(task: dict[str, Any]) -> str:
    return str(task.get("created_at") or task.get("id") or "")


def _import_sort_key(item: dict[str, Any]) -> str:
    return str(item.get("created_at") or item.get("id") or "")


def _task_text_key(text: str) -> str:
    return " ".join(text.casefold().split())


def _import_record_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(item.get("source") or "manual"),
        str(item.get("kind") or "task"),
        _task_text_key(str(item.get("text") or "")),
    )


def _validate_import_record(value: object, *, label: str) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return None, [f"{label}: expected JSON object"]

    text = value.get("text")
    if not isinstance(text, str) or not text.strip():
        errors.append(f"{label}: text must be a non-empty string")
    kind = value.get("kind", "task")
    if not isinstance(kind, str) or kind not in IMPORT_KINDS:
        errors.append(f"{label}: kind must be one of: {', '.join(IMPORT_KINDS)}")
    source = value.get("source", "manual")
    if not isinstance(source, str) or not source.strip():
        errors.append(f"{label}: source must be a non-empty string")
    metadata = value.get("metadata", {})
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        errors.append(f"{label}: metadata must be an object when present")

    if errors:
        return None, errors
    return {
        "text": text.strip(),
        "kind": kind,
        "source": source.strip(),
        "metadata": metadata,
    }, []


def _load_import_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        return records, [f"{path}: {exc}"]
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        label = f"line {line_number}"
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"{label}: invalid JSON: {exc.msg}")
            continue
        record, record_errors = _validate_import_record(value, label=label)
        errors.extend(record_errors)
        if record is not None:
            records.append(record)
    return records, errors


def _append_import_records(
    target: Path,
    records: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    imports = _read_imports(target)
    existing = {
        _import_record_key(item)
        for item in imports
        if isinstance(item, dict) and item.get("status", "pending") == "pending"
    }
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for record in records:
        key = _import_record_key(record)
        if key[2] and key in existing:
            skipped.append(record)
            continue
        item = _make_import(
            str(record["text"]),
            kind=str(record["kind"]),
            source=str(record["source"]),
            metadata=record.get("metadata") if isinstance(record.get("metadata"), dict) else None,
        )
        imported.append(item)
        existing.add(key)
    if imported and not dry_run:
        imports.extend(imported)
        _write_imports(target, imports)
    return imported, skipped


def _pending_tasks(target: Path) -> list[dict[str, Any]]:
    ledger = _read_task_ledger(target)
    tasks = [
        task
        for task in ledger["tasks"]
        if isinstance(task, dict)
        and task.get("status", "pending") == "pending"
        and isinstance(task.get("text"), str)
        and task["text"].strip()
    ]
    tasks.sort(key=_task_sort_key)
    return tasks


def _pending_imports(target: Path) -> list[dict[str, Any]]:
    imports = [
        item
        for item in _read_imports(target)
        if isinstance(item, dict)
        and item.get("status", "pending") == "pending"
        and isinstance(item.get("text"), str)
        and item["text"].strip()
    ]
    imports.sort(key=_import_sort_key)
    return imports


def _import_counts(imports: list[dict[str, Any]]) -> dict[str, Any]:
    by_source: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for item in imports:
        source = str(item.get("source") or "manual")
        kind = str(item.get("kind") or "task")
        by_source[source] = by_source.get(source, 0) + 1
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return {
        "total": len(imports),
        "by_source": dict(sorted(by_source.items())),
        "by_kind": dict(sorted(by_kind.items())),
    }


def _matching_pending_imports(
    target: Path,
    *,
    kind: str | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    imports = _pending_imports(target)
    if kind:
        imports = [item for item in imports if item.get("kind") == kind]
    if source:
        imports = [item for item in imports if item.get("source") == source]
    return imports


def _find_pending_task_by_text(target: Path, text: str) -> dict[str, Any] | None:
    wanted = _task_text_key(text)
    if not wanted:
        return None
    for task in _pending_tasks(target):
        if _task_text_key(str(task.get("text") or "")) == wanted:
            return task
    return None


def _find_import(target: Path, import_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    imports = _read_imports(target)
    matches: list[dict[str, Any]] = []
    for item in imports:
        if not isinstance(item, dict):
            continue
        if item.get("id") == import_id:
            return item, imports
        if isinstance(item.get("id"), str) and item["id"].startswith(import_id):
            matches.append(item)
    if len(matches) == 1:
        return matches[0], imports
    return None, imports


def _mark_import_promoted(target: Path, item: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    text = str(item.get("text") or "").strip()
    metadata: dict[str, Any] = {
        "import_id": item.get("id"),
        "import_kind": item.get("kind"),
        "import_source": item.get("source"),
    }
    item_metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    metadata.update(item_metadata)
    task, created = _add_task(
        target,
        text,
        source=f"import:{item.get('source') or 'manual'}",
        metadata=metadata,
    )
    now = _now().isoformat()
    item["status"] = "promoted"
    item["updated_at"] = now
    item["promoted_at"] = now
    item["task_id"] = task["id"]
    return task, created


def _find_task(target: Path, task_id: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    ledger = _read_task_ledger(target)
    matches: list[dict[str, Any]] = []
    for task in ledger["tasks"]:
        if not isinstance(task, dict):
            continue
        if task.get("id") == task_id:
            return task, ledger
        if isinstance(task.get("id"), str) and task["id"].startswith(task_id):
            matches.append(task)
    if len(matches) == 1:
        return matches[0], ledger
    return None, ledger


def _make_task(text: str, *, source: str = "manual", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    now = _now()
    created = now.isoformat()
    task = {
        "id": f"{now.strftime('%Y%m%d-%H%M%S')}-{_slug(text)}-{uuid4().hex[:6]}",
        "text": text,
        "status": "pending",
        "source": source,
        "created_at": created,
        "updated_at": created,
    }
    if metadata:
        task["metadata"] = metadata
    return task


def _parse_metadata(items: list[str] | None) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError("--metadata entries must use key=value")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError("--metadata entries must have a key")
        metadata[key] = value.strip()
    return metadata


def _make_import(
    text: str,
    *,
    kind: str,
    source: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = _now()
    created = now.isoformat()
    item: dict[str, Any] = {
        "id": f"{now.strftime('%Y%m%d-%H%M%S')}-{kind}-{_slug(text)}-{uuid4().hex[:6]}",
        "kind": kind,
        "source": source,
        "text": text,
        "status": "pending",
        "created_at": created,
        "updated_at": created,
    }
    if metadata:
        item["metadata"] = metadata
    return item


def _add_task(
    target: Path,
    text: str,
    *,
    source: str = "manual",
    metadata: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    ledger = _read_task_ledger(target)
    existing = _find_pending_task_by_text(target, text)
    if existing is not None:
        return existing, False
    task = _make_task(text, source=source, metadata=metadata)
    ledger["tasks"].append(task)
    _write_task_ledger(target, ledger)
    return task, True


def _latest_run_next_metadata(target: Path) -> tuple[str | None, dict[str, Any]]:
    dogfood = _dogfood_snapshot(target)
    next_step = dogfood.get("next") if isinstance(dogfood.get("next"), str) else None
    latest = dogfood.get("latest_run") if isinstance(dogfood.get("latest_run"), dict) else None
    metadata: dict[str, Any] = {
        "dogfood_next_source": dogfood.get("next_source"),
    }
    if isinstance(latest, dict):
        metadata.update(
            {
                "run_path": latest.get("path"),
                "run_started_at": latest.get("started_at"),
                "run_status": latest.get("status"),
                "run_task": latest.get("task"),
            }
        )
    return next_step.strip() if next_step and next_step.strip() else None, metadata


def _queue_latest_next(
    target: Path,
    *,
    session_dir: Path | None = None,
    session_title: str | None = None,
) -> tuple[dict[str, Any] | None, bool, str | None]:
    next_step, metadata = _latest_run_next_metadata(target)
    if not next_step:
        return None, False, "no extracted next step is available"
    if session_dir is not None:
        metadata["session_path"] = str(session_dir)
    if session_title:
        metadata["session_title"] = session_title
    task, created = _add_task(
        target,
        next_step,
        source="latest_dogfood_run",
        metadata=metadata,
    )
    return task, created, None


def _read_session(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads((path / "session.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _session_sort_key(item: tuple[Path, dict[str, Any]]) -> str:
    path, payload = item
    return str(payload.get("ended_at") or payload.get("started_at") or path.name)


def _parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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


def _resolve_next_task(target: Path) -> dict[str, Any]:
    pending = _pending_tasks(target)
    if pending:
        task = pending[0]
        return {
            "task": str(task.get("text", "")).strip(),
            "source": "task_ledger",
            "task_id": task.get("id"),
            "ledger_task": task,
            "dogfood": _dogfood_snapshot(target),
        }
    dogfood = _dogfood_snapshot(target)
    next_step = dogfood.get("next") if isinstance(dogfood.get("next"), str) else None
    if next_step and next_step.strip():
        return {
            "task": next_step.strip(),
            "source": "latest_dogfood_run",
            "task_id": None,
            "dogfood": dogfood,
        }
    return {
        "task": dogfood_cmd.DEFAULT_TASK,
        "source": "default_review",
        "task_id": None,
        "dogfood": dogfood,
    }


def _display_session(path: Path, payload: dict[str, Any]) -> None:
    print(f"session: {path}")
    print(f"id: {payload.get('id', path.name)}")
    print(f"status: {payload.get('status', 'unknown')}")
    if payload.get("title"):
        print(f"title: {payload['title']}")
    print(f"target: {payload.get('target', '')}")
    print(f"started: {payload.get('started_at', '')}")
    if payload.get("ended_at"):
        print(f"ended: {payload['ended_at']}")
    if payload.get("note"):
        print(f"note: {payload['note']}")
    notes = payload.get("notes")
    if isinstance(notes, list):
        print(f"notes: {len(notes)}")
        if notes and isinstance(notes[-1], dict) and notes[-1].get("text"):
            print(f"latest_note: {_short(str(notes[-1]['text']))}")
    if payload.get("handoff"):
        print(f"handoff: {payload['handoff']}")

    start_snapshot = payload.get("start") if isinstance(payload.get("start"), dict) else {}
    end_snapshot = payload.get("end") if isinstance(payload.get("end"), dict) else {}
    snapshot = end_snapshot or start_snapshot
    git = snapshot.get("git") if isinstance(snapshot, dict) else {}
    if isinstance(git, dict) and git.get("available"):
        print("git:")
        print(f"  branch: {git.get('branch')}")
        dirty = git.get("dirty_files") if isinstance(git.get("dirty_files"), list) else []
        print(f"  dirty_files: {len(dirty)}")
        for item in dirty[:20]:
            print(f"    {item}")
    dogfood = snapshot.get("dogfood") if isinstance(snapshot, dict) else {}
    if isinstance(dogfood, dict):
        print("dogfood:")
        print(f"  ready: {dogfood.get('ready')}")
        latest = dogfood.get("latest_run")
        if isinstance(latest, dict):
            print(f"  latest_run: {latest.get('started_at')} [{latest.get('status')}] {latest.get('path')}")
            if latest.get("task"):
                print(f"  latest_task: {_short(str(latest['task']))}")
        if dogfood.get("next"):
            print(f"  next: {_short(str(dogfood['next']))}")


def _write_session_markdown(path: Path, *, title: str, payload: dict[str, Any], key: str) -> None:
    snapshot = payload[key]
    git = snapshot.get("git", {})
    dogfood = snapshot.get("dogfood", {})
    lines = [
        f"# {title}",
        "",
        f"- Session: {payload['id']}",
        f"- Target: {payload['target']}",
        f"- Started: {payload['started_at']}",
    ]
    if payload.get("ended_at"):
        lines.append(f"- Ended: {payload['ended_at']}")
    if payload.get("title"):
        lines.append(f"- Title: {payload['title']}")
    if payload.get("note"):
        lines.append(f"- Note: {payload['note']}")
    lines.extend(["", "## Git", ""])
    if git.get("available"):
        lines.append(f"- Branch: {git.get('branch')}")
        dirty = git.get("dirty_files") or []
        lines.append(f"- Dirty files: {len(dirty)}")
        for item in dirty[:20]:
            lines.append(f"  - `{item}`")
    else:
        lines.append("- unavailable")
    lines.extend(["", "## Dogfood", ""])
    lines.append(f"- Ready: {dogfood.get('ready')}")
    if dogfood.get("latest_run"):
        latest = dogfood["latest_run"]
        lines.append(f"- Latest run: {latest.get('started_at')} [{latest.get('status')}] {latest.get('path')}")
    if dogfood.get("next"):
        lines.append(f"- Next: {dogfood['next']}")
    path.write_text("\n".join(lines) + "\n")


def _handoff_inbox(target: Path, payload: dict[str, Any], override: Path | None) -> Path:
    if override is not None:
        return override.expanduser()
    dogfood = payload.get("end", {}).get("dogfood", {})
    configured = dogfood.get("handoff_inbox")
    if isinstance(configured, str) and configured:
        return Path(configured).expanduser()
    return dogfood_cmd.default_handoff_inbox(target)


def _write_work_handoff(target: Path, session_dir: Path, payload: dict[str, Any], inbox: Path) -> Path:
    ended = payload.get("ended_at") or _now().isoformat()
    ended_slug = re.sub(r"[^0-9]", "", str(ended))[:12] or _now().strftime("%Y%m%d%H%M")
    title = payload.get("title") or payload.get("id") or "work-session"
    path = inbox / f"{ended_slug}-brigade-work-{_slug(str(title))}-{uuid4().hex[:6]}.md"
    end_snapshot = payload.get("end", {})
    git = end_snapshot.get("git", {})
    dogfood = end_snapshot.get("dogfood", {})
    dirty = git.get("dirty_files") if isinstance(git, dict) else []
    dirty_lines = "\n".join(f"  - `{item}`" for item in dirty[:20]) if isinstance(dirty, list) else "  - unavailable"
    latest = dogfood.get("latest_run") if isinstance(dogfood, dict) else None
    latest_line = "- latest run: none"
    if isinstance(latest, dict):
        latest_line = f"- latest run: `{latest.get('path')}` ({latest.get('status')})"
    next_step = dogfood.get("next") if isinstance(dogfood, dict) else None
    next_line = f"- next: {next_step}" if next_step else "- next: none extracted"
    note = payload.get("note") or ""
    document_content = f"""### Brigade work session: {payload.get('id')}
- target: `{target}`
- session artifacts: `{session_dir}`
- branch: {git.get('branch') if isinstance(git, dict) else 'unknown'}
- dirty files: {len(dirty) if isinstance(dirty, list) else 'unknown'}
{latest_line}
{next_line}
"""
    if note:
        document_content += f"- note: {note}\n"
    body = f"""# Memory Handoff

## Type

workflow

## Title

Brigade work session ended: {_slug(str(title))}

## Summary

A Brigade work session was ended and local session artifacts were written. This handoff captures the session path, final git state, latest dogfood run, and extracted next step so the memory owner can route durable workflow context.

## Durable facts

- session: `{payload.get('id')}`
- target: `{target}`
- session artifacts: `{session_dir}`
- status: {payload.get('status')}
- started: {payload.get('started_at')}
- ended: {payload.get('ended_at')}
- note: {note or 'none'}
- branch: {git.get('branch') if isinstance(git, dict) else 'unknown'}
- dirty files:
{dirty_lines}
{latest_line}
{next_line}

## Evidence

- session.json: `{session_dir / 'session.json'}`
- start summary: `{session_dir / 'start.md'}`
- end summary: `{session_dir / 'end.md'}`

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

{document_content.strip()}
"""
    inbox.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _print_dirty(lines: list[str], *, limit: int) -> None:
    print(f"dirty_files: {len(lines)}")
    for line in lines[:limit]:
        print(f"  {line}")
    remaining = len(lines) - limit
    if remaining > 0:
        print(f"  ... {remaining} more")


def _doctor_line(level: str, name: str, detail: object) -> None:
    print(f"[{level}] {name}: {detail}")


def _doctor_ignore_level(value: str) -> str:
    if value in {"yes", "outside-target"}:
        return OK
    if value == "no":
        return WARN
    return WARN


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


def _next_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    active = _active_session_info(target)
    resolved = _resolve_next_task(target)
    dogfood = resolved["dogfood"]
    suggested = 'brigade work end --note "..." --handoff' if active is not None else "brigade work run"
    return {
        "target": str(target),
        "active_session": active,
        "dogfood": dogfood,
        "next_source": resolved["source"],
        "task_id": resolved.get("task_id"),
        "next": str(resolved["task"]),
        "suggested_command": suggested,
    }


def _suggested_command(active: dict[str, Any] | None, next_text: object, source: object) -> str:
    if active is not None:
        return 'brigade work end --note "..." --handoff'
    if source == "task_ledger":
        return "brigade work run"
    if isinstance(next_text, str) and next_text.strip() and source != "default_review":
        return f"brigade work run {shlex.quote(next_text.strip())}"
    return "brigade work run"


def _brief_payload(target: Path, *, limit: int = 3) -> dict[str, Any]:
    target = target.expanduser().resolve()
    active = _active_session_info(target)
    sessions, skipped = _collect_sessions(_work_root(target))
    latest_session = _session_info(sessions[0][0], sessions[0][1]) if sessions else None
    recent_sessions = [_session_info(path, payload) for path, payload in sessions[:limit]]
    resolved = _resolve_next_task(target)
    git = _git_snapshot(target)
    suggested = _suggested_command(active, resolved["task"], resolved["source"])
    pending = _pending_tasks(target)
    pending_imports = _pending_imports(target)
    pending_import_counts = _import_counts(pending_imports)
    return {
        "target": str(target),
        "git": git,
        "active_session": active,
        "latest_session": latest_session,
        "recent_sessions": recent_sessions,
        "skipped_sessions": skipped,
        "tasks_path": str(_tasks_path(target)),
        "pending_tasks": pending,
        "imports_path": str(_imports_path(target)),
        "pending_imports": pending_imports,
        "pending_import_counts": pending_import_counts,
        "dogfood": resolved["dogfood"],
        "next_source": resolved["source"],
        "task_id": resolved.get("task_id"),
        "next": str(resolved["task"]),
        "suggested_command": suggested,
    }


def _print_bootstrap_line(level: str, name: str, detail: object) -> None:
    print(f"[{level}] {name}: {detail}")


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


def start(*, target: Path, title: str | None = None, force: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    root = _work_root(target)
    current = _current_path(target)
    if current.exists() and not force:
        print(f"error: work session already active: {current.read_text().strip()}", file=sys.stderr)
        return 2

    started = _now()
    session_id = f"{started.strftime('%Y%m%d-%H%M%S')}-{_slug(title or 'work-session')}"
    session_dir = root / session_id
    session_dir.mkdir(parents=True, exist_ok=False)
    payload: dict[str, Any] = {
        "id": session_id,
        "title": title,
        "target": str(target),
        "status": "active",
        "started_at": started.isoformat(),
        "start": _session_snapshot(target),
    }
    _write_json(session_dir / "session.json", payload)
    _write_session_markdown(session_dir / "start.md", title="Brigade Work Session Start", payload=payload, key="start")
    current.write_text(session_id + "\n")
    print(f"session: {session_dir}")
    print(f"status: active")
    return 0


def end(*, target: Path, note: str | None = None, handoff: bool = False, handoff_inbox: Path | None = None) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    current = _current_path(target)
    if not current.exists():
        print(f"error: no active work session in {_work_root(target)}", file=sys.stderr)
        return 1
    session_id = current.read_text().strip()
    session_dir = _work_root(target) / session_id
    session_json = session_dir / "session.json"
    try:
        payload = json.loads(session_json.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: invalid active work session: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("error: invalid active work session: session.json must contain an object", file=sys.stderr)
        return 2

    payload["status"] = "ended"
    payload["ended_at"] = _now().isoformat()
    payload["note"] = note
    payload["end"] = _session_snapshot(target)
    _write_json(session_json, payload)
    _write_session_markdown(session_dir / "end.md", title="Brigade Work Session End", payload=payload, key="end")
    if handoff:
        inbox = _handoff_inbox(target, payload, handoff_inbox)
        handoff_path = _write_work_handoff(target, session_dir, payload, inbox)
        payload["handoff"] = str(handoff_path)
        _write_json(session_json, payload)
    current.unlink()
    print(f"session: {session_dir}")
    if handoff:
        print(f"handoff: {payload['handoff']}")
    print("status: ended")
    return 0


def note(*, target: Path, text: str) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    rendered = text.strip()
    if not rendered:
        print("error: note text is required", file=sys.stderr)
        return 2

    current = _current_path(target)
    if not current.exists():
        print(f"error: no active work session in {_work_root(target)}", file=sys.stderr)
        return 1
    session_id = current.read_text().strip()
    session_dir = _work_root(target) / session_id
    session_json = session_dir / "session.json"
    try:
        payload = json.loads(session_json.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: invalid active work session: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("error: invalid active work session: session.json must contain an object", file=sys.stderr)
        return 2

    entry = {
        "created_at": _now().isoformat(),
        "text": rendered,
    }
    notes = payload.setdefault("notes", [])
    if not isinstance(notes, list):
        print("error: invalid active work session: notes must be a list", file=sys.stderr)
        return 2
    notes.append(entry)
    _write_json(session_json, payload)

    notes_path = session_dir / "notes.md"
    prefix = "" if notes_path.exists() and notes_path.read_text().endswith("\n") else "\n"
    with notes_path.open("a") as handle:
        if notes_path.stat().st_size == 0:
            handle.write("# Brigade Work Session Notes\n")
        else:
            handle.write(prefix)
        handle.write(f"\n## {entry['created_at']}\n\n{rendered}\n")
    print(f"session: {session_dir}")
    print(f"note: {_short(rendered)}")
    return 0


def list_sessions(*, target: Path, limit: int = 10) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    root = _work_root(target)
    sessions, skipped = _collect_sessions(root)
    for path, payload in sessions[:limit]:
        snapshot = payload.get("end") if isinstance(payload.get("end"), dict) else payload.get("start", {})
        dirty = _dirty_count(snapshot) if isinstance(snapshot, dict) else 0
        title = _short(str(payload.get("title") or ""))
        ended = payload.get("ended_at") or "active"
        print(
            f"{payload.get('started_at', path.name)} [{payload.get('status', 'unknown')}] "
            f"dirty={dirty} ended={ended} {path}"
        )
        if title:
            print(f"  {title}")
    if not sessions:
        print(f"no work sessions found in {root}")
    if skipped:
        print(f"skipped {skipped} invalid work session{'s' if skipped != 1 else ''}", file=sys.stderr)
    return 0


def latest(*, target: Path) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    root = _work_root(target)
    sessions, skipped = _collect_sessions(root)
    if skipped:
        print(f"skipped {skipped} invalid work session{'s' if skipped != 1 else ''}", file=sys.stderr)
    if not sessions:
        print(f"error: no work sessions found in {root}", file=sys.stderr)
        return 1
    path, payload = sessions[0]
    _display_session(path, payload)
    return 0


def show(*, target: Path, session: str | Path) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = _resolve_session(target, session)
    if not path.is_dir():
        print(f"error: work session not found: {path}", file=sys.stderr)
        return 2
    payload = _read_session(path)
    if payload is None:
        print(f"error: session.json not found or invalid in {path}", file=sys.stderr)
        return 2
    _display_session(path, payload)
    return 0


def recap(*, target: Path, limit: int = 5, since: str | None = None) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    try:
        since_dt = _parse_since(since)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    root = _work_root(target)
    sessions, skipped = _collect_sessions(root)
    if since_dt is not None:
        sessions = [
            (path, payload)
            for path, payload in sessions
            if (_parse_iso_datetime(payload.get("ended_at") or payload.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc))
            >= since_dt
        ]
    sessions = sessions[:limit]

    print(f"work recap: {target}")
    if since:
        print(f"since: {since}")
    print(f"sessions: {len(sessions)}")
    if skipped:
        print(f"skipped: {skipped}", file=sys.stderr)
    if not sessions:
        print(f"no work sessions found in {root}")
        return 0

    branches = sorted({branch for _, payload in sessions if (branch := _branch(_snapshot(payload)))})
    if branches:
        print(f"branches: {', '.join(branches)}")
    handoffs = [str(payload.get("handoff")) for _, payload in sessions if payload.get("handoff")]
    if handoffs:
        print(f"handoffs: {len(handoffs)}")

    print("items:")
    for path, payload in sessions:
        snapshot = _snapshot(payload)
        title = str(payload.get("title") or payload.get("id") or path.name)
        print(f"- {title}")
        print(f"  id: {payload.get('id', path.name)}")
        print(f"  status: {payload.get('status', 'unknown')}")
        print(f"  started: {payload.get('started_at', '')}")
        if payload.get("ended_at"):
            print(f"  ended: {payload['ended_at']}")
        branch = _branch(snapshot)
        if branch:
            print(f"  branch: {branch}")
        print(f"  dirty_files: {_dirty_count(snapshot)}")
        if payload.get("note"):
            print(f"  note: {_short(str(payload['note']))}")
        if payload.get("handoff"):
            print(f"  handoff: {payload['handoff']}")
        next_text = _next_step(snapshot)
        if next_text:
            print(f"  next: {_short(next_text)}")
    return 0


def _print_resume_session(label: str, path: Path, payload: dict[str, Any]) -> None:
    print(f"{label}: {path}")
    print(f"{label}_status: {payload.get('status', 'unknown')}")
    if payload.get("title"):
        print(f"{label}_title: {_short(str(payload['title']))}")
    print(f"{label}_started: {payload.get('started_at', '')}")
    if payload.get("ended_at"):
        print(f"{label}_ended: {payload['ended_at']}")
    if payload.get("note"):
        print(f"{label}_note: {_short(str(payload['note']))}")
    notes = payload.get("notes")
    if isinstance(notes, list):
        print(f"{label}_notes: {len(notes)}")
        if notes and isinstance(notes[-1], dict) and notes[-1].get("text"):
            print(f"{label}_latest_note: {_short(str(notes[-1]['text']))}")
    if payload.get("handoff"):
        print(f"{label}_handoff: {payload['handoff']}")


def resume(*, target: Path) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    print(f"work resume: {target}")
    root = _work_root(target)
    current = _current_path(target)
    active_payload: dict[str, Any] | None = None
    if current.exists():
        active_dir = root / current.read_text().strip()
        active_payload = _read_session(active_dir)
        if active_payload is None:
            print(f"active_session: invalid ({active_dir})")
        else:
            _print_resume_session("active_session", active_dir, active_payload)
    else:
        print("active_session: none")

    sessions, skipped = _collect_sessions(root)
    if skipped:
        print(f"skipped: {skipped}", file=sys.stderr)
    if sessions:
        latest_path, latest_payload = sessions[0]
        if active_payload is None or latest_payload.get("id") != active_payload.get("id"):
            _print_resume_session("latest_session", latest_path, latest_payload)
    else:
        print(f"latest_session: none ({root})")

    dogfood = _dogfood_snapshot(target)
    print(f"dogfood_ready: {dogfood.get('ready')}")
    if dogfood.get("error"):
        print(f"dogfood_error: {dogfood['error']}")
    if dogfood.get("target"):
        print(f"dogfood_target: {dogfood['target']}")
    if dogfood.get("artifacts_dir"):
        print(f"dogfood_artifacts: {dogfood['artifacts_dir']}")
    latest_run = dogfood.get("latest_run")
    if isinstance(latest_run, dict):
        print(
            "latest_run: "
            f"{latest_run.get('started_at', '')} "
            f"[{latest_run.get('status', 'unknown')}] {latest_run.get('path')}"
        )
        if latest_run.get("task"):
            print(f"latest_task: {_short(str(latest_run['task']))}")
    else:
        print("latest_run: none")

    next_step = dogfood.get("next") if isinstance(dogfood.get("next"), str) else None
    print(f"next: {_short(next_step) if next_step else 'none'}")
    if active_payload is not None:
        print('suggested_command: brigade work end --note "..." --handoff')
    elif next_step:
        print(f"suggested_command: brigade work run {shlex.quote(next_step)}")
    else:
        print("suggested_command: brigade work run")
    return 0


def brief(*, target: Path, limit: int = 3, json_output: bool = False) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    payload = _brief_payload(target, limit=limit)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"work brief: {target}")
    git = payload["git"]
    if isinstance(git, dict) and git.get("available"):
        print(f"branch: {git.get('branch')}")
        dirty = git.get("dirty_files") if isinstance(git.get("dirty_files"), list) else []
        print(f"dirty_files: {len(dirty)}")
        for item in dirty[:8]:
            print(f"  {item}")
        if len(dirty) > 8:
            print(f"  ... {len(dirty) - 8} more")
    else:
        print("git: unavailable")

    active = payload["active_session"]
    if isinstance(active, dict):
        if active.get("valid"):
            print(f"active_session: {active.get('path')}")
            if active.get("title"):
                print(f"active_session_title: {_short(str(active['title']))}")
        else:
            print(f"active_session: invalid ({active.get('path')})")
    else:
        print("active_session: none")

    latest_session = payload["latest_session"]
    if isinstance(latest_session, dict):
        print(f"latest_session: {latest_session.get('path')}")
        if latest_session.get("title"):
            print(f"latest_session_title: {_short(str(latest_session['title']))}")
        if latest_session.get("note"):
            print(f"latest_session_note: {_short(str(latest_session['note']))}")
        if latest_session.get("handoff"):
            print(f"latest_session_handoff: {latest_session['handoff']}")
    else:
        print(f"latest_session: none ({_work_root(target)})")

    dogfood = payload["dogfood"]
    print(f"dogfood_ready: {dogfood.get('ready')}")
    if dogfood.get("error"):
        print(f"dogfood_error: {dogfood['error']}")
    latest_run = dogfood.get("latest_run")
    if isinstance(latest_run, dict):
        print(
            "latest_run: "
            f"{latest_run.get('started_at', '')} "
            f"[{latest_run.get('status', 'unknown')}] {latest_run.get('path')}"
        )
        if latest_run.get("task"):
            print(f"latest_task: {_short(str(latest_run['task']))}")
    else:
        print("latest_run: none")

    print(f"next_source: {payload['next_source']}")
    if payload.get("task_id"):
        print(f"task_id: {payload['task_id']}")
    print(f"next: {_short(str(payload['next']))}")
    print(f"suggested_command: {payload['suggested_command']}")

    pending = payload["pending_tasks"]
    if isinstance(pending, list) and pending:
        print("pending_tasks:")
        for task in pending[:5]:
            if not isinstance(task, dict):
                continue
            print(f"  - {task.get('id')} {_short(str(task.get('text', '')))}")
        if len(pending) > 5:
            print(f"  ... {len(pending) - 5} more")

    pending_imports = payload["pending_imports"]
    if isinstance(pending_imports, list) and pending_imports:
        counts = payload.get("pending_import_counts")
        if isinstance(counts, dict):
            print(f"pending_import_count: {counts.get('total', len(pending_imports))}")
            by_source = counts.get("by_source") if isinstance(counts.get("by_source"), dict) else {}
            if by_source:
                print("pending_imports_by_source:")
                for source, count in by_source.items():
                    print(f"  {source}: {count}")
            by_kind = counts.get("by_kind") if isinstance(counts.get("by_kind"), dict) else {}
            if by_kind:
                print("pending_imports_by_kind:")
                for kind, count in by_kind.items():
                    print(f"  {kind}: {count}")
        print("pending_imports:")
        for item in pending_imports[:5]:
            if not isinstance(item, dict):
                continue
            source = item.get("source") or "unknown"
            kind = item.get("kind") or "task"
            print(f"  - {item.get('id')} [{kind}] {source}: {_short(str(item.get('text', '')))}")
        if len(pending_imports) > 5:
            print(f"  ... {len(pending_imports) - 5} more")

    recent = payload["recent_sessions"]
    if isinstance(recent, list) and recent:
        print("recent_sessions:")
        for item in recent:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("id")
            print(f"  - {item.get('started_at')} [{item.get('status')}] {_short(str(title))}")
    if payload.get("skipped_sessions"):
        print(f"skipped_sessions: {payload['skipped_sessions']}", file=sys.stderr)
    return 0


def tasks(*, target: Path, all_tasks: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    ledger = _read_task_ledger(target)
    task_items = [task for task in ledger["tasks"] if isinstance(task, dict)]
    task_items.sort(key=_task_sort_key)
    if not all_tasks:
        task_items = [task for task in task_items if task.get("status", "pending") == "pending"]

    if json_output:
        print(json.dumps({"tasks_path": str(_tasks_path(target)), "tasks": task_items}, indent=2, sort_keys=True))
        return 0

    print(f"work tasks: {target}")
    print(f"tasks_path: {_tasks_path(target)}")
    if not task_items:
        print("tasks: none")
        return 0
    for task in task_items:
        status_text = task.get("status", "pending")
        print(f"- {task.get('id')} [{status_text}] {_short(str(task.get('text', '')))}")
        if task.get("source"):
            print(f"  source: {task['source']}")
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        if metadata.get("run_path"):
            print(f"  run: {metadata['run_path']}")
        if metadata.get("session_path"):
            print(f"  session: {metadata['session_path']}")
        if task.get("completed_at"):
            print(f"  completed_at: {task['completed_at']}")
    return 0


def task_add(*, target: Path, text: str | None = None, from_next: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if from_next and text:
        print("error: pass task text or --from-next, not both", file=sys.stderr)
        return 2
    task_text = (text or "").strip()
    source = "manual"
    if from_next:
        next_step, metadata = _latest_run_next_metadata(target)
        if not next_step:
            print("error: no extracted next step is available", file=sys.stderr)
            return 1
        task_text = next_step
        source = "latest_dogfood_run"
    else:
        metadata = None
    if not task_text:
        print("error: task text is required", file=sys.stderr)
        return 2
    task, created = _add_task(target, task_text, source=source, metadata=metadata)
    print(f"task: {task['id']}")
    print(f"status: {task['status']}")
    print(f"created: {created}")
    print(f"text: {task['text']}")
    return 0


def task_show(*, target: Path, task_id: str) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    task, _ = _find_task(target, task_id)
    if task is None:
        print(f"error: task not found: {task_id}", file=sys.stderr)
        return 1
    print(f"task: {task.get('id')}")
    print(f"status: {task.get('status', 'pending')}")
    print(f"source: {task.get('source', '')}")
    print(f"created_at: {task.get('created_at', '')}")
    print(f"updated_at: {task.get('updated_at', '')}")
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    if metadata:
        print("metadata:")
        for key in sorted(metadata):
            print(f"  {key}: {metadata[key]}")
    if task.get("completed_at"):
        print(f"completed_at: {task['completed_at']}")
    print(f"text: {task.get('text', '')}")
    return 0


def task_done(*, target: Path, task_id: str) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    task, ledger = _find_task(target, task_id)
    if task is None:
        print(f"error: task not found: {task_id}", file=sys.stderr)
        return 1
    now = _now().isoformat()
    task["status"] = "done"
    task["updated_at"] = now
    task["completed_at"] = now
    _write_task_ledger(target, ledger)
    print(f"task: {task.get('id')}")
    print("status: done")
    return 0


def import_add(
    *,
    target: Path,
    text: str,
    kind: str = "task",
    source: str = "manual",
    metadata: list[str] | None = None,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    rendered = text.strip()
    if not rendered:
        print("error: import text is required", file=sys.stderr)
        return 2
    if kind not in IMPORT_KINDS:
        print(f"error: --kind must be one of: {', '.join(IMPORT_KINDS)}", file=sys.stderr)
        return 2
    source_text = source.strip() or "manual"
    try:
        parsed_metadata = _parse_metadata(metadata)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    imports = _read_imports(target)
    item = _make_import(rendered, kind=kind, source=source_text, metadata=parsed_metadata)
    imports.append(item)
    _write_imports(target, imports)
    print(f"import: {item['id']}")
    print(f"status: {item['status']}")
    print(f"kind: {item['kind']}")
    print(f"source: {item['source']}")
    print(f"text: {item['text']}")
    return 0


def import_list(*, target: Path, all_imports: bool = False, json_output: bool = False, limit: int = 20) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    imports = [item for item in _read_imports(target) if isinstance(item, dict)]
    imports.sort(key=_import_sort_key)
    if not all_imports:
        imports = [item for item in imports if item.get("status", "pending") == "pending"]
    imports = imports[:limit]

    if json_output:
        print(json.dumps({"imports_path": str(_imports_path(target)), "imports": imports}, indent=2, sort_keys=True))
        return 0

    print(f"work imports: {target}")
    print(f"imports_path: {_imports_path(target)}")
    if not imports:
        print("imports: none")
        return 0
    for item in imports:
        status_text = item.get("status", "pending")
        kind = item.get("kind", "task")
        source = item.get("source", "manual")
        print(f"- {item.get('id')} [{status_text}] {kind} from {source}: {_short(str(item.get('text', '')))}")
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if metadata:
            rendered = ", ".join(f"{key}={metadata[key]}" for key in sorted(metadata))
            print(f"  metadata: {rendered}")
        if item.get("task_id"):
            print(f"  task: {item['task_id']}")
    return 0


def import_validate(*, input_path: Path, json_output: bool = False) -> int:
    path = input_path.expanduser().resolve()
    if not path.is_file():
        print(f"error: import file not found: {path}", file=sys.stderr)
        return 2
    records, errors = _load_import_jsonl(path)
    payload = {
        "path": str(path),
        "valid": not errors,
        "records": len(records),
        "errors": errors,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if not errors else 1
    print(f"import file: {path}")
    print(f"records: {len(records)}")
    if errors:
        print(f"errors: {len(errors)}")
        for error in errors:
            print(f"- {error}")
        return 1
    print("status: valid")
    return 0


def import_ingest(
    *,
    target: Path,
    input_path: Path,
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = input_path.expanduser().resolve()
    if not path.is_file():
        print(f"error: import file not found: {path}", file=sys.stderr)
        return 2
    records, errors = _load_import_jsonl(path)
    if errors:
        if json_output:
            print(
                json.dumps(
                    {
                        "path": str(path),
                        "imports_path": str(_imports_path(target)),
                        "valid": False,
                        "errors": errors,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(f"error: import file is invalid: {path}", file=sys.stderr)
            for error in errors:
                print(f"- {error}", file=sys.stderr)
        return 2

    imported, skipped = _append_import_records(target, records, dry_run=dry_run)
    payload = {
        "path": str(path),
        "imports_path": str(_imports_path(target)),
        "dry_run": dry_run,
        "imported": len(imported),
        "skipped_duplicates": len(skipped),
        "imports": imported,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"import file: {path}")
    print(f"imports_path: {_imports_path(target)}")
    print(f"dry_run: {dry_run}")
    print(f"imported: {len(imported)}")
    print(f"skipped_duplicates: {len(skipped)}")
    for item in imported:
        print(f"- {item.get('id')} [{item.get('kind')}] {item.get('source')}: {_short(str(item.get('text', '')))}")
    return 0


def import_memory_care(
    *,
    target: Path,
    queue: Path | None = None,
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    queue_path = queue.expanduser().resolve() if queue is not None else target / "memory" / "cards" / "decay" / "refresh-queue.json"
    if not queue_path.is_file():
        print(f"error: memory-care refresh queue not found: {queue_path}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(queue_path.read_text())
    except json.JSONDecodeError as exc:
        print(f"error: invalid memory-care refresh queue JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print(f"error: memory-care refresh queue must be an object: {queue_path}", file=sys.stderr)
        return 2
    cards = payload.get("cards", [])
    if not isinstance(cards, list):
        print(f"error: memory-care refresh queue `cards` must be a list: {queue_path}", file=sys.stderr)
        return 2

    records: list[dict[str, Any]] = []
    for index, card in enumerate(cards, start=1):
        if not isinstance(card, dict):
            print(f"error: memory-care card entry {index} must be an object", file=sys.stderr)
            return 2
        card_file = card.get("file")
        if not isinstance(card_file, str) or not card_file.strip():
            print(f"error: memory-care card entry {index} requires file", file=sys.stderr)
            return 2
        reason = card.get("reason")
        reason_text = reason.strip() if isinstance(reason, str) and reason.strip() else "stale memory card"
        records.append(
            {
                "text": f"Refresh memory card {card_file.strip()}: {reason_text}",
                "kind": "task",
                "source": "memory-care",
                "metadata": {
                    "card_file": card_file.strip(),
                    "reason": reason_text,
                    "queue_path": str(queue_path),
                },
            }
        )
    imported, skipped = _append_import_records(target, records, dry_run=dry_run)
    output = {
        "queue": str(queue_path),
        "imports_path": str(_imports_path(target)),
        "dry_run": dry_run,
        "queued_cards": len(cards),
        "imported": len(imported),
        "skipped_duplicates": len(skipped),
        "imports": imported,
    }
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    print(f"memory-care queue: {queue_path}")
    print(f"imports_path: {_imports_path(target)}")
    print(f"dry_run: {dry_run}")
    print(f"queued_cards: {len(cards)}")
    print(f"imported: {len(imported)}")
    print(f"skipped_duplicates: {len(skipped)}")
    for item in imported:
        print(f"- {item.get('id')} {_short(str(item.get('text', '')))}")
    return 0


def import_triage(*, target: Path, json_output: bool = False, limit: int = 50) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    pending = _pending_imports(target)
    counts = _import_counts(pending)
    groups: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for item in pending:
        source = str(item.get("source") or "manual")
        kind = str(item.get("kind") or "task")
        groups.setdefault(source, {}).setdefault(kind, []).append(item)

    if json_output:
        print(
            json.dumps(
                {
                    "imports_path": str(_imports_path(target)),
                    "counts": counts,
                    "groups": groups,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    print(f"work import triage: {target}")
    print(f"imports_path: {_imports_path(target)}")
    print(f"pending_imports: {counts['total']}")
    if not pending:
        return 0
    print("sources:")
    for source, by_kind in sorted(groups.items()):
        source_count = sum(len(items) for items in by_kind.values())
        print(f"- {source}: {source_count}")
        for kind, items in sorted(by_kind.items()):
            print(f"  {kind}: {len(items)}")
            for item in items[:limit]:
                print(f"    - {item.get('id')} {_short(str(item.get('text', '')))}")
            if len(items) > limit:
                print(f"    ... {len(items) - limit} more")
    return 0


def import_show(*, target: Path, import_id: str) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    item, _ = _find_import(target, import_id)
    if item is None:
        print(f"error: import not found: {import_id}", file=sys.stderr)
        return 1
    print(f"import: {item.get('id')}")
    print(f"status: {item.get('status', 'pending')}")
    print(f"kind: {item.get('kind', '')}")
    print(f"source: {item.get('source', '')}")
    print(f"created_at: {item.get('created_at', '')}")
    print(f"updated_at: {item.get('updated_at', '')}")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    if metadata:
        print("metadata:")
        for key in sorted(metadata):
            print(f"  {key}: {metadata[key]}")
    if item.get("promoted_at"):
        print(f"promoted_at: {item['promoted_at']}")
    if item.get("task_id"):
        print(f"task: {item['task_id']}")
    print(f"text: {item.get('text', '')}")
    return 0


def import_promote(
    *,
    target: Path,
    import_id: str | None = None,
    all_matching: bool = False,
    kind: str | None = None,
    source: str | None = None,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if kind is not None and kind not in IMPORT_KINDS:
        print(f"error: --kind must be one of: {', '.join(IMPORT_KINDS)}", file=sys.stderr)
        return 2
    if all_matching and import_id:
        print("error: pass an import id or --all, not both", file=sys.stderr)
        return 2
    if all_matching:
        imports = _read_imports(target)
        wanted_ids = {item.get("id") for item in _matching_pending_imports(target, kind=kind, source=source)}
        promoted: list[tuple[dict[str, Any], dict[str, Any], bool]] = []
        for item in imports:
            if item.get("id") not in wanted_ids:
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            task, created = _mark_import_promoted(target, item)
            promoted.append((item, task, created))
        _write_imports(target, imports)
        created_count = len([item for item in promoted if item[2]])
        print(f"promoted: {len(promoted)}")
        print(f"created: {created_count}")
        print(f"existing: {len(promoted) - created_count}")
        for item, task, created in promoted:
            status = "created" if created else "existing"
            print(f"- {item.get('id')} -> {task['id']} [{status}] {_short(str(task.get('text', '')))}")
        return 0
    if not import_id:
        print("error: import id is required unless --all is passed", file=sys.stderr)
        return 2
    item, imports = _find_import(target, import_id)
    if item is None:
        print(f"error: import not found: {import_id}", file=sys.stderr)
        return 1
    text = str(item.get("text") or "").strip()
    if not text:
        print(f"error: import has no text: {import_id}", file=sys.stderr)
        return 2
    task, created = _mark_import_promoted(target, item)
    _write_imports(target, imports)
    print(f"import: {item.get('id')}")
    print(f"status: {item.get('status')}")
    print(f"task: {task['id']}")
    print(f"created: {created}")
    print(f"text: {task['text']}")
    return 0


def import_dismiss(*, target: Path, import_id: str, reason: str | None = None) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    item, imports = _find_import(target, import_id)
    if item is None:
        print(f"error: import not found: {import_id}", file=sys.stderr)
        return 1
    now = _now().isoformat()
    item["status"] = "dismissed"
    item["updated_at"] = now
    item["dismissed_at"] = now
    if reason and reason.strip():
        item["dismiss_reason"] = reason.strip()
    _write_imports(target, imports)
    print(f"import: {item.get('id')}")
    print("status: dismissed")
    if item.get("dismiss_reason"):
        print(f"reason: {item['dismiss_reason']}")
    return 0


def next(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    if json_output:
        print(json.dumps(_next_payload(target), indent=2, sort_keys=True))
        return 0

    print(f"work next: {target}")
    payload = _next_payload(target)
    active = payload["active_session"]
    if isinstance(active, dict):
        if not active.get("valid"):
            print(f"active_session: invalid ({active.get('path')})")
        else:
            print(f"active_session: {active.get('path')}")
            print(f"active_session_status: {active.get('status')}")
            if active.get("title"):
                print(f"active_session_title: {_short(str(active['title']))}")
    else:
        print("active_session: none")

    dogfood = payload["dogfood"]
    print(f"dogfood_ready: {dogfood.get('ready')}")
    if dogfood.get("error"):
        print(f"dogfood_error: {dogfood['error']}")
    latest_run = dogfood.get("latest_run")
    if isinstance(latest_run, dict):
        print(
            "latest_run: "
            f"{latest_run.get('started_at', '')} "
            f"[{latest_run.get('status', 'unknown')}] {latest_run.get('path')}"
        )
        if latest_run.get("task"):
            print(f"latest_task: {_short(str(latest_run['task']))}")
    else:
        print("latest_run: none")

    task = str(payload["next"])
    print(f"next_source: {payload['next_source']}")
    if payload.get("task_id"):
        print(f"task_id: {payload['task_id']}")
    print(f"next: {_short(task)}")
    print(f"suggested_command: {payload['suggested_command']}")
    return 0


def bootstrap(
    *,
    target: Path,
    artifacts_dir: Path | None = None,
    handoff_inbox: Path | None = None,
    force: bool = False,
    handoff: bool = True,
    inspect: bool = True,
    native_read_only_sandbox: bool = False,
    timeout_seconds: float = dogfood_cmd.DEFAULT_TIMEOUT_SECONDS,
    update_gitignore: bool = True,
) -> int:
    if timeout_seconds <= 0:
        print("error: --timeout-seconds must be positive", file=sys.stderr)
        return 2

    target = target.expanduser().resolve()
    print(f"work bootstrap: {target}")
    if not target.is_dir():
        _print_bootstrap_line(FAIL, "target", f"not a directory: {target}")
        return 2
    _print_bootstrap_line(OK, "target", target)

    failures = 0
    repo_root = _git_value(target, "rev-parse", "--show-toplevel")
    if repo_root is None:
        failures += 1
        _print_bootstrap_line(FAIL, "git", "not a git repository")
    else:
        _print_bootstrap_line(OK, "git", repo_root)

    config = dogfood_cmd.config_path(target)
    if config.exists() and not force:
        _print_bootstrap_line(OK, "dogfood_config", f"exists at {config}")
    else:
        rc = dogfood_cmd.init(
            target=target,
            artifacts_dir=artifacts_dir,
            handoff_inbox=handoff_inbox,
            force=force,
            handoff=handoff,
            inspect=inspect,
            native_read_only_sandbox=native_read_only_sandbox,
            timeout_seconds=timeout_seconds,
        )
        if rc != 0:
            failures += 1
            _print_bootstrap_line(FAIL, "dogfood_config", f"init failed with exit code {rc}")
        else:
            _print_bootstrap_line(OK, "dogfood_config", config)

    try:
        effective_target, effective_artifacts_dir, cfg = dogfood_cmd._load_effective_paths(target)
    except (FileNotFoundError, ValueError) as exc:
        failures += 1
        effective_target = target
        effective_artifacts_dir = artifacts_dir or (target / ".brigade" / "runs")
        cfg = None
        _print_bootstrap_line(FAIL, "dogfood_paths", exc)
    else:
        _print_bootstrap_line(OK, "dogfood_target", effective_target)
        _print_bootstrap_line(OK, "dogfood_artifacts", effective_artifacts_dir)

    work_root = _work_root(effective_target)
    effective_artifacts_dir.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)
    _print_bootstrap_line(OK, "artifacts_dir", effective_artifacts_dir)
    _print_bootstrap_line(OK, "work_root", work_root)

    effective_handoff = cfg.handoff if cfg is not None else handoff
    effective_handoff_inbox = (
        cfg.handoff_inbox
        if cfg is not None and cfg.handoff_inbox is not None
        else handoff_inbox.expanduser()
        if handoff_inbox is not None
        else dogfood_cmd.default_handoff_inbox(effective_target)
    )
    if effective_handoff:
        effective_handoff_inbox.mkdir(parents=True, exist_ok=True)
        _print_bootstrap_line(OK, "handoff_inbox", effective_handoff_inbox)
    else:
        _print_bootstrap_line(WARN, "handoff_inbox", "handoff disabled")

    if update_gitignore:
        result = apply_gitignore(effective_target, _work_selection(effective_target, effective_handoff_inbox))
        _print_bootstrap_line(OK, "gitignore", result)
    else:
        _print_bootstrap_line(WARN, "gitignore", "skipped")

    codex_path = shutil.which("codex")
    if codex_path is None:
        failures += 1
        _print_bootstrap_line(FAIL, "codex", "missing on PATH")
    else:
        _print_bootstrap_line(OK, "codex", codex_path)

    config_ignored = dogfood_cmd._check_git_ignored(effective_target, config)
    artifacts_ignored = dogfood_cmd._check_git_ignored(effective_target, effective_artifacts_dir)
    work_ignored = dogfood_cmd._check_git_ignored(effective_target, work_root)
    handoff_ignored = (
        dogfood_cmd._check_git_ignored(effective_target, effective_handoff_inbox)
        if effective_handoff
        else "disabled"
    )
    ignore_values = {
        "config_ignored": config_ignored,
        "artifacts_ignored": artifacts_ignored,
        "work_ignored": work_ignored,
        "handoff_ignored": handoff_ignored,
    }
    for name, value in ignore_values.items():
        level = OK if value in {"yes", "outside-target", "disabled"} else WARN
        _print_bootstrap_line(level, name, value)

    ready = failures == 0
    _print_bootstrap_line(OK if ready else FAIL, "ready", "daily work loop is usable" if ready else f"{failures} blocker{'s' if failures != 1 else ''}")
    print("next_command: brigade work run")
    return 0 if ready else 1


def run(
    task: str | None,
    *,
    target: Path,
    title: str | None = None,
    output_dir: Path | None = None,
    handoff: bool = True,
    handoff_inbox: Path | None = None,
    dogfood_handoff: bool = False,
    inspect: bool = True,
    native_read_only_sandbox: bool = False,
    timeout_seconds: float = dogfood_cmd.DEFAULT_TIMEOUT_SECONDS,
    recap_limit: int = 1,
    queue_next: bool = False,
) -> int:
    if recap_limit < 1:
        print("error: --recap-limit must be a positive integer", file=sys.stderr)
        return 2

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    resolved = _resolve_next_task(target)
    task_text = task or str(resolved["task"])
    consumed_task_id = resolved.get("task_id") if task is None and resolved.get("source") == "task_ledger" else None
    session_title = title or task_text
    start_rc = start(target=target, title=session_title)
    if start_rc != 0:
        return start_rc
    session_dir = _active_session_dir(target)

    dogfood_rc = 1
    try:
        dogfood_rc = dogfood_cmd.run(
            task_text,
            target=target,
            output_dir=output_dir,
            handoff=dogfood_handoff,
            handoff_inbox=handoff_inbox if dogfood_handoff else None,
            inspect=inspect,
            native_read_only_sandbox=native_read_only_sandbox,
            timeout_seconds=timeout_seconds,
        )
    finally:
        note = f"brigade work run completed with dogfood exit code {dogfood_rc}"
        end_rc = end(target=target, note=note, handoff=handoff, handoff_inbox=handoff_inbox)

    if end_rc != 0:
        return end_rc if dogfood_rc == 0 else dogfood_rc
    if dogfood_rc == 0 and isinstance(consumed_task_id, str):
        task, ledger = _find_task(target, consumed_task_id)
        if task is not None:
            now = _now().isoformat()
            task["status"] = "done"
            task["updated_at"] = now
            task["completed_at"] = now
            task["completed_session_title"] = session_title
            _write_task_ledger(target, ledger)
    if dogfood_rc == 0 and queue_next:
        queued_task, created, reason = _queue_latest_next(
            target,
            session_dir=session_dir,
            session_title=session_title,
        )
        if queued_task is None:
            print(f"queued_next: skipped ({reason})")
        else:
            print(f"queued_next: {queued_task.get('id')} ({'created' if created else 'existing'})")
    recap(target=target, limit=recap_limit)
    return dogfood_rc


def status(*, target: Path, limit: int = 12) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    print(f"work: {target}")
    repo_root = _git_value(target, "rev-parse", "--show-toplevel")
    if repo_root is None:
        print("git: unavailable")
    else:
        print(f"repo: {repo_root}")
        branch = _git_value(target, "branch", "--show-current")
        if branch is None:
            branch = _git_value(target, "rev-parse", "--short", "HEAD") or "unknown"
            branch = f"detached:{branch}"
        print(f"branch: {branch}")
        status_out = _git_value(target, "status", "--short") or ""
        _print_dirty(status_out.splitlines(), limit=limit)

    try:
        effective_target, artifacts_dir, cfg = dogfood_cmd._load_effective_paths(target)
    except (FileNotFoundError, ValueError) as exc:
        print(f"dogfood: not ready ({exc})")
        return 0

    config = dogfood_cmd.config_path(target)
    codex_path = shutil.which("codex")
    dogfood_ready = config.exists() and codex_path is not None and effective_target.is_dir()
    print(f"dogfood: {'ready' if dogfood_ready else 'not ready'}")
    print(f"dogfood_config: {config if config.exists() else str(config) + ' (missing)'}")
    print(f"dogfood_target: {effective_target}")
    print(f"dogfood_artifacts: {artifacts_dir}")
    print(f"codex: {codex_path or 'missing'}")
    if cfg and cfg.handoff:
        handoff_inbox = cfg.handoff_inbox or dogfood_cmd.default_handoff_inbox(effective_target)
        print(f"handoff_inbox: {handoff_inbox}")

    latest = dogfood_cmd._latest_run(artifacts_dir)
    if latest is None:
        print("latest_run: none")
        print("next: none")
        return 0

    latest_path, latest_meta = latest
    print(
        "latest_run: "
        f"{latest_meta.get('started_at', latest_path.name)} "
        f"[{latest_meta.get('status', 'unknown')}] {latest_path}"
    )
    task = _short(str(latest_meta.get("task") or ""))
    if task:
        print(f"latest_task: {task}")
    next_step = dogfood_cmd.extract_next_step(dogfood_cmd._read_final(latest_path))
    print(f"next: {_short(next_step) if next_step else 'none'}")
    print("next_command: brigade dogfood next")
    print("inspect_command: brigade dogfood latest")
    return 0


def doctor(*, target: Path) -> int:
    from . import security_cmd

    target = target.expanduser().resolve()
    failures = 0

    print(f"work doctor: {target}")
    if not target.is_dir():
        _doctor_line(FAIL, "target", f"not a directory: {target}")
        return 2
    _doctor_line(OK, "target", target)

    repo_root = _git_value(target, "rev-parse", "--show-toplevel")
    if repo_root is None:
        failures += 1
        _doctor_line(FAIL, "git", "not a git repository")
    else:
        _doctor_line(OK, "git", repo_root)

    config = dogfood_cmd.config_path(target)
    try:
        effective_target, artifacts_dir, cfg = dogfood_cmd._load_effective_paths(target)
    except (FileNotFoundError, ValueError) as exc:
        failures += 1
        _doctor_line(FAIL, "dogfood_config", exc)
        effective_target = target
        artifacts_dir = target / ".brigade" / "runs"
        cfg = None
    else:
        if config.is_file():
            _doctor_line(OK, "dogfood_config", config)
        else:
            failures += 1
            _doctor_line(FAIL, "dogfood_config", f"missing, run `brigade dogfood init --target {target}`")
        _doctor_line(OK, "dogfood_target", effective_target)
        _doctor_line(OK, "dogfood_artifacts", artifacts_dir)

    security_config = security_cmd.config_path(effective_target)
    security_config_valid = True
    if security_config.is_file():
        try:
            loaded_security = security_cmd.load_config(effective_target)
        except ValueError as exc:
            security_config_valid = False
            failures += 1
            _doctor_line(FAIL, "security_config", f"invalid {security_config}: {exc}")
        else:
            policy = loaded_security.policy if loaded_security is not None else "personal"
            _doctor_line(OK, "security_config", f"{security_config} (policy={policy})")
    else:
        _doctor_line(WARN, "security_config", f"missing, run `brigade security init --target {effective_target}`")

    if security_config_valid:
        try:
            suppression_health = security_cmd.suppression_health(effective_target)
        except ValueError as exc:
            failures += 1
            _doctor_line(FAIL, "security_suppressions", f"invalid: {exc}")
        else:
            stale = suppression_health["stale"]
            missing_reasons = suppression_health["missing_reasons"]
            if stale:
                _doctor_line(WARN, "security_stale_suppressions", f"{len(stale)} no longer match current findings: {', '.join(stale[:5])}")
            if missing_reasons:
                _doctor_line(WARN, "security_suppression_reasons", f"{len(missing_reasons)} missing reason: {', '.join(missing_reasons[:5])}")
            if not stale and not missing_reasons:
                _doctor_line(OK, "security_suppressions", f"{suppression_health['suppression_count']} configured")

    security_artifacts = security_cmd.default_artifacts_dir(effective_target)
    security_bundle = security_cmd.inspect_evidence_bundle(security_artifacts)
    if security_bundle.get("ready"):
        _doctor_line(
            OK,
            "security_evidence",
            f"{security_artifacts} "
            f"(generated_at={security_bundle.get('generated_at')}, findings={security_bundle.get('finding_count')})",
        )
    else:
        _doctor_line(
            WARN,
            "security_evidence",
            f"{security_bundle.get('reason')}; run `brigade security scan --target {effective_target} --output-dir {security_artifacts}`",
        )

    codex_path = shutil.which("codex")
    if codex_path is None:
        failures += 1
        _doctor_line(FAIL, "codex", "missing on PATH")
    else:
        _doctor_line(OK, "codex", codex_path)

    work_root = _work_root(effective_target)
    _doctor_line(OK if work_root.parent.exists() else WARN, "work_root", work_root)
    current = _current_path(effective_target)
    if current.exists():
        active_dir = work_root / current.read_text().strip()
        if _read_session(active_dir) is None:
            failures += 1
            _doctor_line(FAIL, "active_session", f"invalid: {active_dir}")
        else:
            _doctor_line(WARN, "active_session", f"active: {active_dir}")
    else:
        _doctor_line(OK, "active_session", "none")

    handoff_inbox = (
        cfg.handoff_inbox
        if cfg and cfg.handoff_inbox is not None
        else dogfood_cmd.default_handoff_inbox(effective_target)
    )
    _doctor_line(OK if handoff_inbox.parent.exists() else WARN, "handoff_inbox", handoff_inbox)

    config_ignored = dogfood_cmd._check_git_ignored(effective_target, config)
    _doctor_line(_doctor_ignore_level(config_ignored), "config_ignored", config_ignored)
    artifacts_ignored = dogfood_cmd._check_git_ignored(effective_target, artifacts_dir)
    _doctor_line(_doctor_ignore_level(artifacts_ignored), "artifacts_ignored", artifacts_ignored)
    security_ignored = dogfood_cmd._check_git_ignored(effective_target, security_artifacts)
    _doctor_line(_doctor_ignore_level(security_ignored), "security_ignored", security_ignored)
    work_ignored = dogfood_cmd._check_git_ignored(effective_target, work_root)
    _doctor_line(_doctor_ignore_level(work_ignored), "work_ignored", work_ignored)
    handoff_ignored = dogfood_cmd._check_git_ignored(effective_target, handoff_inbox)
    _doctor_line(_doctor_ignore_level(handoff_ignored), "handoff_ignored", handoff_ignored)

    latest = dogfood_cmd._latest_run(artifacts_dir)
    if latest is None:
        _doctor_line(WARN, "latest_run", "none")
    else:
        latest_path, latest_meta = latest
        _doctor_line(OK, "latest_run", f"{latest_meta.get('started_at', latest_path.name)} {latest_path}")
        next_step = dogfood_cmd.extract_next_step(dogfood_cmd._read_final(latest_path))
        _doctor_line(OK if next_step else WARN, "latest_next", _short(next_step) if next_step else "none")

    if failures:
        _doctor_line(FAIL, "ready", f"{failures} blocker{'s' if failures != 1 else ''}")
        return 1
    _doctor_line(OK, "ready", "daily work loop is usable")
    return 0
