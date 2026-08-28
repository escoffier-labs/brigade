"""Claude Code local/background session inventory parser (#Task3).

This module only parses the local ``claude agents --json --all`` output, which is
a local/background session inventory, not a documented cloud-task registry. It
does not call ``claude --cloud``, undocumented HTTP endpoints, or any browser/auth
surface. Launch is disabled-by-policy until a structured bindable Claude Cloud
provider surface exists.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LaunchResult:
    ok: bool
    agent_id: str | None = None
    reason: str | None = None


def parse_agents(stdout: str) -> list[dict[str, Any]]:
    """Parse the JSON array printed by ``claude agents --json --all``.

    This is a local/background session inventory diagnostic, not a cloud-task
    authority. Claude Code 2.1.246 uses ``sessionId`` as the canonical identifier
    and ``cwd`` as the workspace. The parser prefers those fields and falls back
    to ``id`` / ``workspace`` / ``status`` for older output.
    """
    text = stdout.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    agents: list[dict[str, Any]] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        agent_id = raw.get("sessionId") if isinstance(raw.get("sessionId"), str) else raw.get("id")
        if not isinstance(agent_id, str):
            continue
        workspace = raw.get("cwd") if isinstance(raw.get("cwd"), str) else raw.get("workspace")
        status = raw.get("status")
        agents.append(
            {
                "id": agent_id,
                "name": raw.get("name") if isinstance(raw.get("name"), str) else None,
                "state": status.lower().strip() if isinstance(status, str) else None,
                "workspace": workspace if isinstance(workspace, str) else None,
            }
        )
    return agents


def list_agents(timeout: float = 30.0) -> list[dict[str, Any]]:
    """Run ``claude agents --json --all`` and return sanitized local rows.

    This is a local/background session diagnostic. The rows are not merged into
    cloud provider_tasks or Cloud Workers because Claude Code does not expose a
    structured bindable cloud provider surface.
    """
    try:
        completed = subprocess.run(
            ["claude", "agents", "--json", "--all"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return []
    code = getattr(completed, "returncode", getattr(completed, "code", None))
    if code != 0:
        return []
    return parse_agents(completed.stdout)


def launch_agent(repo: str, prompt: str) -> LaunchResult:
    """Launch is disabled-by-policy in this slice; no live Claude call is made."""
    del repo, prompt
    return LaunchResult(ok=False, reason="disabled-by-policy")
