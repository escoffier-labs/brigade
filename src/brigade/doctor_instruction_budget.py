"""Doctor rows for the always-loaded instruction files, read from the inventory.

Every file the inventory finds gets one `bootstrap-budget: <display>` row measured
against its single limit. User-level files are operator rows: a repository's doctor
must not fail for a file it cannot fix. Nothing here raises: an invalid
`[instructions]` table and an inventory that cannot be built are each one `FAIL` row,
and rows are still built from the defaults where they can be.

A file's display path is encoded (`encode_value`) before it goes into a row name, so a
file name cannot put free text, spaces, a colon or a newline in front of the reader. An
excluded or disabled report does not silence this check: an over-limit workspace file
keeps failing here, because the loader cuts it either way.
"""

from __future__ import annotations

from pathlib import Path

from . import instruction_inventory as inventory_module
from .doctor import FAIL, OK, WARN, CheckResult, ScopedCheckResult, _operator_check
from .instruction_inventory import InstructionFile, InstructionsConfig, load_instructions_config
from .memory_care_instructions import (
    NEXT_DRY_RUN,
    NOT_A_FILE,
    QUIET_LOADER_REASONS,
    SIZE_UNKNOWN,
    code,
    config_change_summary,
    config_error_detail,
    encode_value,
    non_file_names,
    severity,
    state_of,
    text_name,
)

_STATUS = {"ok": OK, "warn": WARN, "fail": FAIL}


def _detail(file: InstructionFile, state: str) -> str:
    if state == SIZE_UNKNOWN:
        detail = (
            f"size unknown (skipped: {code(file.skip_reason or 'unreadable')}), limit {file.limit} bytes; "
            f"the loader may cut this file. next: {NEXT_DRY_RUN}"
        )
    else:
        base = f"{file.size}/{file.limit} bytes"
        if state == "within":
            detail = f"{base}, act-at {file.act_at} (limit from {code(file.limit_source)})"
        elif state == "over-act-at":
            detail = f"{base}, over act-at {file.act_at}; new handoff appends go to an overflow card"
        else:
            detail = f"{base}, over limit; the loader cuts this file. next: {NEXT_DRY_RUN}"
        if file.skip_reason:
            detail += f" (skipped: {code(file.skip_reason)})"
    if file.excluded:
        detail += f"; excluded by {code(file.exclusion_source or '')}"
    if file.override_reason:
        detail += f"; override rejected: {code(file.override_reason)}"
    if file.limit_clamped:
        detail += f"; configured limit {file.requested_limit_bytes} is above the target and was not applied"
    return detail


def check_instruction_budgets(target: Path) -> list[CheckResult | ScopedCheckResult]:
    results: list[CheckResult | ScopedCheckResult] = []
    for name, why in non_file_names(target):
        detail = f"not a file: {name}" if why == NOT_A_FILE else f"could not stat: {name}"
        results.append((FAIL, f"bootstrap-budget: {encode_value(name)}", detail))
    config = InstructionsConfig()
    try:
        config = load_instructions_config(target)
    except ValueError as exc:
        results.append((FAIL, "instructions: config", config_error_detail(exc, home=Path.home(), target=target)))
    try:
        found = inventory_module.discover(target, config)
    except (OSError, ValueError, RuntimeError, RecursionError) as exc:
        results.append((FAIL, "instructions: inventory", f"could not read: {code(type(exc).__name__)}"))
        return results
    for item in found.files:
        state, _reason = state_of(item)
        status = _STATUS[severity(item, target)]
        name = f"bootstrap-budget: {encode_value(text_name(item))}"
        if item.level == "user":
            results.append(_operator_check(status, name, _detail(item, state)))
        else:
            results.append((status, name, _detail(item, state)))
    for total in found.totals:
        if total.cap is not None and total.total > total.cap:
            results.append(
                (
                    WARN,
                    f"instructions: loader-total {encode_value(total.loader)}",
                    f"{total.total}/{total.cap} bytes, over cap; the harness cuts at the total. next: {NEXT_DRY_RUN}",
                )
            )
    summary = config_change_summary(found)
    if summary is not None:
        results.append((WARN, "instructions: config-changes", f"{summary}; next: {NEXT_DRY_RUN}"))
    if found.loader_reason is not None and found.loader_reason not in QUIET_LOADER_REASONS:
        results.append(
            _operator_check(
                WARN, "instructions: openclaw-config", f"OpenClaw caps not read: {code(found.loader_reason)}"
            )
        )
    return results
