"""Read-only Command Deck page for the ownership map (``/deck/owners``)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from . import fleet_command_deck as deck
from .fleet_owners import Holding, Overlap, OwnersView, charter_only

DISPLAY_LIMIT = 50


def _items(values: Sequence[str]) -> str:
    if not values:
        return '<span class="station-meta">none declared</span>'
    return '<ul class="observer-list">' + "".join(f"<li>{deck._esc(value)}</li>" for value in values) + "</ul>"


def _table(items: Sequence[Holding], *, empty: str) -> str:
    if not items:
        return f'<p class="empty">{deck._esc(empty)}</p>'
    rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{deck._esc(value)}</td>"
            for value in (item.kind, item.ref, item.repo, item.node_label, item.detail, item.since)
        )
        + "</tr>"
        for item in items[:DISPLAY_LIMIT]
    )
    note = (
        f'<p class="station-meta">Showing {DISPLAY_LIMIT} of {len(items)} items.</p>'
        if len(items) > DISPLAY_LIMIT
        else ""
    )
    return (
        '<div class="table-wrap"><table class="owners-table"><thead><tr><th>Kind</th><th>Item</th><th>Repository</th><th>Node</th>'
        f"<th>Detail</th><th>Since</th></tr></thead><tbody>{rows}</tbody></table></div>{note}"
    )


def _principals(match: Mapping[str, Any], enrolled: frozenset[str]) -> str:
    names = match.get("principals") or []
    if not names:
        return ""
    entries = "".join(
        f"<li>{deck._esc(name)}: {'enrolled' if name in enrolled else 'not enrolled'}</li>" for name in names
    )
    return f'<dt>Message principals</dt><dd><ul class="observer-list">{entries}</ul></dd>'


def _holdings(view: OwnersView, name: str, match: Mapping[str, Any]) -> str:
    if charter_only(match):
        return '<p class="station-meta">Charter only, no live attribution.</p>'
    # Principals never attribute holdings, so only a holding source failure counts here.
    holding_gaps = [source for source in view.unknown_sources if source != "principals"]
    if holding_gaps:
        sources = ", ".join(holding_gaps)
        return f'<p class="station-meta">Holdings unknown: could not read {deck._esc(sources)}.</p>' + _table(
            view.by_owner.get(name, ()), empty="No holdings from the sources that could be read."
        )
    return _table(view.by_owner.get(name, ()), empty="Nothing held right now.")


def _card(view: OwnersView, name: str) -> str:
    charter = view.charters[name]
    match = charter.get("match") or {}
    summary = charter.get("summary")
    contact = charter.get("contact")
    return (
        '<article class="panel owner-card"><h2>'
        + deck._esc(charter.get("label") or name)
        + f' <span class="station-meta">{deck._esc(name)}</span></h2>'
        + (f"<p>{deck._esc(summary)}</p>" if summary else "")
        + '<dl class="tile-facts">'
        + f"<dt>Owns</dt><dd>{_items(charter.get('owns') or [])}</dd>"
        + f"<dt>Never</dt><dd>{_items(charter.get('never') or [])}</dd>"
        + f"<dt>Intake</dt><dd>{_items(charter.get('intake') or [])}</dd>"
        + f"<dt>Contact</dt><dd>{deck._esc(contact) if contact else '(none declared)'}</dd>"
        + _principals(match, view.enrolled_principals)
        + "</dl><h3>Holds now</h3>"
        + _holdings(view, name, match)
        + "</article>"
    )


def _overlaps(overlaps: Sequence[Overlap]) -> str:
    if not overlaps:
        return '<p class="empty">No overlaps.</p>'
    entries = "".join(
        f"<li>{deck._esc(overlap.item.kind)} {deck._esc(overlap.item.ref)}: "
        + deck._esc(", ".join(overlap.owners))
        + "</li>"
        for overlap in overlaps[:DISPLAY_LIMIT]
    )
    note = (
        f'<p class="station-meta">Showing {DISPLAY_LIMIT} of {len(overlaps)} overlaps.</p>'
        if len(overlaps) > DISPLAY_LIMIT
        else ""
    )
    return f'<ul class="observer-list">{entries}</ul>{note}'


def render(view: OwnersView, *, nonce: str, now: datetime) -> str:
    """Charters beside live holdings, then Unassigned and Overlaps. No forms, no actions."""
    banners = ""
    if view.policy_error:
        banners += (
            '<section class="panel"><h2>Policy unavailable</h2><p>'
            + deck._esc(view.policy_error)
            + ". Charters are not shown; every live item is listed as unassigned.</p></section>"
        )
    revision = "unknown" if view.policy_version is None else str(view.policy_version)
    cards = "".join(_card(view, name) for name in sorted(view.charters))
    if not cards and not view.policy_error:
        cards = (
            '<section class="panel"><p class="empty">No charters declared. Save an owners section in the fleet '
            "policy document with <code>brigade fleet policy save</code> to populate this page.</p></section>"
        )
    unassigned_note = (
        f'<p class="station-meta">Could not read: {deck._esc(", ".join(view.unknown_sources))}.</p>'
        if view.unknown_sources
        else ""
    ) + (
        f'<p class="station-meta">Truncated at the source limit: {deck._esc(", ".join(view.truncated_sources))}.</p>'
        if view.truncated_sources
        else ""
    )
    body = (
        '<main class="deck-shell"><header class="masthead"><div><p class="eyebrow">Fleet operations</p>'
        "<h1>Command Deck &middot; Owners</h1></div>"
        f'<p class="header-meta">{deck._esc(deck._stamp(now))}</p></header>'
        + banners
        + '<section class="panel"><h2>Charters</h2><p>Read-only. Each charter is declared in the fleet policy '
        f"document (revision {deck._esc(revision)}) and describes responsibility. It does not block claims, "
        "dispatch, or merges.</p></section>"
        + cards
        + '<section class="panel"><h2>Unassigned</h2><p>Live items that no charter matches.</p>'
        + unassigned_note
        + _table(view.unassigned, empty="Nothing unassigned.")
        + "</section>"
        + '<section class="panel"><h2>Overlaps</h2><p>Items that more than one charter matches. '
        "Edit a charter to resolve one.</p>" + _overlaps(view.overlaps) + "</section></main>"
    )
    return deck._document(body, nonce=nonce, now=now, title="Owners", refresh=False, active="owners")
