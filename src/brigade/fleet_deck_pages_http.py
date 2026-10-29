"""Data loading and rendering for the Fleet Hub admin deck tabs."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import fleet_command_deck, fleet_hub as _hub, fleet_hub_routing, fleet_hub_sessions
from .fleet_hub import FleetHubError

CAPACITY_TABS = {
    "/deck/capacity": "capacity",
    "/deck/capacity/cloud": "cloud-details",
    "/deck/capacity/github": "github-reads",
}
ADMIN_PAGES = frozenset({*CAPACITY_TABS, "/deck/activity", "/deck/projects", "/deck/pulls", "/deck/reporting"})
DECK_PATHS = frozenset({"/", "/deck", "/deck/repos", "/deck/alerts", *ADMIN_PAGES})


def claims_from_rows(rows: Iterable[Mapping[str, Any]], now: datetime) -> list[fleet_command_deck.Claim]:
    claims = []
    for row in rows:
        expires = datetime.fromisoformat(str(row["expires_at"]))
        claims.append(
            fleet_command_deck.Claim(
                target=str(row["target"]),
                owner_node=str(row["owner_node"]),
                owner_conductor=str(row["owner_conductor"] or ""),
                ttl_remaining=max(0, int((expires - now).total_seconds())),
            )
        )
    return claims


def control_plane_from_hub(conn: Any) -> Any:
    """Adapt the live routing snapshot for the Deck.

    Missing or unreadable projection stays unknown. This helper does not
    recompute routing or quota precedence.
    """
    try:
        raw = fleet_hub_routing.control_plane_status(conn)
    except (FleetHubError, sqlite3.Error, KeyError, ValueError, TypeError):
        return None
    return fleet_command_deck.control_plane_from_snapshot(raw)


def build_view(
    conn: sqlite3.Connection, config: fleet_command_deck.DeckConfig, now: datetime
) -> tuple[fleet_command_deck.DeckView, Any, Any]:
    """The deck view plus the live runs and interactive sessions it was built from."""
    deck = fleet_command_deck
    live_runs = deck.fetch_live_runs(conn, now=now, stale_after_seconds=config.stale_after_seconds)
    station_ids = [station.node_id for station in config.stations]
    interactive_sessions = deck.fetch_interactive_sessions(conn, now=now)
    view = deck.build_view(
        config,
        live_runs=live_runs,
        claims=claims_from_rows(_hub.list_claims(conn), now),
        # Only unrevoked enrollments feed the label/enrolled mapping.
        enrolled_labels={
            node["node_id"]: str(node["label"] or "")
            for node in _hub.list_nodes(conn)
            if node.get("revoked_at") is None
        },
        last_heard=deck.fetch_last_heard(conn, station_ids),
        outcomes=deck.fetch_outcomes(conn, outcome_window=config.outcome_window),
        failed_outcomes=deck.fetch_failed_outcomes(
            conn,
            now=now,
            lookback_seconds=config.failed_lookback_seconds,
            stale_after_seconds=config.stale_after_seconds,
        ),
        observers=deck.fetch_observers(conn, frozenset(station_ids)),
        now=now,
        cloud_workers=deck.cloud_workers_from_snapshot(_hub.cloud_snapshot(conn, config)),
        interactive_sessions=interactive_sessions,
        control_plane=control_plane_from_hub(conn),
        # Uncapped per-node counts: the Activity list above stops at a row limit.
        session_counts=fleet_hub_sessions.count_live_by_node(conn, now_epoch=now.timestamp()),
    )
    return view, live_runs, interactive_sessions


@dataclass
class AdminExtras:
    reporting: Any = None
    ci_view: Any = None
    project_page: Any = None
    board: Any = None


def load_extras(
    conn: sqlite3.Connection, path: str, *, operator: bool, worklore_enabled: bool, selected_project: Any
) -> AdminExtras:
    """Per-tab data on the deck's connection. Raises ValueError for an unknown project."""
    extras = AdminExtras()
    if path == "/deck/reporting":
        from . import fleet_admin_stats

        try:
            extras.reporting = fleet_admin_stats.load_view(conn)
        except (sqlite3.Error, ValueError):
            extras.reporting = {"unavailable": True}
    if path in ("/deck/pulls", "/deck/projects"):
        try:
            from . import fleet_ci_board

            # Same visibility rule as /deck/ci: private repositories only for the admin bearer.
            extras.ci_view = fleet_ci_board.load_view(conn, operator=operator)
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            extras.ci_view = None
    if path == "/deck/projects":
        from . import fleet_admin_pages, fleet_project_board

        extras.project_page = fleet_admin_pages.load_projects(conn, enabled=worklore_enabled, selected=selected_project)
        extras.board = fleet_project_board.load_board(conn)
    return extras


def render(
    path: str,
    view: fleet_command_deck.DeckView,
    now: datetime,
    *,
    nonce: str,
    worklore_enabled: bool,
    extras: AdminExtras,
    board_options: Any,
) -> str:
    from . import fleet_admin_pages

    if path in CAPACITY_TABS:
        return fleet_admin_pages.render_capacity(
            view, now, nonce=nonce, worklore_enabled=worklore_enabled, tab=CAPACITY_TABS[path]
        )
    if path == "/deck/reporting":
        return fleet_admin_pages.render_reporting(
            extras.reporting or {}, view, now, nonce=nonce, worklore_enabled=worklore_enabled
        )
    if path == "/deck/activity":
        return fleet_admin_pages.render_activity(view, now, nonce=nonce, worklore_enabled=worklore_enabled)
    if path == "/deck/projects":
        return fleet_admin_pages.render_projects(
            extras.project_page or {},
            view,
            now,
            nonce=nonce,
            worklore_enabled=worklore_enabled,
            board=extras.board,
            board_options=board_options,
            ci_view=extras.ci_view,
        )
    return fleet_admin_pages.render_pulls(extras.ci_view, view, now, nonce=nonce, worklore_enabled=worklore_enabled)
