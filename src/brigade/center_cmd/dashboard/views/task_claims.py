"""Claim state and three-phase footprint dashboard view.

Sourced from ``brigade work tasks --all --json`` (full task records carry
``claim`` and ``metadata.footprint``). Marks stale claims (age ≥ claim stale
hours) and footprint/lifecycle mismatches without introducing write surfaces.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from brigade.center_cmd.dashboard import render as html
from brigade.center_cmd.dashboard import work_graph as wg
from brigade.work_cmd import constants as work_constants

NAME = "claims"
TITLE = "Claims"
ORDER = 9


def fetch(target: Path) -> dict:
    return wg.fetch_tasks(target)


def render(payload: dict, nonce: str) -> str:
    if payload.get("error"):
        return html.error_panel(TITLE, str(payload["error"]))

    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return html.panel(html.esc(TITLE), f"<p>{html.esc('Nothing here.')}</p>")

    now = datetime.now(timezone.utc)
    rows: list[list[str]] = []
    stale_claim_count = 0
    stale_footprint_count = 0

    for task in tasks:
        if not isinstance(task, dict):
            continue
        claim = wg.claim_record(task)
        footprint = wg.footprint_of(task)
        claim_stale = wg.claim_is_stale(claim, now=now)
        footprint_stale = wg.footprint_staleness(task, footprint)
        if claim_stale:
            stale_claim_count += 1
        if footprint_stale:
            stale_footprint_count += 1
        rows.append(_row(task, claim, footprint, claim_stale, footprint_stale, now=now))

    if not rows:
        return html.panel(html.esc(TITLE), f"<p>{html.esc('Nothing here.')}</p>")

    summary_rows = [
        [html.esc("Tasks"), html.esc(len(rows))],
        [html.esc("Stale claims"), html.esc(stale_claim_count)],
        [html.esc("Stale footprints"), html.esc(stale_footprint_count)],
        [html.esc("Claim stale after (h)"), html.esc(work_constants.CLAIM_STALE_HOURS)],
    ]
    table = html.table(
        [
            html.esc("Task"),
            html.esc("Status"),
            html.esc("Claim"),
            html.esc("Claim age"),
            html.esc("Footprint"),
            html.esc("Files"),
            html.esc("Symbols"),
            html.esc("Flags"),
        ],
        rows,
    )
    return (
        f"{_stylesheet(nonce)}"
        f"{html.panel(html.esc('Claim & footprint summary'), html.table([html.esc('Signal'), html.esc('Value')], summary_rows))}"
        f"{html.panel(html.esc(TITLE), table)}"
    )


def _row(
    task: dict,
    claim: dict | None,
    footprint: dict | None,
    claim_stale: bool,
    footprint_stale: str | None,
    *,
    now: datetime,
) -> list[str]:
    task_id = str(task.get("id") or "-")
    status = str(task.get("status") or "pending")

    if claim is None:
        claim_cell = html.esc("unclaimed")
        age_cell = html.esc("-")
    else:
        actor = claim.get("actor") or task.get("assignee") or "-"
        claim_id = claim.get("claim_id") or "-"
        claim_cell = html.esc(f"{actor} / {claim_id}")
        age = wg.claim_age_hours(claim, now=now)
        age_cell = html.esc("-" if age is None else f"{age:.1f}h")

    if footprint is None:
        phase_cell = html.esc("none")
        files_cell = html.esc("-")
        symbols_cell = html.esc("-")
    else:
        phase = str(footprint.get("phase") or "predicted")
        files = footprint.get("files") if isinstance(footprint.get("files"), list) else []
        symbols = footprint.get("symbol_ids") if isinstance(footprint.get("symbol_ids"), list) else []
        phase_cell = html.esc(phase)
        files_cell = html.esc(len(files))
        symbols_cell = html.esc(len(symbols))

    flags: list[str] = []
    if claim_stale:
        flags.append("stale-claim")
    if footprint_stale:
        flags.append(f"stale-footprint: {footprint_stale}")
    flag_cell = html.esc(", ".join(flags) if flags else "ok")

    # render.table does not accept row classes; mark staleness on the Flags cell.
    if claim_stale or footprint_stale:
        flag_cell = f'<span class="wg-flag-stale">{flag_cell}</span>'
    else:
        flag_cell = f'<span class="wg-flag-ok">{flag_cell}</span>'

    return [
        html.esc(task_id),
        html.esc(status),
        claim_cell,
        age_cell,
        phase_cell,
        files_cell,
        symbols_cell,
        flag_cell,
    ]


def _stylesheet(nonce: str) -> str:
    return f"""<style nonce="{html.esc(nonce)}">
.wg-flag-stale {{
  color: #8b0000;
  font-weight: 600;
}}
.wg-flag-ok {{
  color: #1a7f4b;
}}
</style>"""
