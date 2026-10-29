"""Bounded, read-only Worklore task projection for the Command Deck."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from . import fleet_command_deck as deck
from . import fleet_investigation as investigation
from . import worklore_store, fleet_admin_explorer as explorer
from .fleet_deck_overview import format_age, seconds_since
from .ui_theme import empty_state
from .worklore_validate import WorkloreValidationError, safe_https_url

PAGE_SIZE = 25
CURSOR_MAX = 2048
REFERENCE_LIMIT = 8
TEXT_LIMIT = 256
DESCRIPTION_LIMIT = 1000
BLOCKER_LIMIT = 500


def parse_query(query: str) -> str | None:
    """Accept only one bounded cursor; never reflect invalid query contents."""
    # Python 3.10's strict parser treats an empty query as a malformed field.
    if query == "":
        return None
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
    if url:
        parsed = urlsplit(url)
        if parsed.path == "/work" or parsed.path.startswith("/work/"):
            return None
        try:
            params = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=32)
        except ValueError:
            return None
        if "token" in params:
            return None
    return url


def _references(item: Mapping[str, Any]) -> str:
    links = item.get("links") or []
    if not links and not item.get("links_truncated"):
        return ""
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
        parts.append(
            empty_state(
                "No stored references in this projection", "The projection omitted every reference for this task."
            )
        )
    if len(links) > REFERENCE_LIMIT:
        parts.append(f'<p class="station-meta">Showing {REFERENCE_LIMIT} of {len(links)} projected references.</p>')
    if item.get("links_truncated"):
        parts.append('<p class="station-meta">Store reports additional references omitted from this projection.</p>')
    return "".join(parts)


def _item(item: Mapping[str, Any], *, now: datetime) -> str:
    fields = (
        ("Execution mode", "execution_mode", TEXT_LIMIT),
        ("Version", "version", TEXT_LIMIT),
        ("Updated at (work-record update time)", "updated_at", TEXT_LIMIT),
        ("Work ID", "work_id", TEXT_LIMIT),
    )
    facts = "".join(
        f'<div><dt>{label}</dt><dd class="work-text">{_text(item.get(key), limit=limit)}</dd></div>'
        for label, key, limit in fields
        if item.get(key) is not None and item.get(key) != ""
    )
    burn = item.get("burn_eligible")
    burn_fact = (
        f"<div><dt>Burn eligible</dt><dd>{'true' if burn else 'false'}</dd></div>" if isinstance(burn, bool) else ""
    )
    scope = (
        f'<dl class="fields"><div><dt>Repository</dt><dd>{_text(item["scope"])}</dd></div></dl>'
        if item.get("scope")
        else ""
    )
    blocker = f"<p>Blocker: {_text(item['blocker'], limit=BLOCKER_LIMIT)}</p>" if item.get("blocker") else ""
    age = format_age(seconds_since(str(item.get("updated_at") or ""), now))
    return (
        '<article class="work-item panel"><div class="detail-hero"><h2>'
        + _text(item.get("title") or "Untitled task")
        + "</h2>"
        + explorer.pill(str(item.get("status") or "unknown")[:TEXT_LIMIT])
        + "</div>"
        + scope
        + (
            f'<h3>Description</h3><p class="work-text">{_text(item["description"], limit=DESCRIPTION_LIMIT)}</p>'
            if item.get("description")
            else ""
        )
        + blocker
        + (
            "<h3>Acceptance</h3><ul>"
            + "".join(f"<li>{_text(value, limit=DESCRIPTION_LIMIT)}</li>" for value in item.get("acceptance", []))
            + "</ul>"
            if item.get("acceptance")
            else ""
        )
        + _references(item)
        + explorer.agent(
            "<h3>Evidence</h3>"
            + f'<p>Record age: {_text(age)}</p><dl class="fields">'
            + facts
            + burn_fact
            + "</dl>"
            + "<p>Recorded task status does not confirm live execution.</p>"
        )
        + (
            investigation.render(
                str(item.get("title") or "Untitled task")[:TEXT_LIMIT]
                + (" [truncated]" if len(str(item.get("title") or "")) > TEXT_LIMIT else ""),
                route="/deck/work",
                observation=str(item.get("updated_at") or "unknown") + "; record age " + age,
                facts={
                    label: (
                        str(item.get(key))[: BLOCKER_LIMIT if key == "blocker" else TEXT_LIMIT]
                        + (
                            " [truncated]"
                            if len(str(item.get(key))) > (BLOCKER_LIMIT if key == "blocker" else TEXT_LIMIT)
                            else ""
                        )
                    )
                    if item.get(key) is not None
                    else None
                    for label, key in (
                        ("Work ID", "work_id"),
                        ("Stored status", "status"),
                        ("Blocker", "blocker"),
                        ("Scope", "scope"),
                        ("Execution mode", "execution_mode"),
                        ("Version", "version"),
                    )
                },
                limits="Stored task record, not live execution. Work-record update time is separate from snapshot observation time.",
            )
            if investigation.needed(str(item.get("status") or "unknown")) or item.get("blocker")
            else ""
        )
        + "</article>"
    )


def render(page: Mapping[str, Any], *, nonce: str, now: datetime) -> str:
    """Render plain stored fields. No description parsing or execution inference."""
    items = page.get("items") or []
    entries = [
        explorer.Entry(
            str(item.get("work_id") or index),
            str(item.get("title") or "Untitled task"),
            f"Stored {item.get('status') or 'unknown'} · {item.get('scope') or 'scope unspecified'}",
            _item(item, now=now),
        )
        for index, item in enumerate(items[:PAGE_SIZE])
    ]
    blocks = explorer.render(
        entries,
        label="stored tasks",
        empty="No Worklore tasks on this page",
        empty_hint="Tasks appear here once Worklore holds one, for example after `brigade fleet work sync-github` imports labeled GitHub issues.",
    )
    pagination = '<nav class="pager" aria-label="Worklore pages"><a href="/deck/work">First page</a>'
    cursor = page.get("next_cursor")
    if cursor:
        href = "/deck/work?" + urlencode({"cursor": cursor})
        pagination += f'<a href="{deck._esc(href)}">Next page</a>'
    pagination += "</nav>"
    body = '<main class="deck-shell admin-page">' + pagination + blocks + pagination + "</main>"
    return deck._document(
        body,
        nonce=nonce,
        now=now,
        title="Stored tasks",
        refresh=False,
        active="work",
        worklore_enabled=True,
        extra_css=explorer.CSS,
        script=explorer.SCRIPT,
    )


def load_observed_group(conn: sqlite3.Connection, group: deck.ObservedWorkGroup) -> dict[str, Any]:
    """Read only configured IDs through the store's bounded item projection."""
    items = []
    unavailable = []
    for work_id in group.work_ids:
        try:
            item = worklore_store.get_item(conn, work_id)
        except worklore_store.WorkloreNotFound:
            unavailable.append((work_id, "missing"))
            continue
        if item.get("archived_at") or item.get("status") == "archived":
            unavailable.append((work_id, "archived"))
        else:
            references = worklore_store.list_links_page(conn, work_id, limit=REFERENCE_LIMIT)
            item["links"] = references["links"]
            item["links_truncated"] = bool(references["next_cursor"])
            items.append(item)
    return {"group": group, "items": items, "unavailable": unavailable}


def render_observed_group(page: Mapping[str, Any], *, nonce: str, now: datetime, stale_after_seconds: int) -> str:
    group = page["group"]
    stamp, freshness = deck.snapshot_freshness(
        group.snapshot_observed_at, now=now, stale_after_seconds=stale_after_seconds
    )
    blocks = "".join(_item(item, now=now) for item in page["items"][:PAGE_SIZE])
    blocks += "".join(
        '<article class="panel"><h2>Unavailable work reference</h2><p>Work ID: '
        + _text(work_id)
        + "</p><p>Record unavailable: "
        + _text(reason)
        + "</p>"
        + investigation.render(
            "Retained work reference",
            route="/deck/observed/" + group.key,
            observation=stamp,
            facts={
                "Work ID": work_id,
                "State": reason,
                "Coverage": group.coverage,
                "Configured source": group.source_ref,
            },
            limits="Configured retained observation; missing or archived task is not evidence of execution failure.",
        )
        + "</article>"
        for work_id, reason in page["unavailable"][:PAGE_SIZE]
    )
    body = (
        '<main class="deck-shell"><header class="masthead"><div><p class="eyebrow">Observed work</p><h1>'
        + _text(group.label)
        + "</h1></div></header>"
        '<section class="panel"><h2>Retained task observations</h2>'
        + f"<p>{len(group.work_ids)} configured observation records</p><p>Coverage: "
        + _text(group.coverage)
        + "</p><p>Configured source: "
        + _text(group.source_ref)
        + "<br>Configured proxy: "
        + _text(group.proxy_ref)
        + "<br>Configured parent: "
        + _text(group.parent_ref)
        + "</p><p>Snapshot observation time (operator-configured metadata): "
        + _text(stamp)
        + f"<br>Snapshot freshness: {freshness}. Stale after {stale_after_seconds} seconds.</p>"
        "<p>Recent observation metadata does not establish live activity. Configured references are plain "
        "attribution. Work-record update time describes changes to the stored task and is separate from "
        "snapshot observation time.</p><p>Task previews and stored references are bounded. Truncation is marked.</p>"
        "</section>" + blocks + "</main>"
    )
    return deck._document(
        body, nonce=nonce, now=now, title=group.label, refresh=False, active="work", worklore_enabled=True
    )
