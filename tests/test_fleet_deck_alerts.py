"""Alerts page contracts over synthetic fleet evidence."""

import html
import re
from dataclasses import replace

from brigade import fleet_command_deck as deck
from brigade import fleet_deck_overview as overview
from tests.test_fleet_command_deck import CONFIG, _bearer, _request, _start_hub
from tests.test_fleet_deck_overview import HOSTILE, NODE_A, NOW, _alert_view, _plane, _station, _view


def render(view, **kwargs):
    from brigade.fleet_deck_alerts import render_alerts_page

    return render_alerts_page(view, NOW, nonce="alerts-nonce", worklore_enabled=True, **kwargs)


def test_alerts_table_preserves_order_details_and_fix_subject():
    view = _alert_view()
    alerts = overview.page_alerts(view, NOW)
    page = render(view, action_sources=frozenset({alerts[0].source_ref}))
    assert '<main class="deck-shell alerts-page">' in page
    assert "<h1>Alerts</h1>" in page and "7 open" in page
    table = page.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    rows = re.findall(r"<tr>(.*?)</tr>", table, re.S)
    assert len(rows) == 7
    assert [html.unescape(re.findall(r"<td>(.*?)</td>", row, re.S)[0]) for row in rows] == [
        "failed",
        "unknown",
        "integration",
        "blocked",
        "collision",
        "stale telemetry",
        "coverage",
    ]
    assert "held by linux-a, live on shared" in rows[4]
    assert "first observed 2h 0m ago" in rows[0]
    assert 'class="alert-detail"' in rows[0]
    assert 'aria-label="Have an agent fix skillet on linux-a"' in rows[0]
    assert ">Fix</a>" in rows[0] and "/deck/actions/compose?source=" in rows[0]
    assert page.count(">Fix</a>") == 1
    assert 'href="/deck/alerts" aria-current="page"' in page
    assert 'alerts <span class="nav-count">7</span></a>' in page


def test_alerts_escape_hostile_subject_and_detail():
    rail = (deck.RailEntry("failed", NODE_A, HOSTILE, "run-1", HOSTILE, 5),)
    view = _view([_station(NODE_A, HOSTILE, 2)], rail=rail)
    source = overview.page_alerts(view, NOW)[0].source_ref
    page = render(view, action_sources=frozenset({source}))
    assert HOSTILE not in page
    assert 'aria-label="Have an agent fix &lt;script&gt;' in page
    assert page.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 3


def test_empty_alerts_hide_nav_count_and_show_empty_state():
    page = render(
        _view(
            plane=replace(
                _plane(present=deck.CONTROL_PLANE_SECTIONS),
                authority_present=True,
                authority=deck.AuthorityState("true", "active"),
            )
        ),
        action_sources=frozenset(),
    )
    assert '<p class="empty">No active alerts</p>' in page and "0 open" in page
    assert "alerts</a>" in page
    assert "<tbody>" not in page
    sidebar = overview.render_sidebar(None, "alerts", NOW, worklore_enabled=True)
    assert "alerts</a>" in sidebar


def test_alerts_route_uses_deck_auth_and_nonce_headers(tmp_path):
    with _start_hub(tmp_path, CONFIG) as (hub, _db):
        assert _request(hub, "GET", "/deck/alerts")[0] == 401
        status, headers, page = _request(hub, "GET", "/deck/alerts", headers=_bearer())
        assert status == 200
        assert "<h1>Alerts</h1>" in page and "<tbody><tr>" in page
        nonce = re.search(r'<style nonce="([^"]+)"', page).group(1)
        assert "'nonce-" + nonce + "'" in headers["content-security-policy"]


def test_alerts_page_ships_its_own_table_styles():
    page = render(_alert_view())
    style = page.split("<style", 1)[1].split("</style>", 1)[0]
    assert ".alert-detail { display: block;" in style
    assert ".alerts-table th:nth-child(1)" in style
    assert ".visually-hidden {" in style
    assert ".page-sub {" in style


def test_alerts_time_column_heading_fits_every_time_form():
    page = render(_alert_view())
    assert "<th>When</th>" in page and "First seen" not in page
