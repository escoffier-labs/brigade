"""Bounded, read-only Worklore task projection for the Command Deck."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from . import fleet_command_deck as deck
from . import worklore_store
from .worklore_validate import WorkloreValidationError, safe_https_url

PAGE_SIZE = 25
CURSOR_MAX = 2048
REFERENCE_LIMIT = 8
TEXT_LIMIT = 256
DESCRIPTION_LIMIT = 1000
BLOCKER_LIMIT = 500


def parse_query(query: str) -> str | None:
    """Accept only one bounded cursor; never reflect invalid query contents."""
    params = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=1)
    if set(params) - {"cursor"}:
        raise ValueError("invalid work page query")
    return params["cursor"][0] if "cursor" in params else None


def load_page(conn: sqlite3.Connection, *, cursor: str | None = None) -> dict[str, Any]:
    # The store decoder assumes ASCII; reject Unicode before it can raise an
    # encoding error outside the store's validation-error contract.
    if cursor is not None and (not cursor or not cursor.isascii() or len(cursor) > CURSOR_MAX):
        raise ValueError("invalid work page cursor")
    return worklore_store.list_items(conn, limit=PAGE_SIZE, cursor=cursor)


def _text(value: object, *, limit: int = TEXT_LIMIT) -> str:
    text = "" if value is None else str(value)
    if not text:
        return "(none stored)"
    suffix = f" [truncated at {limit} characters]" if len(text) > limit else ""
    return deck._esc(text[:limit]) + suffix


def _reference_url(value: object) -> str | None:
    if not isinstance(value, str) or any(ch.isspace() or ch in "\\\"'<>" for ch in value):
        return None
    try:
        url = safe_https_url(value)
    except WorkloreValidationError:
        return None
    # Stored references can outlive their importer. Never offer the hub's
    # bearer-only Worklore API as a browser destination.
    if url and (urlsplit(url).path == "/work" or urlsplit(url).path.startswith("/work/")):
        return None
    return url


def _references(item: Mapping[str, Any]) -> str:
    links = item.get("links") or []
    parts = [
        '<h3>Stored references</h3><p class="station-meta">Source, parent and evidence '
        "references are shown as stored. External keys are opaque identifiers.</p>"
    ]
    visible = links[:REFERENCE_LIMIT]
    if visible:
        parts.append('<ul class="observer-list">')
        for link in visible:
            parts.append(
                "<li>Type: "
                + _text(link.get("link_type"))
                + "<br>Display reference: "
                + _text(link.get("display_ref"))
                + "<br>External key: "
                + _text(link.get("external_key"))
            )
            raw_url = link.get("url")
            if raw_url:
                url = _reference_url(raw_url)
                if url:
                    parts.append(f'<br>URL: <a href="{deck._esc(url)}" rel="noopener noreferrer">{_text(url)}</a>')
                else:
                    parts.append("<br>URL (plain text): " + _text(raw_url))
            parts.append("</li>")
        parts.append("</ul>")
    else:
        parts.append('<p class="empty">No stored references in this projection.</p>')
    if len(links) > REFERENCE_LIMIT:
        parts.append(f'<p class="station-meta">Showing {REFERENCE_LIMIT} of {len(links)} projected references.</p>')
    if item.get("links_truncated"):
        parts.append('<p class="station-meta">Store reports additional references omitted from this projection.</p>')
    return "".join(parts)


def _item(item: Mapping[str, Any]) -> str:
    fields = (
        ("Description", "description", DESCRIPTION_LIMIT),
        ("Blocker", "blocker", BLOCKER_LIMIT),
        ("Scope", "scope", TEXT_LIMIT),
        ("Stored status", "status", TEXT_LIMIT),
        ("Execution mode", "execution_mode", TEXT_LIMIT),
        ("Version", "version", TEXT_LIMIT),
        ("Updated at", "updated_at", TEXT_LIMIT),
        ("Work ID", "work_id", TEXT_LIMIT),
    )
    facts = "".join(
        f'<dt>{label}</dt><dd class="work-text">{_text(item.get(key), limit=limit)}</dd>'
        for label, key, limit in fields
    )
    burn = item.get("burn_eligible")
    burn_text = "true" if burn is True else "false" if burn is False else "unknown"
    return (
        '<article class="work-item panel"><h2>'
        + _text(item.get("title"))
        + '</h2><dl class="tile-facts">'
        + facts
        + f"<dt>Burn eligible</dt><dd>{burn_text}</dd></dl>"
        + _references(item)
        + "</article>"
    )


def render(page: Mapping[str, Any], *, nonce: str, now: datetime) -> str:
    """Render plain stored fields. No description parsing or execution inference."""
    items = page.get("items") or []
    blocks = "".join(_item(item) for item in items[:PAGE_SIZE])
    if not blocks:
        blocks = '<section class="panel"><p class="empty">No Worklore tasks on this page.</p></section>'
    pagination = '<nav class="deck-nav" aria-label="Worklore pages"><a href="/deck/work">First page</a>'
    cursor = page.get("next_cursor")
    if cursor:
        href = "/deck/work?" + urlencode({"cursor": cursor})
        pagination += f'<a href="{deck._esc(href)}">Next page</a>'
    pagination += "</nav>"
    body = (
        '<main class="deck-shell"><header class="masthead"><div><p class="eyebrow">Fleet operations</p>'
        "<h1>Command Deck &middot; Worklore</h1></div>"
        f'<p class="header-meta">{deck._esc(deck._stamp(now))}</p></header>'
        '<nav class="deck-nav" aria-label="Command Deck"><a href="/">deck</a> <a href="/deck/repos">repos</a> '
        '<a href="/deck/roster">roster</a> <a href="/deck/policy">policy</a> '
        '<a href="/deck/work" aria-current="page">work</a> <a href="/view/machines">machines board</a></nav>'
        '<section class="panel"><h2>Stored tasks</h2>'
        "<p>Read-only view of stored Worklore tasks. captured / manual / burn_eligible=false records describe captured work "
        "configured for manual execution and excluded from burn selection. Execution state is not inferred "
        "from these fields or from descriptions.</p>"
        f'<p class="station-meta">At most {PAGE_SIZE} tasks per page; description previews are limited to '
        f"{DESCRIPTION_LIMIT} characters, blockers to {BLOCKER_LIMIT}, other text to {TEXT_LIMIT}, "
        f"and reference summaries to {REFERENCE_LIMIT} per task. Truncation is marked.</p></section>"
        + pagination
        + blocks
        + pagination
        + "</main>"
    )
    return deck._document(body, nonce=nonce, now=now, title="Worklore", refresh=False)
