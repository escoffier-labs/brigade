"""First attempts over bounded, unsigned hook observations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import observed_checks

REPORT_LABEL = "observed, not independently verified"
MIN_RESULTS = 5


@dataclass(frozen=True)
class FirstPassRow:
    harness: str
    model: str | None
    samples: int = 0
    passed: int = 0
    failed: int = 0
    unknown: int = 0

    def rate(self) -> float | None:
        results = self.passed + self.failed
        return self.passed / results if results >= MIN_RESULTS else None


@dataclass(frozen=True)
class ReportNotes:
    observed_read: int
    skipped_lines: int
    ungrouped: int
    retries: int


def first_pass_rows(
    target: Path, *, since: datetime | None = None, group_by: str = "model"
) -> tuple[list[FirstPassRow], ReportNotes]:
    """Select the earliest retained attempt per key by timestamp, then filter by window.

    A record's timestamp is taken before it is appended, so concurrent captures can
    land out of order; storage order only breaks timestamp ties. A retry whose first
    retained attempt predates the window cannot become a first attempt by filtering.
    """
    if group_by not in {"harness", "model"}:
        raise ValueError("group_by must be harness or model")
    checks, skipped = observed_checks.read_checks(target.resolve(), snapshot=True)
    first: dict[tuple[str, str, str, str], observed_checks.ObservedCheck] = {}
    ungrouped = retries = 0
    # Oldest first by timestamp; the sort is stable, so storage order breaks ties.
    for check in sorted(reversed(checks), key=lambda c: datetime.fromisoformat(c.ts)):
        if observed_checks.DIGEST_UNAVAILABLE in (check.command_digest, check.session_fingerprint):
            if since is None or datetime.fromisoformat(check.ts) >= since:
                ungrouped += 1
            continue
        key = (check.harness, check.session_fingerprint, check.tool, check.command_digest)
        if key in first:
            if since is None or datetime.fromisoformat(check.ts) >= since:
                retries += 1
        else:
            first[key] = check
    counts: dict[tuple[str, str | None], dict[str, int]] = {}
    for check in first.values():
        if since is not None and datetime.fromisoformat(check.ts) < since:
            continue
        key_row = (check.harness, check.model if group_by == "model" else None)
        row = counts.setdefault(key_row, dict(samples=0, passed=0, failed=0, unknown=0))
        row["samples"] += 1
        # Codex has no documented exit status, including legacy declared statuses.
        status = "unknown" if check.harness == "codex" else check.status
        row[{"ok": "passed", "failed": "failed", "unknown": "unknown"}[status]] += 1
    rows = [FirstPassRow(harness, model, **count) for (harness, model), count in counts.items()]
    rows.sort(key=lambda r: (r.harness == "unknown", r.harness, r.model == "unknown", r.model or ""))
    return rows, ReportNotes(len(checks), skipped, ungrouped, retries)


def first_pass_json(rows: list[FirstPassRow], notes: ReportNotes, *, group_by: str) -> dict[str, Any]:
    return {
        "schema": "brigade.work_checks.v1",
        "label": REPORT_LABEL,
        "group_by": group_by,
        "min_results": MIN_RESULTS,
        "rows": [dict(asdict(row), rate=row.rate()) for row in rows],
        **asdict(notes),
    }
