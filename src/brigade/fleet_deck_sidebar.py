"""Pure sidebar rendering for every Command Deck page."""

from __future__ import annotations

from datetime import datetime

from . import __version__, fleet_deck_icons as icons
from .fleet_command_deck import UNKNOWN, DeckView, StationView, Tile
from .fleet_deck_overview import (
    POLICY_LIMITS,
    _esc,
    format_age,
    machine_for,
    seconds_since,
    station_status,
    page_alerts,
)

NAV_ITEMS = (
    ("overview", "/deck", "overview"),
    ("alerts", "/deck/alerts", "alerts"),
    ("repos", "/deck/repos", "repos"),
    ("work", "/deck/work", "work"),
    ("roster", "/deck/roster", "roster"),
    ("policy", "/deck/policy", "policy"),
    ("owners", "/deck/owners", "owners"),
    ("ci", "/deck/ci", "CI"),
    ("stats", "/deck/stats", "stats"),
    ("boards", "/view/machines", "boards"),
)


def _tree_summary(station: StationView, status: str, machine_state: str = UNKNOWN) -> str:
    if status == "offline":
        return "not enrolled" if not station.enrolled else "offline"
    if status == "stale":
        return "stale"
    if status == "limited":
        return machine_state
    if status == "unknown":
        return "unknown"
    return f"{station.busy} busy" if station.busy else "idle"


def _tree_run(tile: Tile) -> str:
    run = tile.run
    return (
        f'<li class="tree-run"><span class="tree-run-name">{_esc(run.repo or UNKNOWN)} · {_esc(run.seat or UNKNOWN)}'
        f'</span><span class="tree-time">{_esc(format_age(run.elapsed_seconds))}</span></li>'
    )


def _machine_tree(view: DeckView) -> str:
    rows = []
    for station in view.stations:
        machine = machine_for(view, station)
        status = station_status(station, machine)
        machine_state = POLICY_LIMITS.get(machine.unused_reason, machine.state) if machine is not None else UNKNOWN
        runs = "".join(_tree_run(tile) for tile in station.tiles)
        rows.append(
            f'<li class="tree-row" data-status="{status}" data-filter-row title="{_esc(station.station.node_id)}"><div class="tree-line">'
            f'<span class="tree-label">{_esc(station.label)}</span>'
            f'<span class="tree-meta">{_esc(_tree_summary(station, status, machine_state))}</span></div>'
            + (f'<ul class="tree-children">{runs}</ul>' if runs else "")
            + "</li>"
        )
    body = f'<ul class="tree-list">{"".join(rows)}</ul>' if rows else '<p class="tree-empty">No stations.</p>'
    return (
        '<section class="tree" aria-labelledby="tree-machines"><h2 id="tree-machines" class="tree-heading">'
        f'Machines <span class="nav-count">{len(view.stations)}</span></h2>{body}</section>'
    )


def _cloud_tree(view: DeckView) -> str:
    rows = "".join(
        '<li class="tree-row" data-filter-row><div class="tree-line">'
        f'<span class="tree-label">{_esc(worker.provider)}</span>'
        f'<span class="tree-meta">{worker.used}/{worker.limit}'
        + ("" if worker.circuit_state == "closed" else f" · {_esc(worker.circuit_state)}")
        + "</span></div></li>"
        for worker in view.cloud_workers
    )
    body = f'<ul class="tree-list">{rows}</ul>' if rows else '<p class="tree-empty">None configured.</p>'
    return (
        '<section class="tree" aria-labelledby="tree-cloud"><h2 id="tree-cloud" class="tree-heading">'
        f'Cloud <span class="nav-count">{len(view.cloud_workers)}</span></h2>{body}</section>'
    )


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
        stale = age is not None and age > view.stale_after_seconds
        modifier = " sidebar-meta--stale" if stale else ""
        text = "telemetry unknown" if age is None else f"telemetry {'stale ' if stale else ''}{format_age(age)}"
        telemetry = f'<span class="mono sidebar-telemetry">{_esc(text)}</span>'
    return f'<p class="sidebar-meta{modifier}"><span class="mono sidebar-version" title="v{_esc(__version__)}">v{_esc(__version__)}</span>{telemetry}</p>'


def render_sidebar(view: DeckView | None, active: str, now: datetime, *, worklore_enabled: bool) -> str:
    """Brand, icon nav, optional trees, Resources, and version and telemetry meta."""
    links = []
    for key, href, label in NAV_ITEMS:
        if key == "work" and not worklore_enabled:
            continue
        current = ' aria-current="page"' if key == active else ""
        count = (
            f' <span class="nav-count">{len(view.stations)}</span>' if key == "overview" and view is not None else ""
        )
        if key == "alerts" and view is not None:
            alerts = len(page_alerts(view, now))
            count = f' <span class="nav-count">{alerts}</span>' if alerts else ""
        links.append(f'<a href="{href}"{current}>{icons.NAV_ICONS[key]}{label}{count}</a>')
    parts = [
        '<aside class="shell-sidebar" aria-label="Fleet navigation"><div class="brand">'
        '<p class="brand-name">Brigade Fleet Hub</p><p class="brand-sub">self-hosted</p></div>',
        f'<nav class="deck-nav" aria-label="Command Deck">{"".join(links)}</nav>',
    ]
    if view is not None:
        parts += ['<div class="tree-filter" data-tree-filter></div>', _machine_tree(view), _cloud_tree(view)]
    parts += [_resources(), _sidebar_meta(view, now), "</aside>"]
    return "".join(parts)
