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

from . import lifecycle as _family_base

globals().update({name: value for name, value in vars(_family_base).items() if not name.startswith("__")})


def tasks(*, target: Path, all_tasks: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    ledger = ledger_mod._read_task_ledger(target)
    task_items = [task for task in ledger["tasks"] if isinstance(task, dict)]
    task_items.sort(key=ledger_mod._task_sort_key)
    if not all_tasks:
        task_items = [task for task in task_items if task.get("status", "pending") == "pending"]

    if json_output:
        print(
            json.dumps({"tasks_path": str(helpers._tasks_path(target)), "tasks": task_items}, indent=2, sort_keys=True)
        )
        return 0

    print(f"work tasks: {target}")
    print(f"tasks_path: {helpers._tasks_path(target)}")
    if not task_items:
        print("tasks: none")
        return 0
    for task in task_items:
        status_text = task.get("status", "pending")
        summary = ledger_mod._task_summary(task)
        print(
            f"- {task.get('id')} [{status_text}] "
            f"[{summary['type']} {summary['priority']} acceptance={summary['acceptance_count']}] "
            f"{helpers._short(str(task.get('text', '')))}"
        )
        if task.get("source"):
            print(f"  source: {task['source']}")
        if task.get("template"):
            print(f"  template: {task['template']}")
        issue = ledger_mod._task_issue_metadata(task)
        if issue:
            print(f"  issue: {issue.get('url') or issue.get('number')}")
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        if metadata.get("run_path"):
            print(f"  run: {metadata['run_path']}")
        if metadata.get("session_path"):
            print(f"  session: {metadata['session_path']}")
        if task.get("completed_at"):
            print(f"  completed_at: {task['completed_at']}")
    return 0


def task_add(
    *,
    target: Path,
    text: str | None = None,
    from_next: bool = False,
    from_issue: str | None = None,
    task_type: str = "task",
    priority: str = "normal",
    acceptance: list[str] | None = None,
    template: str | None = None,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if template and template not in constants.TASK_TEMPLATES:
        print(f"error: --template must be one of: {', '.join(constants.TASK_TEMPLATES)}", file=sys.stderr)
        return 2
    import_sources = [bool(from_next), bool(from_issue)]
    if sum(import_sources) > 1 or ((from_next or from_issue) and text):
        print("error: pass task text, --from-next, or --from-issue, not more than one", file=sys.stderr)
        return 2
    if task_type not in constants.TASK_TYPES:
        print(f"error: --type must be one of: {', '.join(constants.TASK_TYPES)}", file=sys.stderr)
        return 2
    if priority not in constants.TASK_PRIORITIES:
        print(f"error: --priority must be one of: {', '.join(constants.TASK_PRIORITIES)}", file=sys.stderr)
        return 2
    task_text = (text or "").strip()
    source = "manual"
    dedupe = True
    if from_next:
        next_step, metadata = _latest_run_next_metadata(target)
        if not next_step:
            print("error: no extracted next step is available", file=sys.stderr)
            return 1
        task_text = next_step
        source = "latest_dogfood_run"
    elif from_issue:
        issue_ref = from_issue.strip()
        if not issue_ref:
            print("error: --from-issue requires an issue URL or number", file=sys.stderr)
            return 2
        issue, issue_acceptance, error = ledger_mod._read_github_issue(target, issue_ref)
        if issue is None:
            print(f"error: could not read GitHub issue {issue_ref}: {error}", file=sys.stderr)
            return 1
        task_text = str(issue["title"]).strip()
        source = "github_issue"
        metadata = {"github_issue": issue}
        acceptance = [*issue_acceptance, *(acceptance or [])]
        dedupe = False
    else:
        metadata = None
    if not task_text:
        print("error: task text is required", file=sys.stderr)
        return 2
    task, created = ledger_mod._add_task(
        target,
        task_text,
        source=source,
        metadata=metadata,
        task_type=task_type,
        priority=priority,
        acceptance=ledger_mod._combined_acceptance(template, acceptance),
        template=template,
        dedupe=dedupe,
    )
    print(f"task: {task['id']}")
    print(f"status: {task['status']}")
    print(f"created: {created}")
    print(f"type: {ledger_mod._normalize_task_type(task.get('type'))}")
    print(f"priority: {ledger_mod._normalize_task_priority(task.get('priority'))}")
    if task.get("template"):
        print(f"template: {task['template']}")
    criteria = ledger_mod._task_acceptance(task)
    print(f"acceptance: {len(criteria)}")
    issue = ledger_mod._task_issue_metadata(task)
    if issue:
        print(f"issue: {issue.get('url') or issue.get('number')}")
    print(f"text: {task['text']}")
    return 0


def task_show(*, target: Path, task_id: str) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    task, _ = ledger_mod._find_task(target, task_id)
    if task is None:
        print(f"error: task not found: {task_id}", file=sys.stderr)
        return 1
    print(f"task: {task.get('id')}")
    print(f"status: {task.get('status', 'pending')}")
    print(f"source: {task.get('source', '')}")
    print(f"type: {ledger_mod._normalize_task_type(task.get('type'))}")
    print(f"priority: {ledger_mod._normalize_task_priority(task.get('priority'))}")
    if task.get("template"):
        print(f"template: {task['template']}")
    print(f"created_at: {task.get('created_at', '')}")
    print(f"updated_at: {task.get('updated_at', '')}")
    criteria = ledger_mod._task_acceptance(task)
    print(f"acceptance: {len(criteria)}")
    for item in criteria:
        print(f"  - {item}")
    issue = ledger_mod._task_issue_metadata(task)
    if issue:
        print("issue:")
        print(f"  url: {issue.get('url', '')}")
        print(f"  number: {issue.get('number', '')}")
        print(f"  title: {issue.get('title', '')}")
        print(f"  state: {issue.get('state', '')}")
        labels = issue.get("labels")
        if isinstance(labels, list) and labels:
            print(f"  labels: {', '.join(str(label) for label in labels)}")
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    if metadata:
        print("metadata:")
        for key in sorted(metadata):
            print(f"  {key}: {metadata[key]}")
    closeouts = metadata.get("review_closeouts")
    if isinstance(closeouts, list) and closeouts:
        print(f"review_closeouts: {len(closeouts)}")
        for item in closeouts:
            if not isinstance(item, dict):
                continue
            print(
                "  - "
                f"{item.get('review_run_id')} "
                f"resolved={item.get('resolved')} "
                f"findings={item.get('finding_count')} "
                f"unresolved={item.get('unresolved_count')}"
            )
    if task.get("completed_at"):
        print(f"completed_at: {task['completed_at']}")
    if task.get("completed_session_title"):
        print(f"completed_session_title: {task['completed_session_title']}")
    if task.get("completed_session_path"):
        print(f"completed_session_path: {task['completed_session_path']}")
    if task.get("completed_run_path"):
        print(f"completed_run_path: {task['completed_run_path']}")
    completed_acceptance = task.get("completed_acceptance")
    if isinstance(completed_acceptance, list):
        print(f"completed_acceptance: {len(completed_acceptance)}")
        for item in completed_acceptance:
            print(f"  - {item}")
    print(f"text: {task.get('text', '')}")
    return 0


def task_plan(
    *,
    target: Path,
    task_id: str,
    json_output: bool = False,
    write: bool = False,
    title: str | None = None,
    assumptions: list[str] | None = None,
    risks: list[str] | None = None,
    sources: list[str] | None = None,
    next_command: str | None = None,
    accept: bool = False,
    kind: str = "plan",
    steps: list[str] | None = None,
    from_research: str | None = None,
) -> int:
    if write:
        return ledger_mod._write_plan_artifact(
            target=target,
            task_id=task_id,
            title=title,
            assumptions=assumptions,
            risks=risks,
            sources=sources,
            next_command=next_command,
            accept=accept,
            json_output=json_output,
            kind=kind,
            steps=steps,
            from_research=from_research,
        )
    payload, rc = _task_plan_payload(target, task_id)
    if payload is None:
        return rc
    resolved_target = target.expanduser().resolve()
    resolved_id = str(payload.get("id") or task_id)
    artifact = ledger_mod._plan_artifact_summary(resolved_target, resolved_id)
    meta_artifact = ledger_mod._plan_artifact_summary(resolved_target, resolved_id, kind="meta")
    payload["plan_artifact"] = artifact
    payload["meta_artifact"] = meta_artifact
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"task: {payload['id']}")
    print(f"type: {payload['type']}")
    print(f"priority: {payload['priority']}")
    if payload.get("template"):
        print(f"template: {payload['template']}")
    print(f"status: {payload['status']}")
    print(f"source: {payload['source']}")
    print(f"text: {payload['text']}")
    if payload.get("issue"):
        issue = payload["issue"]
        print("issue:")
        print(f"  url: {issue.get('url', '')}")
        print(f"  number: {issue.get('number', '')}")
        print(f"  title: {issue.get('title', '')}")
        print(f"  state: {issue.get('state', '')}")
        labels = issue.get("labels")
        if isinstance(labels, list) and labels:
            print(f"  labels: {', '.join(str(label) for label in labels)}")
    if payload.get("guidance"):
        print("guidance:")
        for item in payload["guidance"]:
            print(f"  - {item}")
    print("acceptance:")
    if payload["acceptance"]:
        for item in payload["acceptance"]:
            print(f"  - {item}")
    else:
        print("  missing")
    print(f"suggested_command: {payload['suggested_command']}")
    if artifact is None:
        print("plan_artifact: none")
    else:
        print(f"plan_artifact: {artifact['status']} ({artifact['path']})")
    if meta_artifact is None:
        print("meta_artifact: none")
    else:
        print(f"meta_artifact: {meta_artifact['status']} ({meta_artifact['path']})")
    return 0


def task_done(*, target: Path, task_id: str) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    task, ledger = ledger_mod._find_task(target, task_id)
    if task is None:
        print(f"error: task not found: {task_id}", file=sys.stderr)
        return 1
    now = helpers._now().isoformat()
    task["status"] = "done"
    task["updated_at"] = now
    task["completed_at"] = now
    ledger_mod._write_task_ledger(target, ledger)
    print(f"task: {task.get('id')}")
    print("status: done")
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
                print(f"active_session_title: {helpers._short(str(active['title']))}")
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
            print(f"latest_task: {helpers._short(str(latest_run['task']))}")
    else:
        print("latest_run: none")

    task = str(payload["next"])
    print(f"next_source: {payload['next_source']}")
    if payload.get("task_id"):
        print(f"task_id: {payload['task_id']}")
    print(f"next: {helpers._short(task)}")
    print(f"suggested_command: {payload['suggested_command']}")
    return 0
