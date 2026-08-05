"""Memory cards dashboard view."""

from __future__ import annotations

from pathlib import Path

from brigade.center_cmd.dashboard import data
from brigade.center_cmd.dashboard import render as html

NAME = "cards"
TITLE = "Cards"
ORDER = 3

# memory search has no list-all mode; card frontmatter contains YAML key separators.
# A leading-dash query such as "---" is parsed as an option by argparse.
_LIST_QUERY = ":"
_LIST_LIMIT = "500"
_TABLE_ID = "memory-cards-table"


def fetch(target: Path) -> dict:
    return data.run_json(
        target,
        ["memory", "search", _LIST_QUERY, "--limit", _LIST_LIMIT],
    )


def render(payload: dict, nonce: str) -> str:
    del nonce
    if payload.get("error"):
        return html.error_panel(TITLE, str(payload["error"]))

    matches = payload.get("matches")
    if not isinstance(matches, list) or not matches:
        return html.panel(html.esc(TITLE), f"<p>{html.esc('Nothing here.')}</p>")

    rows: list[list[str]] = []
    for item in matches:
        if not isinstance(item, dict):
            continue
        rows.append(
            [
                _esc_cell(_cell(item, "title")),
                _esc_cell(_tags(item.get("tags"))),
                # memory search does not currently emit reviewed/freshness fields.
                _esc_cell(_cell(item, "last_reviewed", "reviewed", "reviewed_at")),
                _esc_cell(_cell(item, "fresh_until", "freshness", "expires_at")),
            ]
        )

    if not rows:
        return html.panel(html.esc(TITLE), f"<p>{html.esc('Nothing here.')}</p>")

    table_id = html.esc(_TABLE_ID)
    table_html = html.table(
        [
            html.esc("Title"),
            html.esc("Tags"),
            html.esc("Reviewed"),
            html.esc("Freshness"),
        ],
        rows,
    )
    table_html = table_html.replace(
        '<table class="data-table">',
        f'<table id="{table_id}" class="data-table">',
        1,
    )
    filter_input = (
        f'<p><input type="search" '
        f'placeholder="{html.esc("Filter cards")}" '
        f'data-filter-target="{table_id}" '
        f'aria-label="{html.esc("Filter cards")}"></p>'
    )
    return html.panel(html.esc(TITLE), filter_input + table_html)


def _esc_cell(value: object) -> str:
    """Escape a card field for table output.

    Plain HTML escaping is the whole defence: once ``<`` and ``>`` are entities,
    the value is text and no tag or attribute can form from it. An ``on*=``
    substring surviving inside that escaped text is inert, so do not add a
    cosmetic ``=`` transform to make a substring search look clean.
    """
    return html.esc(value)


def _cell(item: dict, *keys: str) -> object:
    for key in keys:
        value = item.get(key)
        if value is not None and value != "":
            return value
    return "-"


def _tags(value: object) -> str:
    if isinstance(value, list):
        parts = [str(part) for part in value if part is not None and str(part) != ""]
        return ", ".join(parts) if parts else "-"
    if value is None or value == "":
        return "-"
    return str(value)
