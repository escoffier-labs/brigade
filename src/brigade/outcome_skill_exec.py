"""Operator commands for the physical side of a skill reconcile decision.

``outcome reconcile --apply`` is routing only (owner decision on #2589, 2026-11-18).
It records status and the route policy marker and never installs, rolls back,
uninstalls or otherwise writes a skill file. Every local input to a physical
change (install receipts, rollback snapshot pointers and fingerprints, the
registry) is writable by the agent the ratchet gates, so a scheduled physical
change could install bytes that agent chose. Forgeable state may veto a change,
never authorize one.

This module only reads. For a promoted skill it lists the install commands for
copies that are missing or stale; for a demoted skill, the rollback commands for
copies that still exist. The operator runs them. A read failure yields no
commands, never an exception, so one artifact cannot abort the autonomous run.
"""

from __future__ import annotations

import importlib
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROUTING_ONLY = "routing-only"

# Rollback restores whatever the install receipt's snapshot pointer and
# fingerprint name, and both live in the workspace beside the agent the ratchet
# gates. Printed beside every suggested rollback so the operator looks first.
ROLLBACK_WARNING = (
    "warning: rollback inputs (the install receipt and its snapshot) live in the workspace and are "
    "not authenticated; inspect them before running the rollback"
)


def _install_command(target: Path, artifact_id: str, harness: str) -> str:
    return shlex.join(
        ["brigade", "skills", "install", artifact_id, "--target", harness, "--force", "--workspace", str(target)]
    )


def _rollback_command(target: Path, artifact_id: str, harness: str) -> str:
    return shlex.join(["brigade", "skills", "rollback", artifact_id, "--target", harness, "--workspace", str(target)])


def rollback_inspect_command(target: Path, artifact_id: str, harness: str) -> str:
    """Read-only command that shows the receipt, snapshot path and fingerprint a rollback would trust."""
    return shlex.join(
        ["brigade", "skills", "history", artifact_id, "--target", str(target), "--harness", harness, "--json"]
    )


def inspect_command_for(command: str) -> str | None:
    """The read-only inspection command that pairs with a suggested rollback command, else ``None``."""
    try:
        words = shlex.split(command)
        if words[:3] != ["brigade", "skills", "rollback"]:
            return None
        return rollback_inspect_command(
            Path(words[words.index("--workspace") + 1]), words[3], words[words.index("--target") + 1]
        )
    except (ValueError, IndexError):
        return None


def _stale_harnesses(artifact_id: str, plan: list[dict[str, Any]]) -> list[str]:
    """Harnesses whose copy is missing or differs from the registry, from the read-only sync plan.

    A hand-edited copy is not listed: ``--force`` alone would refuse it, and the
    operator reviews it through the skills status and diff commands instead.
    """
    return [
        str(row["harness"])
        for row in plan
        if row.get("skill_id") == artifact_id and row.get("state") in {"missing", "changed"}
    ]


def _installed_harnesses(target: Path, artifact_id: str) -> list[str]:
    """Harnesses that still have a copy of the skill, judged read-only."""
    install_mod = importlib.import_module("brigade.skills_cmd.install")

    return [
        harness
        for harness in install_mod._install_targets(target)
        if install_mod.installed_local_edit(target, artifact_id, harness) != "missing"
    ]


def _read_plan(target: Path) -> list[dict[str, Any]]:
    sync_history = importlib.import_module("brigade.skills_cmd.sync_history")
    items, error = sync_history._sync_plan(workspace=target, harness="all", trust="unreviewed")
    return [] if error else items


def pending_commands(
    target: Path, artifact_id: str, status: str, *, plan: list[dict[str, Any]] | None = None
) -> list[str]:
    """Exact operator commands that would apply this skill's status physically.

    Computed from current drift and never executed. ``plan`` lets a caller that
    asks about several skills read the registry once.
    """
    try:
        if status == "promoted":
            rows = _read_plan(target) if plan is None else plan
            return [_install_command(target, artifact_id, h) for h in _stale_harnesses(artifact_id, rows)]
        if status == "demoted":
            return [_rollback_command(target, artifact_id, h) for h in _installed_harnesses(target, artifact_id)]
    except Exception:  # noqa: BLE001 - a read failure must never abort the autonomous run
        return []
    return []


def pending_by_artifact(
    target: Path, status_map: Mapping[str, Mapping[str, Any]], kinds: Mapping[str, str]
) -> dict[str, list[str]]:
    """Pending operator commands for every skill currently promoted or demoted."""
    wanted = {
        artifact_id: str(entry.get("status"))
        for artifact_id, entry in status_map.items()
        if kinds.get(artifact_id, "skill") == "skill" and entry.get("status") in {"promoted", "demoted"}
    }
    if not wanted:
        return {}
    plan: list[dict[str, Any]] | None = None
    if "promoted" in wanted.values():
        try:
            plan = _read_plan(target)
        except Exception:  # noqa: BLE001 - see pending_commands
            plan = []
    result = {
        artifact_id: pending_commands(target, artifact_id, status, plan=plan) for artifact_id, status in wanted.items()
    }
    return {artifact_id: commands for artifact_id, commands in result.items() if commands}
