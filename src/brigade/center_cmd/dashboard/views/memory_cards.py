"""Memory cards dashboard view (stub)."""

from __future__ import annotations

from pathlib import Path

from brigade.center_cmd.dashboard import render as html

NAME = "cards"
TITLE = "Cards"
ORDER = 3


def fetch(target: Path) -> dict:
    return {}


def render(payload: dict, nonce: str) -> str:
    del payload, nonce
    return html.panel(
        html.esc("Cards"),
        f"<p>{html.esc('Not implemented yet.')}</p>",
    )
