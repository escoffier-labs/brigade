"""Scheduled drift check for installed bundled-skill copies (#2590).

``brigade skills fleet status`` finds drifted copies, but only when someone runs
it. This module is the unattended pass the weekly care runbook runs after the
ratchet. It compares nothing itself. It reuses three existing rules:

* ``skills fleet status`` (``_fleet_status_payload``) discovers every installed
  copy and says which are current, stale (receipt-known, never edited) or edited.
* A copy init wired without a receipt reads ``unknown`` there, so a bundled one is
  judged by ``skill_wiring.classify_copy``: identical to the bundled text is
  current, byte-identical to a text a past release shipped (#2716) is refreshed
  through ``SkillWirer.place``, and anything else is a local edit.
* A stale copy of a bundled skill is refreshed through ``skills install --force``,
  which keeps its own local-edit guard (#2697).

Owner policy (2026-11-21): a copy byte-identical to a released version is
refreshed automatically. Every other drifted or locally edited copy is only
flagged and never written. Copies that cannot be compared (a link, a path outside
the workspace, a missing copy, a copy with no receipt for a skill Brigade does not
bundle) are counted as skipped, not flagged.

The result is a small bounded mapping. ``outcome_ratchet_report.merge_skill_drift``
stores it in the ratchet report, which the session brief and the dashboard read.
Counts are complete. The flagged list is capped and says how many rows it dropped.
"""

from __future__ import annotations

import contextlib
import io
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import localio, shipped_history, skill_wiring
from .templates import template_root

DRIFT_VERSION = 1
FLAGGED_MAX_ROWS = 20
_BUNDLED_KIND = "brigade-bundle"
_SKIPPED_STATES = {"symlink", "external", "unusable", "unsupported", "missing"}


def _bundled_ids() -> set[str]:
    root = template_root() / "skills"
    if not root.is_dir():
        return set()
    return {path.name for path in root.iterdir() if (path / "SKILL.md").is_file()}


def _display_path(target: Path, raw: object) -> str:
    """Workspace-relative path when the copy lives in the workspace, else the path as recorded."""
    path = Path(str(raw))
    try:
        return path.relative_to(target).as_posix()
    except ValueError:
        return str(path)


def _read_installed(path: Path) -> bytes | None:
    try:
        return shipped_history.read_regular_file(path / "SKILL.md", shipped_history.MAX_INSTALLED_BYTES)
    except OSError:
        return None


def _quiet(call: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a command that prints for operators and return its result; its stdout is not ours."""
    with contextlib.redirect_stdout(io.StringIO()):
        return call(*args, **kwargs)


def run(target: Path, *, refresh: bool, now: datetime | None = None) -> dict[str, Any]:
    """Check every installed copy and, with ``refresh``, replace the released-identical ones.

    Without ``refresh`` nothing is written and a copy that would be replaced is
    counted as ``refreshable``. A refresh that is refused or loses a race leaves
    the copy as it was and lists it as flagged.
    """
    from .skills_cmd import _fleet_status_payload
    from .skills_cmd import install as skills_install

    target = target.expanduser().resolve()
    bundled = _bundled_ids()
    counts = {"current": 0, "refreshed": 0, "refreshable": 0, "flagged": 0, "skipped": 0}
    flagged: list[dict[str, str]] = []
    wirer = skill_wiring.SkillWirer(target)

    def flag(row: dict[str, Any], reason: str) -> None:
        counts["flagged"] += 1
        flagged.append(
            {
                "skill_id": str(row["skill_id"]),
                "harness": str(row["harness"]),
                "path": _display_path(target, row["installed_path"]),
                "reason": reason,
            }
        )

    try:
        for row in _fleet_status_payload(target)["copies"]:
            status = row["status"]
            skill_id = str(row["skill_id"])
            if status == "current":
                counts["current"] += 1
                continue
            if status in _SKIPPED_STATES:
                counts["skipped"] += 1
                continue
            source = row.get("source") if isinstance(row.get("source"), dict) else {}
            is_bundled = skill_id in bundled
            if status == "stale" and source.get("kind") == _BUNDLED_KIND and row.get("update_command"):
                # Receipt-known and never edited: the copy is what a past Brigade installed.
                if not refresh:
                    counts["refreshable"] += 1
                    continue
                rc = _quiet(
                    skills_install, workspace=target, skill=f"bundled:{skill_id}", harness=row["harness"], force=True
                )
                if rc == 0:
                    counts["refreshed"] += 1
                else:
                    flag(row, "refresh refused, copy kept")
                continue
            if status == "unknown" and is_bundled:
                _check_wired_copy(target, row, skill_id, wirer, refresh=refresh, counts=counts, flag=flag)
                continue
            if status == "unknown":
                counts["skipped"] += 1
                continue
            if status == "edited":
                flag(row, "locally edited, copy kept")
            else:
                flag(row, "drifted from its source, copy kept")
    finally:
        wirer.finish()

    omitted = max(0, len(flagged) - FLAGGED_MAX_ROWS)
    flagged.sort(key=lambda item: (item["skill_id"], item["harness"], item["path"]))
    return {
        "version": DRIFT_VERSION,
        "generated_at": (now or localio.utc_now()).isoformat(),
        "mode": "refresh" if refresh else "check",
        "counts": counts,
        "flagged": flagged[:FLAGGED_MAX_ROWS],
        "flagged_omitted": omitted,
    }


def _check_wired_copy(
    target: Path,
    row: dict[str, Any],
    skill_id: str,
    wirer: skill_wiring.SkillWirer,
    *,
    refresh: bool,
    counts: dict[str, int],
    flag: Any,
) -> None:
    """Judge one bundled copy that has no install receipt, the way init wired it."""
    installed_path = Path(str(row["installed_path"]))
    try:
        rel_parts = installed_path.relative_to(target).parts
    except ValueError:
        counts["skipped"] += 1
        return
    template_dir = template_root() / "skills" / skill_id
    current = (template_dir / "SKILL.md").read_text(encoding="utf-8")
    installed = _read_installed(installed_path)
    if installed is None:
        counts["skipped"] += 1
        return
    verdict = skill_wiring.classify_copy(skill_id, installed, current.encode("utf-8"))
    if verdict == skill_wiring.IDENTICAL:
        counts["current"] += 1
    elif verdict == skill_wiring.EDITED:
        flag(row, "differs from the bundled text and every past release, copy kept")
    elif not refresh:
        counts["refreshable"] += 1
    else:
        # place() re-reads the copy under a no-follow descriptor and keeps it if it changed.
        state = _quiet(
            wirer.place,
            (*rel_parts, "SKILL.md"),
            template_dir,
            current,
            skill_id=skill_id,
            harness=str(row["harness"]),
        )
        if state == skill_wiring.REFRESHED:
            counts["refreshed"] += 1
        else:
            flag(row, f"refresh did not apply ({state}), copy kept")


def text_lines(result: dict[str, Any]) -> list[str]:
    """Operator-facing lines for ``skills fleet drift``."""
    counts = result["counts"]
    lines = [
        f"skills fleet drift ({result['mode']}): current={counts['current']} refreshed={counts['refreshed']} "
        f"refreshable={counts['refreshable']} flagged={counts['flagged']} skipped={counts['skipped']}"
    ]
    for row in result["flagged"]:
        lines.append(f"- {row['skill_id']} [{row['harness']}] {row['path']}: {row['reason']}")
    if result["flagged_omitted"]:
        lines.append(f"... {result['flagged_omitted']} more flagged row(s) not listed")
    return lines


def print_result(result: dict[str, Any], *, json_output: bool) -> None:
    import json

    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    print(*text_lines(result), sep="\n", file=sys.stdout)
