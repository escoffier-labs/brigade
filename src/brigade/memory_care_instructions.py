"""Report on the always-loaded instruction files: state per file, alert lines, the tend stage.

Slice 1 measures and reports. It moves nothing, so a file over its limit is
`could-not-trim` with reason `report-only`, and the band between act-at and the
limit (`over-act-at`) raises no alert: appends already go to overflow cards there.

Every reader of the inventory (the tend stage, doctor, `budgets check` and the work
brief) goes through this module. None of them may raise or block on a bad
configuration, and none prints a full home path or any file content: rows carry
display paths.

The work brief is read by a model at the start of every session, and a file name, a
configured path, an override name or an OpenClaw agent id can be written by an agent or by
content an agent ingested. So printable is not safe. Every such value in a line of text goes
through `encode_value`: only ASCII letters, digits and `. _ - / ~` pass, every other character
is written as an escape (`\\x20`, `\\u202e`), the length is capped with a visible mark, and
the line puts the result in a fixed quoted field (`path="..."`). The fixed parts of a line
(the prefix, the state names, the reason codes, `next=`) come from constants only, and a code
that does not match the allow pattern is encoded too (`code`). JSON payloads keep the real
values as JSON strings, which are unambiguous data, minus the home and target directories.

A file that exists but whose size is unknown is never `within`: its state is `size-unknown`.

A configuration change that weakens protection stays visible to the next agent:
rejected overrides, clamped limits, excluded files, keys that move every limit and an
unreadable OpenClaw configuration are all listed in the report, and the brief prints
one `config-changed` line while any of them holds.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from . import budgets
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


_SAFE_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-/~")
_EXTERNAL_PREFIX = "<external>/"  # the fixed marker `display_path` gives a path outside the target and home
VALUE_MAX = 96  # encoded characters in one value
TRUNCATED = "[truncated]"  # `[` is never produced by an escape, so a real value cannot imitate it
_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")


def _escape(char: str) -> str:
    point = ord(char)
    if point < 0x100:
        return f"\\x{point:02x}"
    return f"\\u{point:04x}" if point < 0x10000 else f"\\U{point:08x}"


def encode_value(text: str, *, max_len: int = VALUE_MAX) -> str:
    """`text` as a visibly encoded data value: the safe path characters as they are, every other one escaped.

    The fixed external marker passes whole. The result is at most `max_len` characters,
    ending in `[truncated]` when the text was longer, and an escape is never split. Put it
    inside a quoted field: `path="..."`.
    """
    prefix = _EXTERNAL_PREFIX if text.startswith(_EXTERNAL_PREFIX) else ""
    pieces = [prefix, *(char if char in _SAFE_CHARS else _escape(char) for char in text[len(prefix) :])]
    if sum(len(piece) for piece in pieces) <= max_len:
        return "".join(pieces)
    out: list[str] = []
    used = 0
    for piece in pieces:
        if used + len(piece) > max_len - len(TRUNCATED):
            break
        out.append(piece)
        used += len(piece)
    return "".join(out) + TRUNCATED


def field(name: str, text: str) -> str:
    """`name="<encoded text>"`, the one shape an untrusted value takes in a line."""
    return f'{name}="{encode_value(text)}"'


def code(text: str) -> str:
    """A code from Brigade's own constants: as is when it matches the allow pattern, else encoded."""
    return text if _CODE.fullmatch(text) else encode_value(text)


def shorten(text: str, *, home: Path | None = None, target: Path | None = None) -> str:
    """The home directory as `~` and the target as `.`, capped at 200 characters. For JSON values."""
    shown = text
    for base, short in ((home, "~"), (target, ".")):
        if base is None:
            continue
        for form in dict.fromkeys((str(base), _resolved(base).as_posix(), base.as_posix())):
            if len(form) > 1:
                shown = shown.replace(form, short)
    return shown[:_DETAIL_MAX]


_FIXED_CONFIG_ERROR = re.compile(
    r"invalid instructions"
    r"(\.(enabled|act_at_percent|trim_to_percent|default_limit_bytes|exclude|files))?"
    r"|invalid instructions: \.brigade/memory-care\.toml (is over 1 MiB|is not a regular file"
    r"|could not be read|is not UTF-8|is not valid TOML)"
)
_FILES_CONFIG_ERROR = re.compile(r"(invalid instructions\.files\.)(.*?)(\.class|\.limit_bytes)?", re.DOTALL)


def config_error_detail(exc: ValueError, *, home: Path | None = None, target: Path | None = None) -> str:
    """The text of an invalid `[instructions]` table, safe to print on a line.

    `load_instructions_config` messages are fixed text, except that
    `invalid instructions.files.<name>` carries the key the person wrote. The fixed
    messages pass as they are, the name is shortened and encoded, and anything else is
    encoded whole.
    """
    message = str(exc)
    if _FIXED_CONFIG_ERROR.fullmatch(message):
        return message
    hit = _FILES_CONFIG_ERROR.fullmatch(message)
    if hit:
        return f"{hit[1]}{encode_value(shorten(hit[2], home=home, target=target))}{hit[3] or ''}"
    return encode_value(shorten(message, home=home, target=target))


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return path


SIZE_UNKNOWN = "size-unknown"


def state_of(file: InstructionFile) -> tuple[str, str | None]:
    """`within` at or under act-at, `over-act-at` up to the limit, `could-not-trim` over it.

    A file that exists and whose size nothing could read is `size-unknown`, with the
    inventory's skip reason: it is never reported as within a limit it was not measured against.
    """
    if not file.size_known:
        return SIZE_UNKNOWN, file.skip_reason or "unreadable"
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
    # `could-not-trim` and `size-unknown` split the same way: the owner can fix a workspace file.
    return "fail" if file.level in ("workspace", "config") and inside_target(file, target) else "warn"


NOT_A_FILE = "not-a-file"
STAT_FAILED = "stat-failed"


def non_file_names(target: Path) -> list[tuple[str, str]]:
    """Per-name budget paths that exist and are not regular files, as (name, `not-a-file` | `stat-failed`).

    One function for doctor and `budgets check`, so they agree. A directory, a FIFO or a
    device at a name the loaders read is `not-a-file`. A path `stat` cannot answer for
    (a permission error, say) is `stat-failed`. A name that is missing, or a link to
    nothing, is neither: it is a missing file, as before. It uses `stat` only, never an
    open, so a FIFO cannot block it, and it never raises.
    """
    found: list[tuple[str, str]] = []
    for name in sorted(budgets.BOOTSTRAP_BUDGETS):
        try:
            info = os.stat(target / name)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except (OSError, ValueError):
            found.append((name, STAT_FAILED))
            continue
        if not stat.S_ISREG(info.st_mode):
            found.append((name, NOT_A_FILE))
    return found


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
        "bytes_before": file.size if file.size_known else None,
        "bytes_after": file.size if file.size_known else None,
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
    if not file.size_known:
        row["size_known"] = False
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


def config_change_summary(inventory: Inventory) -> str | None:
    """Counts and key names while any override was rejected, any limit clamped, any file excluded or any config key moved a limit."""
    changes = config_changes(inventory)
    if not (
        changes["overrides_rejected"]
        or changes["limits_clamped"]
        or changes["files_excluded"]
        or changes["config_notes"]
    ):
        return None
    notes = ",".join(code(note) for note in changes["config_notes"]) or "none"
    return (
        f"overrides-rejected={changes['overrides_rejected']} limits-clamped={changes['limits_clamped']} "
        f"excluded={changes['files_excluded']} config={notes}"
    )


def config_change_line(inventory: Inventory) -> str | None:
    """The one short brief line for `config_change_summary`, or None when nothing changed."""
    summary = config_change_summary(inventory)
    return None if summary is None else f"instruction_budget: config-changed {summary} {_NEXT}"


# Reasons that are the normal state for a target with no OpenClaw agent: no line for them.
QUIET_LOADER_REASONS = frozenset({"config-missing", "config-workspace-unmatched"})


def file_alert_lines(inventory: Inventory) -> list[str]:
    """One line per tended file over its limit or of unknown size, then one per loader total over its cap.

    Rows marked excluded are skipped: they are never tended. Their bytes still count in the totals.
    """
    lines = []
    for item in inventory.files:
        if item.excluded:
            continue
        state, reason = state_of(item)
        if state == "could-not-trim":
            lines.append(
                f"instruction_budget: could-not-trim {field('path', item.display)} {item.size}/{item.limit} "
                f"reason={code(reason or '')} {_NEXT}"
            )
        elif state == SIZE_UNKNOWN:
            lines.append(
                f"instruction_budget: size-unknown {field('path', item.display)} limit={item.limit} "
                f"reason={code(reason or '')} {_NEXT}"
            )
    for total in inventory.totals:
        if total.cap is not None and total.total > total.cap:
            lines.append(
                f"instruction_budget: total-over-cap {field('loader', total.loader)} {total.total}/{total.cap} {_NEXT}"
            )
    return lines


def alert_lines(inventory: Inventory) -> list[str]:
    """Every alert line for an enabled report: file and total alerts, then the config-changed line."""
    change = config_change_line(inventory)
    return [*file_alert_lines(inventory), *([change] if change else [])]


def total_row(total: Any, loader_reason: str | None) -> dict[str, Any]:
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
        "totals": [total_row(total, inventory.loader_reason) for total in inventory.totals],
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
    detail = config_error_detail(error, home=Path.home(), target=target)
    return f'instruction_budget: config-invalid detail="{detail}"'


def inventory_failed_line(error: BaseException) -> str:
    """The one fixed line for an inventory that could not be built. Only the exception class name varies."""
    return f"instruction_budget: inventory-failed reason={code(type(error).__name__)} {_NEXT}"


_STAGE_ERRORS = (OSError, ValueError, RuntimeError, RecursionError)


def run_stage(target: Path, *, today: date, dry_run: bool, fail_fast: bool) -> dict[str, Any]:
    """The `instructions` stage of `memory care tend`, as that command calls it.

    `fail_fast` is a run that named this stage: a bad `[instructions]` table or an inventory
    that cannot be built raises, like a bad `[tend]` table. A default run records the failure
    as a stage error with a fixed code (`config-invalid` or `inventory-failed`) and an alert
    line, so the other stages finish and the run is saved. The caller marks the run failed.
    """
    try:
        config = load_instructions_config(target)
    except ValueError as exc:
        if fail_fast:
            raise
        return {
            "enabled": True,
            "error": "config-invalid",
            "detail": config_error_detail(exc, home=Path.home(), target=target),
            "alerts": [invalid_config_line(exc, target=target)],
        }
    try:
        return apply(target, config, today=today, dry_run=dry_run)
    except _STAGE_ERRORS as exc:
        if fail_fast:
            raise
        return {
            "enabled": True,
            "error": "inventory-failed",
            "detail": type(exc).__name__,
            "alerts": [inventory_failed_line(exc)],
        }


def brief_payload(target: Path) -> dict[str, Any]:
    """The work brief's `instruction_budget` value. It never raises.

    An invalid table gives one `config-invalid` line. A disabled stage keeps only the
    `config-changed` line, so turning the report off is itself visible. An inventory
    that cannot be built gives one fixed `inventory-failed` line and `error`.
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
    except _STAGE_ERRORS as exc:
        return {"alerts": [inventory_failed_line(exc)], "error": type(exc).__name__}
