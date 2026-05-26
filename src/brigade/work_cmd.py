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
    snapshot["latest_run"] = {
        "path": str(latest_path),
        "started_at": latest_meta.get("started_at"),
        "status": latest_meta.get("status"),
        "task": latest_meta.get("task"),
    }
    snapshot["next"] = dogfood_cmd.extract_next_step(dogfood_cmd._read_final(latest_path))
    return snapshot


def _session_snapshot(target: Path) -> dict[str, Any]:
    return {
        "git": _git_snapshot(target),
        "dogfood": _dogfood_snapshot(target),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


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
) -> int:
    if recap_limit < 1:
        print("error: --recap-limit must be a positive integer", file=sys.stderr)
        return 2

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    task_text = task or dogfood_cmd.DEFAULT_TASK
    session_title = title or task_text
    start_rc = start(target=target, title=session_title)
    if start_rc != 0:
        return start_rc

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
