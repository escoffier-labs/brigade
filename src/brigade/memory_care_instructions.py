"""Report on the always-loaded instruction files: state per file, alert lines, the tend stage.

Slice 1 measures and reports. It moves nothing, so a file over its limit is
`could-not-trim` with reason `report-only`, and the band between act-at and the
limit (`over-act-at`) raises no alert: appends already go to overflow cards there.

Every reader of the inventory (the tend stage, doctor, `budgets check` and the work
brief) goes through this module. None of them may raise or block on a bad
configuration, and none prints a full home path or any file content: rows carry
display paths, and free text that came from a configuration file is scrubbed
before it is printed (`safe_text`).

A configuration change that weakens protection stays visible to the next agent:
rejected overrides, clamped limits, excluded files, keys that move every limit and an
unreadable OpenClaw configuration are all listed in the report, and the brief prints
one `config-changed` line while any of them holds.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from .bounded_read import read_regular_file
from .instruction_inventory import (
    INSPECTION_CEILING,
    InstructionFile,
    InstructionsConfig,
    Inventory,
    discover,
    load_instructions_config,
)

NEXT_DRY_RUN = "brigade memory care tend --stage instructions --dry-run --json"
OVERFLOW_GLOB = "memory/cards/instructions/{key}--overflow-*.md"
_NEXT = f'next="{NEXT_DRY_RUN}"'
_DETAIL_MAX = 200


def safe_text(text: str, *, home: Path | None = None, target: Path | None = None) -> str:
    """One printable line with the home and target directories shortened.

    Text that came from a configuration file or a file name can hold a newline, and a
    reader would take the next line for a line of its own. Control characters become
    `?`. The home directory is shown as `~` and the target as `.`.
    """
    shown = "".join(ch if ch.isprintable() else "?" for ch in text)
    for base, short in ((home, "~"), (target, ".")):
        if base is None:
            continue
        for form in dict.fromkeys((str(base), _resolved(base).as_posix(), base.as_posix())):
            if len(form) > 1:
                shown = shown.replace(form, short)
    return shown[:_DETAIL_MAX]


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return path


def state_of(file: InstructionFile) -> tuple[str, str | None]:
    """`within` at or under act-at, `over-act-at` up to the limit, `could-not-trim` over it."""
    if file.size > file.limit:
        return "could-not-trim", "report-only"
    if file.size > file.act_at:
        return "over-act-at", None
    return "within", None


def inside_target(file: InstructionFile, target: Path) -> bool:
    for base in (target, _resolved(target)):
        try:
            file.real_path.relative_to(base)
            return True
        except ValueError:
            continue
    return False


def severity(file: InstructionFile, target: Path) -> str:
    """`ok`, `warn` or `fail`, one rule for doctor and `budgets check`.

    Only a workspace or configured file whose real file is inside the target can fail:
    the person running that target's doctor can fix it. A user-level file, or a link
    that leaves the target, warns.
    """
    state, _reason = state_of(file)
    if state == "within":
        return "ok"
    if state == "over-act-at":
        return "warn"
    return "fail" if file.level in ("workspace", "config") and inside_target(file, target) else "warn"


def _pending_pointers(file: InstructionFile, target: Path) -> list[str]:
    folder = target / "memory" / "cards" / "instructions"
    try:
        cards = sorted(
            f"memory/cards/instructions/{path.name}"
            for path in folder.glob(f"{file.key}--overflow-*.md")
            if path.is_file()
        )
    except OSError:
        return []
    if not cards:
        return []
    read = read_regular_file(file.real_path, max_bytes=INSPECTION_CEILING)
    text = read.data.decode("utf-8", "replace") if read.data is not None else ""
    return [card for card in cards if card not in text]


def file_row(file: InstructionFile, *, target: Path) -> dict[str, Any]:
    state, reason = state_of(file)
    row: dict[str, Any] = {
        "path": file.display,
        "paths": list(file.paths),
        "level": file.level,
        "loaders": list(file.loaders),
        "class": file.file_class,
        "class_source": file.class_source,
        "bytes_before": file.size,
        "bytes_after": file.size,
        "limit": file.limit,
        "limit_source": file.limit_source,
        "target_budget": file.target_budget,
        "act_at": file.act_at,
        "trim_to": file.trim_to,
        "state": state,
        "excluded": file.excluded,
        "limit_clamped": file.limit_clamped,
    }
    optional: dict[str, Any] = {
        "reason": reason,
        "limit_reason": file.limit_reason,
        "skip_reason": file.skip_reason,
        "exclusion_source": file.exclusion_source,
        "requested_class": file.requested_class,
        "requested_limit_bytes": file.requested_limit_bytes,
        "requested_source": file.requested_source,
        "override_reason": file.override_reason,
    }
    row.update({key: value for key, value in optional.items() if value is not None})
    if file.bootstrap_names:
        row["bootstrap_names"] = list(file.bootstrap_names)
    row["pending_pointers"] = _pending_pointers(file, target)
    return row


def config_changes(inventory: Inventory) -> dict[str, Any]:
    """What configuration did to protection in this inventory, as counts and names."""
    rejected = [note for note in inventory.override_notes if note.action == "forced-report"]
    return {
        "overrides_rejected": len(rejected),
        "limits_clamped": sum(1 for item in inventory.files if item.limit_clamped),
        "files_excluded": sum(1 for item in inventory.files if item.excluded),
        "config_notes": list(inventory.config_notes),
    }


def config_change_line(inventory: Inventory) -> str | None:
    """One short line while any override was rejected, any limit clamped, any file excluded or any config key moved a limit."""
    changes = config_changes(inventory)
    if not (
        changes["overrides_rejected"]
        or changes["limits_clamped"]
        or changes["files_excluded"]
        or changes["config_notes"]
    ):
        return None
    notes = ",".join(changes["config_notes"]) or "none"
    return (
        f"instruction_budget: config-changed overrides-rejected={changes['overrides_rejected']} "
        f"limits-clamped={changes['limits_clamped']} excluded={changes['files_excluded']} "
        f"config={safe_text(notes)} {_NEXT}"
    )


def file_alert_lines(inventory: Inventory) -> list[str]:
    """One `could-not-trim` line per tended file over its limit, then one per loader total over its cap.

    Rows marked excluded are skipped: they are never tended. Their bytes still count in the totals.
    """
    lines = []
    for item in inventory.files:
        state, reason = state_of(item)
        if state == "could-not-trim" and not item.excluded:
            lines.append(
                f"instruction_budget: could-not-trim {safe_text(item.display)} {item.size}/{item.limit} "
                f"reason={reason} {_NEXT}"
            )
    for total in inventory.totals:
        if total.cap is not None and total.total > total.cap:
            lines.append(
                f"instruction_budget: total-over-cap {safe_text(total.loader)} {total.total}/{total.cap} {_NEXT}"
            )
    return lines


def alert_lines(inventory: Inventory) -> list[str]:
    """Every alert line for an enabled report: file and total alerts, then the config-changed line."""
    change = config_change_line(inventory)
    return [*file_alert_lines(inventory), *([change] if change else [])]


def _total_row(total: Any, loader_reason: str | None) -> dict[str, Any]:
    if total.cap is None:
        return {"loader": total.loader, "bytes": total.total, "cap": None, "state": "unknown", "reason": loader_reason}
    return {
        "loader": total.loader,
        "bytes": total.total,
        "cap": total.cap,
        "state": "over-cap" if total.total > total.cap else "within",
    }


def apply(
    target: Path,
    config: InstructionsConfig,
    *,
    today: date,
    dry_run: bool,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The `instructions` tend stage. It writes nothing in slice 1, so `dry_run` changes nothing.

    `today` is read by the move planner in a later release. The parameter is here now
    so the stage signature does not change then.
    """
    if not config.enabled:
        return {"enabled": False}
    inventory = discover(target, config, home=home, environ=environ)
    return {
        "enabled": True,
        "files": [file_row(item, target=target) for item in inventory.files],
        "totals": [_total_row(total, inventory.loader_reason) for total in inventory.totals],
        "alerts": alert_lines(inventory),
        "override_notes": [
            {"name": note.name, "reason": note.reason, "action": note.action} for note in inventory.override_notes
        ],
        "config_notes": list(inventory.config_notes),
        "loader_reason": inventory.loader_reason,
        "repository_depth": inventory.repository_depth,
    }


def invalid_config_line(error: ValueError, *, target: Path) -> str:
    """The one line every reader prints when the `[instructions]` table cannot be read."""
    detail = safe_text(str(error), home=Path.home(), target=target)
    return f'instruction_budget: config-invalid detail="{detail}"'


def brief_payload(target: Path) -> dict[str, Any]:
    """The work brief's `instruction_budget` value. It never raises.

    An invalid table gives one `config-invalid` line. A disabled stage keeps only the
    `config-changed` line, so turning the report off is itself visible.
    """
    try:
        try:
            config = load_instructions_config(target)
        except ValueError as exc:
            return {"alerts": [invalid_config_line(exc, target=target)]}
        inventory = discover(target, config)
        if not config.enabled:
            change = config_change_line(inventory)
            return {"alerts": [change] if change else []}
        return {"alerts": alert_lines(inventory)}
    except (OSError, ValueError, RuntimeError, RecursionError) as exc:
        return {"alerts": [], "error": type(exc).__name__}
