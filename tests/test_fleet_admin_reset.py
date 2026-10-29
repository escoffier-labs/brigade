"""Human admin reset contracts at HTML and authenticated read boundaries."""

from dataclasses import replace
from html.parser import HTMLParser

import pytest

from tests.fleet_investigation_assertions import investigation_markup

from brigade import fleet_deck_overview as overview
from tests.test_fleet_deck_overview import NOW, HOSTILE, _mixed_view
from tests.test_fleet_work_page import _hub, _request, _cookie


class Elements(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.tags = []
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_explorer_no_js_has_all_details_escaped_and_stable_fragment_links():
    from brigade import fleet_admin_explorer as explorer

    rows = [
        explorer.Entry('unsafe"<key>', HOSTILE, "reported", "<p>Evidence</p>"),
        explorer.Entry("other", "Second", "blocked", "<p>Another record</p>"),
    ]
    page = explorer.render(rows, label="Tasks")
    tags = Elements(page).tags
    details = [attrs for tag, attrs in tags if tag == "article"]
    assert len(details) == 2 and all("hidden" not in attrs for attrs in details)
    ids = {attrs["id"] for attrs in details}
    links = {attrs["href"][1:] for tag, attrs in tags if tag == "a"}
    assert ids == links
    assert HOSTILE not in page and 'unsafe"<key>' not in page
    assert "Evidence" in page and "Another record" in page
    search = next(attrs for tag, attrs in tags if tag == "input")
    assert "disabled" in search
    assert explorer.render(rows, label="Tasks") == page


def test_overview_keeps_machine_capacity_in_a_short_scan_without_diagnostic_folds():
    from brigade import fleet_admin_explorer as explorer

    page = overview.render_overview(_mixed_view(), NOW, worklore_enabled=True)
    tags = Elements(page).tags
    assert len([1 for tag, attrs in tags if tag == "section"]) == 3
    assert all(tag not in {"details", "table"} for tag, attrs in tags)
    assert all(f'href="{href}"' in page for href in ("/deck/alerts", "/deck/activity", "/deck/capacity"))
    assert f"/deck/capacity{explorer.selection_href('11111111-1111-4111-8111-111111111111')}" in page
    assert "linux-a" in page and "windows" in page
    assert "10 slots" in page and "2/10 used" in page and "Reported load" in page and "44%" in page
    assert "stale" in page and "2h 0m" in page
    assert 'id="fleet-title"' not in page and "Capacity telemetry unavailable" not in page
    assert "Ready to run: 3" in page


def test_overview_unknown_machine_data_stays_explicit_and_labels_are_escaped():
    view = replace(_mixed_view(), stations=(replace(_mixed_view().stations[0], label=HOSTILE),), control_plane=None)
    page = overview.render_overview(view, NOW, worklore_enabled=True)
    assert HOSTILE not in page and "&lt;script&gt;" in page
    assert "unknown" in page and "Reported load</span><b>unknown</b>" in page
    assert "Capacity telemetry unavailable" not in page
    assert ">0%</b>" not in page


@pytest.mark.parametrize("route", ["/deck/activity", "/deck/projects", "/deck/capacity/cloud", "/deck/capacity/github"])
def test_new_pages_share_browser_auth_and_csp(tmp_path, route):
    with _hub(tmp_path) as (hub, _):
        assert _request(hub, route)[0] == 401
        headers = _cookie(hub)
        status, response, page = _request(hub, route, headers=headers)
        assert status == 200
        assert "Content-Security-Policy" in response
        assert "<form" not in page
        assert 'http-equiv="refresh"' not in page
        assert _request(hub, route + "?token=refused", headers=headers)[0] == 401


@pytest.mark.parametrize("query", ["project=a&project=b", "other=a", "project=", "project=%3Cscript%3E"])
def test_projects_reject_invalid_queries_without_reflection(tmp_path, query):
    with _hub(tmp_path) as (hub, _):
        status, _, page = _request(hub, "/deck/projects?" + query, headers=_cookie(hub))
        assert status == 400
        assert query not in page and "<script>" not in page


def test_projects_distinguish_unavailable_disabled_and_empty_without_schema_writes(tmp_path):
    from brigade import fleet_admin_pages as pages, worklore_campaigns as campaigns
    import sqlite3

    conn = sqlite3.connect(tmp_path / "projects.db")
    try:
        before = list(conn.iterdump())
        assert pages.load_projects(conn, enabled=False)["state"] == "disabled"
        assert pages.load_projects(conn, enabled=True)["state"] == "unavailable"
        assert list(conn.iterdump()) == before
        from brigade import worklore_store

        worklore_store.ensure_schema(conn)
        campaigns.ensure_schema(conn)
        conn.commit()
        before = list(conn.iterdump())
        conn.execute("PRAGMA query_only=ON")
        assert pages.load_projects(conn, enabled=True)["state"] == "empty"
        assert list(conn.iterdump()) == before and not conn.in_transaction
    finally:
        conn.close()


def test_capacity_routes_show_only_the_selected_resource():
    from brigade import fleet_admin_pages as pages

    for tab in ("capacity", "cloud-details", "github-reads"):
        page = pages.render_capacity(_mixed_view(), NOW, nonce="p", worklore_enabled=True, tab=tab)
        assert ('id="machines-table"' in page) == (tab == "capacity")
        assert ("Octopool reporting not connected" in page) == (tab == "github-reads")
        investigation_markup(page, only_investigation_details=True)


def test_activity_does_not_merge_distinct_kinds_or_claim_reported_execution():
    from brigade import fleet_admin_pages as pages

    view = _mixed_view()
    page = pages.render_activity(
        replace(view, outcomes=(view.stations[0].tiles[0].run,)), NOW, nonce="p", worklore_enabled=True
    )
    assert "Managed run" in page and "Recorded outcome" in page
    assert "Cloud task states are self-reported." not in page  # removed page subtitle, spec section 3
    assert 'aria-label="Search activity"' in page and "Search this page" not in page
    assert 'data-audience="agent"' in page  # evidence stays in the page, hidden for admins by script


def test_project_reads_only_selected_members_and_escapes_recorded_goals(tmp_path):
    from brigade import fleet_admin_pages as pages, fleet_hub, worklore_campaigns as campaigns
    from tests.test_worklore_campaigns import _body

    conn = fleet_hub.init_db(tmp_path / "goals.db")
    try:
        for index in range(22):
            campaigns.create_campaign(
                conn, _body(campaign_key=f"goal-{index:02}", title=f"Goal {index}"), actor_id="fixture"
            )
        campaigns.create_campaign(
            conn,
            _body(campaign_key="selected-goal", title=HOSTILE, acceptance=["Keys rotated", "Café ready"]),
            actor_id="fixture",
        )
        before = list(conn.iterdump())
        conn.execute("PRAGMA query_only=ON")
        statements = []
        conn.set_trace_callback(statements.append)
        page = pages.load_projects(conn, enabled=True, selected="selected-goal")
        assert len(page["campaigns"]) == 20
        assert {member["repo"] for member in page["selected"]["members"]} == {"acme/one", "acme/two"}
        member_reads = [statement for statement in statements if "FROM work_campaign_members" in statement]
        assert member_reads and all("'selected-goal'" in statement for statement in member_reads)
        rendered = pages.render_projects(page, _mixed_view(), NOW, nonce="p", worklore_enabled=True)
        assert HOSTILE not in rendered and "&lt;script&gt;" in rendered
        assert any("Café ready" in attrs.get("data-search", "") for _, attrs in Elements(rendered).tags)
        assert "Keys rotated" in rendered and "acme/one" in rendered and "unowned" in rendered
        assert list(conn.iterdump()) == before and not conn.in_transaction
    finally:
        conn.close()


def test_reporting_default_is_concise_and_keeps_history_separate(tmp_path):
    with _hub(tmp_path) as (hub, _):
        assert _request(hub, "/deck/reporting")[0] == 401
        status, _, page = _request(hub, "/deck/reporting", headers=_cookie(hub))
        assert status == 200
        assert "No adoption observations available" in page
        investigation_markup(page, only_investigation_details=True)
        assert 'href="/deck/stats"' in page


def test_overview_missing_telemetry_stays_unknown_without_duplicate_fleet_card():
    page = overview.render_overview(replace(_mixed_view(), control_plane=None), NOW, worklore_enabled=True)
    assert "telemetry unknown" in page and "Reported load</span><b>unknown</b>" in page
    assert 'id="fleet-title"' not in page and "Capacity is reported" not in page


@pytest.mark.parametrize("active", [False, True])
def test_read_only_retirement_labels_follow_current_authority(tmp_path, active):
    from brigade import fleet_command_deck as deck

    from brigade import fleet_hub, fleet_hub_roster_page as roster, fleet_policy_page as policy

    conn = fleet_hub.init_db(tmp_path / "routing.db")
    try:
        seat = roster.SeatRow(
            seat="actual-seat",
            provider="example",
            model="actual-model",
            reasoning="medium",
            limit=None,
            enabled=True,
            brigade_cli="",
            brigade_model="",
            t3_instance_id="",
            t3_service_tier="",
            t3_model="",
            notes=None,
            retired=False,
        )
        view = replace(
            roster.load_view(conn, deck.DeckConfig()),
            seats=(seat,),
            preference={"impl": seat.seat},
            activation=policy.Activation("active" if active else "staged", "fixture"),
            policy=roster.PolicyContext(
                available=True,
                roles={"impl": seat.seat},
                retired_seats=(seat.seat,),
                seat_details={
                    seat.seat: roster.PolicySeat(
                        provider=seat.provider,
                        model=seat.model,
                        reasoning=seat.reasoning,
                        enabled=seat.enabled,
                        retired=True,
                    )
                },
            ),
        )
        page = roster.render(view, nonce="p", now=NOW, csrf="private", editable=False)
        assert "actual-model" in page
        assert ("Retired seat" in page) == active
        assert "private" not in page and "<form" not in page
    finally:
        conn.close()
