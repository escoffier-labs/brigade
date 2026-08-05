"""Run timeline dashboard view (stub)."""

from __future__ import annotations

import json
from pathlib import Path

from brigade.center_cmd.dashboard import data
from brigade.center_cmd.dashboard import render as html

NAME = "runs"
TITLE = "Runs"
ORDER = 5


def fetch(target: Path) -> dict:
    return data.run_json(target, ["work", "verify", "runs"])


def render(payload: dict, nonce: str) -> str:
    del nonce
    if payload.get("error"):
        return html.error_panel(TITLE, str(payload["error"]))

    runs = payload.get("runs")
    if not isinstance(runs, list) or not runs:
        return _empty_panel()

    rows = []
    for receipt in sorted(
        (item for item in runs if isinstance(item, dict)),
        key=lambda item: str(item.get("started_at") or item.get("run_id") or ""),
        reverse=True,
    ):
        commands = receipt.get("commands")
        command_records = commands if isinstance(commands, list) else []
        rows.append(
            [
                html.esc(_value(receipt, "run_id")),
                html.esc(_value(receipt, "status")),
                html.esc(_command_values(command_records, "command")),
                html.esc(_command_values(command_records, "exit_code")),
                html.esc(_value(receipt, "duration_seconds")),
                html.esc(_value(receipt, "started_at")),
                _receipt_details(receipt),
            ]
        )

    if not rows:
        return _empty_panel()

    return html.panel(
        html.esc(TITLE),
        html.table(
            [
                html.esc("Run ID"),
                html.esc("Status"),
                html.esc("Command"),
                html.esc("Exit code"),
                html.esc("Duration"),
                html.esc("Timestamp"),
                html.esc("Receipt"),
            ],
            rows,
        ),
    )


def _empty_panel() -> str:
    return html.panel(html.esc(TITLE), f"<p>{html.esc('Nothing here.')}</p>")


def _value(receipt: dict, name: str) -> object:
    value = receipt.get(name)
    return "-" if value is None else value


def _command_values(commands: list[object], name: str) -> str:
    values = [str(command[name]) for command in commands if isinstance(command, dict) and name in command]
    return "; ".join(values) if values else "-"


def _receipt_details(receipt: dict) -> str:
    rendered = json.dumps(receipt, indent=2, sort_keys=True)
    if len(rendered) > 4_000:
        rendered = f"{rendered[:4_000]}\n[Receipt truncated at 4000 characters.]"
    return f"<details><summary>Receipt JSON</summary><pre>{html.esc(rendered)}</pre></details>"
