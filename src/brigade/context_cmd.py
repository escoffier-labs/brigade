"""Local context engineering packs."""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import work_cmd

OK = "ok"
WARN = "warn"
CONTEXT_KINDS = {"task", "repo", "release", "tool-use"}
SYNC_MARKER = "brigade-context-sync:"
SYNC_CONFIG_REL_PATH = ".brigade/context/sync-targets.json"
CONTEXT_PACK_STALE_HOURS = 72


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _context_root(target: Path) -> Path:
    return target / ".brigade" / "context"


def _packs_root(target: Path) -> Path:
    return _context_root(target) / "packs"


def _archive_root(target: Path) -> Path:
    return _context_root(target) / "archive"


def _sync_config_path(target: Path) -> Path:
    return target / SYNC_CONFIG_REL_PATH


def _sync_plans_root(target: Path) -> Path:
    return _context_root(target) / "sync-plans"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _stable_hash(value: object) -> str:
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    import hashlib

    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]


def _parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    rendered = value.strip()
    if rendered.endswith("Z"):
        rendered = rendered[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _short(text: object, limit: int = 160) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "..."


def _doc_summary(target: Path) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for name in ("README.md", "ROADMAP.md", "CHANGELOG.md"):
        path = target / name
        if path.is_file():
            line_count = len(path.read_text().splitlines())
            docs.append({"path": name, "exists": True, "summary": f"present ({line_count} lines)"})
        else:
            docs.append({"path": name, "exists": False, "summary": "missing"})
    return docs


def _guidance_summary(target: Path) -> dict[str, Any]:
    sources = []
    for name in ("AGENTS.md", "CLAUDE.md", ".claude/CLAUDE.md"):
        path = target / name
        if path.is_file():
            sources.append({"path": name, "exists": True, "summary": "present, content excluded"})
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


def _pack_fingerprint(pack: dict[str, Any]) -> str:
    return _stable_hash(
        {
            "pack_id": pack.get("pack_id"),
            "kind": pack.get("kind"),
            "task": pack.get("task"),
            "tool_id": pack.get("tool_id"),
            "release_id": pack.get("release_id"),
            "source_references": pack.get("source_references"),
            "freshness": pack.get("freshness"),
            "issues": pack.get("issues"),
        }
    )


def _load_sync_targets(target: Path) -> tuple[list[dict[str, Any]], list[str]]:
    path = _sync_config_path(target)
    if not path.is_file():
        return [], []
    payload = _read_json(path)
    if payload is None:
        return [], [f"invalid context sync config: {path}"]
    values = payload.get("targets")
    if not isinstance(values, list):
        return [], ["context sync config must contain a targets list"]
    targets: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values, start=1):
        label = f"context sync target {index}"
        if not isinstance(value, dict):
            errors.append(f"{label} must be an object")
            continue
        target_id = value.get("id")
        harness = value.get("harness")
        path_value = value.get("path")
        if not isinstance(target_id, str) or not target_id.strip():
            errors.append(f"{label}: id must be a non-empty string")
            continue
        if target_id in seen:
            errors.append(f"{label}: duplicate id {target_id}")
        seen.add(target_id)
        if not isinstance(harness, str) or not harness.strip():
            errors.append(f"{label}: harness must be a non-empty string")
        if not isinstance(path_value, str) or not path_value.strip():
            errors.append(f"{label}: path must be a non-empty string")
        enabled = value.get("enabled", True)
        if not isinstance(enabled, bool):
            errors.append(f"{label}: enabled must be true or false")
            enabled = True
        if isinstance(harness, str) and isinstance(path_value, str):
            path = Path(path_value).expanduser()
            targets.append(
                {
                    "id": target_id.strip(),
                    "harness": harness.strip(),
                    "path": str(path if path.is_absolute() else target / path),
                    "path_label": path_value.strip(),
                    "enabled": enabled,
                }
            )
    return [item for item in targets if item.get("enabled", True)], errors


def _read_sync_metadata(path: Path) -> dict[str, Any] | None:
    try:
        first_line = path.read_text().splitlines()[0]
    except (OSError, IndexError):
        return None
    match = re.search(rf"{re.escape(SYNC_MARKER)}\s*(\{{.*\}})", first_line)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _context_sync_plan_payload(target: Path, pack_id: str = "latest") -> dict[str, Any]:
    target = target.expanduser().resolve()
    pack, error = _find_pack(target, pack_id)
    targets, errors = _load_sync_targets(target)
    checks: list[dict[str, Any]] = []
    if error:
        checks.append({"status": WARN, "name": "context_sync_pack", "detail": error})
    checks.extend({"status": WARN, "name": "context_sync_config", "detail": item} for item in errors)
    if not targets and _sync_config_path(target).is_file() and not errors:
        checks.append({"status": WARN, "name": "context_sync_targets", "detail": "no enabled context sync targets"})
    pack_fingerprint = _pack_fingerprint(pack) if isinstance(pack, dict) else None
    if isinstance(pack, dict):
        freshness = pack.get("freshness") if isinstance(pack.get("freshness"), dict) else {}
        created = _parse_iso_datetime(pack.get("created_at") or freshness.get("generated_at"))
        if created is not None:
            age_hours = (_now() - created).total_seconds() / 3600
            if age_hours > CONTEXT_PACK_STALE_HOURS:
                checks.append({"status": WARN, "name": "context_sync_pack_stale", "detail": f"{pack.get('pack_id')} is {age_hours:.1f}h old"})
        for ref in pack.get("source_references", []) if isinstance(pack.get("source_references"), list) else []:
            if isinstance(ref, dict) and ref.get("exists") and not (target / str(ref.get("path"))).exists():
                checks.append({"status": WARN, "name": "context_sync_missing_source_reference", "detail": str(ref.get("path"))})
    planned: list[dict[str, Any]] = []
    for sync_target in targets:
        destination = Path(str(sync_target["path"]))
        item = {
            "target_id": sync_target["id"],
            "harness": sync_target["harness"],
            "path": str(destination),
            "path_label": sync_target["path_label"],
            "pack_id": pack.get("pack_id") if isinstance(pack, dict) else None,
            "pack_fingerprint": pack_fingerprint,
            "writes": False,
        }
        if pack is None:
            item.update({"status": "blocked", "action": "skip", "detail": "context pack is missing"})
        elif not destination.exists():
            item.update({"status": "missing", "action": "create", "detail": "destination does not exist"})
        else:
            metadata = _read_sync_metadata(destination)
            if metadata is None:
                item.update({"status": "conflict", "action": "skip", "detail": "destination exists without Brigade context sync metadata"})
            elif metadata.get("pack_fingerprint") == pack_fingerprint:
                item.update({"status": "current", "action": "skip", "detail": "destination is current"})
            else:
                item.update({"status": "stale", "action": "update", "detail": "destination was built from older context evidence"})
        planned.append(item)
    blockers = [item for item in planned if item.get("status") in {"blocked", "conflict"}]
    return {
        "target": str(target),
        "config_path": str(_sync_config_path(target)),
        "pack_id": pack.get("pack_id") if isinstance(pack, dict) else pack_id,
        "pack_fingerprint": pack_fingerprint,
        "valid": not errors and pack is not None,
        "checks": checks,
        "issues": [check for check in checks if check["status"] != OK],
        "destinations": planned,
        "destination_count": len(planned),
        "blocker_count": len(blockers),
        "blockers": blockers,
        "write_default": False,
        "suggested_next_commands": [
            f"brigade context show {pack.get('pack_id')}" if isinstance(pack, dict) else "brigade context build",
            "review configured harness destinations",
        ],
    }


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


def sync_plan(*, target: Path, pack_id: str = "latest", json_output: bool = False) -> int:
    payload = _context_sync_plan_payload(target, pack_id=pack_id)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] and payload["blocker_count"] == 0 else 1
    print(f"context sync plan: {payload['target']}")
    print(f"pack: {payload.get('pack_id')}")
    print(f"destinations: {payload['destination_count']}")
    print(f"blockers: {payload['blocker_count']}")
    print("writes: 0")
    for item in payload["destinations"]:
        print(f"- {item.get('target_id')} [{item.get('status')}] {item.get('path_label')}")
    return 0 if payload["valid"] and payload["blocker_count"] == 0 else 1


def sync_record(*, target: Path, pack_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = _context_sync_plan_payload(target, pack_id=pack_id)
    sync_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-context-sync-plan"
    payload.update({"sync_id": sync_id, "created_at": _now().isoformat(), "status": "planned"})
    root = _sync_plans_root(target) / sync_id
    _write_json(root / "sync-plan.json", payload)
    payload["path"] = str(root)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] and payload["blocker_count"] == 0 else 1
    print(f"context_sync_plan: {sync_id}")
    print(f"path: {root}")
    print(f"blockers: {payload['blocker_count']}")
    print("writes: 0")
    return 0 if payload["valid"] and payload["blocker_count"] == 0 else 1


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
    sync = None
    if latest is not None and _sync_config_path(target).is_file():
        sync = _context_sync_plan_payload(target, pack_id=str(latest.get("pack_id") or "latest"))
        for item in sync.get("checks", []):
            if item.get("status") != OK:
                issues.append(item)
        if sync.get("blocker_count", 0):
            issues.append({"status": WARN, "name": "context_sync_blocked", "detail": f"{sync.get('blocker_count')} context sync destination(s) blocked"})
    return {
        "target": str(target),
        "pack_count": len(packs),
        "latest": latest,
        "sync": sync,
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }
