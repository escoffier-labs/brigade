"""Session lifecycle, run/status/doctor/brief, and task operations."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import json
import re
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from ... import dogfood_cmd, localio
from ...install import apply_gitignore
from .. import constants, helpers, ledger as ledger_mod, config as config_mod, services as services_mod
from .. import scanners as scanners_mod, reviews as reviews_mod


def _latest_run_next_metadata(target: Path) -> tuple[str | None, dict[str, Any]]:
    dogfood = helpers._dogfood_snapshot(target)
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
    task, created = ledger_mod._add_task(
        target,
        next_step,
        source="latest_dogfood_run",
        metadata=metadata,
    )
    return task, created, None


def _latest_completed_run_path(target: Path, output_dir: Path | None) -> str | None:
    if output_dir is not None:
        candidate = output_dir.expanduser()
        if (candidate / "run.json").is_file():
            return str(candidate)
    dogfood = helpers._dogfood_snapshot(target)
    latest = dogfood.get("latest_run") if isinstance(dogfood.get("latest_run"), dict) else None
    path = latest.get("path") if isinstance(latest, dict) else None
    return path if isinstance(path, str) and path else None


def _resolve_next_task(target: Path) -> dict[str, Any]:
    pending = ledger_mod._pending_tasks(target)
    if pending:
        task = pending[0]
        return {
            "task": str(task.get("text", "")).strip(),
            "source": "task_ledger",
            "task_id": task.get("id"),
            "ledger_task": task,
            "dogfood": helpers._dogfood_snapshot(target),
        }
    dogfood = helpers._dogfood_snapshot(target)
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


def _render_task_run_prompt(task: dict[str, Any]) -> str:
    text = str(task.get("text") or "").strip()
    lines = [text]
    acceptance = ledger_mod._task_acceptance(task)
    if acceptance:
        lines.extend(["", "Acceptance criteria:"])
        lines.extend(f"- {item}" for item in acceptance)
    lines.extend(
        [
            "",
            "Task metadata:",
            f"- type: {ledger_mod._normalize_task_type(task.get('type'))}",
            f"- priority: {ledger_mod._normalize_task_priority(task.get('priority'))}",
            "",
            "Definition of done:",
            "- Treat the acceptance criteria above as the completion checklist.",
            "- Report the verification command you ran, or explain the blocker.",
        ]
    )
    return "\n".join(lines).strip()


def _task_plan_payload(target: Path, task_id: str) -> tuple[dict[str, Any] | None, int]:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return None, 2
    task, _ = ledger_mod._find_task(target, task_id)
    if task is None:
        print(f"error: task not found: {task_id}", file=sys.stderr)
        return None, 1
    summary = ledger_mod._task_summary(task)
    template = summary.get("template") if isinstance(summary.get("template"), str) else None
    if template:
        summary["guidance"] = list(constants.TASK_TEMPLATES.get(template, {}).get("guidance", ()))
    summary["suggested_command"] = "brigade work run"
    summary["tasks_path"] = str(helpers._tasks_path(target))
    return summary, 0


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
            print(f"latest_note: {helpers._short(str(notes[-1]['text']))}")
    if payload.get("handoff"):
        print(f"handoff: {payload['handoff']}")
    task = payload.get("task")
    if isinstance(task, dict):
        print("task:")
        print(f"  id: {task.get('id', '')}")
        print(f"  source: {task.get('source', '')}")
        print(f"  type: {task.get('type', '')}")
        print(f"  priority: {task.get('priority', '')}")
        if task.get("template"):
            print(f"  template: {task['template']}")
        acceptance = task.get("acceptance") if isinstance(task.get("acceptance"), list) else []
        print(f"  acceptance: {len(acceptance)}")
        issue = task.get("issue") if isinstance(task.get("issue"), dict) else None
        if issue:
            print(f"  issue: {issue.get('url') or issue.get('number')}")

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
                print(f"  latest_task: {helpers._short(str(latest['task']))}")
        if dogfood.get("next"):
            print(f"  next: {helpers._short(str(dogfood['next']))}")


def _session_task_markdown(task: object) -> list[str]:
    if not isinstance(task, dict):
        return []
    lines = ["", "## Task", ""]
    lines.append(f"- Task: `{task.get('id', '')}`")
    if task.get("text"):
        lines.append(f"- Text: {task['text']}")
    lines.append(f"- Source: {task.get('source', '')}")
    lines.append(f"- Type: {task.get('type', '')}")
    lines.append(f"- Priority: {task.get('priority', '')}")
    if task.get("template"):
        lines.append(f"- Template: {task['template']}")
    issue = task.get("issue") if isinstance(task.get("issue"), dict) else None
    if issue:
        lines.append(f"- Issue: {issue.get('url') or issue.get('number')}")
        if issue.get("title"):
            lines.append(f"- Issue title: {issue['title']}")
        if issue.get("state"):
            lines.append(f"- Issue state: {issue['state']}")
    acceptance = task.get("acceptance") if isinstance(task.get("acceptance"), list) else []
    lines.extend(["", "### Acceptance Criteria", ""])
    if acceptance:
        lines.extend(f"- {item}" for item in acceptance)
    else:
        lines.append("- none")
    return lines


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
    lines.extend(_session_task_markdown(payload.get("task")))
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


def _write_work_handoff(target: Path, session_dir: Path, payload: dict[str, Any], inbox: Path) -> Path:
    ended = payload.get("ended_at") or helpers._now().isoformat()
    ended_slug = re.sub(r"[^0-9]", "", str(ended))[:12] or helpers._now().strftime("%Y%m%d%H%M")
    title = payload.get("title") or payload.get("id") or "work-session"
    path = inbox / f"{ended_slug}-brigade-work-{helpers._slug(str(title))}-{uuid4().hex[:6]}.md"
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
    document_content = f"""### Brigade work session: {payload.get("id")}
- target: `{target}`
- session artifacts: `{session_dir}`
- branch: {git.get("branch") if isinstance(git, dict) else "unknown"}
- dirty files: {len(dirty) if isinstance(dirty, list) else "unknown"}
{latest_line}
{next_line}
"""
    if note:
        document_content += f"- note: {note}\n"
    body = f"""# Memory Handoff

## Type

workflow

## Title

Brigade work session ended: {helpers._slug(str(title))}

## Summary

A Brigade work session was ended and local session artifacts were written. This handoff captures the session path, final git state, latest dogfood run, and extracted next step so the memory owner can route durable workflow context.

## Durable facts

- session: `{payload.get("id")}`
- target: `{target}`
- session artifacts: `{session_dir}`
- status: {payload.get("status")}
- started: {payload.get("started_at")}
- ended: {payload.get("ended_at")}
- note: {note or "none"}
- branch: {git.get("branch") if isinstance(git, dict) else "unknown"}
- dirty files:
{dirty_lines}
{latest_line}
{next_line}

## Evidence

- session.json: `{session_dir / "session.json"}`
- start summary: `{session_dir / "start.md"}`
- end summary: `{session_dir / "end.md"}`

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


def start(
    *,
    target: Path,
    title: str | None = None,
    force: bool = False,
    task_snapshot: dict[str, Any] | None = None,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    root = helpers._work_root(target)
    current = helpers._current_path(target)
    if current.exists() and not force:
        print(f"error: work session already active: {current.read_text().strip()}", file=sys.stderr)
        return 2

    started = helpers._now()
    session_id = f"{started.strftime('%Y%m%d-%H%M%S')}-{helpers._slug(title or 'work-session')}"
    session_dir = root / session_id
    session_dir.mkdir(parents=True, exist_ok=False)
    payload: dict[str, Any] = {
        "id": session_id,
        "title": title,
        "target": str(target),
        "status": "active",
        "started_at": started.isoformat(),
        "start": helpers._session_snapshot(target),
    }
    if task_snapshot is not None:
        payload["task"] = task_snapshot
    helpers._write_json(session_dir / "session.json", payload)
    _write_session_markdown(session_dir / "start.md", title="Brigade Work Session Start", payload=payload, key="start")
    current.write_text(session_id + "\n")
    print(f"session: {session_dir}")
    print("status: active")
    return 0


def end(*, target: Path, note: str | None = None, handoff: bool = False, handoff_inbox: Path | None = None) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    current = helpers._current_path(target)
    if not current.exists():
        print(f"error: no active work session in {helpers._work_root(target)}", file=sys.stderr)
        return 1
    session_id = current.read_text().strip()
    session_dir = helpers._work_root(target) / session_id
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
    payload["ended_at"] = helpers._now().isoformat()
    payload["note"] = note
    payload["end"] = helpers._session_snapshot(target)
    helpers._write_json(session_json, payload)
    _write_session_markdown(session_dir / "end.md", title="Brigade Work Session End", payload=payload, key="end")
    if handoff:
        inbox = helpers._handoff_inbox(target, payload, handoff_inbox)
        handoff_path = _write_work_handoff(target, session_dir, payload, inbox)
        payload["handoff"] = str(handoff_path)
        helpers._write_json(session_json, payload)
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

    current = helpers._current_path(target)
    if not current.exists():
        print(f"error: no active work session in {helpers._work_root(target)}", file=sys.stderr)
        return 1
    session_id = current.read_text().strip()
    session_dir = helpers._work_root(target) / session_id
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
        "created_at": helpers._now().isoformat(),
        "text": rendered,
    }
    notes = payload.setdefault("notes", [])
    if not isinstance(notes, list):
        print("error: invalid active work session: notes must be a list", file=sys.stderr)
        return 2
    notes.append(entry)
    helpers._write_json(session_json, payload)

    notes_path = session_dir / "notes.md"
    prefix = "" if notes_path.exists() and notes_path.read_text().endswith("\n") else "\n"
    with notes_path.open("a") as handle:
        if notes_path.stat().st_size == 0:
            handle.write("# Brigade Work Session Notes\n")
        else:
            handle.write(prefix)
        handle.write(f"\n## {entry['created_at']}\n\n{rendered}\n")
    print(f"session: {session_dir}")
    print(f"note: {helpers._short(rendered)}")
    return 0


def list_sessions(*, target: Path, limit: int = 10) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    root = helpers._work_root(target)
    sessions, skipped = helpers._collect_sessions(root)
    for path, payload in sessions[:limit]:
        snapshot = payload.get("end") if isinstance(payload.get("end"), dict) else payload.get("start", {})
        dirty = helpers._dirty_count(snapshot) if isinstance(snapshot, dict) else 0
        title = helpers._short(str(payload.get("title") or ""))
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
    root = helpers._work_root(target)
    sessions, skipped = helpers._collect_sessions(root)
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
    path = helpers._resolve_session(target, session)
    if not path.is_dir():
        print(f"error: work session not found: {path}", file=sys.stderr)
        return 2
    payload = helpers._read_session(path)
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
        since_dt = helpers._parse_since(since)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    root = helpers._work_root(target)
    sessions, skipped = helpers._collect_sessions(root)
    if since_dt is not None:
        sessions = [
            (path, payload)
            for path, payload in sessions
            if (
                helpers._parse_iso_datetime(payload.get("ended_at") or payload.get("started_at"))
                or datetime.min.replace(tzinfo=timezone.utc)
            )
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

    branches = sorted({branch for _, payload in sessions if (branch := helpers._branch(helpers._snapshot(payload)))})
    if branches:
        print(f"branches: {', '.join(branches)}")
    handoffs = [str(payload.get("handoff")) for _, payload in sessions if payload.get("handoff")]
    if handoffs:
        print(f"handoffs: {len(handoffs)}")

    print("items:")
    for path, payload in sessions:
        snapshot = helpers._snapshot(payload)
        title = str(payload.get("title") or payload.get("id") or path.name)
        print(f"- {title}")
        print(f"  id: {payload.get('id', path.name)}")
        print(f"  status: {payload.get('status', 'unknown')}")
        print(f"  started: {payload.get('started_at', '')}")
        if payload.get("ended_at"):
            print(f"  ended: {payload['ended_at']}")
        branch = helpers._branch(snapshot)
        if branch:
            print(f"  branch: {branch}")
        print(f"  dirty_files: {helpers._dirty_count(snapshot)}")
        if payload.get("note"):
            print(f"  note: {helpers._short(str(payload['note']))}")
        if payload.get("handoff"):
            print(f"  handoff: {payload['handoff']}")
        next_text = helpers._next_step(snapshot)
        if next_text:
            print(f"  next: {helpers._short(next_text)}")
    return 0


def _print_resume_session(label: str, path: Path, payload: dict[str, Any]) -> None:
    print(f"{label}: {path}")
    print(f"{label}_status: {payload.get('status', 'unknown')}")
    if payload.get("title"):
        print(f"{label}_title: {helpers._short(str(payload['title']))}")
    print(f"{label}_started: {payload.get('started_at', '')}")
    if payload.get("ended_at"):
        print(f"{label}_ended: {payload['ended_at']}")
    if payload.get("note"):
        print(f"{label}_note: {helpers._short(str(payload['note']))}")
    notes = payload.get("notes")
    if isinstance(notes, list):
        print(f"{label}_notes: {len(notes)}")
        if notes and isinstance(notes[-1], dict) and notes[-1].get("text"):
            print(f"{label}_latest_note: {helpers._short(str(notes[-1]['text']))}")
    if payload.get("handoff"):
        print(f"{label}_handoff: {payload['handoff']}")


def resume(*, target: Path) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    print(f"work resume: {target}")
    root = helpers._work_root(target)
    current = helpers._current_path(target)
    active_payload: dict[str, Any] | None = None
    if current.exists():
        active_dir = root / current.read_text().strip()
        active_payload = helpers._read_session(active_dir)
        if active_payload is None:
            print(f"active_session: invalid ({active_dir})")
        else:
            _print_resume_session("active_session", active_dir, active_payload)
    else:
        print("active_session: none")

    sessions, skipped = helpers._collect_sessions(root)
    if skipped:
        print(f"skipped: {skipped}", file=sys.stderr)
    if sessions:
        latest_path, latest_payload = sessions[0]
        if active_payload is None or latest_payload.get("id") != active_payload.get("id"):
            _print_resume_session("latest_session", latest_path, latest_payload)
    else:
        print(f"latest_session: none ({root})")

    dogfood = helpers._dogfood_snapshot(target)
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
            print(f"latest_task: {helpers._short(str(latest_run['task']))}")
    else:
        print("latest_run: none")

    next_step = dogfood.get("next") if isinstance(dogfood.get("next"), str) else None
    print(f"next: {helpers._short(next_step) if next_step else 'none'}")
    if active_payload is not None:
        print('suggested_command: brigade work end --note "..." --handoff')
    elif next_step:
        print(f"suggested_command: brigade work run {shlex.quote(next_step)}")
    else:
        print("suggested_command: brigade work run")
    return 0
