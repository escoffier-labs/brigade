"""Read-only aggregate adoption page rendered with the Deck's ui_theme shell."""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any

from . import fleet_admin_stats as stats, fleet_command_deck as deck


def _number(value: Any, field: str) -> str:
    return f'<span data-field="{field}">{"unknown" if value is None else escape(str(value))}</span>'


def render(view: dict[str, Any], *, nonce: str, now: datetime) -> str:
    latest = view["latest"]
    body = '<main class="deck-shell"><h1>Adoption statistics</h1><section class="panel">'
    body += "<p>Counts describe active sources, not people or installs. Source cells below 5 are suppressed. Related retention and histogram counts may also be hidden to prevent disclosure by subtraction. "
    body += "Unknown includes suppressed cells and unavailable data. Retention means activity on 2+ or 3+ distinct days within one week. "
    body += "Possible upgrades link versions observed in time order within the same week. Weekly source identities rotate.</p>"
    body += "<p>Controller and erasure contact: Solomon Neas. Contact the maintainer at the address in "
    body += '<a href="https://github.com/escoffier-labs/brigade/blob/main/SECURITY.md">SECURITY.md</a>. '
    body += "The Hub retains aggregate snapshots for 400 days. Opt-in usage aggregates are not available yet. "
    body += "Future opt-in counts will undercount usage.</p>"
    body += '</section><section class="panel"><h2>Latest snapshot</h2>'
    for field in ("schema", "snapshot_id", "taken_at", "source_set", "digest"):
        value = latest[field] if latest else "unknown"
        if isinstance(value, list):
            value = ", ".join(value) or "unknown"
        body += f'<p data-field="{field}">{escape(field)}: {escape(str(value))}</p>'
    body += '<div data-field="downloads"><h3>Public downloads over 30 completed UTC days</h3>'
    for name in ("pypi", "npm"):
        body += (
            f"<p>{name} downloads: "
            + _number(latest["downloads"][name] if latest else None, "downloads." + name)
            + "</p>"
        )
    body += '</div><div data-field="daily"><h3>Worker activity over 30 completed UTC days</h3>'
    if not latest or not latest["daily"]:
        body += "<p>unknown</p>"
    for day in latest["daily"] if latest else []:
        body += (
            "<p>"
            + _number(day["day"], "daily.day")
            + " Worker requests: "
            + _number(day["worker_requests"], "daily.worker_requests")
        )
        body += " Worker errors: " + _number(day["worker_errors"], "daily.worker_errors") + "</p>"
    body += '</div></section><section class="panel" data-field="weeks"><h2>Weekly series</h2>'
    if not view["weeks"]:
        body += "<p>unknown</p>"
    for week in view["weeks"]:
        body += "<article><h3>" + _number(week["week"], "weeks.week") + '</h3><p data-field="weeks.partial">'
        body += "partial" if week["partial"] else "complete"
        body += "</p>"
        for field, label in zip(
            stats.SOURCE_FIELDS, ("Active sources", "2+ day active sources", "3+ day active sources"), strict=True
        ):
            body += f"<p>{label}: " + _number(week[field], "weeks." + field) + "</p>"
        for field in ("versions", "upgrades"):
            body += f'<div data-field="weeks.{field}"><h4>{field.title()}</h4>'
            for cell in week[field]:
                body += "<p>"
                for key, value in cell.items():
                    body += escape(key.replace("_", " ")) + ": " + _number(value, f"weeks.{field}.{key}") + " "
                body += "</p>"
            body += "<p>other (suppressed): unknown</p></div>"
        body += "</article>"
    body += "</section></main>"
    return deck._document(body, nonce=nonce, now=now, title="Statistics", refresh=False, active="stats")
