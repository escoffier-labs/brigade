"""Local context engineering packs."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import work_cmd

OK = "ok"
WARN = "warn"
CONTEXT_KINDS = {"task", "repo", "release", "tool-use"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _context_root(target: Path) -> Path:
    return target / ".brigade" / "context"


def _packs_root(target: Path) -> Path:
    return _context_root(target) / "packs"


def _archive_root(target: Path) -> Path:
    return _context_root(target) / "archive"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _short(text: object, limit: int = 160) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "..."


def _doc_summary(target: Path) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for name in ("README.md", "ROADMAP.md", "CHANGELOG.md"):
        path = target / name
        docs.append({"path": name, "exists": path.is_file(), "summary": _short(path.read_text() if path.is_file() else "", 120)})
    return docs


def _guidance_summary(target: Path) -> dict[str, Any]:
    sources = []
    for name in ("AGENTS.md", "CLAUDE.md", ".claude/CLAUDE.md"):
        path = target / name
        if path.is_file():
            sources.append({"path": name, "exists": True, "summary": _short(path.read_text(), 120)})
    return {
        "has_agents": (target / "AGENTS.md").is_file(),
        "has_claude": (target / "CLAUDE.md").is_file() or (target / ".claude" / "CLAUDE.md").is_file(),
        "sources": sources,
    }


def _latest_json(root: Path, filename: str) -> dict[str, Any] | None:
    if not root.is_dir():
        return None
    candidates = sorted(root.glob(f"*/{filename}"), key=lambda path: path.stat().st_mtime, reverse=True)
    return _read_json(candidates[0]) if candidates else None


def _context_payload(target: Path, *, kind: str = "repo", task_id: str | None = None, tool_id: str | None = None, release_id: str | None = None) -> dict[str, Any]:
    target = target.expanduser().resolve()
    pending_tasks = work_cmd._pending_tasks(target)
    selected_task = None
    if task_id:
        selected_task = next((task for task in work_cmd._read_task_ledger(target).get("tasks", []) if task.get("id") == task_id), None)
    elif pending_tasks:
        selected_task = pending_tasks[0]
    excluded = [
        "raw chat exports",
        "secret-looking values",
        "private infrastructure values",
        "full local logs",
        "private absolute paths",
    ]
    checks: list[dict[str, Any]] = []
    if kind not in CONTEXT_KINDS:
        checks.append({"status": WARN, "name": "context_kind", "detail": f"unsupported kind: {kind}"})
    else:
        checks.append({"status": OK, "name": "context_kind", "detail": kind})
    if kind == "task" and selected_task is None:
        checks.append({"status": WARN, "name": "context_task", "detail": "no matching task"})
    latest_closeout = _latest_json(target / ".brigade" / "work" / "closeouts", "closeout.json")
    latest_security = _latest_json(target / ".brigade" / "security", "security-report.json")
    return {
        "target": str(target),
        "kind": kind,
        "task_id": task_id,
        "tool_id": tool_id,
        "release_id": release_id,
        "docs": _doc_summary(target),
        "guidance": _guidance_summary(target),
        "task": {
            "id": selected_task.get("id") if isinstance(selected_task, dict) else None,
            "text": _short(selected_task.get("text")) if isinstance(selected_task, dict) else None,
            "acceptance": work_cmd._task_acceptance(selected_task) if isinstance(selected_task, dict) else [],
        },
        "recent_work_closeout": latest_closeout,
        "recent_security": {
            "finding_count": latest_security.get("finding_count") if isinstance(latest_security, dict) else None,
            "summary": latest_security.get("summary") if isinstance(latest_security, dict) else None,
        },
        "recent_review_findings": [
            work_cmd._import_summary(item)
            for item in work_cmd._read_imports(target)
            if item.get("source") == "code-review"
        ][:10],
        "selected_tools": [{"tool_id": tool_id}] if tool_id else [],
        "excluded_private_evidence": excluded,
        "source_references": [
            {"path": "README.md", "exists": (target / "README.md").is_file()},
            {"path": "ROADMAP.md", "exists": (target / "ROADMAP.md").is_file()},
            {"path": ".brigade/work/tasks.json", "exists": work_cmd._tasks_path(target).is_file()},
        ],
        "freshness": {"status": "current", "generated_at": _now().isoformat()},
        "sync_plan": {"writes": [], "status": "planned-only"},
        "checks": checks,
        "issues": [check for check in checks if check["status"] != OK],
    }


def plan(*, target: Path, kind: str = "repo", task_id: str | None = None, tool_id: str | None = None, release_id: str | None = None, json_output: bool = False) -> int:
    payload = _context_payload(target, kind=kind, task_id=task_id, tool_id=tool_id, release_id=release_id)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"context plan: {payload['target']}")
    print(f"kind: {payload['kind']}")
    print(f"issues: {len(payload['issues'])}")
    print("writes: 0")
    return 0


def build(*, target: Path, kind: str = "repo", task_id: str | None = None, tool_id: str | None = None, release_id: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = _context_payload(target, kind=kind, task_id=task_id, tool_id=tool_id, release_id=release_id)
    pack_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-context-{kind}-{uuid4().hex[:6]}"
    payload.update({"pack_id": pack_id, "status": "built", "created_at": _now().isoformat()})
    pack_dir = _packs_root(target) / pack_id
    _write_json(pack_dir / "context.json", payload)
    markdown = [
        f"# Context Pack {pack_id}",
        "",
        f"- kind: {kind}",
        f"- task: {payload['task'].get('id') or 'none'}",
        f"- issues: {len(payload['issues'])}",
        "",
        "## Excluded Private Evidence",
        *[f"- {item}" for item in payload["excluded_private_evidence"]],
        "",
    ]
    (pack_dir / "CONTEXT.md").write_text("\n".join(markdown))
    payload["path"] = str(pack_dir)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"context_pack: {pack_id}")
    print(f"path: {pack_dir}")
    print(f"issues: {len(payload['issues'])}")
    return 0


def _packs(target: Path) -> list[dict[str, Any]]:
    root = _packs_root(target)
    packs: list[dict[str, Any]] = []
    if root.is_dir():
        for path in root.iterdir():
            payload = _read_json(path / "context.json") if path.is_dir() else None
            if payload is not None:
                payload.setdefault("path", str(path))
                packs.append(payload)
    packs.sort(key=lambda item: str(item.get("created_at") or item.get("pack_id") or ""), reverse=True)
    return packs


def list_packs(*, target: Path, json_output: bool = False, limit: int = 20) -> int:
    target = target.expanduser().resolve()
    packs = _packs(target)[:limit]
    payload = {"target": str(target), "packs": packs, "pack_count": len(packs)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"context packs: {target}")
    for pack in packs:
        print(f"- {pack.get('pack_id')} [{pack.get('kind')}] {pack.get('status')}")
    return 0


def _find_pack(target: Path, pack_id: str) -> tuple[dict[str, Any] | None, str | None]:
    packs = _packs(target)
    if pack_id == "latest":
        return (packs[0], None) if packs else (None, "context pack not found: latest")
    matches = [pack for pack in packs if str(pack.get("pack_id") or "").startswith(pack_id)]
    if not matches:
        return None, f"context pack not found: {pack_id}"
    if len(matches) > 1:
        return None, f"context pack id is ambiguous: {pack_id}"
    return matches[0], None


def show(*, target: Path, pack_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    pack, error = _find_pack(target, pack_id)
    if pack is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if json_output:
        print(json.dumps({"target": str(target), "pack": pack}, indent=2, sort_keys=True))
        return 0
    print(f"context_pack: {pack.get('pack_id')}")
    print(f"kind: {pack.get('kind')}")
    print(f"status: {pack.get('status')}")
    print(f"issues: {len(pack.get('issues') or [])}")
    return 0


def archive(*, target: Path, pack_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    pack, error = _find_pack(target, pack_id)
    if pack is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    source = Path(str(pack.get("path") or _packs_root(target) / str(pack.get("pack_id"))))
    destination = _archive_root(target) / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        print(f"error: archived context pack already exists: {destination}", file=sys.stderr)
        return 2
    source.rename(destination)
    payload = {"target": str(target), "pack_id": pack.get("pack_id"), "status": "archived", "archive_path": str(destination)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"archived: {pack.get('pack_id')}")
    print(f"path: {destination}")
    return 0


def health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    packs = _packs(target)
    latest = packs[0] if packs else None
    issues = []
    if not packs:
        issues.append({"status": WARN, "name": "context_pack_missing", "detail": "no context packs"})
    return {
        "target": str(target),
        "pack_count": len(packs),
        "latest": latest,
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }
