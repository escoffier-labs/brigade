from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .. import (
    chat_cmd,
    daily_cmd,
    dogfood_cmd,
    handoff_cmd,
    memory_cmd,
    repos_cmd,
    security_cmd,
    tools_cmd,
    work_cmd,
)

PROFILES = {"local-operator", "internal-dogfood"}


def guide_payload(*, profile: str = "internal-dogfood") -> dict[str, Any]:
    profile = _validate_profile(profile)
    return {
        "profile": profile,
        "purpose": "Use Brigade as an explicit repo-local operator loop before substantial work.",
        "startup_commands": [
            f"brigade operator status --profile {profile} --target .",
            "brigade daily status --target .",
            "brigade daily plan --target .",
        ],
        "safe_run_command": "brigade daily run --target .",
        "onboarding_command": f"brigade operator init --profile {profile} --target .",
        "tool_sync_command": "brigade operator sync-tools --target .",
        "handoff_expectations": [
            "Write a Memory Handoff when Brigade usage, setup, readiness waivers, or repo workflow changes.",
            "Include concrete commands, what changed, what remains manual, and any local-only setup assumption.",
            "Do not copy raw .brigade evidence into committed docs.",
        ],
        "boundaries": [
            "Brigade does not run automatically.",
            "Brigade does not start daemons. Templates may include an inactive hooks/pre-push file, but Brigade never activates hooks (git config core.hooksPath stays your call).",
            "Brigade does not send notifications unless a separate hook is explicitly configured.",
            "Brigade does not ingest handoffs into canonical memory from operator init/status.",
            "Brigade does not publish, push, tag, or mutate remotes.",
        ],
    }


def guide(*, profile: str = "internal-dogfood", json_output: bool = False) -> int:
    try:
        payload = guide_payload(profile=profile)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("operator guide")
    print(f"profile: {payload['profile']}")
    print(payload["purpose"])
    print("startup:")
    for command in payload["startup_commands"]:
        print(f"- {command}")
    print(f"safe_run: {payload['safe_run_command']}")
    print(f"onboard: {payload['onboarding_command']}")
    print(f"sync_tools: {payload['tool_sync_command']}")
    print("handoffs:")
    for item in payload["handoff_expectations"]:
        print(f"- {item}")
    print("boundaries:")
    for item in payload["boundaries"]:
        print(f"- {item}")
    return 0


def _steps(
    target: Path, *, profile: str = "local-operator", handoff_inboxes: list[str] | None = None
) -> list[dict[str, Any]]:
    steps = [
        {"id": "daily", "path": daily_cmd._config_path(target), "command": daily_cmd.init, "kwargs": {}},
        {
            "id": "handoff-sources",
            "path": handoff_cmd.default_sources_path(target),
            "command": handoff_cmd.sources_init,
            "kwargs": {"inboxes": handoff_inboxes} if handoff_inboxes is not None else {},
        },
        {
            "id": "work-backup",
            "path": work_cmd._backup_config_path(target),
            "command": work_cmd.backup_init,
            "kwargs": {"update_gitignore": False},
        },
        {
            "id": "work-scanners",
            "path": work_cmd._scanner_config_path(target),
            "command": work_cmd.scanners_init,
            "kwargs": {"update_gitignore": False},
        },
        {
            "id": "work-review",
            "path": work_cmd._review_config_path(target),
            "command": work_cmd.review_init,
            "kwargs": {"update_gitignore": False},
        },
        {
            "id": "chat-surfaces",
            "path": chat_cmd._config_path(target),
            "command": chat_cmd.surfaces_init,
            "kwargs": {"update_gitignore": False},
        },
        {
            "id": "memory-care",
            "path": memory_cmd.config_path(target),
            "command": memory_cmd.init,
            "kwargs": {"update_gitignore": False},
        },
        {
            "id": "repo-fleet",
            "path": repos_cmd.config_path(target),
            "command": repos_cmd.init,
            "kwargs": {"update_gitignore": False},
        },
        {"id": "security", "path": security_cmd.config_path(target), "command": security_cmd.init, "kwargs": {}},
        {
            "id": "tools",
            "path": tools_cmd.config_path(target),
            "command": tools_cmd.init,
            "kwargs": {"update_gitignore": False},
        },
    ]
    if profile == "internal-dogfood":
        steps.insert(
            1, {"id": "dogfood", "path": dogfood_cmd.config_path(target), "command": dogfood_cmd.init, "kwargs": {}}
        )
    return steps


def _validate_profile(profile: str) -> str:
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of: {', '.join(sorted(PROFILES))}")
    return profile


def plan_payload(
    target: Path, *, profile: str = "local-operator", handoff_inboxes: list[str] | None = None
) -> dict[str, Any]:
    target = target.expanduser().resolve()
    profile = _validate_profile(profile)
    steps = []
    for step in _steps(target, profile=profile, handoff_inboxes=handoff_inboxes):
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
        "profile": profile,
        "steps": steps,
        "missing_count": sum(1 for step in steps if step["action"] == "write"),
        "boundaries": [
            "Does not start services.",
            "Only the internal-dogfood profile refreshes read-only security evidence.",
            "Does not ingest handoffs.",
            "Does not write canonical memory.",
            "Does not publish, push, tag, or mutate remotes.",
        ],
    }


def plan(*, target: Path, profile: str = "local-operator", json_output: bool = False) -> int:
    try:
        payload = plan_payload(target, profile=profile)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"operator bootstrap plan: {payload['target']}")
    print(f"profile: {payload['profile']}")
    for row in payload["steps"]:
        print(f"[{row['action']}] {row['id']}: {row['path']}")
    return 0
