"""Read-only aggregate adoption page rendered with the Deck's ui_theme shell."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

from . import fleet_admin_explorer as explorer
from . import fleet_admin_stats as stats, fleet_command_deck as deck
from . import fleet_investigation as investigation


def _number(value: Any, field: str) -> str:
    return f'<span data-field="{field}">{"unknown" if value is None else escape(str(value))}</span>'


def _plain(value: Any) -> str:
    return "unknown" if value is None else str(value)


def _headline(view: dict[str, Any]) -> str:
    """The numbers an admin reads first. The data-field markup stays in the agent blocks below."""
    latest = view["latest"]
    weeks = view["weeks"]
    if not latest and not weeks:
        return (
            '<section class="panel"><h2>No adoption observations available</h2>'
            "<p>Collection may be unconfigured or not yet reported. Missing and privacy-suppressed data do not "
            "establish zero activity.</p></section>"
        )
    downloads = latest["downloads"] if latest else {}
    daily = latest["daily"] if latest else []
    requests = sum(day["worker_requests"] or 0 for day in daily) if daily else None
    errors = sum(day["worker_errors"] or 0 for day in daily) if daily else None
    current = weeks[0] if weeks else None
    values = {
        "PyPI downloads, 30 days": _plain(downloads.get("pypi")),
        "npm downloads, 30 days": _plain(downloads.get("npm")),
        "Active sources, latest week": _plain(current["active_sources"] if current else None),
        "2+ day active sources": _plain(current["retained_2d_sources"] if current else None),
        "3+ day active sources": _plain(current["retained_3d_sources"] if current else None),
        "Worker requests, 30 days": _plain(requests),
        "Worker errors, 30 days": _plain(errors),
    }
    rows = "".join(
        f"<tr><td>{escape(str(week['week']))}"
        f"{' ' + explorer.pill('partial') if week['partial'] else ''}</td>"
        + "".join(f"<td>{escape(_plain(week[field]))}</td>" for field in stats.SOURCE_FIELDS)
        + "</tr>"
        for week in weeks[:8]
    )
    table = (
        '<h3>Recent weeks</h3><div class="table-wrap"><table><thead><tr><th>Week</th><th>Active sources</th>'
        f"<th>2+ day</th><th>3+ day</th></tr></thead><tbody>{rows}</tbody></table></div>"
        if rows
        else ""
    )
    return (
        '<section class="panel" aria-label="Adoption headline"><h2>Adoption</h2>'
        + explorer.facts(values)
        + table
        + "<p>Counts describe active sources, not people or installs. Unknown means suppressed or unavailable.</p>"
        "</section>"
    )


def render(view: dict[str, Any], *, nonce: str, now: datetime) -> str:
    latest = view["latest"]
    # None hides the panel: opted out, no observation yet, or a retained v1 snapshot.
    cloudflare = view.get("cloudflare")
    stale = False
    if cloudflare:
        collected_at = datetime.strptime(cloudflare["collected_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age = now.astimezone(timezone.utc) - collected_at
        stale = age > timedelta(minutes=15) or age < -timedelta(minutes=5)
    body = '<main class="deck-shell admin-page">' + _headline(view)
    if latest or view["weeks"]:
        body += explorer.agent(
            f"<p>Weekly observations: {len(view['weeks'])}. "
            f"Latest snapshot: {escape(str(latest['taken_at'])) if latest else 'unavailable'}.</p>"
        )
    if not latest and not view["weeks"]:
        body += investigation.render(
            "Adoption observations",
            route="/deck/stats",
            observation=now,
            facts={"State": "no observations", "Weekly observations": 0},
            limits="Page render time; collection may be unconfigured or not yet reported. Missing and privacy-suppressed data do not establish zero activity.",
        )
    body += '<div data-audience="agent"><section class="panel"><h2>Methodology</h2>'
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
        if day["worker_errors"]:
            body += investigation.render(
                "Worker error observations",
                route="/deck/stats",
                observation=latest["taken_at"],
                facts={
                    "Source set": ", ".join(latest["source_set"]),
                    "Day": day["day"],
                    "Worker requests": day["worker_requests"],
                    "Worker errors": day["worker_errors"],
                },
                limits="Reported daily aggregate counters, not request logs. Unknown includes privacy-suppressed data. Current service state and individual exceptions not collected.",
            )
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
    body += "</section>"
    body += "</div>"
    if cloudflare:
        # A stale observation renders every cell as unknown, never old values.
        shown = None if stale else cloudflare
        body += '<section class="panel" data-field="cloudflare"><h2>Cloudflare status</h2>'
        credential = "unknown" if shown is None else shown["credential"]
        if credential == "unavailable":
            credential = "credential unavailable"
        body += f'<p>Credential: <span data-field="cloudflare.credential">{escape(credential)}</span></p>'
        body += explorer.agent(
            "<p>Collected: " + _number(shown["collected_at"] if shown else None, "cloudflare.collected_at") + "</p>"
        )
        for field, label in (
            ("worker_version", "Deployed Worker version"),
            ("worker_published_at", "Deployment time"),
            ("repository_head", "Repository head"),
            ("d1_rows", "D1 rows"),
            ("last_purge", "Last purge"),
            ("requests_24h", "Worker requests, 24 hours"),
            ("errors_24h", "Worker errors, 24 hours"),
        ):
            value = shown[field] if shown else None
            line = f"<p>{label}: " + _number(value, "cloudflare." + field) + "</p>"
            body += explorer.agent(line) if field in {"worker_published_at", "repository_head", "last_purge"} else line
        if stale or credential != "available" or (shown and shown.get("errors_24h")):
            body += investigation.render(
                "Cloudflare reporting",
                route="/deck/stats",
                observation=cloudflare["collected_at"],
                facts={
                    "State": "stale" if stale else credential,
                    "Worker requests, 24 hours": shown.get("requests_24h") if shown else "unknown (stale)",
                    "Worker errors, 24 hours": shown.get("errors_24h") if shown else "unknown (stale)",
                },
                limits="Read-only aggregate observation. Stale cells are unknown; no raw credential or request logs collected.",
            )
        body += explorer.agent("<p>Read-only Cloudflare values older than 15 minutes are unknown.</p>") + "</section>"
    body += "</main>"
    return deck._document(body, nonce=nonce, now=now, title="Statistics", refresh=False, active="stats")
