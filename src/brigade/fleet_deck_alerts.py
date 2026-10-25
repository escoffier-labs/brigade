"""Table of active alerts, using the overview's unchanged evidence projection."""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from . import fleet_action_page, fleet_command_deck as deck, fleet_deck_activity as activity, ui_theme
from .fleet_deck_overview import Alert, _esc, page_alerts


def render_alerts_page(
    view: deck.DeckView,
    now: datetime,
    *,
    nonce: str,
    worklore_enabled: bool,
    action_sources: frozenset[str] = frozenset(),
) -> str:
    alerts = page_alerts(view, now)
    content = render_alert_rows(alerts, action_sources=action_sources)
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


def render_alert_rows(
    alerts: Sequence[Alert], *, action_sources: frozenset[str] = frozenset(), compact: bool = False
) -> str:
    """Keep subject, state, age and the actual action together at every width."""
    rows = []
    for alert in alerts:
        detail = activity.REASONS.get(alert.detail, alert.detail) if alert.kind == "blocked" else alert.detail
        action = fleet_action_page.alert_link(alert.source_ref, action_sources, alert.title)
        if not action:
            action = '<span class="station-meta">Inspect evidence' + ("" if compact else " below") + "</span>"
        evidence = (
            f'<details class="{"alert-evidence-inline" if compact else "alert-evidence"}"><summary>Evidence</summary>'
            f'<p class="alert-detail">{_esc(detail)}</p></details>'
        )
        rows.append(
            '<li class="alert-row"><div><span class="attention-kind">' + _esc(alert.kind) + "</span>"
            f'<h3>{_esc(alert.title)}</h3><p class="alert-age">{_esc(alert.since)}</p>'
            + (evidence if compact else "")
            + "</div>"
            f'<div class="alert-action">{action}</div>' + ("" if compact else evidence) + "</li>"
        )
    return '<ul class="alert-rows">' + "".join(rows) + "</ul>" if rows else '<p class="empty">No active alerts</p>'
