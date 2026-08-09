"""Outcome rank dashboard view."""

from __future__ import annotations

from pathlib import Path

from brigade.center_cmd.dashboard import data
from brigade.center_cmd.dashboard import render as html

NAME = "outcomes"
TITLE = "Outcomes"
ORDER = 4

_EMPTY_MESSAGE = (
    "The outcome ranking is empty. The outcome loop is not being fed: "
    "run verification through a registered verifier and capture outcomes."
)


def fetch(target: Path) -> dict:
    return data.run_json(target, ["outcome", "rank"])


def render(payload: dict, nonce: str) -> str:
    del nonce
    if payload.get("error"):
        return html.error_panel(TITLE, str(payload["error"]))

    ranking = payload.get("ranking")
    if not isinstance(ranking, list):
        ranking = []
    entries = [entry for entry in ranking if isinstance(entry, dict)]
    if not entries:
        return html.panel(html.esc(TITLE), f"<p>{html.esc(_EMPTY_MESSAGE)}</p>")

    headers = [
        "Artifact",
        "Score",
        "Helped",
        "Hurt",
        "Capability score",
        "Capability helped",
        "Capability hurt",
        "Stale records",
        "Legacy records",
    ]
    rows = []
    for entry in entries:
        rows.append(
            [
                html.esc(entry.get("artifact_id", "-")),
                html.esc(_fmt(entry.get("score"))),
                html.esc(_fmt(entry.get("helped"))),
                html.esc(_fmt(entry.get("hurt"))),
                html.esc(_fmt(entry.get("capability_score"))),
                html.esc(_fmt(entry.get("capability_helped"))),
                html.esc(_fmt(entry.get("capability_hurt"))),
                html.esc(_fmt(entry.get("stale_records"))),
                html.esc(_fmt(entry.get("legacy_records"))),
            ]
        )
    escaped_headers = [html.esc(header) for header in headers]
    return html.panel(html.esc(TITLE), html.table(escaped_headers, rows))


def _fmt(value: object) -> object:
    """Format a cell value; floats get 3 decimals, missing keys a dash."""
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}"
    return value
