"""Status grid dashboard view (stub)."""

from __future__ import annotations

from pathlib import Path

from brigade.center_cmd.dashboard import data
from brigade.center_cmd.dashboard import render as html

NAME = "status"
TITLE = "Status"
ORDER = 1


def fetch(target: Path) -> dict:
    return data.run_json(target, ["work", "brief"], timeout=60.0)


def render(payload: dict, nonce: str) -> str:
    del nonce
    if payload.get("error"):
        return html.error_panel(TITLE, str(payload["error"]))
    if not payload:
        return html.panel(html.esc(TITLE), f"<p>{html.esc('Nothing here.')}</p>")

    handoff_issues = _section(payload, "handoff_issues")
    memory_care = _section(payload, "memory_care")
    outcome_loop = _section(payload, "outcome_loop")
    inbox_hygiene = _section(payload, "inbox_hygiene")
    pending_tasks = payload.get("pending_tasks")

    rows = [
        ("Pending handoff issues", _value(handoff_issues, "count")),
        ("Total handoff issues", _value(handoff_issues, "total_count")),
        ("Memory cards at refresh risk", _value(memory_care, "issue_count")),
        # work brief does not currently emit the latest receipt or signal.
        ("Latest verify receipt", "-"),
        ("Latest verify signal", "-"),
        ("Verify runs", _value(outcome_loop, "verify_run_count")),
        ("Outcome records", _value(outcome_loop, "record_count")),
        ("Eligible outcome receipts", _value(outcome_loop, "eligible_receipt_count")),
        ("Open work-inbox items", len(pending_tasks) if isinstance(pending_tasks, list) else "-"),
        ("Inbox hygiene issues", _value(inbox_hygiene, "issue_count")),
    ]
    escaped_rows = [[html.esc(label), html.esc(value)] for label, value in rows]
    return html.panel(
        html.esc(TITLE),
        html.table([html.esc("Signal"), html.esc("Value")], escaped_rows),
    )


def _section(payload: dict, name: str) -> dict:
    value = payload.get(name)
    return value if isinstance(value, dict) else {}


def _value(section: dict, name: str) -> object:
    value = section.get(name)
    return "-" if value is None else value
