"""Reproductions of adjudicated late whole-diff review findings."""

import re
from urllib.parse import urlsplit

from brigade import fleet_admin_pages as pages, fleet_command_deck as deck
from brigade import fleet_admin_explorer as explorer, fleet_deck_alerts as alerts, fleet_deck_overview as overview
from brigade import fleet_hub
from tests.fleet_script_dom import run_script
from tests.test_fleet_deck_overview import NOW, NODE_A, _run, _view, _plane, _station, _machine
from tests.test_fleet_policy_page import _RouteHub, _seed
from tests.test_ui_theme_integration import Markup


def test_readonly_repository_policy_links_have_real_destinations(tmp_path):
    with _RouteHub(tmp_path) as hub:
        conn = fleet_hub.open_db(hub.db)
        try:
            _seed(conn)
        finally:
            conn.close()
        headers = {"Cookie": hub.cookie()}
        status, _, page = hub.request("GET", "/deck/repos", headers=headers)
        assert status == 200
        parsed = Markup(page)
        links = [
            (i, attrs["href"])
            for i, (tag, attrs) in enumerate(parsed.tags)
            if tag == "a" and attrs.get("href", "").startswith("/deck/policy")
        ]
        assert links
        for i, href in links:
            target = urlsplit(href)
            status, _, destination = hub.request("GET", target.path, headers=headers)
            assert status == 200
            if target.fragment:
                assert any(attrs.get("id") == target.fragment for _, attrs in Markup(destination).tags)
            assert "edit" not in parsed.text[i].lower()
        assert "View saved assignments" in page


def test_reported_and_managed_outcomes_never_deduplicate_each_other():
    context = deck.CloudSessionContext(
        version=2,
        provider="dot",
        agent_label="Agent",
        sequence=1,
        observed_at=NOW.isoformat(),
        source="cloud_threads",
        source_scope="authorized-visible-threads",
        coverage="explicitly-reported-sessions",
        task="Reported result",
        reported_state="completed",
        result="Reported output",
    )
    session = deck.InteractiveSession(
        NODE_A,
        "dot",
        "same-id",
        "repo",
        "fleet",
        "repo",
        "",
        None,
        (),
        False,
        cloud_context=context,
        presence_state="stale",
    )
    view = _view(
        outcomes=(_run(NODE_A, "same-id", "Managed result", state="run.completed"),), recent_cloud_reports=(session,)
    )
    entries = pages.activity_entries(view)
    assert len(entries) == 2 and len({entry.key for entry in entries}) == 2
    assert entries[0].status.startswith("Recorded outcome")
    assert entries[1].status.startswith("Reported outcome")
    assert all(
        value in entries[1].detail
        for value in ("Historical report", "Provider lifecycle unverified", "Reported output", "cloud_threads")
    )


def test_empty_provider_lease_title_is_consistent():
    worker = deck.CloudWorker("fixture-provider", 1, 2, "closed", (deck.CloudLease("", "", "", "running", ""),))
    entry = pages.activity_entries(_view(cloud_workers=(worker,)))[0]
    assert entry.title == "Provider lease" and "<h2>Provider lease</h2>" in entry.detail


def test_worklore_disabled_queue_count_does_not_promise_an_activity_queue():
    plane = _plane(
        queue=deck.QueueState("3", "3", complete=True, blocked=({"reason": "blocker", "count": 1},), blocked_known=True)
    )
    view = _view(plane=plane)
    page = alerts.render_alerts_page(view, NOW, nonce="n", worklore_enabled=False)
    assert 'href="/deck/activity">View work activity</a>' in page and "View ready queue" not in page
    page = overview.render_overview(view, NOW, worklore_enabled=False)
    parsed = Markup(page)
    assert not any("Ready to run:" in parsed.text[i] for i, (tag, _) in enumerate(parsed.tags) if tag == "a")
    assert "Ready to run: 3" in page and "reported queue" in page


def test_project_filter_explains_matching_unloaded_selection():
    campaigns = [
        {"campaign_key": "alpha", "title": "Alpha", "status": "active", "description": "Goal", "acceptance": []},
        {"campaign_key": "beta", "title": "Beta", "status": "active"},
    ]
    page = pages.render_projects(
        {"state": "ready", "campaigns": campaigns, "selected": {"campaign": campaigns[0], "members": []}},
        _view(),
        NOW,
        nonce="n",
        worklore_enabled=True,
    )
    script = re.search(r'<script nonce="n">(.*?)</script>', page, re.S).group(1)
    run_script(
        page,
        script,
        """
const root=dom.querySelector('.project-explorer'), input=root.querySelector('input');
input.value='beta';input.handlers.input();
assert.equal(root.querySelectorAll('nav')[0].querySelectorAll('a')[1].hidden,false);
const message=root.querySelector('[data-project-select]');
assert.ok(message);assert.equal(message.hidden,false);
assert.match(message.textContent,/Select a matching project to view its details/);
assert.equal(root.querySelector('.explorer-details').hidden,false);
assert.equal(root.querySelector('article').hidden,true);
assert.equal(root.querySelectorAll('a')[1].attrs.href,'/deck/projects?project=beta');
input.value='alpha';input.handlers.input();
assert.equal(message.hidden,true);assert.equal(root.querySelector('article').hidden,false);
input.value='no-match';input.handlers.input();
assert.match(message.textContent,/No matching projects/);
""",
    )
    assert "<h2>Beta</h2>" not in page


def test_capacity_search_keeps_real_facts_and_ids_but_excludes_prompt_boilerplate():
    machine = _machine("known", NODE_A, state="offline", unused_reason="connection-lost")
    station = _station(NODE_A, "Known offline", 4)
    page = pages.render_capacity(_view((station,), plane=_plane((machine,))), NOW, nonce="n", worklore_enabled=True)
    run_script(
        page,
        explorer.SCRIPT,
        """
const root=dom.querySelector('[data-explorer]'), input=root.querySelector('input'), link=root.querySelector('[data-record-link]');
input.value='unknown';input.handlers.input();assert.equal(link.hidden,true);
for(const value of ['connection-lost','Known offline','11111111-1111-4111-8111-111111111111']){
 input.value=value;input.handlers.input();assert.equal(link.hidden,false);}
assert.equal(root.querySelector('.explorer-search').hidden,false);
""",
    )
    # Missing actual telemetry remains searchable; filtering cannot erase real unknown values.
    page = pages.render_capacity(_view((station,)), NOW, nonce="n", worklore_enabled=True)
    run_script(
        page,
        explorer.SCRIPT,
        """
const root=dom.querySelector('[data-explorer]'), input=root.querySelector('input');
input.value='unknown';input.handlers.input();assert.equal(root.querySelector('[data-record-link]').hidden,false);
""",
    )
    # Alert run identifiers appear only in the prompt evidence and must still match.
    page = alerts.render_alerts_page(
        _view(rail=(deck.RailEntry("failed", NODE_A, "Repo", "run-evidence-id", "run.failed", 5),)),
        NOW,
        nonce="n",
        worklore_enabled=False,
    )
    run_script(
        page,
        explorer.SCRIPT,
        """
const root=dom.querySelector('[data-explorer]'),input=root.querySelector('input');
input.value='run-evidence-id';input.handlers.input();assert.equal(root.querySelector('[data-record-link]').hidden,false);
""",
    )


def test_search_helper_is_hidden_without_javascript():
    page = explorer.render([explorer.Entry("one", "Title", "active", "<p>Fact</p>")], label="records")
    parsed = Markup(page)
    assert not any("explorer-bound" in attrs.get("class", "").split() for tag, attrs in parsed.tags)
    search = next(attrs for tag, attrs in parsed.tags if "explorer-search" in attrs.get("class", "").split())
    assert "hidden" in search
    assert all("hidden" not in attrs for tag, attrs in parsed.tags if tag == "article")
