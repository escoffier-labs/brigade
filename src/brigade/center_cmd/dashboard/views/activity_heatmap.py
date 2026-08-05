"""Activity heatmap dashboard view (stub)."""

from __future__ import annotations

from pathlib import Path

from brigade.center_cmd.dashboard import render as html

NAME = "activity"
TITLE = "Activity"
ORDER = 6


def fetch(target: Path) -> dict:
    return {}


def render(payload: dict, nonce: str) -> str:
    del payload, nonce
    return html.panel(
        html.esc("Activity"),
        f"<p>{html.esc('Not implemented yet.')}</p>",
    )
