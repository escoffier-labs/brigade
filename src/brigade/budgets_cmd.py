"""CLI helpers for Brigade's canonical operator budgets."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import budgets


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


def check(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    checks: list[dict[str, Any]] = []
    hard_fail = False
    for name, limit in sorted(budgets.BOOTSTRAP_BUDGETS.items()):
        path = target / name
        size = path.stat().st_size if path.is_file() else 0
        status = "missing" if not path.exists() else "ok"
        if path.is_file() and size > limit:
            status = "fail"
            hard_fail = True
        checks.append(
            {
                "name": name,
                "path": str(path),
                "exists": path.exists(),
                "size_bytes": size,
                "budget_bytes": limit,
                "status": status,
            }
        )

    payload = {
        "target": str(target),
        "valid": not hard_fail,
        "checks": checks,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1

    print(f"budget check: {target}")
    for row in checks:
        print(
            f"[{row['status']}] {row['name']}: "
            f"{row['size_bytes']}/{row['budget_bytes']} bytes"
        )
    return 0 if payload["valid"] else 1
