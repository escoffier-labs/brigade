"""Alerts page contracts over synthetic fleet evidence."""

import html
import re
from dataclasses import replace

import pytest

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
    rows = page.split('<li class="alert-row">')[1:]
    assert len(rows) == 7
    assert [html.unescape(re.findall(r'class="attention-kind">(.*?)</span>', row, re.S)[0]) for row in rows] == [
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
    assert 'aria-label="Prepare repair prompt for skillet on linux-a"' in rows[0]
    assert ">Prepare repair</a>" in rows[0] and "/deck/actions/compose?source=" in rows[0]
    assert page.count(">Prepare repair</a>") == 1
    assert 'href="/deck" aria-current="page"' in page
    assert 'Alerts <span class="nav-count">7</span></a>' in page


def test_alerts_escape_hostile_subject_and_detail():
    rail = (deck.RailEntry("failed", NODE_A, HOSTILE, "run-1", HOSTILE, 5),)
    view = _view([_station(NODE_A, HOSTILE, 2)], rail=rail)
    source = overview.page_alerts(view, NOW)[0].source_ref
    page = render(view, action_sources=frozenset({source}))
    assert HOSTILE not in page
    assert 'aria-label="Prepare repair prompt for &lt;script&gt;' in page
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
    assert "Alerts</a>" in page
    assert 'class="alert-row"' not in page
    sidebar = overview.render_sidebar(None, "alerts", NOW, worklore_enabled=True)
    assert "Alerts</a>" in sidebar


def test_alerts_route_uses_deck_auth_and_nonce_headers(tmp_path):
    with _start_hub(tmp_path, CONFIG) as (hub, _db):
        assert _request(hub, "GET", "/deck/alerts")[0] == 401
        status, headers, page = _request(hub, "GET", "/deck/alerts", headers=_bearer())
        assert status == 200
        assert "<h1>Alerts</h1>" in page and 'class="alert-row"' in page
        nonce = re.search(r'<style nonce="([^"]+)"', page).group(1)
        assert "'nonce-" + nonce + "'" in headers["content-security-policy"]


def test_alerts_evidence_disclosures_retain_every_time_form():
    page = render(_alert_view())
    rows = page.split('<li class="alert-row">')[1:]
    assert len(rows) == 7
    for row in rows:
        assert 'class="alert-age"' in row and "<summary>Evidence</summary>" in row
        assert row.index('class="alert-age"') < row.index("<summary>Evidence</summary>")
    assert "first observed 2h 0m ago" in rows[0]
    assert "reported by queue" not in rows[0]


@pytest.mark.parametrize("detail", ["run.failed", "external.failed", "", "unknown"])
def test_code_only_failure_explains_uncertainty_and_has_real_runs_fallback(detail):
    view = _view(rail=(deck.RailEntry("failed", NODE_A, "example-repo", "run-1", detail, 5),))
    row = render(view).split('<li class="alert-row">', 1)[1].split("</li>", 1)[0]
    visible, evidence = row.split("<summary>Evidence</summary>")
    assert "This run reported a failure. No failure reason was reported." in visible
    assert "Why it matters" in visible and "requested work may be incomplete" in visible
    assert "run or repository logs before retrying" in visible
    assert 'href="/view/machines">View runs</a>' in row
    assert "not narrowed to this run" in visible
    assert "Inspect evidence" not in row and "Prepare repair" not in row
    assert "source_ref" not in row and "run-1" not in row
    if detail:
        assert html.escape(detail) in evidence
        assert f">{detail}<" not in visible


def test_failure_preserves_reported_free_text_without_treating_it_as_a_known_cause():
    detail = 'Test suite exited 1: <script>alert("x")</script> & check logs'
    view = _view(rail=(deck.RailEntry("failed", NODE_A, "example-repo", "run-1", detail, 5),))
    row = render(view).split('<li class="alert-row">', 1)[1].split("</li>", 1)[0]
    assert html.escape(detail) in row and detail not in row
    assert "No failure reason was reported" not in row
    assert "This run reported a failure" in row and "before retrying" in row


@pytest.mark.parametrize(
    "kind,detail,meaning,impact,next_step,destination",
    [
        (
            "unknown",
            "external.unknown",
            "outcome could not be determined",
            "duplicate work",
            "originating session or run",
            "/view/machines",
        ),
        (
            "stale",
            "run.stale",
            "Run updates are too old",
            "current progress is unknown",
            "originating session or run",
            "/view/machines",
        ),
        (
            "awaiting approval",
            "approval.requested",
            "approval or input",
            "may be waiting",
            "originating session",
            "/view/machines",
        ),
        (
            "stale telemetry",
            "stale-telemetry",
            "capacity report is too old",
            "machine is offline or work has stopped",
            "reporting agent and machine connection",
            "/deck/capacity",
        ),
        (
            "no telemetry row",
            "node not in the routing snapshot",
            "No capacity report",
            "cannot assess this machine",
            "reporting agent and machine connection",
            "/deck/capacity",
        ),
        (
            "coverage",
            "no telemetry for: quota, queue",
            "Monitoring evidence is missing",
            "cannot assess the missing sections",
            "reporting sources",
            "/deck/capacity",
        ),
        (
            "collision",
            "held by one, live on another",
            "conflict was reported",
            "may overlap",
            "claims and live runs",
            "/deck/repos",
        ),
        (
            "integration",
            "routine needs-attention",
            "routine requested attention",
            "Pickup and completion are not established",
            "routine's latest report",
            "/deck/capacity",
        ),
        (
            "future-kind",
            HOSTILE,
            "reported an attention signal",
            "impact is unknown",
            "originating report",
            "/deck/capacity",
        ),
    ],
)
def test_guidance_covers_projected_kinds_without_inventing_causes(
    kind, detail, meaning, impact, next_step, destination
):
    from brigade.fleet_deck_alerts import render_alert_rows

    row = render_alert_rows([overview.Alert(kind, "Example subject", detail, "last observed 1h ago")])
    visible, evidence = row.split("<summary>Evidence</summary>")
    assert meaning in html.unescape(visible) and impact in html.unescape(visible)
    assert next_step in html.unescape(visible) and f'href="{destination}"' in visible
    assert html.escape(detail) in evidence and HOSTILE not in row
    assert "last observed 1h ago" in row and "Inspect evidence" not in row
    assert "No executable repair" not in row


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "reason,next_step",
    [
        ("acceptance-required", "acceptance criteria"),
        ("not-ready", "readiness requirements"),
        ("not-eligible", "automatic-work eligibility"),
        ("manual-mode", "manual execution"),
        ("blocker", "recorded blocker"),
        ("attempt-limit", "previous attempts"),
        ("review-after", "review date"),
        ("source-policy", "source policy"),
        ("no eligible machine", "capacity and routing requirements"),
        ("blocked", "No specific blocker was recorded"),
        (HOSTILE, "reported blocker"),
    ],
)
def test_queue_guidance_decodes_reasons_and_respects_stored_tasks_flag(enabled, reason, next_step):
    from brigade.fleet_deck_alerts import render_alerts_page

    queue = deck.QueueState("0", "3", blocked=({"id": "job-1", "reason": reason},), blocked_known=True)
    view = _view(plane=_plane(queue=queue))
    row = (
        render_alerts_page(view, NOW, nonce="n", worklore_enabled=enabled)
        .split('<li class="alert-row">', 1)[1]
        .split("</li>", 1)[0]
    )
    assert next_step in html.unescape(row) and "Why it matters" in row
    assert ('href="/deck/work"' in row) == enabled
    assert 'href="/deck#queued-title"' in row if not enabled else "Stored tasks" in row
    assert HOSTILE not in row and "Inspect evidence" not in row
    assert html.escape(reason) in row.split("<summary>Evidence</summary>")[1]


def test_aggregate_blocker_has_no_invented_task_link_or_identity():
    queue = deck.QueueState("0", "3", blocked=({"reason": "attempt-limit", "count": 3},), blocked_known=True)
    row = render(_view(plane=_plane(queue=queue))).split('<li class="alert-row">', 1)[1].split("</li>", 1)[0]
    assert "3 work items blocked" in row and "previous attempts" in row
    assert "does not identify individual tasks" in row
    assert "/deck/work/" not in row and "/deck/actions/compose?" not in row


@pytest.mark.parametrize("enabled", [False, True])
def test_compact_rows_keep_summary_navigation_and_evidence_without_full_explanations(enabled):
    queue = deck.QueueState("0", "1", blocked=({"id": "job-1", "reason": "blocked"},), blocked_known=True)
    view = _view(
        plane=_plane(queue=queue), rail=(deck.RailEntry("failed", NODE_A, "example-repo", "run-1", "run.failed", 5),)
    )
    page = overview.render_overview(view, NOW, worklore_enabled=enabled)
    rows = page.split('<li class="alert-row">')[1:3]
    assert "No failure reason was reported" in rows[0]
    assert 'href="/view/machines">View runs</a>' in rows[0]
    assert "No specific blocker was recorded" in rows[1]
    assert ('href="/deck/work"' in rows[1].split("</li>", 1)[0]) == enabled
    for row in rows:
        row = row.split("</li>", 1)[0]
        assert "Why it matters" not in row and "Next step" not in row
        assert row.count("<p") <= 3
        assert "<summary>Evidence</summary>" in row


def test_eligible_repair_link_explains_preparation_does_not_execute():
    view = _alert_view()
    source = overview.page_alerts(view, NOW)[0].source_ref
    row = render(view, action_sources=frozenset({source})).split('<li class="alert-row">', 1)[1].split("</li>", 1)[0]
    assert ">Prepare repair</a>" in row
    assert "Preparing or copying a prompt does not execute a repair" in row
    assert "run or repository logs before retrying" in row


def test_missing_telemetry_does_not_invent_a_previous_capacity_report():
    from brigade.fleet_deck_alerts import render_alert_rows

    alert = overview.Alert("stale telemetry", "Example machine", "unknown-telemetry", "no telemetry received")
    row = render_alert_rows([alert])
    assert "No recent capacity report is available" in row
    assert "last capacity report is too old" not in row
    assert "reporting agent and machine connection" in row
    assert "does not prove the machine is offline or work has stopped" in row
    assert "unknown-telemetry" in row.split("<summary>Evidence</summary>")[1]
