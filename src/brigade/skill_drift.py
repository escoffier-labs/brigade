"""Scheduled, read-only drift check for installed bundled-skill copies (#2590).

``brigade skills fleet status`` finds drifted copies, but only when someone runs
it. This module is the unattended pass the weekly care runbook runs after the
ratchet. It compares nothing itself. It reuses the existing rules:

* ``skills fleet status`` (``_fleet_status_payload``) discovers every installed
  copy and says which are current, stale (receipt-known, never edited) or edited.
* A copy init wired without a receipt reads ``unknown`` there, so a bundled one is
  judged by ``skill_wiring.classify_copy``: identical to the bundled text is
  current, byte-identical to a text a past release shipped (#2716) is refreshable,
  and anything else is a local edit.

The step is strictly read-only. It never installs, places, replaces, quarantines
or deletes anything under a skill directory, so a workspace file can never
authorize a write. Each copy lands in one of four buckets:

* ``current``: matches the source.
* ``refreshable``: byte-identical to a recorded release, so an operator can safely
  update it. The row carries the exact command to run, which is the existing
  ``skills install --force`` path (``brigade init`` refreshes a wired copy the
  same way).
* ``flagged``: edited, unknown, receiptless, linked (a symlink, or a SKILL.md with
  more than one hard link), missing, or anything else that is not provably a
  released copy.
* ``skipped``: a copy outside the workspace. Hermes copies live under
  ``$HERMES_HOME``, so they are external and left to the Hermes install.

Unattended refresh is deferred: a receipt under the workspace cannot prove who
wrote it, so it cannot authorize replacing a skill directory. Refresh stays with
``brigade init`` and ``brigade skills install``. Filing an inbox proposal for an
edited copy is a later slice of #2590, so nothing is filed today. Report paths are
always normalized workspace-relative POSIX paths, never an absolute home path.

The result is a small bounded mapping. ``outcome_ratchet_report.merge_skill_drift``
stores it in the ratchet report, which the session brief and the dashboard read.
Counts are complete. The listed rows are capped and the result says how many it dropped.
"""

from __future__ import annotations

import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import localio, shipped_history, skill_wiring
from .outcome_ratchet_report import DRIFT_MAX_ROWS, DRIFT_VERSION, refresh_command
from .templates import template_root

_BUNDLED_KIND = "brigade-bundle"


def _bundled_ids() -> set[str]:
    root = template_root() / "skills"
    if not root.is_dir():
        return set()
    return {path.name for path in root.iterdir() if (path / "SKILL.md").is_file()}


def _workspace_parts(target: Path, row: dict[str, Any]) -> tuple[str, ...] | None:
    """Workspace-relative parts of a copy's path, or None when it is not a workspace copy.

    Hermes copies live under ``$HERMES_HOME`` and any other path outside the workspace
    is external: this scan neither inspects it nor reports its absolute path.
    """
    if row.get("harness") == "hermes":
        return None
    try:
        parts = Path(str(row["installed_path"])).relative_to(target).parts
    except ValueError:
        return None
    return parts or None


def _read_installed(path: Path) -> bytes | None:
    try:
        return shipped_history.read_regular_file(path / "SKILL.md", shipped_history.MAX_INSTALLED_BYTES)
    except OSError:
        return None


def _is_release_text(skill_id: str, installed: bytes | None) -> bool:
    """True when the installed SKILL.md is a text some Brigade release shipped for this skill."""
    return installed is not None and shipped_history.skill_digest(installed) in shipped_history.digests(
        shipped_history.skill_key(skill_id)
    )


def _link_in_path(target: Path, rel_parts: tuple[str, ...]) -> bool:
    """True when any component of the copy's path below the workspace is a symlink."""
    path = target
    for part in rel_parts:
        path = path / part
        try:
            if stat.S_ISLNK(path.lstat().st_mode):
                return True
        except OSError:
            return False
    return False


def _linked(skill_md: Path) -> bool:
    """True when SKILL.md is a symlink or a regular file with more than one hard link."""
    try:
        info = skill_md.lstat()
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1)


def run(target: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Classify every installed copy without writing anything.

    ``refreshable`` copies are listed with the command that would update them. This
    function never calls an installer, a placer or any other writer.
    """
    from .skills_cmd import _fleet_status_payload

    target = target.expanduser().resolve()
    bundled = _bundled_ids()
    counts = {"current": 0, "refreshable": 0, "flagged": 0, "skipped": 0}
    flagged: list[dict[str, str]] = []
    refreshable: list[dict[str, str]] = []

    def entry(row: dict[str, Any], rel_parts: tuple[str, ...]) -> dict[str, str]:
        return {"skill_id": str(row["skill_id"]), "harness": str(row["harness"]), "path": "/".join(rel_parts)}

    def flag(row: dict[str, Any], rel_parts: tuple[str, ...], reason: str) -> None:
        counts["flagged"] += 1
        flagged.append({**entry(row, rel_parts), "reason": reason})

    def can_refresh(row: dict[str, Any], rel_parts: tuple[str, ...]) -> None:
        counts["refreshable"] += 1
        refreshable.append({**entry(row, rel_parts), "command": refresh_command(row["skill_id"], row["harness"])})

    for row in _fleet_status_payload(target)["copies"]:
        status = row["status"]
        skill_id = str(row["skill_id"])
        rel_parts = _workspace_parts(target, row)
        if rel_parts is None:
            counts["skipped"] += 1
            continue
        installed_path = target.joinpath(*rel_parts)
        if status in {"symlink", "unusable"} or _link_in_path(target, rel_parts):
            # A linked, dangling or otherwise non-plain copy directory. Nothing is read through it.
            flag(row, rel_parts, "linked or non-plain copy directory, left alone")
            continue
        if status == "missing":
            flag(row, rel_parts, "copy is missing")
            continue
        if _linked(installed_path / "SKILL.md"):
            flag(row, rel_parts, "SKILL.md is linked, left alone")
            continue
        if status == "current":
            counts["current"] += 1
            continue
        source = row.get("source") if isinstance(row.get("source"), dict) else {}
        drift = row.get("drift") if isinstance(row.get("drift"), dict) else {}
        if status == "stale" and source.get("kind") == _BUNDLED_KIND and row.get("update_command"):
            # A receipt that cannot vouch for the copy never qualifies: only a copy the receipt proves
            # unedited whose SKILL.md is a recorded release text is safe for an operator to update.
            if drift.get("local_edit") == "current" and _is_release_text(skill_id, _read_installed(installed_path)):
                can_refresh(row, rel_parts)
            else:
                flag(row, rel_parts, "stale, but not provably an unedited release copy")
            continue
        # Only a copy with no receipt whose bundled source linted is a wired copy. A row that
        # failed to lint (no ``supported`` verdict) is also ``unknown`` and must not be judged here.
        receiptless = not drift.get("receipt_known") and row.get("supported") is not None
        if status == "unknown" and skill_id in bundled and receiptless:
            _check_wired_copy(row, rel_parts, installed_path, current=counts, flag=flag, can_refresh=can_refresh)
        elif status == "unknown":
            flag(row, rel_parts, "unknown, no install receipt to compare against")
        elif status == "edited":
            flag(row, rel_parts, "locally edited")
        else:
            flag(row, rel_parts, "drifted from its source")

    flagged.sort(key=lambda item: (item["skill_id"], item["harness"], item["path"]))
    refreshable.sort(key=lambda item: (item["skill_id"], item["harness"], item["path"]))
    return {
        "version": DRIFT_VERSION,
        "generated_at": (now or localio.utc_now()).isoformat(),
        "counts": counts,
        "flagged": flagged[:DRIFT_MAX_ROWS],
        "flagged_omitted": max(0, len(flagged) - DRIFT_MAX_ROWS),
        "refreshable": refreshable[:DRIFT_MAX_ROWS],
        "refreshable_omitted": max(0, len(refreshable) - DRIFT_MAX_ROWS),
    }


def _check_wired_copy(
    row: dict[str, Any],
    rel_parts: tuple[str, ...],
    installed_path: Path,
    *,
    current: dict[str, int],
    flag: Any,
    can_refresh: Any,
) -> None:
    """Judge one bundled copy that has no install receipt, the way init wired it."""
    skill_id = str(row["skill_id"])
    bundled_text = (template_root() / "skills" / skill_id / "SKILL.md").read_bytes()
    installed = _read_installed(installed_path)
    if installed is None:
        flag(row, rel_parts, "SKILL.md could not be read as a plain file")
        return
    verdict = skill_wiring.classify_copy(skill_id, installed, bundled_text)
    if verdict == skill_wiring.IDENTICAL:
        current["current"] += 1
    elif verdict == skill_wiring.REFRESHABLE:
        can_refresh(row, rel_parts)
    else:
        flag(row, rel_parts, "differs from the bundled text and every past release")


def text_lines(result: dict[str, Any]) -> list[str]:
    """Operator-facing lines for ``skills fleet drift``."""
    counts = result["counts"]
    lines = [
        f"skills fleet drift (read-only): current={counts['current']} refreshable={counts['refreshable']} "
        f"flagged={counts['flagged']} skipped={counts['skipped']}"
    ]
    for row in result["flagged"]:
        lines.append(f"- flagged: {row['skill_id']} [{row['harness']}] {row['path']}: {row['reason']}")
    if result["flagged_omitted"]:
        lines.append(f"... {result['flagged_omitted']} more flagged row(s) not listed")
    for row in result["refreshable"]:
        lines.append(f"- refreshable: {row['skill_id']} [{row['harness']}] {row['path']}: run `{row['command']}`")
    if result["refreshable_omitted"]:
        lines.append(f"... {result['refreshable_omitted']} more refreshable row(s) not listed")
    return lines


def print_result(result: dict[str, Any], *, json_output: bool) -> None:
    import json

    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    print(*text_lines(result), sep="\n", file=sys.stdout)
