"""Review regressions at real loaded projections and browser-authorized routes."""

import html
import re
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest

from brigade import fleet_deck_overview as overview, fleet_hub
from tests.test_fleet_deck_overview import NOW, HOSTILE, _alert_view
from tests.test_fleet_policy_page import _RouteHub, _document, _seed
from tests.test_fleet_command_deck import (
    _fake_activated_marker,
    CONFIG,
    NODE_A,
    _start_hub,
    _seed_run,
    _bearer,
    _request,
)


@pytest.mark.parametrize(
    "conflict,enabled,retired", [(False, True, False), (True, True, False), (True, False, False), (False, True, True)]
)
def test_active_roster_uses_loaded_policy_seat_details_over_legacy(tmp_path, conflict, enabled, retired):
    with _RouteHub(tmp_path) as hub:
        conn = fleet_hub.open_db(hub.db)
        try:
            document = _document()
            document["seats"]["seat-alpha"]["enabled"] = enabled
            _seed(conn, document)
            if conflict:
                conn.execute(
                    "INSERT INTO model_policy (seat,provider,model,reasoning,enabled,updated_at) VALUES ('seat-alpha','legacy-provider','legacy-model','low',1,'fixture')"
                )
            if retired:
                conn.execute(
                    "INSERT INTO retired_models (provider,family,match_kind,permanent,reason_code,created_at) VALUES ('provider-a','model-a-1','exact',0,'fixture','fixture')"
                )
            conn.commit()
        finally:
            conn.close()
        _fake_activated_marker(hub.db)
        status, _, page = hub.request("GET", "/deck/roster", headers={"Cookie": hub.cookie()})
        assert status == 200
        detail = page.split("<h2>Worker</h2>", 1)[1].split("</article>", 1)[0]
        for value in ("model-a-1", "provider-a", "high", "active policy"):
            assert value in detail
        assert "legacy-model" not in detail and "legacy-provider" not in detail
        assert ("Disabled seat" in detail) == (not enabled)
        assert ("Retired seat" in detail) == retired
        assert "Model configuration missing" not in detail
        assert "<form" not in page


@pytest.mark.parametrize(
    "state,expected", [("run.started", "running"), ("run.completed", "completed"), ("run.failed", "failed")]
)
def test_activity_history_links_reach_their_actual_board_rows(tmp_path, state, expected):
    from brigade import fleet_dashboard as board
    from datetime import datetime, timezone

    with _start_hub(tmp_path, CONFIG) as (hub, _):
        run_id = "review-" + expected
        repo = "example/" + expected
        _seed_run(hub, NODE_A, run_id, state, repo=repo)
        status, _, page = _request(hub, "GET", "/deck/activity", headers=_bearer())
        assert status == 200
        detail = next(
            part.split("</article>", 1)[0]
            for part in page.split(' data-record-detail tabindex="-1">')[1:]
            if run_id in part.split("</article>", 1)[0]
        )
        href = html.unescape(re.search(r'href="([^"]+)">Open run history', detail).group(1))
        params = parse_qs(urlsplit(href).query)
        assert params == {"node": [NODE_A], "repo": [repo], "all": ["1"]}
        rows = board.build_rows(
            [
                {
                    "node_id": NODE_A,
                    "run_id": run_id,
                    "repo": repo,
                    "state": state,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            ],
            {},
            now=datetime.now(timezone.utc),
        )
        assert [row.run_id for row in board.filter_rows(rows, board.parse_query(urlsplit(href).query))] == [run_id]
        status, _, history = _request(hub, "GET", href, headers=_bearer())
        assert status == 200 and f'title="{run_id}">{run_id}' in history
        assert f" · {expected}</span>" in page
        assert state in detail


def test_overview_subject_fragments_select_their_own_alert_detail():
    from brigade import fleet_deck_alerts
    from tests.test_ui_theme_integration import Markup

    view = _alert_view()
    view = replace(view, rail=(replace(view.rail[0], repo=HOSTILE), *view.rail[1:]))
    compact = Markup(overview.render_overview(view, NOW, worklore_enabled=True))
    full = Markup(fleet_deck_alerts.render_alerts_page(view, NOW, nonce="n", worklore_enabled=True))
    subjects = [
        i
        for i, (tag, attrs) in enumerate(compact.tags)
        if tag == "a" and any(compact.tags[parent][0] == "li" for parent in compact.ancestors[i])
    ][:3]
    assert len(subjects) == 3
    for index, alert in zip(subjects, overview.page_alerts(view, NOW)[:3], strict=True):
        fragment = urlsplit(compact.tags[index][1]["href"]).fragment
        assert fragment
        selected = next(i for i, (_, attrs) in enumerate(full.tags) if attrs.get("id") == fragment)
        assert alert.title in full.text[selected]
    assert 'href="/deck/alerts">View all</a>' in overview.render_overview(view, NOW, worklore_enabled=True)
    assert HOSTILE not in overview.render_overview(view, NOW, worklore_enabled=True)


def test_task_detail_leads_with_work_and_keeps_record_metadata_in_evidence():
    from brigade import fleet_work_page

    item = {
        "title": "Task",
        "status": "captured",
        "description": "Useful description",
        "blocker": "Need approval",
        "acceptance": ["Human checks"],
        "work_id": "record-id",
        "version": 2,
        "updated_at": NOW.isoformat(),
        "execution_mode": "manual",
        "burn_eligible": False,
        "links": [
            {
                "link_type": "github",
                "external_key": "opaque-key",
                "display_ref": "Supplied source",
                "url": "https://example.test/issue",
            }
        ],
    }
    page = fleet_work_page.render({"items": [item]}, nonce="n", now=NOW)
    detail = page.split('<article class="work-item panel">', 1)[1].split("</article>", 1)[0]
    markers = [
        "Useful description",
        "Need approval",
        "Human checks",
        "Supplied source",
        "<h3>Evidence</h3>",
        "record-id",
    ]
    assert [detail.index(marker) for marker in markers] == sorted(detail.index(marker) for marker in markers)
    assert "Recorded task status does not confirm live execution." in page
    assert "captured / manual / burn_eligible=false" not in page
    empty = fleet_work_page.render({"items": [dict(item, links=[])]}, nonce="n", now=NOW)
    assert "Stored references" not in empty and "No stored references" not in empty
    assert all(value in detail for value in ("opaque-key", "Version", "Execution mode", "Burn eligible"))


def test_read_only_source_copy_and_absent_fallbacks(tmp_path):
    with _RouteHub(tmp_path) as hub:
        status, _, page = hub.request("GET", "/deck/roster", headers={"Cookie": hub.cookie()})
        assert status == 200
        assert "Current assignments. Saved settings are not active." in page
        assert "Legacy routing" not in page
        assert "run preference" in page
        assert "fallback" not in page.lower()
        assert 'aria-label="Search roles"' in page and "Search this page" not in page
        assert "Search covers the displayed bounded set" not in page
