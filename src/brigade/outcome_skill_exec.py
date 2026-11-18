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
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import localio

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


@dataclass(frozen=True)
class PendingAction:
    """Commands for one skill, with the status they apply and when that status was set."""

    status: str
    changed_at: str | None
    commands: list[str]
    current_run: bool = False


@dataclass(frozen=True)
class PendingPhysical:
    """Pending operator commands: ``recent`` is listed as pending, ``stale`` is old rollback history.

    ``stale_actions`` counts every stale command, so a bounded ``stale`` list never understates it.
    """

    recent: dict[str, PendingAction] = field(default_factory=dict)
    stale: dict[str, PendingAction] = field(default_factory=dict)
    stale_actions: int = 0


def age_label(status: str, changed_at: Any, now: datetime) -> str:
    """Human age of a status, such as ``demoted 3 days ago``; ``unknown age`` without a timestamp."""
    changed = localio.parse_iso_datetime(changed_at)
    if changed is None:
        return f"{status}, unknown age"
    days = max(0, (now - changed).days)
    if days == 0:
        return f"{status} today"
    return f"{status} {days} day{'' if days == 1 else 's'} ago"


def sort_pending(rows: Mapping[str, PendingAction]) -> list[tuple[str, PendingAction]]:
    """Order rows current run first, then newest status change (unreadable last), then artifact id."""

    def key(item: tuple[str, PendingAction]) -> tuple[int, float, str]:
        changed = localio.parse_iso_datetime(item[1].changed_at)
        return (0 if item[1].current_run else 1, -changed.timestamp() if changed else float("inf"), item[0])

    return sorted(rows.items(), key=key)


def pending_by_artifact(
    target: Path,
    status_map: Mapping[str, Mapping[str, Any]],
    kinds: Mapping[str, str],
    *,
    now: datetime,
    window_days: int,
    current_run_ids: Collection[str] = (),
) -> PendingPhysical:
    """Pending operator commands for every skill currently promoted or demoted.

    Install commands for a promoted skill come from current drift, not history, so
    they are always listed. Rollback commands for a demoted skill are listed only
    when its ``last_action_ts`` is within ``window_days`` of ``now`` (exact time, not
    whole days) or it was decided in this run (``current_run_ids``); a missing or
    unreadable timestamp cannot prove recency. Older rollbacks are kept in ``stale``
    and counted in ``stale_actions``, so a demotion that predates routing-only
    reconcile never reads as an action to take but stays reachable.
    """
    wanted = {
        artifact_id: str(entry.get("status"))
        for artifact_id, entry in status_map.items()
        if kinds.get(artifact_id, "skill") == "skill" and entry.get("status") in {"promoted", "demoted"}
    }
    if not wanted:
        return PendingPhysical()
    plan: list[dict[str, Any]] | None = None
    if "promoted" in wanted.values():
        try:
            plan = _read_plan(target)
        except Exception:  # noqa: BLE001 - see pending_commands
            plan = []
    window = timedelta(days=window_days)
    recent: dict[str, PendingAction] = {}
    stale: dict[str, PendingAction] = {}
    stale_actions = 0
    for artifact_id, status in wanted.items():
        commands = pending_commands(target, artifact_id, status, plan=plan)
        if not commands:
            continue
        changed_at = status_map[artifact_id].get("last_action_ts")
        changed = localio.parse_iso_datetime(changed_at)
        in_run = artifact_id in current_run_ids
        action = PendingAction(status, changed_at if isinstance(changed_at, str) else None, commands, in_run)
        if status == "promoted" or in_run or (changed is not None and now - changed <= window):
            recent[artifact_id] = action
        else:
            stale[artifact_id] = action
            stale_actions += len(commands)
    return PendingPhysical(recent=recent, stale=stale, stale_actions=stale_actions)
