"""CLI helpers for Brigade's canonical operator budgets."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import budgets
from .instruction_inventory import InstructionFile, InstructionsConfig, Inventory, discover, load_instructions_config
from .memory_care_instructions import config_change_summary, inside_target, safe_text, severity, state_of


def _budget_payload() -> dict[str, Any]:
    return {
        "bootstrap_budgets": budgets.BOOTSTRAP_BUDGETS,
        "bootstrap_soft_limit": budgets.DEFAULT_BOOTSTRAP_SOFT_LIMIT,
        "bootstrap_hard_limit": budgets.DEFAULT_BOOTSTRAP_HARD_LIMIT,
        "bootstrap_hard_limit_ceiling": budgets.BOOTSTRAP_HARD_LIMIT_CEILING,
        "memory_card_budget_bytes": budgets.MEMORY_CARD_BUDGET_BYTES,
        "memory_index_max_lines": budgets.MEMORY_INDEX_MAX_LINES,
        "memory_care_scan_stale_days": budgets.MEMORY_CARE_SCAN_STALE_DAYS,
        "handoff_backlog_stale_days": budgets.HANDOFF_BACKLOG_STALE_DAYS,
        "handoff_backlog_stale_seconds": budgets.HANDOFF_BACKLOG_STALE_SECONDS,
    }


def show(*, json_output: bool = False) -> int:
    payload = _budget_payload()
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("budgets:")
    print(f"bootstrap_soft_limit: {payload['bootstrap_soft_limit']}")
    print(f"bootstrap_hard_limit: {payload['bootstrap_hard_limit']}")
    print(f"memory_card_budget_bytes: {payload['memory_card_budget_bytes']}")
    print(f"memory_index_max_lines: {payload['memory_index_max_lines']}")
    print(f"handoff_backlog_stale_days: {payload['handoff_backlog_stale_days']}")
    print("bootstrap_files:")
    for name, limit in sorted(budgets.BOOTSTRAP_BUDGETS.items()):
        print(f"- {name}: {limit}")
    return 0


def _check_row(item: InstructionFile, target: Path) -> dict[str, Any]:
    state, _reason = state_of(item)
    status = "missing" if not item.exists else severity(item, target)
    row: dict[str, Any] = {
        "name": item.display,
        # The real path only for a file inside the target. A link that leaves it shows its display form.
        "path": str(item.real_path) if inside_target(item, target) else item.display,
        "exists": item.exists,
        "size_bytes": item.size,
        "budget_bytes": item.limit,
        "status": status,
        "limit_source": item.limit_source,
        "act_at_bytes": item.act_at,
        "trim_to_bytes": item.trim_to,
        "loaders": list(item.loaders),
        "state": state,
        "level": item.level,
        "class": item.file_class,
        "excluded": item.excluded,
        "limit_clamped": item.limit_clamped,
    }
    optional = {
        "skip_reason": item.skip_reason,
        "exclusion_source": item.exclusion_source,
        "override_reason": item.override_reason,
        "requested_limit_bytes": item.requested_limit_bytes,
    }
    row.update({key: value for key, value in optional.items() if value is not None})
    return row


def check(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    config = InstructionsConfig()
    config_error: str | None = None
    try:
        config = load_instructions_config(target)
    except ValueError as exc:
        config_error = safe_text(str(exc), home=Path.home(), target=target)
    found: Inventory | None = None
    inventory_error: str | None = None
    try:
        found = discover(target, config, include_missing=True)
    except (OSError, ValueError, RuntimeError, RecursionError) as exc:
        inventory_error = f"could not read: {type(exc).__name__}"

    checks = [_check_row(item, target) for item in found.files] if found is not None else []
    hard_fail = (
        any(row["status"] == "fail" for row in checks) or config_error is not None or inventory_error is not None
    )
    payload = {
        "target": str(target),
        "valid": not hard_fail,
        "checks": checks,
        "config_error": config_error,
        "inventory_error": inventory_error,
        "override_notes": [
            {"name": note.name, "reason": note.reason, "action": note.action}
            for note in (found.override_notes if found is not None else ())
        ],
        "config_notes": list(found.config_notes) if found is not None else [],
        "loader_reason": found.loader_reason if found is not None else None,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1

    print(f"budget check: {target}")
    for row in checks:
        print(
            f"[{row['status']}] {safe_text(row['name'])}: {row['size_bytes']}/{row['budget_bytes']} bytes "
            f"(act-at {row['act_at_bytes']}, {row['limit_source']})"
        )
    if config_error is not None:
        print(f"[fail] instructions: config: {config_error}")
    if inventory_error is not None:
        print(f"[fail] instructions: inventory: {inventory_error}")
    summary = config_change_summary(found) if found is not None else None
    if summary is not None:
        print(f"[warn] instructions: config-changes: {summary}")
    return 0 if payload["valid"] else 1
