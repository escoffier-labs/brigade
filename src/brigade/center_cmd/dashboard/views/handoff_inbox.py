"""Handoff inbox dashboard view."""

from __future__ import annotations

from pathlib import Path

from brigade.center_cmd.dashboard import data
from brigade.center_cmd.dashboard import render as html

NAME = "handoffs"
TITLE = "Handoffs"
ORDER = 2


def fetch(target: Path) -> dict:
    return data.run_json(target, ["handoff", "lint"])


def render(payload: dict, nonce: str) -> str:
    del nonce
    if payload.get("error"):
        return html.error_panel(TITLE, str(payload["error"]))

    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return html.panel(html.esc(TITLE), f"<p>{html.esc('Nothing pending.')}</p>")

    parts: list[str] = []
    injection_count = payload.get("injection_flagged_count", 0)
    parts.append(
        html.panel(
            html.esc("Injection-flagged handoffs"),
            f'<p class="error"><strong>{html.esc(injection_count)}</strong> '
            f"{html.esc('file(s) with prompt-injection signals')}</p>",
        )
    )

    count = payload.get("count", len(results))
    valid = payload.get("valid")
    valid_label = "yes" if valid is True else "no" if valid is False else "-"
    summary_rows = [
        [html.esc("Pending files"), html.esc(count)],
        [html.esc("All valid"), html.esc(valid_label)],
    ]
    parts.append(
        html.panel(
            html.esc("Queue summary"),
            html.table([html.esc("Metric"), html.esc("Value")], summary_rows),
        )
    )

    table_rows: list[list[str]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        path = item.get("path", "-")
        valid_item = item.get("valid")
        if valid_item is True:
            lint_state = "valid"
        elif valid_item is False:
            lint_state = "invalid"
        else:
            lint_state = "-"
        issues = _lint_issues(item)
        injection_signals = item.get("injection_signals", 0)
        if injection_signals:
            issues = (
                f"{issues}; injection signals: {injection_signals}"
                if issues
                else (f"injection signals: {injection_signals}")
            )
        table_rows.append(
            [
                html.esc(path),
                html.esc(lint_state),
                html.esc(item.get("action") or "-"),
                html.esc(issues or "-"),
            ]
        )

    if not table_rows:
        return html.panel(html.esc(TITLE), f"<p>{html.esc('Nothing pending.')}</p>")

    parts.append(
        html.panel(
            html.esc(TITLE),
            html.table(
                [
                    html.esc("Path"),
                    html.esc("Lint"),
                    html.esc("Action"),
                    html.esc("Issues"),
                ],
                table_rows,
            ),
        )
    )
    return "".join(parts)


def _lint_issues(item: dict) -> str:
    messages: list[str] = []
    for key in ("errors", "warnings"):
        values = item.get(key)
        if isinstance(values, list):
            messages.extend(str(value) for value in values)
    return "; ".join(messages)
