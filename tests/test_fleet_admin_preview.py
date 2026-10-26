"""Human admin hierarchy at the real renderer and authorized HTTP boundaries."""

import re
from dataclasses import replace
from html.parser import HTMLParser

import pytest

from brigade import fleet_command_deck as deck, fleet_deck_overview as overview, fleet_work_page
from tests.test_fleet_deck_overview import NOW, HOSTILE, _alert_view, _mixed_view
from tests.test_ui_theme_integration import Markup
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
        assert 'aria-label="Section tabs"' in page
        assert "More destinations" not in page and "Section destinations" not in page
        assert 'aria-labelledby="tree-machines"' not in page
        assert 'aria-labelledby="tree-cloud"' not in page
        assert 'href="/deck/alerts"' in page


def test_overview_compact_cards_keep_evidence_below_the_default_scan():
    view = replace(_mixed_view(), rail=_alert_view().rail)
    source = overview.page_alerts(view, NOW)[0].source_ref
    page = overview.render_overview(view, NOW, worklore_enabled=True, action_sources=frozenset({source}))
    attention = page.split('aria-labelledby="attention-title"', 1)[1].split("</section>", 1)[0]
    assert attention.count('class="alert-row"') <= 3
    assert 'class="alert-line"' not in attention and ">Review</a>" not in attention
    assert attention.count(">View all alerts</a>") == 1
    assert page.index('aria-labelledby="attention-title"') < page.index('aria-labelledby="working-title"')
    assert page.index('aria-labelledby="working-title"') < page.index('id="machines-table"')
    assert page.index('id="machines-table"') < page.index('aria-labelledby="queued-title"')
    machines = page.split('id="machines-table"', 1)[1].split("</section>", 1)[0]
    assert "Usable for selected work: unknown" in machines
    assert "Reported counts and capacity context" in machines
    assert 'class="cards"' not in page
    assert 'class="split"' not in page
    assert "About activity" not in page
    assert "Running: 2" in page and "3 ready to run" in page
    assert "Not ready: unknown" in page
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
    assert "<h1>Capacity</h1>" in page and "Octopool reporting not connected" in page
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
        assert "Activation status unknown" in page and "Effective policy</h2>" not in page
        assert "enroll with the fleet token to edit" not in page
        assert all(control not in page for control in ("<select", "<input", "<textarea"))
        assert "secret" not in page
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


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "active, labels, current",
    [
        ("overview", ["Overview", "Alerts"], "Overview"),
        ("alerts", ["Overview", "Alerts"], "Alerts"),
        ("boards", ["Runs", "Stored tasks", "Ready queue"], "Runs"),
        ("work", ["Runs", "Stored tasks", "Ready queue"], "Stored tasks"),
        ("capacity", ["Machines", "GitHub reads", "Cloud details"], "Machines"),
        ("repos", ["Repositories", "Run history", "Checks", "Ownership"], "Repositories"),
        ("repo-board", ["Repositories", "Run history", "Checks", "Ownership"], "Run history"),
        ("ci", ["Repositories", "Run history", "Checks", "Ownership"], "Checks"),
        ("owners", ["Repositories", "Run history", "Checks", "Ownership"], "Ownership"),
        ("roster", ["Live routing", "Model assignments", "Reporting"], "Live routing"),
        ("policy", ["Live routing", "Model assignments", "Reporting"], "Model assignments"),
        ("stats", ["Live routing", "Model assignments", "Reporting"], "Reporting"),
    ],
)
def test_section_tabs_have_exact_current_destination_and_feature_gated_discovery(enabled, active, labels, current):
    page = deck._document("<main>Content</main>", nonce="p", now=NOW, active=active, worklore_enabled=enabled)
    markup = Markup(page)
    tab = next(
        i for i, (tag, attrs) in enumerate(markup.tags) if tag == "nav" and attrs.get("aria-label") == "Section tabs"
    )
    links = markup.descendants(tab, "a")
    expected = [label for label in labels if label != "Stored tasks" or enabled]
    assert [markup.text[i] for i in links] == expected
    assert [markup.text[i] for i in links if markup.tags[i][1].get("aria-current")] == (
        [] if active == "work" and not enabled else [current]
    )
    assert len(Navigation(page).links) == 5
    sidebar = page.split("<aside", 1)[1].split("</aside>", 1)[0]
    assert "More destinations" not in sidebar and "Section destinations" not in sidebar


def test_section_anchor_destinations_have_real_targets_and_http_tabs(hub):  # noqa: F811
    credential = cookie(hub)
    for route, current in (
        ("/deck", "Overview"),
        ("/view/machines", "Runs"),
        ("/view/repos", "Run history"),
        ("/deck/capacity", "Machines"),
    ):
        status, _, page = request(hub, "GET", route, headers={"Cookie": credential})
        assert status == 200
        markup = Markup(page)
        tab = next(
            i
            for i, (tag, attrs) in enumerate(markup.tags)
            if tag == "nav" and attrs.get("aria-label") == "Section tabs"
        )
        assert [markup.text[i] for i in markup.descendants(tab, "a") if markup.tags[i][1].get("aria-current")] == [
            current
        ]
    page = overview.render_capacity_page(_mixed_view(), NOW, nonce="p", worklore_enabled=True)
    for anchor in ("machines-table", "octopool-observation", "cloud-details"):
        assert f'id="{anchor}"' in page
        assert f'href="/deck/capacity#{anchor}"' in page
    assert 'id="queued-title"' in overview.render_overview(_mixed_view(), NOW, worklore_enabled=True)


@pytest.mark.parametrize(
    ("queue", "expected", "ready"),
    (
        # Failed scan, partial empty scan, partial exclusions, and old producers.
        ({"complete": False, "eligible_count": None, "blocked": []}, "unknown", "unknown"),
        ({"complete": False, "eligible_count": 2, "blocked": []}, "unknown", "2"),
        (
            {"complete": False, "eligible_count": None, "blocked": [{"reason": "manual-mode", "count": 7}]},
            "unknown",
            "unknown",
        ),
        ({"eligible_count": 0, "blocked": []}, "unknown", "0"),
        ({"complete": "true", "eligible_count": 0, "blocked": []}, "unknown", "0"),
        ({"complete": True, "eligible_count": 0, "blocked": None}, "unknown", "0"),
        ({"complete": True, "eligible_count": 0, "blocked": []}, "0", "0"),
        (
            {"complete": True, "eligible_count": 3, "blocked": [{"reason": "manual-mode", "count": 7}]},
            "7",
            "3",
        ),
    ),
)
def test_not_ready_total_requires_complete_queue_projection(queue, expected, ready):
    from tests.test_fleet_deck_overview import _card_value, _view

    plane = deck.control_plane_from_snapshot({"queue": queue})
    page = overview.render_summary(_view(plane=plane))
    assert _card_value(page, "not-ready") == expected, queue
    assert _card_value(page, "queued") == ready, queue
    compact = overview.render_ready_queue(_view(plane=plane), worklore_enabled=True)
    assert f"Not ready: {expected}</p>" in compact, queue


def test_policy_heading_distinguishes_active_staged_and_unknown(tmp_path):
    from brigade import fleet_hub, fleet_policy_page as policy

    conn = fleet_hub.init_db(tmp_path / "hub.db")
    try:
        view = policy.load_view(conn)
        for state, heading in (
            ("active", "Settings are active"),
            ("staged", "Saved for later"),
            ("unknown", "Activation status unknown"),
        ):
            page = policy.render(
                replace(view, activation=policy.Activation(state, "fixture")),
                nonce="p",
                now=NOW,
                csrf="fixture",
                editable=False,
            )
            assert f'<h2 id="activation">{heading}</h2>' in page
    finally:
        conn.close()


def test_overview_limits_attention_while_alerts_retain_every_subject_and_evidence():
    from html import escape, unescape
    from urllib.parse import parse_qs, urlsplit

    from brigade import fleet_deck_alerts as alerts_page

    view = _alert_view()
    view = replace(view, rail=(replace(view.rail[0], repo=HOSTILE), *view.rail[1:]))
    alerts = overview.page_alerts(view, NOW)
    assert len(alerts) > 3
    sources = frozenset(alert.source_ref for alert in alerts if alert.source_ref)
    compact = overview.render_attention(view, NOW, action_sources=sources)
    full = alerts_page.render_alerts_page(view, NOW, nonce="p", worklore_enabled=True, action_sources=sources)
    assert compact.count('<li class="alert-row">') == 3
    assert full.count('<li class="alert-row">') == len(alerts)
    assert f"{len(alerts) - 3} more alerts" in compact
    assert 'href="/deck/alerts"' in compact and "View all alerts" in compact
    assert '<details class="alert-evidence">' not in compact
    assert full.count('<details class="alert-evidence">') == len(alerts)
    assert HOSTILE not in compact and HOSTILE not in full
    for alert in alerts[:3]:
        assert escape(alert.title) in compact
        assert escape(alert.kind) in compact and escape(alert.since) in compact
    for alert in alerts:
        assert escape(alert.title) in full
        assert escape(alert.since) in full
    for page, expected in ((compact, alerts[:3]), (full, alerts)):
        links = re.findall(r'href="(/deck/actions/compose\?[^" ]+)"', page)
        linked_sources = {parse_qs(urlsplit(unescape(link)).query)["source"][0] for link in links}
        assert linked_sources == {alert.source_ref for alert in expected if alert.source_ref}
