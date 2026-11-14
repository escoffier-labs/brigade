"""Plain text and JSON rendering for the read-only check report."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .observed_report import (
    MIN_RESULTS,
    REPORT_LABEL,
    FirstPassRow,
    ReportNotes,
    first_pass_json,
    first_pass_rows,
)

EMPTY = "No observed checks. Enable capture with brigade work mode set observe."


def render_first_pass(rows: list[FirstPassRow], notes: ReportNotes) -> str:
    if notes.observed_read == 0:
        return EMPTY + (f" Skipped lines: {notes.skipped_lines}." if notes.skipped_lines else "")
    lines = [REPORT_LABEL, f"Minimum reported results for a rate: {MIN_RESULTS}"]
    table = [["harness", "model", "n", "passed", "failed", "unknown", "rate"]]
    for row in rows:
        rate = row.rate()
        label = (
            f"{rate:.1%}"
            if rate is not None
            else "no result reported"
            if row.passed + row.failed == 0
            else "insufficient results"
        )
        table.append(
            [
                row.harness,
                row.model if row.model is not None else "*",
                str(row.samples),
                str(row.passed),
                str(row.failed),
                str(row.unknown),
                label,
            ]
        )
    widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
    lines.extend(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip() for row in table
    )
    lines.append(
        f"Observed read: {notes.observed_read}  Retries: {notes.retries}  "
        f"Ungrouped: {notes.ungrouped}  Skipped lines: {notes.skipped_lines}"
    )
    return "\n".join(lines)


def checks_command(*, target: Path, since_days: int | None, group_by: str, json_output: bool) -> int:
    since = datetime.now(timezone.utc) - timedelta(days=since_days) if since_days is not None else None
    rows, notes = first_pass_rows(target, since=since, group_by=group_by)
    print(
        json.dumps(first_pass_json(rows, notes, group_by=group_by), sort_keys=True)
        if json_output
        else render_first_pass(rows, notes)
    )
    return 0
