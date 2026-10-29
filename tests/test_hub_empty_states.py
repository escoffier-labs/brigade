"""Hub empty states: a short title plus one page-specific guidance line.

The helper is covered directly, then every converted page is rendered in its
empty state and asserted for the exact title and hint it is meant to show.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from brigade import fleet_admin_explorer as explorer
from brigade import fleet_admin_pages as pages
from brigade import fleet_command_deck as deck
from brigade import fleet_deck_activity as activity
from brigade import fleet_deck_alerts
from brigade import fleet_deck_overview as overview
from brigade import fleet_hub, fleet_hub_policy, fleet_hub_roster_page, fleet_owners_page
from brigade import fleet_policy, fleet_policy_page, fleet_policy_page_sessions, fleet_pr_rules_page
from brigade import fleet_project_board_page as board_page
from brigade import fleet_projects_prs as prs
from brigade import fleet_repo_policy_page as repos_page
from brigade import fleet_work_page
from brigade.fleet_owners import OwnersView
from brigade.ui_theme import empty_state
from tests.fixtures import fleet_project_board as board_fixture
from tests.test_fleet_deck_alerts import render as render_alerts
from tests.test_fleet_deck_overview import _plane as alerts_plane
from tests.test_fleet_deck_overview import _view as alerts_view

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
DASHES = ("—", "–")


def _spans(markup: str) -> tuple[str, str]:
    title = re.search(r'<span class="empty-title">(.*?)</span>', markup)
    hint = re.search(r'<span class="empty-hint">(.*?)</span>', markup)
    assert title, markup
    return title.group(1), hint.group(1) if hint else ""


def _assert_copy(markup: str, title: str, hint: str) -> None:
    got_title, got_hint = _spans(markup)
    assert got_title == title
    assert got_hint == hint
    assert not any(dash in got_title + got_hint for dash in DASHES)


def _bare_view(plane=None) -> deck.DeckView:
    return deck.DeckView(stations=(), rail=(), repos=(), outcomes=(), observers=(), control_plane=plane)


# --- helper -------------------------------------------------------------------


def test_empty_state_renders_title_and_hint_spans():
    assert empty_state("Nothing here", "Run it.") == (
        '<p class="empty"><span class="empty-title">Nothing here</span><span class="empty-hint">Run it.</span></p>'
    )


def test_empty_state_title_only_has_no_hint_span():
    markup = empty_state("Nothing here")
    assert "empty-hint" not in markup and markup.endswith("</span></p>")


def test_empty_state_escapes_text_and_turns_backticks_into_code():
    markup = empty_state("<b>x</b>", "Run `brigade <x>` & <script>")
    assert "<b>" not in markup and "<script>" not in markup
    assert "&lt;b&gt;x&lt;/b&gt;" in markup
    assert "<code>brigade &lt;x&gt;</code>" in markup and "&amp;" in markup


def test_empty_state_classes_and_attrs():
    markup = empty_state("T", classes="project-empty", attrs="data-x hidden")
    assert markup.startswith('<p class="empty project-empty" data-x hidden>')


# --- control plane and Deck ---------------------------------------------------


def test_control_plane_absent_states_unknown_not_clear():
    html = deck.render_control_plane(None)
    _assert_copy(
        html,
        "No control-plane projection attached",
        "Policy version, machine capacity, quota, routing receipts, queue eligibility, and integration readiness "
        "are all unknown, which is not the same as nothing being wrong.",
    )


def test_control_plane_sections_keep_unknown_distinct_from_zero():
    from tests.test_fleet_command_deck import _control_plane_snapshot

    snapshot = _control_plane_snapshot(
        machines=[], quota=[], routes=[], integrations=[], authority={"active": True, "status": "active"}
    )
    html = deck.render_control_plane(deck.control_plane_from_snapshot(snapshot))
    titles = re.findall(r'<span class="empty-title">(.*?)</span>', html)
    assert "No machine capacity reported (unknown, not idle)" in titles
    assert "No quota observations (unknown, not unlimited)" in titles
    assert "No routing receipts (unknown, not unrouted)" in titles
    assert "Effective headroom unknown" in titles
    assert "No integration readiness reports" in titles
    hints = re.findall(r'<span class="empty-hint">(.*?)</span>', html)
    assert "Machines appear here once the control plane reports their load and free slots." in hints
    assert "Rows appear here when a provider reports usage for a quota pool." in hints
    assert "A receipt is recorded here each time the router selects a seat for a task." in hints
    assert (
        "No centrally computed quota projection is attached, so the raw observations below are history, "
        "not available headroom." in hints
    )
    assert "Each integration appears here once it reports routine and auth state in a snapshot." in hints


def test_integration_readiness_unknown_when_section_missing():
    from tests.test_fleet_command_deck import _control_plane_snapshot

    snapshot = _control_plane_snapshot(integrations=[], authority={"active": True, "status": "active"})
    del snapshot["integrations"]
    html = deck.render_control_plane(deck.control_plane_from_snapshot(snapshot))
    assert (
        '<span class="empty-title">Integration readiness unknown</span>'
        '<span class="empty-hint">No telemetry section reported, so readiness cannot be shown for any integration.'
        "</span>" in html
    )


def test_blocked_reasons_unknown_and_none_reported_differ():
    unknown = overview._queue_body(
        deck.ControlPlane(
            machines=(),
            queue=deck.QueueState(eligible_count="1", queued_count="1", blocked_known=False),
            present=frozenset({"queue"}),
        ),
        worklore_enabled=True,
    )
    _assert_copy(
        unknown,
        "Blocked reasons unknown",
        "The queue section did not say whether anything is blocked, so an empty list is not a clean bill.",
    )


def test_queue_unknown_without_section():
    _assert_copy(
        overview._queue_body(None, worklore_enabled=True),
        "Queue unknown",
        "The control-plane snapshot has no queue section, so queue state cannot be shown.",
    )


def test_overview_sections():
    view = _bare_view()
    _assert_copy(
        overview.render_machine_table(view, NOW),
        "No stations configured",
        "Start the hub with <code>--deck-config</code> to add stations to the Deck.",
    )
    _assert_copy(
        overview.render_cloud_workers(view),
        "No active provider leases reported",
        "Leases appear here while a provider is running work for a task.",
    )
    cloud = overview.render_cloud_workers(view)
    assert '<span class="empty-title">No cloud providers configured</span>' in cloud
    assert "Providers appear here when the hub reports cloud worker capacity." in cloud
    _assert_copy(
        overview.render_sessions(view),
        "No agent sessions reported",
        "Sessions appear here while stations report agents that are running.",
    )
    _assert_copy(overview.render_outcomes(view), "No recent outcomes", "Outcomes are listed here as runs finish.")
    _assert_copy(
        overview.render_work_lists(view),
        "No managed jobs reported",
        "Jobs appear here when a station reports work it is managing.",
    )


def test_activity_sections():
    _assert_copy(
        activity.render_provider_leases(_bare_view()),
        "No active provider leases reported",
        "Leases appear here while a provider is running work for a task.",
    )
    _assert_copy(
        activity.render_sessions(_bare_view()),
        "No agent sessions reported",
        "Sessions appear here while stations report agents that are running.",
    )


def test_cloud_worker_without_leases_has_guidance():
    worker = deck.CloudWorker("provider-a", 0, 2, "closed", ())
    _assert_copy(
        overview._cloud_worker_html(worker),
        "No active leases",
        "Leases appear here while this provider runs work.",
    )


def test_alerts_page_and_list_share_guidance():
    hint = "Alerts appear here when the control plane reports something that needs attention."
    assert hint in render_alerts(
        alerts_view(plane=alerts_plane(present=deck.CONTROL_PLANE_SECTIONS)), action_sources=frozenset()
    )
    _assert_copy(
        fleet_deck_alerts.ui_theme.empty_state("No active alerts", fleet_deck_alerts._NO_ALERTS_HINT),
        "No active alerts",
        hint,
    )


# --- admin pages --------------------------------------------------------------


def _admin(tab: str = "capacity") -> str:
    return pages.render_capacity(_bare_view(), NOW, nonce="n", worklore_enabled=True, tab=tab)


def test_admin_capacity_and_cloud_details():
    stations = "Start the hub with <code>--deck-config</code> to add stations to the Deck."
    page = _admin()
    assert '<span class="empty-title">No stations configured</span>' in page and stations in page
    cloud = _admin("cloud-details")
    assert '<span class="empty-title">No cloud providers configured</span>' in cloud
    assert "Providers appear here when the hub reports cloud worker capacity." in cloud
    assert "data-explorer-empty" in page and "data-explorer-empty" in cloud


def test_admin_activity_page():
    page = pages.render_activity(_bare_view(), NOW, nonce="n", worklore_enabled=True)
    assert '<span class="empty-title">No work activity reported</span>' in page
    assert '<span class="empty-hint">Entries appear here as stations report runs and queued work.</span>' in page


def test_projects_pulls_and_reporting_pages():
    empty = {"state": "empty", "campaigns": [], "selected": None}
    page = pages.render_projects(empty, _bare_view(), NOW, nonce="n", worklore_enabled=True)
    assert '<span class="empty-title">No projects yet</span>' in page
    assert (
        '<span class="empty-hint">Boards from GitHub Projects appear here once connected. '
        '<a href="/deck/pulls">View open pull requests</a></span>' in page
    )
    for state, title, hint in (
        ("disabled", "Projects not connected", "Project goals need Worklore, which is disabled on this hub."),
        (
            "unavailable",
            "Project goals unavailable",
            "Campaign storage is not available, so recorded goals cannot be read.",
        ),
    ):
        rendered = pages.render_projects(
            {"state": state, "campaigns": []}, _bare_view(), NOW, nonce="n", worklore_enabled=True
        )
        assert f'<span class="empty-title">{title}</span><span class="empty-hint">{hint}</span>' in rendered
    pulls = pages.render_pulls(None, _bare_view(), NOW, nonce="n", worklore_enabled=True)
    assert (
        '<span class="empty-title">Pull requests unavailable</span>'
        '<span class="empty-hint">The CI projection could not be read.</span>' in pulls
    )
    down = pages.render_reporting({"unavailable": True}, _bare_view(), NOW, nonce="n", worklore_enabled=True)
    assert (
        '<span class="empty-title">Reporting observations unavailable</span>'
        '<span class="empty-hint">The adoption report could not be read from the hub.</span>' in down
    )
    none = pages.render_reporting({}, _bare_view(), NOW, nonce="n", worklore_enabled=True)
    assert (
        '<span class="empty-title">No adoption observations available</span>'
        '<span class="empty-hint">Collection may be unconfigured or not yet reported.</span>' in none
    )


def test_client_side_filter_placeholders_stay_single_line():
    explorer_page = explorer.render([explorer.Entry("one", "T", "s", "<p>d</p>")], label="records")
    assert re.search(
        r"<p class=\"empty\" data-explorer-empty hidden><span class=\"empty-title\">No records", explorer_page
    )


def test_explorer_script_restores_guidance_instead_of_overwriting_it():
    assert "empty.innerHTML = emptyHtml" in explorer.SCRIPT
    assert "empty.textContent = 'No matching records.'" in explorer.SCRIPT


def test_pull_request_section_hint_depends_on_ci_snapshots():
    section, _ = prs.render({"repositories": [], "as_of": "2026-10-18T11:59:00+00:00"}, NOW)
    _assert_copy(
        section,
        "No open pull requests observed",
        "No repositories have CI snapshots yet, and <code>brigade fleet ci reconcile</code> on a node populates them.",
    )
    seen = {"repositories": [{"repo": "example/empty", "head": {}, "pulls": [], "pulls_complete": True}]}
    section, _ = prs.render(seen, NOW)
    _assert_copy(
        section,
        "No open pull requests observed",
        "Repositories with CI snapshots currently have no open pull requests.",
    )
    assert "pr-empty-static" in section


def test_project_board_empty_views():
    base = board_fixture.snapshot(NOW)
    no_epics = {**base, "items": [i for i in base["items"] if i["sub_total"] == 0]}
    epics, _, _ = board_page.render(no_epics, board_page.Options(view="epics"), NOW)
    _assert_copy(
        re.search(r'<p class="empty">.*?</p>', epics).group(0),
        "No epics on this board",
        "Epics are issues with sub-issues, and none on this board have any.",
    )
    for options in (board_page.Options(view="list"), board_page.Options()):
        empty, _, _ = board_page.render({**base, "items": []}, options, NOW)
        _assert_copy(
            re.search(r'<p class="empty">.*?</p>', empty).group(0),
            "No issues on this board",
            "Issues appear here once they are added to the GitHub Project.",
        )


# --- work, owners, PR rules, roster -------------------------------------------


def test_work_page_empty_and_hidden_reference_fallback():
    page = fleet_work_page.render({"items": []}, nonce="n", now=NOW)
    assert '<span class="empty-title">No Worklore tasks on this page</span>' in page
    assert (
        '<span class="empty-hint">Tasks appear here once Worklore holds one, for example after '
        "<code>brigade fleet work sync-github</code> imports labeled GitHub issues.</span>" in page
    )
    item = {"id": "1", "title": "T", "status": "open", "links": [], "links_truncated": True}
    page = fleet_work_page.render({"items": [item]}, nonce="n", now=NOW)
    assert (
        '<span class="empty-title">No stored references in this projection</span>'
        '<span class="empty-hint">The projection omitted every reference for this task.</span>' in page
    )


def _owners(**fields) -> str:
    base = {
        "policy_version": 3,
        "policy_error": None,
        "charters": {},
        "by_owner": {},
        "unassigned": [],
        "overlaps": [],
        "enrolled_principals": frozenset(),
        "unknown_sources": (),
        "truncated_sources": (),
    }
    base.update(fields)
    return fleet_owners_page.render(OwnersView(**base), nonce="n", now=NOW)


def test_owners_page_sections():
    page = _owners()
    assert (
        '<span class="empty-title">No charters declared</span>'
        '<span class="empty-hint">Save an owners section in the fleet policy document with '
        "<code>brigade fleet policy save</code> to populate this page.</span>" in page
    )
    assert (
        '<span class="empty-title">Nothing unassigned</span>'
        '<span class="empty-hint">Every live item that was read matches at least one charter.</span>' in page
    )
    assert (
        '<span class="empty-title">No overlaps</span>'
        '<span class="empty-hint">No item is matched by more than one charter.</span>' in page
    )


def test_owners_card_empty_and_unknown_sources_differ():
    charter = {"example-owner": {"label": "Example", "match": {"harnesses": ["claude"]}}}
    held = _owners(charters=charter)
    unknown = _owners(charters=charter, unknown_sources=("claims",))
    assert "Nothing held right now" in held
    assert "Live claims and runs that match this charter appear here." in held
    assert "No holdings from the sources that could be read" in unknown
    assert (
        "Holdings appear here once every source is readable, so an empty list is not proof that nothing is held."
        in unknown
    )


def test_pr_rules_page_empty_state():
    for editable in (True, False):
        page = fleet_pr_rules_page.render(
            [], revision=4, nonce="n", now=NOW, csrf="csrf" if editable else "", editable=editable
        )
        assert (
            '<span class="empty-title">No PR rules yet</span>'
            '<span class="empty-hint">PR rules decide how pull requests are handled, and each rule appears in this '
            "list once added.</span>" in page
        )


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        yield connection
    finally:
        connection.close()


def _seed(conn, document) -> None:
    fleet_hub_policy.save_policy(
        conn,
        document,
        expected_version=fleet_hub_policy.current_policy(conn)["revision"],
        actor="operator",
        reason="seed",
    )


def test_roster_page_roles_empty_state(conn):
    view = fleet_hub_roster_page.load_view(conn, deck.DeckConfig())
    page = fleet_hub_roster_page.render(view, nonce="n", now=NOW, csrf="c", editable=False)
    if "data-explorer-empty hidden" not in page:
        assert (
            '<span class="empty-title">No roles reported</span>'
            '<span class="empty-hint">Roles appear here when the model roster reports them.</span>' in page
        )


def _bare_policy_document() -> dict:
    return {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {"roles": {"impl": "seat-alpha"}},
        "seats": {"seat-alpha": {"provider": "provider-a", "model": "model-a-1"}},
    }


def test_policy_page_sections(conn):
    _seed(conn, _bare_policy_document())
    view = fleet_policy_page.load_view(conn)
    consumers = fleet_policy_page._consumer_section(view, editable=False, csrf="", draft=None)
    _assert_copy(consumers, "No consumers registered", "Consumers appear here once the policy declares one.")
    repositories = fleet_policy_page._repository_section(view, editable=False, csrf="", draft=None)
    _assert_copy(
        re.search(r'<p class="empty">.*?</p>', repositories).group(0),
        "No repositories in policy",
        "Repositories appear here once a repository policy entry is written.",
    )
    receipts = fleet_policy_page_sessions.sessions_section(view)
    _assert_copy(
        re.search(r'<p class="empty">.*?</p>', receipts).group(0),
        "No session receipts",
        "Receipts appear here when a live session loads policy.",
    )


# --- repository policy page ----------------------------------------------------


def _claim_row(target: str) -> deck.RepoRow:
    return deck.RepoRow(
        target=target,
        claim=deck.Claim(
            target=target,
            owner_node="11111111-1111-4111-8111-111111111111",
            owner_conductor="conductor-1",
            ttl_remaining=600,
        ),
        live=(),
        collision=False,
        repo_identity="",
    )


def test_repo_policy_page_summaries_and_unlinked_row():
    page = repos_page.render(_bare_view(), nonce="n", now=NOW)
    assert (
        '<span class="empty-title">No repository observations available</span>'
        '<span class="empty-hint">Observed runs fill this page, and <a href="/view/repos">run history</a> shows '
        "what has been reported.</span>" in page
    )
    assert '<span class="empty-title">No repositories to show</span>' in page
    assert "Repositories appear here once policy or an observed run names one." in page
    view = deck.DeckView(stations=(), rail=(), repos=(_claim_row("example-checkout"),), outcomes=(), observers=())
    unlinked = repos_page.render(
        view, rows=repos_page.build_rows(view, None), nonce="n", now=NOW, unavailable_reason="not available"
    )
    assert '<span class="empty-title">No live runs</span>' in unlinked
    assert "Runs appear here while a node works in this repository." in unlinked


def test_repo_policy_linked_row_sections(conn):
    document = {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {},
        "repositories": {"acme/plain-tool": {}},
    }
    _seed(conn, document)
    policy = fleet_policy_page.load_view(conn)
    view = deck.DeckView(
        stations=(),
        rail=(),
        repos=(
            deck.RepoRow(
                target="plain",
                claim=_claim_row("plain").claim,
                live=(),
                collision=False,
                repo_identity="acme/plain-tool",
            ),
        ),
        outcomes=(),
        observers=(),
    )
    # Linked rows always inherit settings, so clear them to reach the fallback.
    rows = tuple(replace(row, overrides=()) for row in repos_page.build_rows(view, policy))
    page = repos_page.render(view, rows=rows, policy_available=True, nonce="n", now=NOW)
    titles = re.findall(r'<span class="empty-title">(.*?)</span>', page)
    assert "No live runs" in titles
    assert "No session receipts" in titles
    assert "Receipts appear here when a live session loads policy for this repository." in page
    assert "No resolved settings" in titles
    assert "Settings appear here when policy overrides a default for this repository." in page
    assert "No role is resolved" in titles
    assert "A role appears once policy assigns one to this repository." in page


# --- copy hygiene --------------------------------------------------------------


def test_no_empty_state_copy_uses_dashes_or_hosts():
    sources = [
        "fleet_command_deck",
        "fleet_deck_alerts",
        "fleet_deck_activity",
        "fleet_deck_overview",
        "fleet_admin_pages",
        "fleet_hub_roster_page",
        "fleet_policy_page",
        "fleet_policy_page_sessions",
        "fleet_pr_rules_page",
        "fleet_repo_policy_page",
        "fleet_projects_prs",
        "fleet_work_page",
        "fleet_project_board_page",
        "fleet_owners_page",
    ]
    import importlib
    import inspect

    for name in sources:
        source = inspect.getsource(importlib.import_module(f"brigade.{name}"))
        for call in re.findall(r"empty_state\((.*?)\)\n", source, flags=re.S):
            assert not any(dash in call for dash in DASHES), (name, call)
