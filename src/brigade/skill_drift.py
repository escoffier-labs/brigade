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
  but only when its receipt proves it was never edited (``local_edit`` is
  ``current``, so a legacy receipt that cannot vouch for a nested ``skill.json``
  never qualifies) and its SKILL.md is a recorded release text. Exit code 0 from
  the install is not proof, so the refresh counts only when its JSON result says
  the copy was installed.

Owner policy (2026-11-21): a copy byte-identical to a released version is
refreshed automatically. Every other drifted or locally edited copy is only
flagged and never written. An edited copy is flagged now. Filing an inbox proposal
for it is a later slice of #2590, so nothing is filed today. Copies that cannot be
compared are counted as skipped, not flagged: a link, a missing copy, a copy with
no receipt for a skill Brigade does not bundle, and every copy outside the
workspace. Hermes copies live under ``$HERMES_HOME``, outside the workspace, so
they are external and left to the Hermes install. Report paths are always
workspace-relative, never an absolute home path.

The result is a small bounded mapping. ``outcome_ratchet_report.merge_skill_drift``
stores it in the ratchet report, which the session brief and the dashboard read.
Counts are complete. The flagged list is capped and says how many rows it dropped.
"""

from __future__ import annotations

import contextlib
import io
import json
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


def _workspace_parts(target: Path, row: dict[str, Any]) -> tuple[str, ...] | None:
    """Workspace-relative parts of a copy's path, or None when it is not a workspace copy.

    Hermes copies live under ``$HERMES_HOME`` and any other path outside the workspace
    is external: this scan neither refreshes it nor reports its absolute path.
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


def _quiet(call: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a command that prints for operators and return its result; its stdout is not ours."""
    with contextlib.redirect_stdout(io.StringIO()):
        return call(*args, **kwargs)


def _is_release_text(skill_id: str, installed: bytes | None) -> bool:
    """True when the installed SKILL.md is a text some Brigade release shipped for this skill."""
    return installed is not None and shipped_history.skill_digest(installed) in shipped_history.digests(
        shipped_history.skill_key(skill_id)
    )


def _installed_by_install(output: str, harness: str) -> bool:
    """True when ``skills install --json`` says it wrote this harness's copy."""
    try:
        payload = json.loads(output)
    except ValueError:
        return False
    if not isinstance(payload, dict) or payload.get("installed") is not True:
        return False
    receipt = payload.get("receipt")
    receipts = receipt.get("receipts") if isinstance(receipt, dict) else None
    return isinstance(receipts, list) and any(
        isinstance(item, dict) and item.get("target") == harness for item in receipts
    )


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

    def flag(row: dict[str, Any], rel_parts: tuple[str, ...], reason: str) -> None:
        counts["flagged"] += 1
        flagged.append(
            {
                "skill_id": str(row["skill_id"]),
                "harness": str(row["harness"]),
                "path": "/".join(rel_parts),
                "reason": reason,
            }
        )

    try:
        for row in _fleet_status_payload(target)["copies"]:
            status = row["status"]
            skill_id = str(row["skill_id"])
            rel_parts = _workspace_parts(target, row)
            if rel_parts is None:
                counts["skipped"] += 1
                continue
            if status == "current":
                counts["current"] += 1
                continue
            if status in _SKIPPED_STATES:
                counts["skipped"] += 1
                continue
            source = row.get("source") if isinstance(row.get("source"), dict) else {}
            drift = row.get("drift") if isinstance(row.get("drift"), dict) else {}
            is_bundled = skill_id in bundled
            if status == "stale" and source.get("kind") == _BUNDLED_KIND and row.get("update_command"):
                # ``install --force`` refuses only a proven edit, and a receipt that cannot vouch for
                # the copy reads ``unknown``. Refresh only a copy the receipt proves unedited whose
                # SKILL.md is a recorded release text. Anything else is flagged and kept.
                installed_path = target.joinpath(*rel_parts)
                if drift.get("local_edit") != "current" or not _is_release_text(
                    skill_id, _read_installed(installed_path)
                ):
                    flag(row, rel_parts, "stale, but not provably an unedited release copy, copy kept")
                    continue
                if not refresh:
                    counts["refreshable"] += 1
                    continue
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    rc = skills_install(
                        workspace=target,
                        skill=f"bundled:{skill_id}",
                        harness=row["harness"],
                        force=True,
                        json_output=True,
                    )
                if rc == 0 and _installed_by_install(output.getvalue(), str(row["harness"])):
                    counts["refreshed"] += 1
                else:
                    flag(row, rel_parts, "refresh refused, copy kept")
                continue
            # Only a copy with no receipt whose bundled source linted is a wired copy. A row that
            # failed to lint (no ``supported`` verdict) is also ``unknown`` and must not be judged here.
            receiptless = not drift.get("receipt_known") and row.get("supported") is not None
            if status == "unknown" and is_bundled and receiptless:
                _check_wired_copy(target, row, rel_parts, wirer, refresh=refresh, counts=counts, flag=flag)
                continue
            if status == "unknown":
                counts["skipped"] += 1
                continue
            if status == "edited":
                flag(row, rel_parts, "locally edited, copy kept")
            else:
                flag(row, rel_parts, "drifted from its source, copy kept")
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
    rel_parts: tuple[str, ...],
    wirer: skill_wiring.SkillWirer,
    *,
    refresh: bool,
    counts: dict[str, int],
    flag: Any,
) -> None:
    """Judge one bundled copy that has no install receipt, the way init wired it."""
    skill_id = str(row["skill_id"])
    installed_path = target.joinpath(*rel_parts)
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
        flag(row, rel_parts, "differs from the bundled text and every past release, copy kept")
    elif not refresh:
        counts["refreshable"] += 1
    elif _read_installed(installed_path) is None:
        # ``place`` would create a missing SKILL.md. A refresh never re-creates a copy that vanished.
        counts["skipped"] += 1
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
        elif state == skill_wiring.WRITTEN:
            flag(row, rel_parts, "copy vanished during the refresh and the bundled text was written")
        else:
            flag(row, rel_parts, f"refresh did not apply ({state}), copy kept")


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
