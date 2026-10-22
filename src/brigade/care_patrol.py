"""Optional run-patrol catalog entry and pinned report-only runbook."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import localio, runbook_cmd

if TYPE_CHECKING:
    from .care_cmd import CareEntry

RUNBOOK_REL = ".brigade/runbooks/run-patrol.json"
RUNBOOK_PAYLOAD = {
    "id": "run-patrol",
    "description": "Inspect local runs and retain a patrol receipt.",
    "allowed_commands": ["brigade"],
    "steps": [{"id": "patrol", "run": "brigade runs patrol --target . --json --receipt --fail-on stalled,zombie,dead"}],
}


def entry(entry_type: type[CareEntry]) -> CareEntry:
    return entry_type(
        entry_id="run-patrol",
        schedule="*/10 * * * *",
        on_calendar="*:0/10",
        description="Brigade run patrol",
        kind="runbook",
        runbook_rel=RUNBOOK_REL,
        runbook_id="run-patrol",
    )


def select(
    entries: tuple[CareEntry, ...], entry_ids: list[str] | None, backend: str, entry_type: type[CareEntry]
) -> tuple[CareEntry, ...]:
    """Crontab includes patrol in its one managed block, other backends opt in."""
    if backend == "crontab" and not entry_ids and not any(item.entry_id == "run-patrol" for item in entries):
        return (*entries, entry(entry_type))
    return entries


def ensure_selected(target: Path, entries: tuple[CareEntry, ...]) -> int:
    if any(item.entry_id == "run-patrol" for item in entries):
        return write_runbook(target, RUNBOOK_REL, RUNBOOK_PAYLOAD)
    return 0


def write_runbook(target: Path, relative: str, payload: dict[str, Any]) -> int:
    """Materialize a validated, binary-pinned runbook only when missing."""
    dest = target / relative
    if dest.is_file():
        return 0
    temp = dest.parent / f".{dest.name}.init-tmp"
    try:
        temp.parent.mkdir(parents=True, exist_ok=True)
        localio.write_json(temp, dict(payload))
        validated, error = runbook_cmd._read_runbook(temp, require_pin_hashes=False)
        if validated is None:
            print(f"error: {error}", file=sys.stderr)
            return 2
        pins, error = runbook_cmd._pin_payload_from_runbook(target, validated)
        if pins is None:
            print(f"error: {error}", file=sys.stderr)
            return 2
        validated["pins"] = pins
        localio.write_json(dest, validated)
        return 0
    finally:
        if temp.is_file():
            temp.unlink()


def receipt_fields(target: Path, entry: CareEntry, last_runbook: dict[str, Any] | None) -> dict[str, Any]:
    """Expose direct patrol receipts alongside the scheduler's runbook receipt."""
    if entry.entry_id != "run-patrol":
        return {}
    from .runs_patrol_receipts import read_receipt

    root = target / ".brigade/patrol"
    latest: tuple[str, dict[str, Any]] | None = None
    if not root.is_symlink() and not root.parent.is_symlink() and root.is_dir():
        # IDs sort by creation time. Bound parsing even with a large fence archive.
        for directory in sorted(root.iterdir(), reverse=True)[:10]:
            if directory.is_symlink() or not directory.is_dir():
                continue
            path = directory / "patrol.json"
            try:
                # The writer's 4 MiB contract applies, not the generic 1 MiB cap.
                payload = read_receipt(directory)
                if payload is None:
                    continue
                observed = str(payload.get("observed_at") or "")
                if latest is None or observed > latest[0]:
                    latest = (
                        observed,
                        {
                            "patrol_id": payload.get("patrol_id"),
                            "observed_at": observed,
                            "exit_code": payload.get("exit_code"),
                            "receipt_path": str(path),
                        },
                    )
                    break
            except (OSError, ValueError):
                continue
    summary = latest[1] if latest else None
    result: dict[str, Any] = {"patrol_receipt": summary}
    if last_runbook is None and summary:
        result["last_receipt"] = {
            "run_id": summary["patrol_id"],
            "status": "completed" if summary["exit_code"] == 0 else "failed",
            "started_at": summary["observed_at"],
            "completed_at": summary["observed_at"],
            "receipt_path": summary["receipt_path"],
        }
    return result
