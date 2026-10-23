"""Table of active alerts, using the overview's unchanged evidence projection."""

from __future__ import annotations

from datetime import datetime

from . import fleet_action_page, fleet_command_deck as deck, fleet_deck_activity as activity, ui_theme
from .fleet_deck_overview import _esc, page_alerts


def render_alerts_page(
    view: deck.DeckView,
    now: datetime,
    *,
    nonce: str,
    worklore_enabled: bool,
    action_sources: frozenset[str] = frozenset(),
) -> str:
    alerts = page_alerts(view, now)
    rows = []
    for alert in alerts:
        detail = activity.REASONS.get(alert.detail, alert.detail) if alert.kind == "blocked" else alert.detail
        action = fleet_action_page.alert_link(alert.source_ref, action_sources, alert.title)
        rows.append(
            f"<tr><td>{_esc(alert.kind)}</td><td>{_esc(alert.title)}"
            f'<span class="alert-detail">{_esc(detail)}</span></td>'
            f"<td>{_esc(alert.since)}</td><td>{action}</td></tr>"
        )
    content = (
        '<div class="table-wrap"><table class="alerts-table"><thead><tr>'
        '<th>Kind</th><th>Subject</th><th>When</th><th><span class="visually-hidden">Action</span></th>'
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
        if rows
        else '<p class="empty">No active alerts</p>'
    )
    body = (
        '<main class="deck-shell alerts-page"><header class="page-head"><div><h1>Alerts</h1>'
        f'<p class="page-sub">{len(alerts)} open</p></div></header>'
        f'<section class="panel">{content}</section></main>'
    )
    return deck._document(
        body,
        nonce=nonce,
        now=now,
        active="alerts",
        title="Alerts",
        view=view,
        worklore_enabled=worklore_enabled,
        extra_css=ui_theme.ALERTS_CSS,
    )
