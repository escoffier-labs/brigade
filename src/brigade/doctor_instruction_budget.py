"""Doctor rows for the always-loaded instruction files, read from the inventory.

Every file the inventory finds gets one `bootstrap-budget: <display>` row measured
against its single limit. User-level files are operator rows: a repository's doctor
must not fail for a file it cannot fix. Nothing here raises: an invalid
`[instructions]` table and an inventory that cannot be built are each one `FAIL` row,
and rows are still built from the defaults where they can be.
"""

from __future__ import annotations

from pathlib import Path

from . import budgets
from . import instruction_inventory as inventory_module
from .doctor import FAIL, OK, WARN, CheckResult, ScopedCheckResult, _operator_check
from .instruction_inventory import InstructionFile, InstructionsConfig, load_instructions_config
from .memory_care_instructions import (
    NEXT_DRY_RUN,
    QUIET_LOADER_REASONS,
    config_change_summary,
    safe_text,
    severity,
    state_of,
)

_STATUS = {"ok": OK, "warn": WARN, "fail": FAIL}


def _detail(file: InstructionFile, state: str) -> str:
    base = f"{file.size}/{file.limit} bytes"
    if state == "within":
        detail = f"{base}, act-at {file.act_at} (limit from {file.limit_source})"
    elif state == "over-act-at":
        detail = f"{base}, over act-at {file.act_at}; new handoff appends go to an overflow card"
    else:
        detail = f"{base}, over limit; the loader cuts this file. next: {NEXT_DRY_RUN}"
    if file.skip_reason:
        detail += f" (skipped: {file.skip_reason})"
    if file.excluded:
        detail += f"; excluded by {file.exclusion_source}"
    if file.override_reason:
        detail += f"; override rejected: {file.override_reason}"
    if file.limit_clamped:
        detail += f"; configured limit {file.requested_limit_bytes} is above the target and was not applied"
    return detail


def check_instruction_budgets(target: Path) -> list[CheckResult | ScopedCheckResult]:
    results: list[CheckResult | ScopedCheckResult] = []
    for name in budgets.BOOTSTRAP_BUDGETS:
        path = target / name
        if path.exists() and not path.is_file():
            results.append((FAIL, f"bootstrap-budget: {name}", f"not a file: {name}"))
    config = InstructionsConfig()
    try:
        config = load_instructions_config(target)
    except ValueError as exc:
        results.append((FAIL, "instructions: config", safe_text(str(exc), home=Path.home(), target=target)))
    try:
        found = inventory_module.discover(target, config)
    except (OSError, ValueError, RuntimeError, RecursionError) as exc:
        results.append((FAIL, "instructions: inventory", f"could not read: {type(exc).__name__}"))
        return results
    for item in found.files:
        state, _reason = state_of(item)
        status = _STATUS[severity(item, target)]
        name = f"bootstrap-budget: {safe_text(item.display)}"
        if item.level == "user":
            results.append(_operator_check(status, name, _detail(item, state)))
        else:
            results.append((status, name, _detail(item, state)))
    summary = config_change_summary(found)
    if summary is not None:
        results.append((WARN, "instructions: config-changes", f"{summary}; next: {NEXT_DRY_RUN}"))
    if found.loader_reason is not None and found.loader_reason not in QUIET_LOADER_REASONS:
        results.append(
            _operator_check(WARN, "instructions: openclaw-config", f"OpenClaw caps not read: {found.loader_reason}")
        )
    return results
