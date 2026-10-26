"""Pure sidebar rendering for every Command Deck page."""

from __future__ import annotations

from datetime import datetime

from . import __version__, fleet_deck_icons as icons
from .fleet_command_deck import DeckView
from .fleet_deck_overview import (
    _esc,
    format_age,
    is_stale,
    machine_for,
    seconds_since,
    page_alerts,
)

NAV_ITEMS = (
    ("overview", "/deck", "Overview"),
    ("work", "/view/machines", "Work"),
    ("capacity", "/deck/capacity", "Capacity"),
    ("repos", "/deck/repos", "Repositories"),
    ("roster", "/deck/roster", "Settings"),
)

SECTION_ROUTES = {
    "overview": (("overview", "/deck", "Overview"), ("alerts", "/deck/alerts", "Alerts")),
    "work": (
        ("boards", "/view/machines", "Runs"),
        ("work", "/deck/work", "Stored tasks"),
        ("queue", "/deck#queued-title", "Ready queue"),
    ),
    "capacity": (
        ("capacity", "/deck/capacity#machines-table", "Machines"),
        ("github-reads", "/deck/capacity#octopool-observation", "GitHub reads"),
        ("cloud-details", "/deck/capacity#cloud-details", "Cloud details"),
    ),
    "repos": (
        ("repos", "/deck/repos", "Repositories"),
        ("repo-board", "/view/repos", "Run history"),
        ("ci", "/deck/ci", "Checks"),
        ("owners", "/deck/owners", "Ownership"),
    ),
    "roster": (
        ("roster", "/deck/roster", "Live routing"),
        ("policy", "/deck/policy", "Model assignments"),
        ("stats", "/deck/stats", "Reporting"),
    ),
}


def section_for(active: str) -> str:
    return {
        "alerts": "overview",
        "boards": "work",
        "queue": "work",
        "repo-board": "repos",
        "policy": "roster",
        "stats": "roster",
        "owners": "repos",
        "ci": "repos",
        "github-reads": "capacity",
        "cloud-details": "capacity",
    }.get(active, active)


def render_section_tabs(active: str, *, worklore_enabled: bool) -> str:
    """Ordinary links with an exact current destination; usable without scripts."""
    routes = SECTION_ROUTES.get(section_for(active), SECTION_ROUTES["overview"])
    links = []
    for key, href, label in routes:
        if key == "work" and not worklore_enabled:
            continue
        current = ' aria-current="page"' if key == active else ""
        links.append(f'<a href="{_esc(href)}"{current}>{_esc(label)}</a>')
    return '<nav class="section-tabs" aria-label="Section tabs">' + "".join(links) + "</nav>"


RESOURCE_LINKS: tuple[tuple[str, str, str], ...] = (
    ("docs", "Docs", "https://brigade.tools/docs"),
    ("bug", "Report a bug", "https://github.com/escoffier-labs/brigade/issues/new?template=bug.yml"),
    ("star", "Star on GitHub", "https://github.com/escoffier-labs/brigade"),
)


def _resources() -> str:
    resource_icons = {"docs": icons.DOCS_ICON, "bug": icons.BUG_ICON, "star": icons.STAR_ICON}
    links = "".join(
        f'<a href="{_esc(href)}" target="_blank" rel="noopener noreferrer">'
        f"{resource_icons[key]}<span>{_esc(label)}</span>{icons.EXTERNAL_ICON}</a>"
        for key, label, href in RESOURCE_LINKS
    )
    return (
        '<section class="resources" aria-labelledby="tree-resources">'
        f'<h2 id="tree-resources" class="tree-heading">Resources</h2>{links}</section>'
    )


def _sidebar_meta(view: DeckView | None, now: datetime) -> str:
    telemetry = modifier = ""
    if view is not None:
        plane = view.control_plane
        ages = [] if plane is None else [seconds_since(machine.last_observed_at, now) for machine in plane.machines]
        known = [age for age in ages if age is not None]
        age = min(known) if known else None
        enrolled = [station for station in view.stations if station.enrolled]
        stale = (
            age is not None
            and bool(enrolled)
            and all(is_stale(station, machine_for(view, station)) for station in enrolled)
        )
        modifier = " sidebar-meta--stale" if stale else ""
        text = "telemetry unknown" if age is None else f"telemetry {'stale ' if stale else ''}{format_age(age)}"
        telemetry = f'<span class="mono sidebar-telemetry">{_esc(text)}</span>'
    return f'<p class="sidebar-meta{modifier}"><span class="mono sidebar-version" title="v{_esc(__version__)}">v{_esc(__version__)}</span>{telemetry}</p>'


def render_sidebar(view: DeckView | None, active: str, now: datetime, *, worklore_enabled: bool) -> str:
    """Five primary destinations, attention, Resources, and telemetry age."""
    section = section_for(active)
    links = []
    for key, href, label in NAV_ITEMS:
        current = ' aria-current="page"' if key == section else ""
        icon = icons.NAV_ICONS.get(key, icons.NAV_ICONS["boards"])
        links.append(f'<a href="{href}"{current}>{icon}{label}</a>')
    alerts = len(page_alerts(view, now)) if view is not None else None
    alert_count = f' <span class="nav-count">{alerts}</span>' if alerts else ""
    return "".join(
        [
            '<aside class="shell-sidebar" aria-label="Fleet navigation"><div class="brand">'
            '<p class="brand-name">Brigade Fleet Hub</p><p class="brand-sub">self-hosted</p></div>',
            f'<nav class="deck-nav" aria-label="Command Deck">{"".join(links)}</nav>',
            f'<a class="attention-shortcut" href="/deck/alerts">Alerts{alert_count}</a>',
            _resources(),
            _sidebar_meta(view, now),
            "</aside>",
        ]
    )
