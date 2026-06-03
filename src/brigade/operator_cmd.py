"""Safe local operator bootstrap commands."""
from __future__ import annotations

import json
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

from . import chat_cmd, daily_cmd, handoff_cmd, memory_cmd, repos_cmd, security_cmd, tools_cmd, work_cmd


def _steps(target: Path) -> list[dict[str, Any]]:
    return [
        {"id": "daily", "path": daily_cmd._config_path(target), "command": daily_cmd.init, "kwargs": {}},
        {"id": "handoff-sources", "path": handoff_cmd.default_sources_path(target), "command": handoff_cmd.sources_init, "kwargs": {}},
        {"id": "work-backup", "path": work_cmd._backup_config_path(target), "command": work_cmd.backup_init, "kwargs": {"update_gitignore": False}},
        {"id": "work-scanners", "path": work_cmd._scanner_config_path(target), "command": work_cmd.scanners_init, "kwargs": {"update_gitignore": False}},
        {"id": "work-review", "path": work_cmd._review_config_path(target), "command": work_cmd.review_init, "kwargs": {"update_gitignore": False}},
        {"id": "chat-surfaces", "path": chat_cmd._config_path(target), "command": chat_cmd.surfaces_init, "kwargs": {"update_gitignore": False}},
        {"id": "memory-care", "path": memory_cmd.config_path(target), "command": memory_cmd.init, "kwargs": {"update_gitignore": False}},
        {"id": "repo-fleet", "path": repos_cmd.config_path(target), "command": repos_cmd.init, "kwargs": {"update_gitignore": False}},
        {"id": "security", "path": security_cmd.config_path(target), "command": security_cmd.init, "kwargs": {}},
        {"id": "tools", "path": tools_cmd.config_path(target), "command": tools_cmd.init, "kwargs": {"update_gitignore": False}},
    ]


def plan_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    steps = []
    for step in _steps(target):
        path = step["path"]
        steps.append(
            {
                "id": step["id"],
                "path": str(path),
                "exists": path.exists(),
                "action": "skip" if path.exists() else "write",
            }
        )
    return {
        "target": str(target),
        "steps": steps,
        "missing_count": sum(1 for step in steps if step["action"] == "write"),
        "boundaries": [
            "Does not start services.",
            "Does not run scanners.",
            "Does not ingest handoffs.",
            "Does not write canonical memory.",
        ],
    }


def plan(*, target: Path, json_output: bool = False) -> int:
    payload = plan_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"operator bootstrap plan: {payload['target']}")
    for row in payload["steps"]:
        print(f"[{row['action']}] {row['id']}: {row['path']}")
    return 0


def init(*, target: Path, force: bool = False, dry_run: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if dry_run:
        payload = plan_payload(target)
        payload["dry_run"] = True
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        print(f"operator bootstrap dry-run: {target}")
        for row in payload["steps"]:
            print(f"[{row['action']}] {row['id']}: {row['path']}")
        return 0

    results: list[dict[str, Any]] = []
    for step in _steps(target):
        path = step["path"]
        if path.exists() and not force:
            results.append({"id": step["id"], "path": str(path), "status": "skipped", "reason": "already exists"})
            continue
        kwargs = dict(step["kwargs"])
        kwargs.update({"target": target, "force": force})
        output = StringIO()
        with redirect_stdout(output):
            rc = step["command"](**kwargs)
        results.append(
            {
                "id": step["id"],
                "path": str(path),
                "status": "written" if rc == 0 else "error",
                "return_code": rc,
                "output": output.getvalue().strip().splitlines(),
            }
        )
    payload = {"target": str(target), "results": results, "written_count": sum(1 for row in results if row["status"] == "written")}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if all(row.get("return_code", 0) == 0 for row in results if row["status"] != "skipped") else 1
    print(f"operator bootstrap: {target}")
    for row in results:
        print(f"[{row['status']}] {row['id']}: {row['path']}")
    return 0 if all(row.get("return_code", 0) == 0 for row in results if row["status"] != "skipped") else 1
