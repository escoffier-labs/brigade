"""Cloud dispatch registry: register, reconcile, and report without auto-delete (#890)."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import localio

REGISTRY_SCHEMA = "brigade.run.cloud.registry.v1"
STATUS_SCHEMA = "brigade.run.cloud.status.v1"
SWEEP_SCHEMA = "brigade.run.cloud.sweep.v1"

DEFAULT_STALE_READY_HOURS = 6
CLOUD_BRANCH_PREFIXES = ("codex/", "cursor/")
READY_STATES = frozenset({"ready", "completed", "succeeded", "applied"})
FAILED_STATES = frozenset({"failed", "errored", "error", "cancelled", "canceled", "expired"})
FINISHED_STATES = frozenset({"finished", "completed", "succeeded", "applied", "ready"})
PENDING_STATES = frozenset({"pending", "running", "queued", "in_progress", "dispatching"})

CLASSIFICATIONS = (
    "pending",
    "ready-to-land",
    "landed",
    "stale",
    "orphaned",
    "needs-investigation",
)


def _root(target: Path) -> Path:
    return target.expanduser().resolve() / ".brigade" / "cloud"


def registry_path(target: Path) -> Path:
    return _root(target) / "registry.json"


def prompt_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def empty_registry(*, stale_ready_hours: int = DEFAULT_STALE_READY_HOURS) -> dict[str, Any]:
    return {
        "schema": REGISTRY_SCHEMA,
        "version": 1,
        "stale_ready_hours": int(stale_ready_hours),
        "entries": [],
    }


def load_registry(target: Path) -> dict[str, Any]:
    path = registry_path(target)
    if not path.is_file():
        return empty_registry()
    data = localio.read_json_dict(path) or {}
    if data.get("schema") != REGISTRY_SCHEMA:
        # Soft-recover unknown/legacy payloads into the current schema shape.
        entries = data.get("entries") if isinstance(data.get("entries"), list) else []
        hours = data.get("stale_ready_hours", DEFAULT_STALE_READY_HOURS)
        try:
            stale_ready_hours = int(hours)
        except (TypeError, ValueError):
            stale_ready_hours = DEFAULT_STALE_READY_HOURS
        return {
            "schema": REGISTRY_SCHEMA,
            "version": 1,
            "stale_ready_hours": stale_ready_hours,
            "entries": [e for e in entries if isinstance(e, dict)],
        }
    entries = data.get("entries") if isinstance(data.get("entries"), list) else []
    try:
        stale_ready_hours = int(data.get("stale_ready_hours", DEFAULT_STALE_READY_HOURS))
    except (TypeError, ValueError):
        stale_ready_hours = DEFAULT_STALE_READY_HOURS
    return {
        "schema": REGISTRY_SCHEMA,
        "version": 1,
        "stale_ready_hours": stale_ready_hours,
        "entries": [e for e in entries if isinstance(e, dict)],
    }


def save_registry(target: Path, registry: dict[str, Any]) -> None:
    path = registry_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": REGISTRY_SCHEMA,
        "version": 1,
        "stale_ready_hours": int(registry.get("stale_ready_hours", DEFAULT_STALE_READY_HOURS)),
        "entries": [e for e in (registry.get("entries") or []) if isinstance(e, dict)],
    }
    localio.write_json(path, payload)


def _new_id() -> str:
    return f"cloud-{uuid4().hex[:12]}"


def register(
    target: Path,
    *,
    provider: str,
    task_id: str | None = None,
    label: str,
    prompt_hash: str | None = None,
    session_id: str | None = None,
    expected_artifact: dict[str, Any] | None = None,
    branch: str | None = None,
    dispatched_at: str | None = None,
    source: str = "dispatch",
) -> dict[str, Any]:
    if provider not in {"codex-cloud", "cursor-cloud"}:
        raise ValueError("provider must be codex-cloud or cursor-cloud")
    if not label.strip():
        raise ValueError("label must not be empty")
    if source == "dispatch" and not task_id:
        raise ValueError("dispatch registration requires task_id")
    registry = load_registry(target)
    entry = {
        "id": _new_id(),
        "provider": provider,
        "task_id": task_id,
        "label": label.strip(),
        "prompt_hash": prompt_hash,
        "session_id": session_id,
        "expected_artifact": expected_artifact or {"kind": "diff"},
        "branch": branch,
        "dispatched_at": dispatched_at or _now_iso(),
        "adopted_at": None,
        "source": source,
    }
    registry["entries"].append(entry)
    save_registry(target, registry)
    return entry


def adopt(
    target: Path,
    *,
    provider: str,
    task_id: str | None = None,
    branch: str | None = None,
    label: str | None = None,
    prompt_hash: str | None = None,
    session_id: str | None = None,
    expected_artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not task_id and not branch:
        raise ValueError("adopt requires --task-id and/or --branch")
    source = "adopt-task" if task_id and not branch else "adopt-branch" if branch and not task_id else "adopt-task"
    if task_id and branch:
        source = "adopt-task"
    resolved_label = (label or task_id or branch or "adopted").strip()
    artifact = expected_artifact
    if artifact is None and branch:
        artifact = {"kind": "branch", "pattern": branch}
    entry = register(
        target,
        provider=provider,
        task_id=task_id,
        label=resolved_label,
        prompt_hash=prompt_hash,
        session_id=session_id,
        expected_artifact=artifact,
        branch=branch,
        source=source,
    )
    # Stamp adopted_at without storing prompt text.
    registry = load_registry(target)
    for item in registry["entries"]:
        if item.get("id") == entry["id"]:
            item["adopted_at"] = _now_iso()
            entry = item
            break
    save_registry(target, registry)
    return entry


def _normalize_provider_state(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    word = value.strip().lower()
    return word or None


def _hours_since(earlier: datetime | None, now: datetime) -> float | None:
    if earlier is None:
        return None
    return max(0.0, (now - earlier).total_seconds() / 3600.0)


def _branch_names(github: dict[str, Any]) -> set[str]:
    branches = github.get("branches") if isinstance(github, dict) else None
    if not isinstance(branches, list):
        return set()
    names: set[str] = set()
    for item in branches:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            names.add(item["name"])
        elif isinstance(item, str):
            names.add(item)
    return names


def _prs_for_branch(github: dict[str, Any], branch: str | None) -> list[dict[str, Any]]:
    if not branch:
        return []
    prs = github.get("prs") if isinstance(github, dict) else None
    if not isinstance(prs, list):
        return []
    matched = []
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        if pr.get("head") == branch:
            matched.append(pr)
    return matched


def _classify_entry(
    entry: dict[str, Any],
    *,
    provider_tasks: dict[str, Any],
    github: dict[str, Any],
    stale_ready_hours: int,
    now: datetime,
    cursor_wired: bool,
) -> dict[str, Any]:
    provider = str(entry.get("provider") or "")
    task_id = entry.get("task_id")
    branch = entry.get("branch") if isinstance(entry.get("branch"), str) else None
    provider_info = provider_tasks.get(str(task_id)) if task_id else None
    provider_state = None
    ready_at = None
    if isinstance(provider_info, dict):
        provider_state = _normalize_provider_state(provider_info.get("state"))
        ready_at = _parse_time(provider_info.get("ready_at"))
    branches = _branch_names(github)
    prs = _prs_for_branch(github, branch)
    merged = any(str(pr.get("state", "")).upper() == "MERGED" for pr in prs)
    open_pr = any(str(pr.get("state", "")).upper() == "OPEN" for pr in prs)
    branch_exists = bool(branch and branch in branches)
    expected = entry.get("expected_artifact") if isinstance(entry.get("expected_artifact"), dict) else {}
    expects_branch = expected.get("kind") == "branch"

    evidence: dict[str, Any] = {
        "registry": {"id": entry.get("id"), "source": entry.get("source"), "dispatched_at": entry.get("dispatched_at")},
        "provider": {
            "wired": provider != "cursor-cloud" or cursor_wired,
            "state": provider_state,
            "task_id": task_id,
        },
        "github": {
            "branch": branch,
            "branch_exists": branch_exists,
            "prs": prs,
        },
    }

    classification = "pending"
    if provider == "cursor-cloud" and not cursor_wired:
        evidence["provider"] = "unwired"
        if branch_exists and (provider_state in FAILED_STATES or not task_id):
            classification = "orphaned"
        elif merged:
            classification = "landed"
        else:
            classification = "pending"
    elif merged:
        classification = "landed"
    elif provider_state in FAILED_STATES and branch_exists:
        classification = "orphaned"
    elif provider_state in READY_STATES or provider_state == "finished":
        # Ready / finished without a merge: landable, stale, or needs investigation.
        if expects_branch and not branch_exists and not open_pr:
            classification = "needs-investigation"
        else:
            ready_mark = ready_at or _parse_time(entry.get("dispatched_at"))
            age_hours = _hours_since(ready_mark, now)
            if age_hours is not None and age_hours >= stale_ready_hours:
                classification = "stale"
            else:
                classification = "ready-to-land"
    elif provider_state in PENDING_STATES or provider_state is None:
        classification = "pending"
    elif provider_state in FAILED_STATES:
        classification = "needs-investigation" if expects_branch and not branch_exists else "pending"
    else:
        classification = "pending"

    return {
        "id": entry.get("id"),
        "provider": provider,
        "task_id": task_id,
        "label": entry.get("label"),
        "branch": branch,
        "classification": classification,
        "provider_state": provider_state,
        "prompt_hash": entry.get("prompt_hash"),
        "session_id": entry.get("session_id"),
        "dispatched_at": entry.get("dispatched_at"),
        "source": entry.get("source"),
        "evidence": evidence,
        "pr": prs[0] if prs else None,
    }


def _orphan_branch_rows(
    *,
    registry_entries: list[dict[str, Any]],
    github: dict[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    known_branches = {e.get("branch") for e in registry_entries if isinstance(e.get("branch"), str) and e.get("branch")}
    known_branches |= {
        e.get("expected_artifact", {}).get("pattern")
        for e in registry_entries
        if isinstance(e.get("expected_artifact"), dict)
        and isinstance(e.get("expected_artifact", {}).get("pattern"), str)
        and "/" in str(e.get("expected_artifact", {}).get("pattern"))
        and "*" not in str(e.get("expected_artifact", {}).get("pattern"))
    }
    rows: list[dict[str, Any]] = []
    for name in sorted(_branch_names(github)):
        if not name.startswith(CLOUD_BRANCH_PREFIXES):
            continue
        if name in known_branches:
            continue
        provider = "cursor-cloud" if name.startswith("cursor/") else "codex-cloud"
        rows.append(
            {
                "id": f"orphan-branch:{name}",
                "provider": provider,
                "task_id": None,
                "label": name,
                "branch": name,
                "classification": "orphaned",
                "provider_state": None,
                "prompt_hash": None,
                "session_id": None,
                "dispatched_at": None,
                "source": "github-branch",
                "evidence": {
                    "registry": None,
                    "provider": {"wired": False, "state": None, "task_id": None},
                    "github": {"branch": name, "branch_exists": True, "prs": _prs_for_branch(github, name)},
                },
                "pr": None,
                "observed_at": _now_iso(now),
            }
        )
    return rows


def status_payload(
    target: Path,
    *,
    now: datetime | None = None,
    provider_tasks: dict[str, Any] | None = None,
    github: dict[str, Any] | None = None,
    cursor_wired: bool = False,
) -> dict[str, Any]:
    observed = now or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    registry = load_registry(target)
    stale_ready_hours = int(registry.get("stale_ready_hours", DEFAULT_STALE_READY_HOURS))
    provider_tasks = provider_tasks if isinstance(provider_tasks, dict) else {}
    github = github if isinstance(github, dict) else {"branches": [], "prs": []}

    entries = [
        _classify_entry(
            entry,
            provider_tasks=provider_tasks,
            github=github,
            stale_ready_hours=stale_ready_hours,
            now=observed,
            cursor_wired=cursor_wired,
        )
        for entry in registry["entries"]
    ]
    entries.extend(_orphan_branch_rows(registry_entries=registry["entries"], github=github, now=observed))

    return {
        "schema": STATUS_SCHEMA,
        "target": str(target.expanduser().resolve()),
        "observed_at": _now_iso(observed),
        "stale_ready_hours": stale_ready_hours,
        "sources": {
            "codex-cloud": {"wired": True, "authority": "best-effort"},
            "cursor-cloud": {
                "wired": bool(cursor_wired),
                "authority": "best-effort" if cursor_wired else "unwired",
                "detail": None if cursor_wired else "no API key; cursor cloud left unwired",
            },
            "github": {"wired": True, "authority": "ground-truth"},
        },
        "entries": entries,
        "counts": {
            classification: sum(1 for row in entries if row.get("classification") == classification)
            for classification in CLASSIFICATIONS
        },
    }


def stale_entries_for_brief(target: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Best-effort stale summary for work brief; fail-open on observe errors."""
    try:
        provider_tasks, github, cursor_wired = observe_providers(target)
        payload = status_payload(
            target,
            now=now,
            provider_tasks=provider_tasks,
            github=github,
            cursor_wired=cursor_wired,
        )
    except Exception as exc:  # noqa: BLE001 - brief must stay available
        return {
            "stale_count": 0,
            "stale_entries": [],
            "error": f"{type(exc).__name__}: {exc}",
            "suggested_command": "brigade run cloud status --json",
        }
    stale = [e for e in payload.get("entries", []) if e.get("classification") == "stale"]
    return {
        "stale_count": len(stale),
        "stale_entries": stale[:10],
        "stale_ready_hours": payload.get("stale_ready_hours"),
        "suggested_command": "brigade run cloud status --json",
    }


def sweep(target: Path, *, now: datetime | None = None, status: dict[str, Any] | None = None) -> dict[str, Any]:
    observed = now or datetime.now(timezone.utc)
    payload = status if isinstance(status, dict) else status_payload(target, now=observed)
    recoverable: list[dict[str, Any]] = []
    deletable: list[dict[str, Any]] = []
    for row in payload.get("entries", []):
        if not isinstance(row, dict):
            continue
        classification = row.get("classification")
        item = {
            "id": row.get("id"),
            "label": row.get("label"),
            "branch": row.get("branch"),
            "classification": classification,
            "evidence": row.get("evidence"),
        }
        if classification in {"ready-to-land", "stale", "needs-investigation", "pending"}:
            recoverable.append(item)
        elif classification == "orphaned":
            deletable.append(item)
        # landed: neither recoverable work nor deletable junk in the sweep sense
    sweep_id = f"{observed.strftime('%Y%m%d-%H%M%S')}-cloud-sweep-{uuid4().hex[:6]}"
    report = {
        "schema": SWEEP_SCHEMA,
        "sweep_id": sweep_id,
        "target": str(target.expanduser().resolve()),
        "observed_at": _now_iso(observed),
        "action": "report-only",
        "recoverable": recoverable,
        "deletable": deletable,
        "note": "Nothing was deleted. Review deletable branches, then remove them manually or via an authorized step.",
    }
    receipt_dir = _root(target) / "sweeps" / sweep_id
    receipt_dir.mkdir(parents=True, exist_ok=True)
    localio.write_json(receipt_dir / "sweep.json", report)
    return report


def _run_text(command: list[str], *, cwd: Path | None = None, timeout: float = 30.0) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", f"{type(exc).__name__}: {exc}"
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _parse_codex_cloud_list(stdout: str) -> dict[str, Any]:
    """Best-effort adapter for `codex cloud list` / status-shaped text."""
    tasks: dict[str, Any] = {}
    # JSON array or object with tasks
    text = stdout.strip()
    if text.startswith("{") or text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("id"):
                    tasks[str(item["id"])] = {
                        "state": _normalize_provider_state(item.get("status") or item.get("state")),
                        "ready_at": item.get("ready_at") or item.get("updated_at"),
                    }
            return tasks
        if isinstance(data, dict):
            items = data.get("tasks") if isinstance(data.get("tasks"), list) else [data]
            for item in items:
                if isinstance(item, dict) and item.get("id"):
                    tasks[str(item["id"])] = {
                        "state": _normalize_provider_state(item.get("status") or item.get("state")),
                        "ready_at": item.get("ready_at") or item.get("updated_at"),
                    }
            return tasks
    # Line adapter: task_id <id> ... [READY] / Status: ready
    current_id: str | None = None
    for line in text.splitlines():
        id_match = re.search(r"\b(task_[A-Za-z0-9-]{4,})\b", line)
        if id_match:
            current_id = id_match.group(1)
            tasks.setdefault(current_id, {"state": None, "ready_at": None})
        bracket = re.search(r"\[([A-Za-z_ -]+)\]", line)
        status_line = re.search(r"(?i)\bstatus\b\s*[:=]\s*([A-Za-z_ -]+)", line)
        state = None
        if bracket:
            state = _normalize_provider_state(bracket.group(1))
        elif status_line:
            state = _normalize_provider_state(status_line.group(1))
        if state and current_id:
            tasks[current_id]["state"] = state
    return tasks


def observe_codex_cloud_tasks(target: Path) -> dict[str, Any]:
    code, stdout, _stderr = _run_text(["codex", "cloud", "list"], cwd=target)
    if code != 0 or not stdout.strip():
        # Fall back to per-task status for known registry ids.
        registry = load_registry(target)
        tasks: dict[str, Any] = {}
        for entry in registry["entries"]:
            task_id = entry.get("task_id")
            if not isinstance(task_id, str) or entry.get("provider") != "codex-cloud":
                continue
            scode, sout, serr = _run_text(["codex", "cloud", "status", task_id], cwd=target)
            blob = (sout + "\n" + serr).strip()
            if scode != 0 and not blob:
                continue
            from . import codex_cloud

            state = codex_cloud._scan_status(blob)  # noqa: SLF001 - shared status scanner
            tasks[task_id] = {"state": state, "ready_at": None}
        return tasks
    return _parse_codex_cloud_list(stdout)


def observe_github(target: Path) -> dict[str, Any]:
    """Ground truth for cloud-shaped branches and PRs via gh."""
    branches: list[dict[str, str]] = []
    code, stdout, _ = _run_text(
        ["gh", "api", "repos/{owner}/{repo}/branches?per_page=100"],
        cwd=target,
    )
    if code == 0 and stdout.strip():
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            data = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    name = item["name"]
                    if name.startswith(CLOUD_BRANCH_PREFIXES):
                        branches.append({"name": name})
    # Also accept local refs when gh is unavailable so adopt/sweep still works offline in tests.
    if not branches:
        code, stdout, _ = _run_text(["git", "branch", "-a", "--format=%(refname:short)"], cwd=target)
        if code == 0:
            for line in stdout.splitlines():
                name = line.strip().removeprefix("origin/")
                if name.startswith(CLOUD_BRANCH_PREFIXES):
                    branches.append({"name": name})

    prs: list[dict[str, Any]] = []
    code, stdout, _ = _run_text(
        ["gh", "pr", "list", "--state", "all", "--json", "number,state,headRefName", "--limit", "100"],
        cwd=target,
    )
    if code == 0 and stdout.strip():
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            data = []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                head = item.get("headRefName")
                if isinstance(head, str) and head.startswith(CLOUD_BRANCH_PREFIXES):
                    prs.append({"head": head, "state": item.get("state"), "number": item.get("number")})
    return {"branches": branches, "prs": prs}


def cursor_cloud_wired() -> bool:
    """Cursor cloud stays unwired until an API key exists; never guess task state."""
    import os

    for key in ("CURSOR_API_KEY", "CURSOR_CLOUD_API_KEY", "BG_AGENT_API_KEY"):
        value = os.environ.get(key, "").strip()
        if value:
            return True
    return False


def observe_providers(target: Path, **_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any], bool]:
    return observe_codex_cloud_tasks(target), observe_github(target), cursor_cloud_wired()


def center_activity_records(
    target: Path,
    *,
    now: datetime | None = None,
    provider_tasks: dict[str, Any] | None = None,
    github: dict[str, Any] | None = None,
    cursor_wired: bool | None = None,
) -> list[dict[str, Any]]:
    """Map registry status rows into Center agent_activity records on host=cloud."""
    observed = now or datetime.now(timezone.utc)
    if provider_tasks is None or github is None or cursor_wired is None:
        observed_tasks, observed_github, observed_cursor = observe_providers(target)
        provider_tasks = provider_tasks if provider_tasks is not None else observed_tasks
        github = github if github is not None else observed_github
        cursor_wired = cursor_cloud_wired() if cursor_wired is None else cursor_wired
    payload = status_payload(
        target,
        now=observed,
        provider_tasks=provider_tasks or {},
        github=github or {"branches": [], "prs": []},
        cursor_wired=bool(cursor_wired),
    )
    state_map = {
        "pending": "running",
        "ready-to-land": "ready",
        "stale": "stale",
        "landed": "succeeded",
        "orphaned": "failed",
        "needs-investigation": "unknown",
    }
    records: list[dict[str, Any]] = []
    for row in payload.get("entries", []):
        if not isinstance(row, dict):
            continue
        classification = str(row.get("classification") or "pending")
        started = row.get("dispatched_at")
        records.append(
            {
                "activity_id": f"cloud:{row.get('id')}",
                "parent_activity_id": None,
                "provider": str(row.get("provider") or "cloud").replace("-cloud", ""),
                "harness": str(row.get("provider") or "cloud"),
                "kind": "cloud-task",
                "host": "cloud",
                "label": str(row.get("provider") or "cloud"),
                "task_label": str(row.get("label") or row.get("branch") or "Cloud task"),
                "model": None,
                "state": state_map.get(classification, "unknown"),
                "classification": classification,
                "started_at": started,
                "last_updated_at": payload.get("observed_at"),
                "elapsed_seconds": None,
                "source": {
                    "name": "cloud-tracker",
                    "authority": "local-registry",
                    "observed_at": payload.get("observed_at"),
                },
                "links": {
                    "status": "brigade run cloud status --json",
                    "task_id": row.get("task_id"),
                    "branch": row.get("branch"),
                },
            }
        )
    return records


def set_stale_ready_hours(target: Path, hours: int) -> dict[str, Any]:
    if hours < 1:
        raise ValueError("stale_ready_hours must be >= 1")
    registry = load_registry(target)
    registry["stale_ready_hours"] = int(hours)
    save_registry(target, registry)
    return registry
