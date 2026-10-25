"""Human admin hierarchy at the real renderer and authorized HTTP boundaries."""

import re
from dataclasses import replace
from html.parser import HTMLParser

from brigade import fleet_command_deck as deck, fleet_deck_overview as overview, fleet_work_page
from tests.test_fleet_deck_overview import NOW, HOSTILE, _alert_view, _mixed_view
from tests.test_fleet_enrollment import cookie, hub, request, ADMIN  # noqa: F401


class Navigation(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.primary = False
        self.links = []
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "nav":
            self.primary = attrs.get("aria-label") == "Command Deck"
        if tag == "a" and self.primary:
            self.links.append(attrs)

    def handle_endtag(self, tag):
        if tag == "nav":
            self.primary = False


def test_primary_sections_keep_work_discoverable_without_worklore():
    for enabled in (False, True):
        page = deck.render_deck(_mixed_view(), nonce="preview", now=NOW, worklore_enabled=enabled)
        assert [a["href"] for a in Navigation(page).links] == [
            "/deck",
            "/view/machines",
            "/deck/capacity",
            "/deck/repos",
            "/deck/roster",
        ]
        assert ('href="/deck/work"' in page) == enabled
        assert 'aria-labelledby="tree-machines"' not in page
        assert 'aria-labelledby="tree-cloud"' not in page
        for route in ("/deck/alerts", "/deck/policy", "/deck/stats", "/deck/owners", "/deck/ci", "/view/repos"):
            assert f'href="{route}"' in page


def test_overview_attention_precedes_counts_and_runs_before_capacity():
    view = replace(_mixed_view(), rail=_alert_view().rail)
    source = overview.page_alerts(view, NOW)[0].source_ref
    page = overview.render_overview(view, NOW, worklore_enabled=True, action_sources=frozenset({source}))
    assert page.index("skillet on linux-a") < page.index('aria-label="Fleet summary"')
    assert page.index('aria-labelledby="working-title"') < page.index('id="machines-table"')
    assert "first observed 2h 0m ago" in page and "external.failed" in page
    assert ">Prepare repair</a>" in page
    assert "Usable for selected work: unknown" in page
    for summary in ("Reported session presence", "Recent outcomes", "Cloud diagnostics"):
        assert f"<summary>{summary}" in page


def test_capacity_shares_deck_authorization_and_browser_read_only_scope(hub):  # noqa: F811
    assert request(hub, "GET", "/deck/capacity")[0] == 401
    assert request(hub, "GET", "/deck/capacity?token=refused", headers={"Authorization": f"Bearer {ADMIN}"})[0] == 401
    credential = cookie(hub)
    status, headers, page = request(hub, "GET", "/deck/capacity", headers={"Cookie": credential})
    assert status == 200
    assert "<h1>Capacity</h1>" in page and "Octopool" in page and "not observed" in page
    assert "Usable for selected work: unknown" in page
    assert "No stations configured" in page
    assert "<form" not in page
    nonce = re.search(r'<style nonce="([^"]+)"', page).group(1)
    assert "'nonce-" + nonce + "'" in headers["Content-Security-Policy"]
    assert request(hub, "GET", "/cloud", headers={"Cookie": credential})[0] == 401


def test_stored_task_details_preserve_evidence_without_empty_field_noise():
    page = fleet_work_page.render(
        {
            "items": [
                {
                    "title": HOSTILE,
                    "scope": "example/tool",
                    "status": "running",
                    "updated_at": "2026-10-18T11:59:00Z",
                    "description": "stored evidence",
                    "work_id": "w-test",
                }
            ]
        },
        nonce="p",
        now=NOW,
    )
    item = page.split('<article class="work-item panel">', 1)[1]
    assert HOSTILE not in page
    assert "(none stored)" not in item
    assert item.index("Stored status") < item.index("<summary>Task details")
    assert item.index("example/tool") < item.index("<summary>Task details")
    assert "1m" in item and "stored evidence" in item and "w-test" in item
    assert "Execution state is not inferred" in page


def test_settings_lead_with_authority_and_read_only_values(tmp_path):
    from brigade import fleet_hub, fleet_hub_roster_page as roster, fleet_policy_page as policy

    conn = fleet_hub.init_db(tmp_path / "hub.db")
    try:
        view = policy.load_view(conn)
        page = policy.render(view, nonce="p", now=NOW, csrf="secret", editable=False)
        assert page.index('id="activation"') < page.index('id="identity"')
        assert "Staged policy" in page and "Effective policy</h2>" not in page
        assert "enroll with the fleet token to edit" not in page
        page = roster.render(
            roster.load_view(conn, deck.DeckConfig()), nonce="p", now=NOW, csrf="secret", editable=False
        )
        assert "Live routing" in page
        assert "<select" not in page and "<textarea" not in page
        assert "secret" not in page
    finally:
        conn.close()


def test_repository_operations_precede_policy_details():
    from brigade import fleet_repo_policy_page as repos

    page = repos.render(_mixed_view(), nonce="p", now=NOW)
    assert page.index("Claims and live runs") < page.index('id="repo-policy"')
    assert "<summary>Repository policy" in page


def test_unobserved_reporting_has_one_explanation_before_snapshot_details():
    from brigade import fleet_admin_stats_page as stats

    page = stats.render({"latest": None, "weeks": []}, nonce="p", now=NOW)
    assert "No adoption observations available" in page
    assert page.index("No adoption observations available") < page.index("<summary>Snapshot and methodology")


def test_repo_board_marks_repository_section_and_keeps_advanced_filters():
    from brigade import fleet_dashboard as board

    page = board.render_page(
        view="repos",
        query_string="repo=example%3Cscript%3E&node=node-1&seat=seat-1&sort=seat&attention=1&all=1",
        runs=[],
        claims=[],
        nodes=[],
        started_at={},
        now=NOW,
        nonce="p",
    )
    primary = Navigation(page).links
    assert next(a["href"] for a in primary if a.get("aria-current") == "page") == "/deck/repos"
    controls = page.split('class="fleet-controls"', 1)[1].split("</form>", 1)[0]
    assert controls.index('name="repo"') < controls.index("<summary>Advanced filters</summary>")
    assert controls.index('name="node"') > controls.index("<summary>Advanced filters</summary>")
    assert 'value="node-1"' in controls and 'value="seat-1"' in controls
    assert 'value="example&lt;script&gt;"' in controls


def test_running_count_does_not_promote_unknown_managed_jobs():
    from tests.test_fleet_deck_overview import _station, _run, _view, NODE_A

    station = _station(NODE_A, "Example", 4, [_run(NODE_A, "a", "one"), _run(NODE_A, "b", "two", bucket="unknown")])
    page = overview.render_summary(_view([station]))
    assert '<h2 class="card-title">Running</h2><p class="card-value">1</p>' in page
    assert "2 reported jobs" in page


def test_read_only_active_routing_uses_authority_values_instead_of_legacy(tmp_path):
    from brigade import fleet_hub, fleet_hub_roster_page as roster, fleet_policy_page as policy

    conn = fleet_hub.init_db(tmp_path / "hub.db")
    try:
        view = replace(
            roster.load_view(conn, deck.DeckConfig()),
            activation=policy.Activation("active", "activated", "authority-owned", "fixture"),
            preference={"impl": "legacy-seat"},
            policy=roster.PolicyContext(
                available=True,
                roles={"impl": "authority-seat"},
                admission_defaults={"brigade-run": "authority-fallback"},
            ),
        )
        page = roster.render(view, nonce="p", now=NOW, csrf="secret", editable=False)
        assert "authority-seat" in page and "authority-fallback" in page
        assert "legacy-seat" not in page and "<select" not in page
        assert "could not be read" not in page
        page = roster.render(
            replace(view, policy=roster.PolicyContext()), nonce="p", now=NOW, csrf="secret", editable=False
        )
        assert "legacy-seat" not in page and "unknown" in page
    finally:
        conn.close()


def test_observed_reporting_leads_with_observation_scope_before_details():
    from brigade import fleet_admin_stats_page as stats
    from tests.test_fleet_admin_stats import snapshot

    value = snapshot()
    page = stats.render({"latest": value, "weeks": value["weeks"]}, nonce="p", now=NOW)
    assert page.index("Weekly observations: 1") < page.index("<summary>Snapshot and methodology")
