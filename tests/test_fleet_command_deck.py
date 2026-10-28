"""Tests for the fleet command deck module (config, projections, bounded reads)."""

import json
import re
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import http.client
import threading
import time
from contextlib import contextmanager

import pytest

from brigade.fleet_deck_icons import NAV_ICONS

from brigade import fleet_command_deck as deck
from brigade import fleet_repo_policy_page as repos_page
from brigade import fleet_hub
from brigade import fleet_hub_policy
from brigade import fleet_hub_status
from brigade import fleet_hub_sessions
from brigade import fleet_policy_migration

NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)

_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    node_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    digest TEXT NOT NULL,
    repo TEXT,
    seat TEXT,
    harness TEXT,
    state TEXT NOT NULL,
    ts TEXT NOT NULL,
    received_at TEXT NOT NULL,
    repo_identity TEXT,
    exit_status INTEGER,
    capability_fingerprint TEXT,
    context TEXT,
    PRIMARY KEY (node_id, run_id, sequence, digest)
);
"""


def _ts(offset_seconds: int) -> str:
    return (NOW - timedelta(seconds=offset_seconds)).isoformat()


def _insert(
    conn: sqlite3.Connection,
    node_id: str,
    run_id: str,
    sequence: int,
    state: str,
    *,
    ts: str | None = None,
    received_at: str | None = None,
    repo: str = "repo",
    repo_identity: str | None = None,
    harness: str = "claude",
) -> None:
    stamp = ts if ts is not None else _ts(10)
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, repo, seat, harness, state, ts, received_at, repo_identity)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            node_id,
            run_id,
            sequence,
            f"{node_id[:8]}-{run_id}-{sequence:04d}",
            repo,
            "seat",
            harness,
            state,
            stamp,
            received_at if received_at is not None else stamp,
            repo_identity,
        ),
    )


def _failed(conn: sqlite3.Connection, *, lookback_seconds: int = 86400, stale_after_seconds: int = 1800):
    return deck.fetch_failed_outcomes(
        conn, now=NOW, lookback_seconds=lookback_seconds, stale_after_seconds=stale_after_seconds
    )


@pytest.fixture()
def conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(_EVENTS_SCHEMA)
    fleet_hub_sessions.init_schema(conn)
    yield conn
    conn.close()


@pytest.fixture()
def now() -> datetime:
    return NOW


def _seed_session(
    conn: sqlite3.Connection,
    *,
    node: str = NODE_A,
    checkout_path: str = "/tmp/project",
    dirty_paths: list[str] | None = None,
    harness: str = "claude",
    session_id: str | None = None,
    repo_identity: str = "github.com/example/project",
    identity_scope: str = "fleet",
    repo_label: str = "project",
    branch: str = "main",
) -> None:
    paths = dirty_paths if dirty_paths is not None else ["src/a.py"]
    started = NOW.isoformat()
    conn.execute(
        "INSERT INTO interactive_sessions ("
        "node_id, harness, session_id, repo_identity, identity_scope, repo_label, "
        "checkout_path, branch, dirty_paths_json, dirty_truncated, state, started_at, "
        "heartbeat_at, ended_at, ttl_seconds, expires_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'active', ?, ?, NULL, 900, ?)",
        (
            node,
            harness,
            session_id or f"sess-{node[:8]}",
            repo_identity,
            identity_scope,
            repo_label,
            checkout_path,
            branch,
            json.dumps(paths),
            started,
            started,
            NOW.timestamp() + 900,
        ),
    )


def test_deck_sessions_are_separate_escaped_and_do_not_consume_capacity(conn, now):
    _seed_session(conn, checkout_path="<script>alert(1)</script>", dirty_paths=["src/a.py"])
    sessions = deck.fetch_interactive_sessions(conn, now=now)
    view = deck.build_view(
        deck.DeckConfig(stations=(deck.StationConfig(NODE_A, "Alpha", 10),)),
        live_runs=[],
        claims=[],
        enrolled_labels={NODE_A: "Alpha"},
        last_heard={},
        outcomes=[],
        failed_outcomes=[],
        observers=[],
        now=now,
        interactive_sessions=sessions,
    )
    html = deck.render_deck(view, nonce="nonce", now=now)
    assert "Reported agent sessions (1)" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert view.stations[0].busy == 0


def _visible_text(html):
    class VisibleText(HTMLParser):
        depth = 0

        def __init__(self):
            super().__init__()
            self.text = []

        def handle_starttag(self, tag, attrs):
            if tag == "details":
                self.depth += 1

        def handle_endtag(self, tag):
            if tag == "details":
                self.depth -= 1

        def handle_data(self, data):
            if not self.depth:
                self.text.append(data)

    parser = VisibleText()
    parser.feed(html)
    return " ".join(parser.text)


def test_reported_cloud_agent_survives_hub_projection_without_becoming_a_job(conn, now, monkeypatch):
    from brigade import fleet_dot, fleet_deck_overview as overview

    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: now.timestamp())
    body = fleet_dot.report_session(
        {
            "version": 1,
            "session_id": "cloud-session",
            "agent_label": "Miller",
            "repo_identity": "github.com/example/cloud-project",
            "parent_session_id": "cloud-parent",
            "work_id": "work-123",
            "ownership_revision": 0,
            "generation": 1,
            "source": "cloud_threads",
            "source_scope": "authorized-visible-threads",
            "observed_at": now.isoformat(),
            "sequence": 1,
        }
    )["presence_payload"]
    fleet_hub_sessions.handle_session(conn, body, caller_node=NODE_A)
    _seed_session(conn, session_id="local-session")
    view = deck.build_view(
        deck.DeckConfig(stations=(deck.StationConfig(NODE_A, "Alpha", 10),)),
        live_runs=[],
        claims=[],
        enrolled_labels={NODE_A: "Alpha"},
        last_heard={},
        outcomes=[],
        failed_outcomes=[],
        observers=[],
        now=now,
        interactive_sessions=deck.fetch_interactive_sessions(conn, now=now),
        cloud_workers=(deck.CloudWorker("codex", 0, 2, "closed", ()),),
    )
    html = deck.render_deck(view, nonce="nonce", now=now)
    activity = html.split('aria-labelledby="working-title"', 1)[1].split("</section>", 1)[0]
    visible = _visible_text(activity)
    assert "Reported cloud agents (1)" in visible and "Miller" in visible and "Dot" in visible
    assert "Presence only; running work unobserved" in visible
    assert "cloud-session" not in visible and "cloud-parent" not in visible and "work-123" not in visible
    assert "authorized-visible-threads" not in visible and "cloud_threads" not in visible
    assert "Parent: cloud-parent" in html and "Work: work-123" in html
    assert "Source: cloud_threads" in html and "Scope: authorized-visible-threads" in html
    assert "Repository: github.com/example/cloud-project" in html
    assert "github.com/example/cloud-project" not in visible
    assert "Coverage: explicitly-reported-sessions" in html
    assert "Observed: 2026-08-25T12:00:00+00:00" in html
    assert "Session: local-session" in html and "Checkout: /tmp/project" in html
    assert "Reported agent sessions (2)" in html
    assert "Running: 0 · 0 managed jobs reported" in html and overview.summarize(view).busy == 0
    assert view.cloud_workers[0].used == 0 and view.cloud_workers[0].leases == ()
    assert "Provider leases" in html and "No active leases." in html
    assert "Lease inventory does not cover reported agent presence." in html
    # Hub expiry remains the authority; a display label never refreshes presence.
    assert deck.fetch_interactive_sessions(conn, now=now + timedelta(seconds=901)) == []


@pytest.mark.parametrize("providers", [(), (deck.CloudWorker("codex", 0, 2, "closed", ()),)])
def test_missing_dot_reports_leave_cloud_coverage_unknown(providers):
    from brigade import fleet_deck_overview as overview

    view = deck.DeckView(stations=(), rail=(), repos=(), outcomes=(), observers=(), cloud_workers=providers)
    html = overview.render_work_lists(view)
    visible = _visible_text(html)
    assert "Dot reporting coverage unknown" in visible
    assert "No current Dot reports; publishing may not be configured." in visible
    assert "no cloud work" not in html.lower()


@pytest.mark.parametrize(
    "change",
    [
        {"agent_label": '<img src=x onerror="alert(1)">'},
        {"parent_session_id": "<script>bad</script>"},
        {"work_id": "w" * 129},
        {"source_scope": "account-wide"},
        {"observed_at": "not-a-time"},
        {"sequence": True},
        {"url": "https://example.test/untrusted"},
    ],
)
def test_deck_rejects_invalid_cloud_metadata_without_creating_links(change):
    from brigade import fleet_deck_overview as overview

    raw = {
        "node_id": NODE_A,
        "harness": "dot",
        "session_id": "cloud-session",
        "cloud_context": {
            "version": 1,
            "provider": "dot",
            "agent_label": "Untrusted-agent",
            "sequence": 1,
            "observed_at": NOW.isoformat(),
            **change,
        },
    }
    view = deck.build_view(
        deck.DeckConfig(stations=()),
        live_runs=[],
        claims=[],
        enrolled_labels={},
        last_heard={},
        outcomes=[],
        failed_outcomes=[],
        observers=[],
        now=NOW,
        interactive_sessions=[raw],
    )
    html = overview.render_work_lists(view)
    assert "Dot reporting coverage unknown" in html
    assert "Untrusted-agent" not in html and "<img" not in html and "<script>bad" not in html
    assert 'href="https://example.test/untrusted"' not in html


def test_cloud_presence_summary_bounds_labels_and_keeps_all_sessions_in_details():
    from brigade import fleet_deck_overview as overview

    sessions = [
        {
            "node_id": NODE_A,
            "harness": "dot",
            "session_id": f"cloud-{index}",
            "cloud_context": {
                "version": 1,
                "provider": "dot",
                "agent_label": f"Agent-{index:02}",
                "sequence": 1,
                "observed_at": NOW.isoformat(),
            },
        }
        for index in range(12)
    ]
    view = deck.build_view(
        deck.DeckConfig(stations=()),
        live_runs=[],
        claims=[],
        enrolled_labels={},
        last_heard={},
        outcomes=[],
        failed_outcomes=[],
        observers=[],
        now=NOW,
        interactive_sessions=sessions + [dict(sessions[0], repo_label="another-membership")],
    )
    html = overview.render_overview(view, NOW, worklore_enabled=True)
    visible = _visible_text(html)
    assert "Reported cloud agents (12)" in visible
    assert len(re.findall(r"Agent-\d+", visible)) == 8
    assert "4 more in session details" in visible
    assert "Agent-11" not in visible and "Agent-11" in html
    assert "Reported agent sessions (12)" in html


def _octopool_capture():
    return {
        "captured_at": NOW.isoformat(),
        "scope": "pool-wide",
        "window": {"seconds": 86400, "label": "untrusted window label"},
        "pool_usage": {
            "requests": 41,
            "saved_github_requests": 7,
            "fallbacks": 2,
            "service_errors": 1,
            "eligible_cache_hit_rate": 0.25,
        },
        "operator": "ignored-private-client",
    }


def test_capacity_without_an_octopool_observation_has_a_concise_connection_state():
    from brigade import fleet_deck_overview as overview

    view = deck.DeckView(stations=(), rail=(), repos=(), outcomes=(), observers=())
    html = overview.render_capacity_page(view, NOW, nonce="nonce", worklore_enabled=False)
    assert "Octopool reporting not connected" in html
    assert "No quota, reset time, cache accounting" not in html
    assert "GitHub reads / Octopool" not in overview.render_overview(view, NOW, worklore_enabled=False)


def test_capacity_renders_a_captured_pool_wide_octopool_observation_without_headroom_claims():
    from brigade import fleet_deck_overview as overview

    observation = deck.octopool_observation_from_snapshot(_octopool_capture())
    view = deck.DeckView(stations=(), rail=(), repos=(), outcomes=(), observers=(), octopool_observation=observation)
    html = overview.render_capacity_page(view, NOW, nonce="nonce", worklore_enabled=False)
    panel = html.split('id="octopool-observation"', 1)[1].split("</section>", 1)[0]
    visible = _visible_text(panel)
    assert "Captured Aug 25 12:00 UTC" in visible
    assert "2026-08-25T12:00:00+00:00" in panel and "2026-08-25T12:00:00+00:00" not in visible
    assert "24h" in visible and "Pool-wide" in visible and "includes clients outside this fleet" in visible
    assert "41 requests" in visible and "7 cache-served responses" in visible
    assert "2 fallbacks" in visible and "1 service error" in visible
    assert "GitHub reads saved" not in panel and "avoided GitHub" not in panel
    assert "cache hits and stale responses" in panel
    assert "expected local fallbacks" in panel
    assert "Source: Octopool stats capture" in panel
    assert "Eligible cache hit rate: 25.00%" in panel and "25.00%" not in visible
    assert "ignored-private-client" not in html and "untrusted window label" not in html
    assert "quota" not in panel.lower() and "available" not in panel.lower() and "live" not in panel.lower()
    assert "GitHub reads / Octopool" not in overview.render_overview(view, NOW, worklore_enabled=False)


@pytest.mark.parametrize("bad", [True, -1, 2**53, "123", 2.5, float("nan"), float("inf")])
def test_octopool_renderer_revalidates_provided_counters_and_omits_invalid_values(bad):
    from brigade import fleet_deck_overview as overview

    raw = _octopool_capture()
    raw["pool_usage"] = {
        "requests": 8,
        "saved_github_requests": bad,
        "fallbacks": bad,
        "service_errors": bad,
        "eligible_cache_hit_rate": bad,
        "url": "https://example.test/untrusted",
        "secret": "private-metadata",
    }
    # Raw mappings may arrive from preview callers; the renderer owns this last boundary too.
    view = deck.DeckView(stations=(), rail=(), repos=(), outcomes=(), observers=(), octopool_observation=raw)
    html = overview.render_capacity_page(view, NOW, nonce="nonce", worklore_enabled=False)
    panel = html.split('id="octopool-observation"', 1)[1].split("</section>", 1)[0]
    assert "8 requests" in panel
    assert "cache-served responses" not in panel and "service errors" not in panel
    assert "Eligible cache hit rate" not in panel
    assert "private-metadata" not in panel and "https://example.test/untrusted" not in panel


@pytest.mark.parametrize(
    "change",
    [
        {"captured_at": "<script>bad</script>"},
        {"captured_at": "2026-01-01T00:00:00"},
        {"scope": "fleet-only"},
        {"window": {"seconds": True}},
        {"window": {"seconds": -1}},
        {"pool_usage": [1, 2]},
    ],
)
def test_octopool_renderer_refuses_malformed_capture_provenance(change):
    from brigade import fleet_deck_overview as overview

    view = deck.DeckView(
        stations=(),
        rail=(),
        repos=(),
        outcomes=(),
        observers=(),
        octopool_observation={**_octopool_capture(), **change},
    )
    html = overview.render_capacity_page(view, NOW, nonce="nonce", worklore_enabled=False)
    assert "Octopool reporting not connected" in html
    assert "41 requests" not in html and "<script>bad" not in html


def test_capacity_leads_with_resources_and_retains_lease_evidence_in_advanced_details():
    from brigade import fleet_deck_overview as overview
    from tests.test_fleet_deck_overview import _mixed_view

    view = replace(
        _mixed_view(),
        cloud_workers=(
            deck.CloudWorker("codex", 0, 64, "closed", ()),
            deck.CloudWorker(
                "cursor", 1, 3, "open", (deck.CloudLease("task-id", "example/tool", "Active task", "running", ""),)
            ),
        ),
    )
    html = overview.render_capacity_page(view, NOW, nonce="nonce", worklore_enabled=False)
    visible = _visible_text(html.split("<main", 1)[1])
    assert html.index('id="machines-table"') < html.index("Usable for selected work: unknown")
    assert "Usable for selected work: unknown" not in visible and "Active task" not in visible
    assert "Dot reporting coverage unknown" not in visible
    assert "0/64" not in visible and "circuit:" not in visible
    assert "Configured slots are not verified usable workers" not in visible
    assert html.count("Configured slots are not verified usable workers") == 1
    assert "<summary>Advanced resource details</summary>" in html and "<summary>Provider details" in html
    advanced = html.split("<summary>Advanced resource details</summary>", 1)[1]
    assert "Usable for selected work: unknown" in advanced and "Active task" in advanced
    assert "0/64" in advanced and "circuit: closed" in advanced
    # Provider leases remain reachable under the grouped Fleet details disclosure.
    overview_html = overview.render_overview(view, NOW, worklore_enabled=False)
    overview_visible = _visible_text(overview_html)
    assert "Active task" not in overview_visible and "0/64" not in overview_visible
    assert "Active task" in overview_html.split("<summary>Fleet details</summary>", 1)[1]


def test_repo_page_marks_only_exact_live_dirty_overlap(conn, now):
    repo_identity = "github.com/example/" + ("a" * 300)
    _seed_session(conn, node=NODE_A, dirty_paths=["src/a.py"], repo_identity=repo_identity)
    _seed_session(conn, node=NODE_B, dirty_paths=["src/a.py"], repo_identity=repo_identity)
    sessions = deck.fetch_interactive_sessions(conn, now=now)
    view = deck.build_view(
        deck.DeckConfig(stations=(deck.StationConfig(NODE_A, "Alpha", 10),)),
        live_runs=[],
        claims=[],
        enrolled_labels={NODE_A: "Alpha", NODE_B: "Bravo"},
        last_heard={},
        outcomes=[],
        failed_outcomes=[],
        observers=[],
        now=now,
        interactive_sessions=sessions,
    )
    assert view.repos[0].target == "project"
    assert view.repos[0].interactive_overlap is True
    assert view.repos[0].repo_identity == repo_identity
    assert all(row.target != repo_identity for row in view.repos)


def test_live_run_plus_two_sessions_marks_run_row_without_identity_duplicate(conn, now):
    identity = "github.com/example/project"
    _insert(conn, NODE_A, "run-1", 1, "run.started", repo="project")
    conn.execute("UPDATE events SET repo_identity = ? WHERE run_id = ?", (identity, "run-1"))
    _seed_session(conn, node=NODE_A, dirty_paths=["src/a.py"], repo_identity=identity, repo_label="project")
    _seed_session(conn, node=NODE_B, dirty_paths=["src/a.py"], repo_identity=identity, repo_label="project")
    sessions = deck.fetch_interactive_sessions(conn, now=now)
    runs = [
        deck.LiveRun(
            node_id=NODE_A,
            run_id="run-1",
            repo="project",
            seat="seat",
            harness="claude",
            state="run.started",
            bucket="running",
            age_seconds=10,
            elapsed_seconds=10,
            repo_identity=identity,
        )
    ]
    view = deck.build_view(
        deck.DeckConfig(stations=(deck.StationConfig(NODE_A, "Alpha", 10),)),
        live_runs=runs,
        claims=[],
        enrolled_labels={NODE_A: "Alpha", NODE_B: "Bravo"},
        last_heard={},
        outcomes=[],
        failed_outcomes=[],
        observers=[],
        now=now,
        interactive_sessions=sessions,
    )
    targets = [row.target for row in view.repos]
    assert targets == ["project"]
    assert identity not in targets
    assert view.repos[0].interactive_overlap is True
    assert view.repos[0].repo_identity == identity


def test_unrelated_brigade_basename_does_not_attach_session_overlap(conn, now):
    run_identity = "github.com/example/brigade"
    session_identity = "github.com/other/brigade"
    _seed_session(
        conn,
        node=NODE_A,
        dirty_paths=["src/a.py"],
        repo_identity=session_identity,
        repo_label="brigade",
    )
    _seed_session(
        conn,
        node=NODE_B,
        dirty_paths=["src/a.py"],
        repo_identity=session_identity,
        repo_label="brigade",
        session_id="sess-other",
    )
    sessions = deck.fetch_interactive_sessions(conn, now=now)
    runs = [
        deck.LiveRun(
            node_id=NODE_A,
            run_id="run-brigade",
            repo="brigade",
            seat="seat",
            harness="claude",
            state="run.started",
            bucket="running",
            age_seconds=10,
            elapsed_seconds=10,
            repo_identity=run_identity,
        )
    ]
    view = deck.build_view(
        deck.DeckConfig(stations=(deck.StationConfig(NODE_A, "Alpha", 10),)),
        live_runs=runs,
        claims=[],
        enrolled_labels={NODE_A: "Alpha", NODE_B: "Bravo"},
        last_heard={},
        outcomes=[],
        failed_outcomes=[],
        observers=[],
        now=now,
        interactive_sessions=sessions,
    )
    run_row = next(row for row in view.repos if row.live)
    session_row = next(row for row in view.repos if row.interactive_overlap)
    assert run_row.target == "brigade"
    assert run_row.interactive_overlap is False
    assert run_row.repo_identity == run_identity
    assert session_row.target != "brigade"
    assert session_row.target != run_row.target
    assert session_row.repo_identity == session_identity
    assert session_row.live == ()
    html = repos_page.render(view, nonce="nonce", now=now)
    assert ">brigade<" in html
    assert session_row.target in html
    table = re.search(r"<table>.*?</table>", html, re.S).group(0)
    assert table.count("! overlap") == 1
    cards = html.split('<section aria-label="Repository summaries">', 1)[1].split("</section>", 1)[0]
    assert cards.count("! overlap") == 1


def test_identity_bearing_session_label_stays_unique_when_truncated():
    identity = "github.com/example/" + ("x" * 300)
    session = deck.InteractiveSession(
        node_id=NODE_A,
        harness="claude",
        session_id="sess-long",
        repo_identity=identity,
        identity_scope="fleet",
        repo_label="x" * 300,
        checkout_path="/tmp/project",
        branch="main",
        dirty_paths=("src/a.py",),
        dirty_truncated=False,
    )
    candidate = f"{session.repo_label} ({identity})"[:256]
    bounded_identity = identity[:256]
    occupied = {candidate, bounded_identity}

    label = deck._identity_bearing_session_label(session, occupied)

    assert len(label) <= 256
    assert label not in occupied
    assert label.endswith(" #2")


def seed_events(
    conn: sqlite3.Connection, *, terminal_then_older_live: bool, extra_live: int, extra_terminal: int
) -> None:
    if terminal_then_older_live:
        # Terminal latest row for one key, with an older live event underneath:
        # the key must be excluded from live runs and appear once in outcomes.
        _insert(conn, NODE_A, "base", 1, "run.created", ts=_ts(600), repo="repo")
        _insert(conn, NODE_A, "base", 2, "run.completed", ts=_ts(300), repo="repo")
    for index in range(extra_live):
        node = NODE_A if index % 2 == 0 else NODE_B
        _insert(conn, node, f"live-{index:04d}", 1, "run.created", ts=_ts(30 + index), repo=f"repo-{index % 7}")
    for index in range(extra_terminal):
        _insert(conn, NODE_B, f"done-{index:04d}", 1, "run.completed", ts=_ts(120 + index), repo="repo")


def test_load_config_validates_and_strips_controls(tmp_path):
    path = tmp_path / "deck.json"
    path.write_text(json.dumps({"stations": [{"node_id": NODE_A, "name": "Alpha\x00", "capacity": 6}]}))
    assert deck.load_config(path).stations == (deck.StationConfig(NODE_A, "Alpha", 6),)
    for raw in ({}, {"stations": []}, {"stations": [{"node_id": "unknown", "name": "A", "capacity": 1}]}):
        path.write_text(json.dumps(raw))
        with pytest.raises(deck.DeckConfigError):
            deck.load_config(path)


def test_bucket_and_collision_rules():
    assert deck.bucket_for("run.dispatch.failed", age_seconds=1, stale_after_seconds=1800) == "failed"
    assert deck.bucket_for("run.paused", age_seconds=3600, stale_after_seconds=1800) == "awaiting approval"
    assert deck.bucket_for("run.created", age_seconds=1, stale_after_seconds=1800) == "queued"
    rows = [
        deck.LiveRun(NODE_A, "a", "repo", "", "", "run.created", "queued", 1, 1),
        deck.LiveRun(NODE_B, "b", "repo", "", "", "run.created", "queued", 1, 1),
    ]
    assert deck.collides("repo", rows, {"repo": deck.Claim("repo", NODE_A, "", 60)}) is True


def test_collision_requires_concurrent_live_nodes_not_claim_drift():
    one_node = [deck.LiveRun(NODE_B, "a", "repo", "", "", "run.created", "queued", 1, 1)]
    same_node = [
        *one_node,
        deck.LiveRun(NODE_B, "b", "repo", "", "", "run.started", "running", 1, 1),
    ]
    claims = {"repo": deck.Claim("repo", NODE_A, "", 60)}

    assert deck.collides("repo", one_node, claims) is False
    assert deck.collides("repo", same_node, claims) is False
    assert (
        deck.collides(
            "repo",
            [*same_node, deck.LiveRun(NODE_A, "c", "repo", "", "", "run.started", "running", 1, 1)],
            claims,
        )
        is True
    )


def test_capped_queries_select_latest_then_limit(conn):
    seed_events(conn, terminal_then_older_live=True, extra_live=201, extra_terminal=101)
    assert len(deck.fetch_live_runs(conn, now=NOW, stale_after_seconds=1800)) == 200
    assert len(deck.fetch_outcomes(conn, outcome_window=100)) == 100
    assert all(
        row.state not in deck.TERMINAL_STATES for row in deck.fetch_live_runs(conn, now=NOW, stale_after_seconds=1800)
    )


def test_terminal_suffix_states_never_appear_in_live_reads_or_outcomes(conn):
    states = (
        "run.dispatch.completed",
        "run.dispatch.interrupted",
        "run.dispatch.failed",
        "worker.cancelled",
        "worker.canceled",
        "run.timed_out",
        "engine.timeout",
    )
    for index, state in enumerate(states):
        _insert(conn, NODE_A, f"key-{index}", 1, "run.created", ts=_ts(600), repo=f"repo-{index}")
        _insert(conn, NODE_A, f"key-{index}", 2, state, ts=_ts(60), repo=f"repo-{index}")
    assert deck.fetch_live_runs(conn, now=NOW, stale_after_seconds=1800) == []
    failed = _failed(conn)
    assert sorted(run.state for run in failed) == [
        "engine.timeout",
        "run.dispatch.failed",
        "run.timed_out",
        "worker.canceled",
        "worker.cancelled",
    ]
    assert all(run.bucket == "failed" for run in failed)
    outcomes = deck.fetch_outcomes(conn, outcome_window=20)
    # Every terminal suffix reaches outcomes except the internal milestones,
    # which are terminal for the event stream but noise for an operator.
    assert sorted(run.state for run in outcomes) == [
        "engine.timeout",
        "run.dispatch.failed",
        "run.dispatch.interrupted",
        "run.timed_out",
        "worker.canceled",
        "worker.cancelled",
    ]
    assert "run.dispatch.completed" in deck.INTERNAL_OUTCOME_STATES
    assert all(run.state not in deck.INTERNAL_OUTCOME_STATES for run in outcomes)
    for state in states:
        assert deck.is_terminal_state(state)
    assert not deck.is_terminal_state("run.unfailed")


def test_unfailed_latest_row_remains_live(conn):
    _insert(conn, NODE_A, "unfailed", 1, "run.unfailed", ts=_ts(30), repo="repo-unfailed")
    runs = deck.fetch_live_runs(conn, now=NOW, stale_after_seconds=1800)
    assert [run.run_id for run in runs] == ["unfailed"]


def test_defensive_stale_runs_stay_in_rail_but_out_of_active_projections():
    config = deck.DeckConfig(
        stations=(deck.StationConfig(NODE_A, "Alpha", 2), deck.StationConfig(NODE_B, "Bravo", 2)),
        stale_after_seconds=1800,
    )
    stale_run = deck.LiveRun(
        node_id=NODE_B,
        run_id="ghost",
        repo="shared",
        seat="seat",
        harness="claude",
        state="run.started",
        bucket="stale",
        age_seconds=3600,
        elapsed_seconds=3600,
    )
    stale_only = deck.LiveRun(
        node_id=NODE_A,
        run_id="stale-only",
        repo="stale-only",
        seat="seat",
        harness="claude",
        state="run.started",
        bucket="stale",
        age_seconds=3600,
        elapsed_seconds=3600,
    )
    fresh_run = deck.LiveRun(
        node_id=NODE_A,
        run_id="fresh",
        repo="shared",
        seat="seat",
        harness="claude",
        state="run.started",
        bucket="running",
        age_seconds=30,
        elapsed_seconds=30,
    )
    view = deck.build_view(
        config,
        live_runs=[stale_run, stale_only, fresh_run],
        claims=[],
        enrolled_labels={NODE_A: "Alpha", NODE_B: "Bravo"},
        last_heard={},
        outcomes=[],
        failed_outcomes=[],
        observers=[],
        now=NOW,
    )
    alpha, bravo = view.stations
    assert [tile.run.run_id for tile in alpha.tiles] == ["fresh"]
    assert alpha.busy == 1
    assert bravo.tiles == () and bravo.busy == 0
    assert [entry.run_id for entry in view.rail] == ["ghost", "stale-only"]
    assert [(row.target, tuple(run.run_id for run in row.live), row.collision) for row in view.repos] == [
        ("shared", ("fresh",), False)
    ]


def test_fetch_live_runs_drops_old_nonterminal_latest_rows(conn):
    _insert(
        conn,
        NODE_A,
        "ancient",
        1,
        "run.started",
        ts=_ts(30),
        received_at=_ts(7200),
        repo="repo-old",
    )
    _insert(conn, NODE_A, "fresh", 1, "run.started", ts=_ts(30), repo="repo-new")
    runs = deck.fetch_live_runs(conn, now=NOW, stale_after_seconds=1800)
    assert [run.run_id for run in runs] == ["fresh"]


def test_fetch_live_runs_uses_hub_receipt_time_not_skewed_host_clock(conn):
    _insert(conn, NODE_A, "clock-behind", 1, "run.started", ts=_ts(7200), received_at=_ts(30), repo="repo-a")
    _insert(conn, NODE_B, "clock-ahead", 1, "run.started", ts=_ts(-7200), received_at=_ts(40), repo="repo-b")

    runs = deck.fetch_live_runs(conn, now=NOW, stale_after_seconds=1800)

    assert {run.run_id for run in runs} == {"clock-behind", "clock-ahead"}


def test_outcomes_hide_internal_milestones_and_keep_distinct_run_ids(conn):
    _insert(conn, NODE_A, "dispatch.alpha", 1, "run.dispatch.completed", repo="repo-a")
    _insert(conn, NODE_A, "dispatch.beta", 1, "run.synthesis.completed", repo="repo-b")
    _insert(conn, NODE_A, "external.alpha", 1, "run.completed", repo="repo-a")
    _insert(conn, NODE_A, "external.beta", 1, "provider.completed", repo="repo-b")

    assert [(run.run_id, run.state) for run in deck.fetch_outcomes(conn, outcome_window=10)] == [
        ("external.alpha", "run.completed"),
        ("external.beta", "provider.completed"),
    ]


def test_failed_attention_keeps_newest_per_node_repo_and_suppresses_active_retries(conn):
    _insert(conn, NODE_A, "dispatch.old", 1, "run.dispatch.failed", ts=_ts(300), repo="retrying")
    _insert(conn, NODE_A, "dispatch.active", 1, "run.dispatch.observed", ts=_ts(30), repo="retrying")
    _insert(conn, NODE_A, "failure.old", 1, "run.dispatch.failed", ts=_ts(240), repo="same-repo")
    _insert(conn, NODE_A, "failure.new", 1, "run.dispatch.failed", ts=_ts(60), repo="same-repo")
    _insert(conn, NODE_A, "prefix.alpha", 1, "run.dispatch.failed", ts=_ts(90), repo="repo-a")
    _insert(conn, NODE_A, "prefix.beta", 1, "run.dispatch.failed", ts=_ts(120), repo="repo-b")

    assert [(run.run_id, run.repo) for run in _failed(conn)] == [
        ("failure.new", "same-repo"),
        ("prefix.alpha", "repo-a"),
        ("prefix.beta", "repo-b"),
    ]


def test_newer_successful_terminal_clears_failed_needs_you_for_same_repo(conn):
    _insert(conn, NODE_A, "failure.old", 1, "run.failed", ts=_ts(180), repo="same-repo")
    _insert(conn, NODE_A, "success.new", 1, "run.completed", ts=_ts(30), repo="same-repo")
    _insert(conn, NODE_A, "other.fail", 1, "run.failed", ts=_ts(60), repo="other-repo")

    assert [(run.run_id, run.repo) for run in _failed(conn)] == [("other.fail", "other-repo")]


def test_stale_nonterminal_retry_does_not_hide_failed_needs_you(conn):
    _insert(conn, NODE_A, "failure.old", 1, "run.failed", ts=_ts(240), repo="retrying")
    _insert(conn, NODE_A, "retry.dead", 1, "run.started", ts=_ts(30), received_at=_ts(7200), repo="retrying")

    assert [(run.run_id, run.repo) for run in _failed(conn, stale_after_seconds=1800)] == [("failure.old", "retrying")]


def test_fresh_active_retry_suppresses_failed_needs_you(conn):
    _insert(conn, NODE_A, "failure.old", 1, "run.failed", ts=_ts(240), repo="retrying")
    _insert(conn, NODE_A, "retry.live", 1, "run.started", ts=_ts(30), repo="retrying")

    assert _failed(conn, stale_after_seconds=1800) == []


def test_different_node_cannot_clear_failed_needs_you(conn):
    identity = "github.com/example/shared"
    _insert(
        conn,
        NODE_A,
        "failure.a",
        1,
        "run.failed",
        ts=_ts(180),
        repo="checkout-a",
        repo_identity=identity,
    )
    _insert(
        conn,
        NODE_B,
        "success.b",
        1,
        "run.completed",
        ts=_ts(30),
        repo="checkout-b",
        repo_identity=identity,
    )

    assert [(run.run_id, run.node_id) for run in _failed(conn)] == [("failure.a", NODE_A)]


def test_repoless_run_cannot_clear_another_repoless_failure(conn):
    _insert(conn, NODE_A, "failure.empty", 1, "run.failed", ts=_ts(180), repo="")
    _insert(conn, NODE_A, "retry.empty", 1, "run.started", ts=_ts(30), repo="")
    _insert(conn, NODE_A, "success.empty", 1, "run.completed", ts=_ts(20), repo="")

    assert [(run.run_id, run.repo) for run in _failed(conn)] == [("failure.empty", "")]


def test_internal_completed_placeholder_does_not_clear_failed_needs_you(conn):
    _insert(conn, NODE_A, "failure.old", 1, "run.failed", ts=_ts(180), repo="same-repo")
    _insert(conn, NODE_A, "dispatch.done", 1, "run.dispatch.completed", ts=_ts(30), repo="same-repo")

    assert [(run.run_id, run.repo) for run in _failed(conn)] == [("failure.old", "same-repo")]


def test_skewed_node_ts_does_not_hide_failed_needs_you(conn):
    _insert(
        conn,
        NODE_A,
        "failure.old",
        1,
        "run.failed",
        ts=_ts(30),
        received_at=_ts(180),
        repo="same-repo",
        repo_identity="github.com/example/same",
    )
    _insert(
        conn,
        NODE_A,
        "retry.skewed",
        1,
        "run.started",
        ts=_ts(-7200),
        received_at=_ts(7200),
        repo="same-repo",
        repo_identity="github.com/example/same",
    )

    assert [(run.run_id, run.repo) for run in _failed(conn, stale_after_seconds=1800)] == [("failure.old", "same-repo")]


def test_needs_you_prefers_repo_identity_over_local_checkout_name(conn):
    _insert(
        conn,
        NODE_A,
        "failure.old",
        1,
        "run.failed",
        ts=_ts(180),
        repo="checkout-a",
        repo_identity="github.com/example/same",
    )
    _insert(
        conn,
        NODE_A,
        "success.new",
        1,
        "run.completed",
        ts=_ts(30),
        repo="checkout-b",
        repo_identity="github.com/example/same",
    )
    _insert(
        conn,
        NODE_A,
        "other.fail",
        1,
        "run.failed",
        ts=_ts(60),
        repo="checkout-a",
        repo_identity="github.com/example/other",
    )

    assert [(run.run_id, run.repo_identity) for run in _failed(conn)] == [("other.fail", "github.com/example/other")]


def test_external_expired_is_terminal_and_does_not_stay_live(conn):
    _insert(conn, NODE_A, "expired-job", 1, "external.expired", ts=_ts(20), repo="repo-expired")
    _insert(conn, NODE_A, "ack-job", 1, "external.cancel-acknowledged", ts=_ts(10), repo="repo-ack")
    _insert(conn, NODE_A, "live-job", 1, "run.started", ts=_ts(15), repo="repo-live")

    assert deck.is_terminal_state("external.expired") is True
    assert deck.is_terminal_state("external.cancel-acknowledged") is True
    live = deck.fetch_live_runs(conn, now=NOW, stale_after_seconds=1800)
    assert [run.run_id for run in live] == ["live-job"]
    outcomes = {run.run_id: run.state for run in deck.fetch_outcomes(conn, outcome_window=10)}
    assert outcomes["expired-job"] == "external.expired"
    assert outcomes["ack-job"] == "external.cancel-acknowledged"
    view = deck.build_view(
        deck.DeckConfig(stations=(deck.StationConfig(NODE_A, "Alpha", 4),)),
        live_runs=live,
        claims=[],
        enrolled_labels={NODE_A: "Alpha"},
        last_heard={},
        outcomes=[],
        failed_outcomes=[],
        observers=[],
        now=NOW,
    )
    assert view.stations[0].busy == 1
    assert [tile.run.run_id for tile in view.stations[0].tiles] == ["live-job"]


def test_grokbot_cross_node_revisions_use_one_global_latest_row(conn):
    terminal_states = ("external.completed", "external.canceled", "external.expired")
    for index, state in enumerate(terminal_states, start=1):
        run_id = f"grok-terminal-{index}"
        _insert(conn, NODE_A, run_id, 1, "external.queued", harness="grokbot", ts=_ts(600))
        _insert(conn, NODE_B, run_id, 2, state, harness="grokbot", ts=_ts(60))

    _insert(conn, NODE_A, "grok-running", 1, "external.queued", harness="grokbot", ts=_ts(600))
    _insert(conn, NODE_B, "grok-running", 2, "external.running", harness="grokbot", ts=_ts(30))
    _insert(conn, NODE_A, "shared-normal-run", 1, "run.started", ts=_ts(50))
    _insert(conn, NODE_B, "shared-normal-run", 1, "run.started", ts=_ts(40))

    live = deck.fetch_live_runs(conn, now=NOW, stale_after_seconds=1800)
    assert [(run.node_id, run.run_id, run.state) for run in live] == [
        (NODE_B, "grok-running", "external.running"),
        (NODE_B, "shared-normal-run", "run.started"),
        (NODE_A, "shared-normal-run", "run.started"),
    ]
    assert {(run.node_id, run.run_id): run.elapsed_seconds for run in live} == {
        (NODE_B, "grok-running"): 600.0,
        (NODE_B, "shared-normal-run"): 40.0,
        (NODE_A, "shared-normal-run"): 50.0,
    }
    outcomes = deck.fetch_outcomes(conn, outcome_window=10)
    assert {(run.node_id, run.run_id, run.state) for run in outcomes} == {
        (NODE_B, "grok-terminal-1", "external.completed"),
        (NODE_B, "grok-terminal-2", "external.canceled"),
        (NODE_B, "grok-terminal-3", "external.expired"),
    }

    active_status = fleet_hub_status.latest_status(conn, now=NOW)
    assert [(row["node_id"], row["run_id"]) for row in active_status] == [
        (NODE_A, "shared-normal-run"),
        (NODE_B, "grok-running"),
        (NODE_B, "shared-normal-run"),
    ]
    all_status = fleet_hub_status.latest_status(conn, include_all=True, now=NOW)
    assert [(row["node_id"], row["run_id"], row["state"]) for row in all_status] == [
        (NODE_A, "shared-normal-run", "run.started"),
        (NODE_B, "grok-running", "external.running"),
        (NODE_B, "grok-terminal-1", "external.completed"),
        (NODE_B, "grok-terminal-2", "external.canceled"),
        (NODE_B, "grok-terminal-3", "external.expired"),
        (NODE_B, "shared-normal-run", "run.started"),
    ]


# --- HTTP integration: hub-served Command Deck (task 2) ----------------------

TOKEN = "test-token-12345"
HOLDER_TOKEN = "secret-holder-token-zz"
NODE_C = "33333333-3333-4333-8333-333333333333"

CONFIG = {
    "stations": [
        {"node_id": NODE_A, "name": "Alpha", "capacity": 4},
        {"node_id": NODE_B, "capacity": 2},
    ]
}


@contextmanager
def _start_hub(tmp_path, config: dict | None):
    db = tmp_path / "hub" / "fleet.db"
    config_path = None
    if config is not None:
        config_path = tmp_path / "deck.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
    server = fleet_hub.make_server(
        "127.0.0.1", 0, db, TOKEN, allow_admin_writes=True, deck_config_path=config_path
    )  # content-guard: allow loopback-ipv4
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield ("127.0.0.1", server.server_address[1]), db  # content-guard: allow loopback-ipv4
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(hub, method: str, path: str, *, headers: dict | None = None, body=None):
    host, port = hub
    conn = http.client.HTTPConnection(host, port, timeout=5)
    data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data, headers=headers or {})
    response = conn.getresponse()
    text = response.read().decode("utf-8")
    result = (response.status, {k.lower(): v for k, v in response.getheaders()}, text)
    conn.close()
    return result


def _bearer(extra: dict | None = None) -> dict:
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json", **(extra or {})}


def _login_cookie(hub, path: str = "/") -> str:
    status, _headers, text = _request(hub, "POST", "/dashboard/enrollment", headers=_bearer(), body={})
    assert status == 201
    code = json.loads(text)["code"]
    status, headers, _text = _request(
        hub,
        "POST",
        "/enroll",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Sec-Fetch-Site": "same-origin"},
        body=f"code={code}".encode(),
    )
    assert status == 303
    return headers["set-cookie"].split(";")[0]


def _seed_run(hub, node: str, run_id: str, state: str, *, repo: str, ts: str | None = None) -> None:
    event = {
        "node_id": node,
        "run_id": run_id,
        "repo": repo,
        "seat": "worker",
        "harness": "claude",
        "state": state,
        "ts": ts or datetime.now(timezone.utc).isoformat(),
        "sequence": 1,
        "digest": f"d-{node[:4]}-{run_id}",
    }
    status, _headers, text = _request(hub, "POST", "/events", headers=_bearer(), body=[event])
    assert status == 200, text


def _seed_claim(hub, target: str, *, node: str = NODE_B, ttl: int = 900) -> None:
    claim = {"action": "acquire", "target": target, "node_id": node, "holder": HOLDER_TOKEN, "ttl_seconds": ttl}
    status, _headers, text = _request(hub, "POST", "/claims", headers=_bearer(), body=claim)
    assert status == 200, text


def test_deck_requires_bearer_or_cookie(tmp_path):
    with _start_hub(tmp_path, CONFIG) as (hub, _db):
        assert _request(hub, "GET", "/deck")[0] == 401
        assert _request(hub, "GET", "/deck/repos", headers=_bearer())[0] == 200
        cookie = _login_cookie(hub, "/deck")
        assert _request(hub, "GET", "/deck", headers={"Cookie": cookie})[0] == 200
        assert _request(hub, "GET", "/status", headers={"Cookie": cookie})[0] == 401


def test_command_deck_is_root_and_legacy_boards_stay_under_view(tmp_path):
    with _start_hub(tmp_path, CONFIG) as (hub, _db):
        root = _request(hub, "GET", "/", headers=_bearer())[2]
        alias = _request(hub, "GET", "/deck", headers=_bearer())[2]
        assert "Command Deck" in root and "Alpha" in root
        assert "Command Deck" in alias and "Alpha" in alias
        assert 'href="/view/machines"' in root and 'href="/view/repos"' in root
        # Root is the deck itself, so the sidebar marks Overview current; what
        # must not exist is a legacy board served from root.
        assert f'<a href="/deck" aria-current="page">{NAV_ICONS["overview"]}Overview</a>' in root
        assert 'href="/machines"' not in root and 'href="/repos"' not in root
        for path, title in (
            ("/view/machines", "Fleet: Machines"),
            ("/view/repos", "Fleet: Repos"),
        ):
            page = _request(hub, "GET", path, headers=_bearer())[2]
            assert title in page
            assert 'href="/"' not in page


def test_headers_secrets_and_responsive_css(tmp_path):
    with _start_hub(tmp_path, CONFIG) as (hub, _db):
        status, headers, body = _request(hub, "GET", "/deck", headers=_bearer())
        assert status == 200 and headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert all(secret not in body for secret in (TOKEN, HOLDER_TOKEN, _login_cookie(hub).split("=", 1)[1]))
        assert "overflow-wrap: anywhere" in body
        assert "table-layout: fixed" in body
        assert "@media (max-width: 700px)" in body
        assert "@media (max-width: 900px)" in body
        assert ".shell-sidebar" in body


def test_invalid_config_refused_at_make_server(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"stations": []}), encoding="utf-8")
    db = tmp_path / "hub" / "fleet.db"
    with pytest.raises(deck.DeckConfigError):
        fleet_hub.make_server("127.0.0.1", 0, db, TOKEN, allow_admin_writes=True, deck_config_path=bad)
    assert not db.exists()


def test_station_order_labels_capacity_and_timeline_bounds(tmp_path):
    config = {
        "stations": [
            {"node_id": NODE_B, "capacity": 1},
            {"node_id": NODE_A, "name": "Alpha", "capacity": 2},
        ],
        "outcome_window": 2,
    }
    with _start_hub(tmp_path, config) as (hub, _db):
        status, _headers, text = _request(
            hub,
            "POST",
            "/nodes",
            headers=_bearer(),
            body={"action": "add", "node_id": NODE_B, "label": "BravoBox"},
        )
        assert status == 200, text
        status, _headers, text = _request(
            hub, "POST", "/nodes", headers=_bearer(), body={"action": "add", "node_id": NODE_A}
        )
        assert status == 200, text
        _seed_run(hub, NODE_A, "ra", "run.created", repo="repo-a")
        _seed_run(hub, NODE_B, "rb", "run.created", repo="repo-b")
        for index in range(3):
            _seed_run(hub, NODE_A, f"done-{index}", "run.completed", repo="repo-a")
        _seed_claim(hub, "repo-b", node=NODE_B)
        _status, _headers, body = _request(hub, "GET", "/deck", headers=_bearer())
        # Fixed station order follows the config, not events.
        machine_rows = body.split('id="machines-table"', 1)[1].split("</section>", 1)[0]
        assert machine_rows.index(NODE_B[:12]) < machine_rows.index("Alpha")
        # Unnamed station falls back to the enrolled label then node id prefix.
        heading = f'title="{NODE_B}"'
        assert heading in body and "BravoBox" in body
        assert "not enrolled" not in body
        # Capacity and busy counts.
        assert "1 busy, 0 unknown" in body and "1 busy, 1 unknown" in body
        # Terminal-only timeline, bounded by outcome_window.
        assert "Recent outcomes" in body
        assert body.count("run.completed") <= 2


@pytest.mark.parametrize("claimed", [False, True])
def test_parallel_activity_is_neutral_and_observers_and_expired_claims(tmp_path, claimed):
    with _start_hub(tmp_path, CONFIG) as (hub, _db):
        for node_id, label in ((NODE_A, "Alpha"), (NODE_B, "Beta")):
            status, _headers, text = _request(
                hub,
                "POST",
                "/nodes",
                headers=_bearer(),
                body={"action": "add", "node_id": node_id, "label": label},
            )
            assert status == 200, text
        _seed_run(hub, NODE_A, "ca", "run.created", repo="shared")
        _seed_run(hub, NODE_B, "cb", "run.created", repo="shared")
        if claimed:
            _seed_claim(hub, "shared", node=NODE_A)
        _seed_claim(hub, "gone", node=NODE_A, ttl=1)
        _seed_run(hub, NODE_C, "obs", "run.created", repo="observer-repo")
        time.sleep(1.5)
        _status, _headers, deck_body = _request(hub, "GET", "/deck", headers=_bearer())
        assert "parallel activity" in deck_body
        assert "! collision" not in deck_body
        assert "Same repo label on Alpha, Beta" in deck_body
        assert "File overlap unknown" in deck_body
        alerts_page = _request(hub, "GET", "/deck/alerts", headers=_bearer())[2]
        rail = alerts_page.split('<ul class="alert-rows">')[1].split("</ul>")[0]
        assert "shared" not in rail and "parallel" not in rail
        _status, _headers, repos_body = _request(hub, "GET", "/deck/repos", headers=_bearer())
        repo_rows = repos_body.split("<tbody>")[1].split("<tr>")[1:]
        assert "observer-repo" in repo_rows[0]
        shared_row = next(row for row in repo_rows if "<td>shared</td>" in row)
        assert "parallel activity" in shared_row
        assert 'class="flag"' not in shared_row
        assert "Alpha:run.created" in shared_row and "Beta:run.created" in shared_row
        # Expired claims are absent.
        assert "gone" not in repos_body
        # Observers are excluded from station totals.
        assert "observer-repo" not in deck_body.split("<main", 1)[1]
        assert "Other observers" in deck_body and NODE_C[:12] in deck_body


def test_hostile_labels_are_escaped_and_no_config_hint(tmp_path):
    hostile = {
        "stations": [
            {
                "node_id": NODE_A,
                "name": '<script>alert("x")</script>',
                "capacity": 3,
            }
        ]
    }
    with _start_hub(tmp_path, hostile) as (hub, _db):
        _status, _headers, body = _request(hub, "GET", "/deck", headers=_bearer())
        assert "<script>" not in body
        assert "&lt;script&gt;" in body
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"stations": [{"node_id": NODE_A, "name": "Solo", "capacity": 1}]}), encoding="utf-8")
    with _start_hub(tmp_path / "no-config", None) as (hub, _db):
        _status, _headers, body = _request(hub, "GET", "/deck", headers=_bearer())
        assert "No stations configured" in body


def test_cloud_workers_render_safe_active_leases_without_using_station_capacity(tmp_path):
    with _start_hub(tmp_path, CONFIG) as (hub, db_path):
        conn = fleet_hub.open_db(db_path)
        try:
            status, admitted = fleet_hub.handle_cloud(
                conn,
                {
                    "action": "admit",
                    "provider": "cursor-cloud",
                    "lease_id": "lease-cursor",
                    "node_id": NODE_A,
                    "holder": HOLDER_TOKEN,
                    "repo": '<repo & "one">',
                    "label": '<task & "one">',
                    "prompt_hash": "a" * 64,
                },
                config=deck.load_config(tmp_path / "deck.json"),
            )
            assert status == 200 and admitted["admitted"] is True
            status, bound = fleet_hub.handle_cloud(
                conn,
                {
                    "action": "bind",
                    "lease_id": "lease-cursor",
                    "node_id": NODE_A,
                    "holder": HOLDER_TOKEN,
                    "provider_task_id": '<task-id & "one">',
                    "artifact_ref": "https://example.invalid/task/1",
                },
                config=deck.load_config(tmp_path / "deck.json"),
            )
            assert status == 200 and bound["bound"] is True
        finally:
            conn.close()

        _seed_run(hub, NODE_A, "physical", "run.created", repo="physical-repo")
        status, headers, body = _request(hub, "GET", "/", headers=_bearer())
        assert status == 200 and "frame-ancestors 'none'" in headers["content-security-policy"]
        assert ">Provider leases</h2>" in body
        assert " cursor</h3>" in body and ">1/3<" in body
        assert "closed" in body
        assert " codex</h3>" in body and ">0/2<" in body and "No active leases." in body
        assert "1 busy, 3 unavailable" in body
        assert "&lt;task &amp; &quot;one&quot;&gt;" in body
        assert "&lt;repo &amp; &quot;one&quot;&gt;" in body
        assert "&lt;task-id &amp; &quot;one&quot;&gt;" in body
        assert HOLDER_TOKEN not in body and "a" * 64 not in body


def test_command_deck_projects_hub_grokbot_rows_and_ignores_terminal_capacity(tmp_path):
    from brigade import fleet_hub_grokbot

    feed = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    worker = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    with _start_hub(tmp_path, CONFIG) as (hub, db_path):
        conn = fleet_hub.open_db(db_path)
        try:
            for node, kind, role in (
                (feed, "feed", None),
                (worker, "implementation-worker", "implementation-worker"),
            ):
                body = {
                    "action": "enroll-actor",
                    "enroll_node_id": node,
                    "queue_owner_node_id": feed,
                    "queue_id": "grokbot-queue-main",
                    "actor_kind": kind,
                    "enabled": True,
                }
                if role:
                    body["role"] = role
                assert fleet_hub_grokbot.handle_grokbot(conn, body, caller_node=None)[0] == 200
            job_id = "grokbot-" + "a" * 24
            queued = fleet_hub_grokbot.handle_grokbot(
                conn,
                {
                    "action": "enqueue",
                    "job_id": job_id,
                    "role": "implementation-worker",
                    "repository": "example/brigade",
                    "label": "deck job",
                    "task_digest": "b" * 64,
                    "idempotency_key_hash": "b" * 64,
                    "timeout_seconds": 900,
                    "artifact_kind": "draft-pr",
                    "private_snapshot_id": job_id,
                    "operation_id": "op-enq",
                },
                caller_node=feed,
            )
            assert queued[0] == 200
            claimed = fleet_hub_grokbot.handle_grokbot(
                conn,
                {
                    "action": "claim",
                    "job_id": job_id,
                    "lease_id": "lease-deck",
                    "expected_item_revision": 1,
                    "lease_seconds": 300,
                    "operation_id": "op-claim",
                },
                caller_node=worker,
            )
            assert claimed[0] == 200
        finally:
            conn.close()
        status, _headers, body = _request(hub, "GET", "/deck", headers=_bearer())
        assert status == 200
        assert " grok-bot</h3>" in body and ">1/64<" in body
        assert "lease-deck" not in body
        conn = fleet_hub.open_db(db_path)
        try:
            fleet_hub_grokbot.handle_grokbot(
                conn,
                {
                    "action": "fail",
                    "job_id": job_id,
                    "lease_id": "lease-deck",
                    "expected_item_revision": 2,
                    "lease_generation": 1,
                    "operation_id": "op-fail",
                },
                caller_node=worker,
            )
        finally:
            conn.close()
        _status, _headers, after = _request(hub, "GET", "/deck", headers=_bearer())
        assert ">0/64<" in after
        assert "lease-deck" not in after


def _asset_request(hub, path: str):
    """Raw bytes request for binary assets; does not decode as text."""
    host, port = hub
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", path, headers={})
    response = conn.getresponse()
    data = response.read()
    headers = {k.lower(): v for k, v in response.getheaders()}
    result = (response.status, headers, data)
    conn.close()
    return result


def test_command_deck_asset_routes_and_metadata(tmp_path):
    with _start_hub(tmp_path, CONFIG) as (hub, _db):
        for path, expected_type in (
            ("/favicon.ico", "image/x-icon"),
            ("/favicon-32x32.png", "image/png"),
            ("/apple-touch-icon.png", "image/png"),
            ("/icon-192.png", "image/png"),
            ("/icon-512.png", "image/png"),
            ("/site.webmanifest", "application/manifest+json"),
        ):
            status, headers, body = _asset_request(hub, path)
            assert status == 200, path
            assert headers["content-type"].startswith(expected_type), path
            assert int(headers["content-length"]) == len(body), path
            assert headers["x-content-type-options"] == "nosniff", path

        for path in ("/", "/deck", "/deck/repos"):
            status, headers, body = _request(hub, "GET", path, headers=_bearer())
            assert status == 200, path
            csp = headers["content-security-policy"]
            assert "img-src 'self'" in csp
            assert "manifest-src 'self'" in csp
            assert '<link rel="icon" type="image/x-icon" href="/favicon.ico"' in body
            assert '<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png"' in body
            assert '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png"' in body
            assert '<link rel="manifest" href="/site.webmanifest"' in body
            assert '<meta name="theme-color" content="#0a0a0a"' in body
            assert '<meta name="application-name" content="Fleet Hub"' in body
            assert '<meta name="apple-mobile-web-app-title" content="Fleet Hub"' in body


class TestDeckCli:
    def test_flag_precedence_env_fallback_and_forwarding(self, tmp_path, monkeypatch):
        from brigade import cli

        captured: dict = {}
        monkeypatch.setattr(fleet_hub, "run", lambda **kw: captured.update(kw) or 0)
        flag = tmp_path / "flag.json"
        env = tmp_path / "env.json"
        monkeypatch.setenv("BRIGADE_FLEET_DECK_CONFIG", str(env))
        argv = ["fleet", "serve", "--host", "127.0.0.1", "--db", str(tmp_path / "x.db"), "--deck-config", str(flag)]
        assert cli.main(argv) == 0
        assert captured["deck_config_path"] == flag
        monkeypatch.delenv("BRIGADE_FLEET_DECK_CONFIG")
        argv.pop()
        argv.pop()
        assert cli.main(argv) == 0
        assert captured["deck_config_path"] is None
        monkeypatch.setenv("BRIGADE_FLEET_DECK_CONFIG", str(env))
        assert cli.main(argv) == 0
        assert captured["deck_config_path"] == env
        monkeypatch.setenv("BRIGADE_FLEET_DECK_CONFIG", "   ")
        assert cli.main(argv) == 0
        assert captured["deck_config_path"] is None


# --- control plane projection (#fleet policy UI) ------------------------------


def _control_plane_snapshot(**overrides):
    snapshot = {
        "policy": {"version": "7", "digest": "sha256:abc", "updated_at": "2026-09-05T11:00:00Z"},
        "sessions": [],
        "machines": [
            {
                "id": "worker-linux-1",
                "os": "linux",
                "state": "online",
                "last_observed_at": "2026-09-05T11:59:00Z",
                "load": 1,
                "capacity": 4,
                "active_claims": 1,
                "available_slots": 3,
                "eligible_queued_work": 0,
                "unused_reason": "no eligible queued work",
            }
        ],
        "quota": [{"pool": "pool-one", "used": 3, "limit": 10, "observed_at": "2026-09-05T11:00:00Z"}],
        "routes": [
            {
                "decision_id": "d-1",
                "policy_version": "7",
                "origin": "brigade-run",
                "selected": "seat-alpha",
                "reason": "role impl",
                "candidates": ["seat-alpha", "seat-beta"],
                "created_at": "2026-09-05T11:30:00Z",
                "override_reason": "",
                "state": "dispatched",
            }
        ],
        "queue": {"eligible_count": 0, "queued_count": 0, "blocked": []},
        "integrations": [
            {
                "id": "routine-one",
                "routine_state": "active",
                "last_pickup": "2026-09-05T11:40:00Z",
                "last_report": "2026-09-05T11:45:00Z",
                "queued_age": "0s",
                "detail": "",
            }
        ],
    }
    snapshot.update(overrides)
    return snapshot


def _empty_view(control_plane=None, rail=()):
    return deck.DeckView(
        stations=(),
        rail=tuple(rail),
        repos=(),
        outcomes=(),
        observers=(),
        control_plane=control_plane,
    )


def test_empty_rail_without_telemetry_is_not_all_clear():
    view = _empty_view()
    assert deck.deck_verdict(view) == "COVERAGE UNKNOWN (1)"
    html = deck.render_deck(view, nonce="nonce", now=NOW)
    assert "ALL CLEAR" not in html
    assert "Coverage unknown" in html
    assert "not the same as nothing being wrong" in html


def test_full_telemetry_with_no_signals_is_all_clear():
    snapshot = _control_plane_snapshot(authority={"active": True, "status": "active"})
    view = _empty_view(deck.control_plane_from_snapshot(snapshot))
    assert deck.coverage_gaps(view.control_plane) == ()
    assert deck.deck_verdict(view) == "ALL CLEAR"


def test_missing_authority_is_a_coverage_gap_not_an_all_clear():
    """A legacy snapshot without ``authority`` must not read as full coverage.

    The panel prints "authority unknown" for such a snapshot. Without authority
    in the coverage calculation it simultaneously claimed every section had
    reported, and deck_verdict answered ALL CLEAR.
    """
    for snapshot in (_control_plane_snapshot(), _control_plane_snapshot(authority={})):
        view = _empty_view(deck.control_plane_from_snapshot(snapshot))
        assert "control-plane authority" in deck.coverage_gaps(view.control_plane)
        assert deck.deck_verdict(view) == "COVERAGE UNKNOWN (1)"
        html = deck.render_deck(view, nonce="nonce", now=NOW)
        assert "authority unknown" in html
        assert "all control-plane sections reported" not in html
        assert "ALL CLEAR" not in html


def test_partial_telemetry_names_the_missing_sections():
    snapshot = _control_plane_snapshot(authority={"active": True, "status": "active"})
    del snapshot["quota"]
    del snapshot["routes"]
    view = _empty_view(deck.control_plane_from_snapshot(snapshot))
    assert set(deck.coverage_gaps(view.control_plane)) == {"quota", "routes"}
    html = deck.render_deck(view, nonce="nonce", now=NOW)
    assert "no telemetry for: quota, routes" in html
    assert "unknown, not unlimited" in html
    assert "unknown, not unrouted" in html


def test_existing_run_and_claim_signals_survive_the_verdict_change():
    entry = deck.RailEntry(kind="failed", node_id=NODE_A, repo="acme/tool", run_id="r-1", detail="exit 1")
    view = _empty_view(deck.control_plane_from_snapshot(_control_plane_snapshot()), rail=[entry])
    assert deck.deck_verdict(view) == "NEEDS ATTENTION (1)"
    from brigade.fleet_deck_alerts import render_alerts_page

    html = render_alerts_page(view, NOW, nonce="nonce", worklore_enabled=True)
    assert "acme/tool" in html
    assert "exit 1" in html


def test_integration_needing_attention_is_not_reported_as_paused():
    snapshot = _control_plane_snapshot(
        integrations=[
            {
                "id": "routine-one",
                "routine_state": "needs-attention",
                "last_pickup": "2026-09-05T11:40:00Z",
                "last_report": None,
                "queued_age": "40m",
                "detail": "away confirmation pending",
            }
        ]
    )
    view = _empty_view(deck.control_plane_from_snapshot(snapshot))
    assert deck.deck_verdict(view) == "NEEDS ATTENTION (1)"
    html = deck.render_deck(view, nonce="nonce", now=NOW)
    assert "needs-attention" in html
    assert "paused" not in html.replace("never proof it is paused", "")
    assert "unknown" in html  # last_report was absent


def test_unknown_integration_state_is_a_coverage_gap_not_active():
    snapshot = _control_plane_snapshot(integrations=[{"id": "routine-one", "routine_state": "running"}])
    plane = deck.control_plane_from_snapshot(snapshot)
    assert plane.integrations[0].routine_state == "unknown"
    assert "integration readiness" in deck.coverage_gaps(plane)


@pytest.mark.parametrize("reported", [False, True])
def test_optional_integration_telemetry_is_distinct_from_a_missing_section(reported):
    snapshot = _control_plane_snapshot(integrations=[], authority={"active": True, "status": "active"})
    if not reported:
        del snapshot["integrations"]
    plane = deck.control_plane_from_snapshot(snapshot)
    assert ("integrations" in deck.coverage_gaps(plane)) is not reported
    html = deck.render_deck(_empty_view(plane), nonce="nonce", now=NOW)
    if reported:
        assert "No integration readiness reports in this snapshot" in html
        assert "Worklore queue sourcing" not in html
        assert "All clear" in html
    else:
        assert "Integration readiness unknown" in html
        assert "Coverage unknown (1)" in html


def test_machine_capacity_and_idle_reasons_render():
    view = _empty_view(deck.control_plane_from_snapshot(_control_plane_snapshot()))
    html = deck.render_deck(view, nonce="nonce", now=NOW)
    assert "worker-linux-1" in html
    assert "1/4" in html
    assert "no eligible queued work" in html


def test_quota_headroom_is_separated_from_execution_availability():
    view = _empty_view(deck.control_plane_from_snapshot(_control_plane_snapshot()))
    html = deck.render_deck(view, nonce="nonce", now=NOW)
    assert "pool-one" in html
    assert "not evidence that a bot or runner will execute" in html


def _quota_effective_rows():
    return [
        {
            "account_id": "account-one",
            "pool_id": "pool-one",
            "provider": "provider-a",
            "admitted": False,
            "reasons": ["window exhausted"],
            "windows": [
                {
                    "window_id": "5h",
                    "state": "exhausted",
                    "used": 10,
                    "limit": 10,
                    "remaining": 0,
                    "resets_at": "2026-09-05T16:00:00Z",
                    "source": "provider-report",
                    "observation_id": "obs-7",
                    "reason": "hard limit reached",
                }
            ],
        }
    ]


def test_effective_quota_is_preferred_over_raw_observations():
    snapshot = _control_plane_snapshot(quota_effective=_quota_effective_rows())
    plane = deck.control_plane_from_snapshot(snapshot)
    assert plane.quota_effective_present is True
    row = plane.quota_effective[0]
    assert row.account_id == "account-one"
    assert row.admitted == "no"
    assert row.windows[0].remaining == "0"
    assert row.windows[0].observation_id == "obs-7"
    html = deck.render_deck(_empty_view(plane), nonce="nonce", now=NOW)
    assert "Effective capacity" in html
    assert "obs-7" in html
    assert "window exhausted" in html
    # The raw rows are still shown, but only as history.
    assert "Quota observation history" in html
    assert "not available subscription headroom" in html


def test_absent_effective_quota_is_unknown_and_raw_rows_stay_history():
    plane = deck.control_plane_from_snapshot(_control_plane_snapshot())
    assert plane.quota_effective_present is False
    html = deck.render_deck(_empty_view(plane), nonce="nonce", now=NOW)
    assert "Effective headroom is <strong>unknown</strong>" in html
    assert "no centrally computed quota projection is attached" in html
    assert "pool-one" in html  # the raw observation is still visible
    assert "Quota observation history" in html


def test_the_deck_applies_no_quota_precedence_of_its_own():
    """Freshness and override precedence belong to the central projection."""
    rows = _quota_effective_rows()
    rows[0]["windows"][0]["remaining"] = 4
    rows[0]["windows"][0]["state"] = "open"
    rows[0]["admitted"] = True
    snapshot = _control_plane_snapshot(
        quota_effective=rows,
        # A raw observation that disagrees, and is newer.
        quota=[{"pool": "pool-one", "used": 10, "limit": 10, "observed_at": "2026-09-05T11:59:00Z"}],
    )
    html = deck.render_deck(_empty_view(deck.control_plane_from_snapshot(snapshot)), nonce="nonce", now=NOW)
    effective = html.split("Effective capacity", 1)[1].split("Quota observation history", 1)[0]
    assert "remaining <strong>4</strong>" in effective
    assert "used 10 of 10" not in effective


def test_integration_health_separates_scheduling_pickup_age_and_auth():
    snapshot = _control_plane_snapshot(
        integrations=[
            {
                "id": "grokbot",
                "routine_state": "needs-attention",
                "last_pickup": "2026-09-05T11:40:00Z",
                "last_report": None,
                "queued_age": "40m",
                "auth_state": "unauthenticated",
                "detail": "away confirmation pending",
            }
        ]
    )
    plane = deck.control_plane_from_snapshot(snapshot)
    item = plane.integrations[0]
    assert (item.routine_state, item.auth_state) == ("needs-attention", "unauthenticated")
    assert item.last_pickup == "2026-09-05T11:40:00Z"
    assert item.last_report == deck.UNKNOWN
    html = deck.render_deck(_empty_view(plane), nonce="nonce", now=NOW)
    assert "routine needs-attention" in html
    assert "auth unauthenticated" in html
    assert "queued for 40m" in html
    assert "away confirmation pending" in html
    assert "not harness capacity" in html
    assert "four separate facts" in html


def test_an_unrecognised_auth_state_is_unknown_not_authenticated():
    snapshot = _control_plane_snapshot(integrations=[{"id": "grokbot", "auth_state": "probably fine"}])
    plane = deck.control_plane_from_snapshot(snapshot)
    assert plane.integrations[0].auth_state == deck.UNKNOWN


def test_routing_receipts_and_overrides_render():
    snapshot = _control_plane_snapshot()
    snapshot["routes"][0]["override_reason"] = "operator pinned seat-beta"
    view = _empty_view(deck.control_plane_from_snapshot(snapshot))
    html = deck.render_deck(view, nonce="nonce", now=NOW)
    assert "d-1" in html
    assert "seat-alpha" in html
    assert "operator pinned seat-beta" in html


def test_blocked_queue_reasons_are_attention_not_zero():
    snapshot = _control_plane_snapshot(
        queue={"eligible_count": 2, "queued_count": 5, "blocked": [{"id": "job-1", "reason": "no eligible machine"}]}
    )
    view = _empty_view(deck.control_plane_from_snapshot(snapshot))
    assert deck.deck_verdict(view) == "NEEDS ATTENTION (1)"
    html = deck.render_deck(view, nonce="nonce", now=NOW)
    assert "no eligible machine" in html


def test_control_plane_adapter_reads_authority():
    missing = deck.control_plane_from_snapshot(_control_plane_snapshot())
    assert missing.authority_present is False
    assert missing.authority.status == deck.UNKNOWN
    html_missing = deck.render_deck(_empty_view(missing), nonce="nonce", now=NOW)
    assert "authority unknown" in html_missing

    plane = deck.control_plane_from_snapshot(_control_plane_snapshot(authority={"active": True, "status": "active"}))
    assert plane.authority_present is True
    assert plane.authority.active == "yes"
    assert plane.authority.status == "active"
    html = deck.render_deck(_empty_view(plane), nonce="nonce", now=NOW)
    assert "authority active" in html
    assert "authority unknown" not in html
    assert "authority inactive" not in html


def test_control_plane_values_are_escaped():
    snapshot = _control_plane_snapshot()
    snapshot["machines"][0]["unused_reason"] = "<script>alert(1)</script>"
    view = _empty_view(deck.control_plane_from_snapshot(snapshot))
    html = deck.render_deck(view, nonce="nonce", now=NOW)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_control_plane_adapter_tolerates_a_missing_snapshot():
    assert deck.control_plane_from_snapshot(None) is None
    assert deck.coverage_gaps(None) == ("control-plane telemetry",)


def test_build_view_carries_the_control_plane(conn, now):
    plane = deck.control_plane_from_snapshot(_control_plane_snapshot())
    view = deck.build_view(
        deck.DeckConfig(stations=(deck.StationConfig(NODE_A, "Alpha", 10),)),
        live_runs=[],
        claims=[],
        enrolled_labels={NODE_A: "Alpha"},
        last_heard={},
        outcomes=[],
        failed_outcomes=[],
        observers=[],
        now=now,
        control_plane=plane,
    )
    assert view.control_plane is plane
    html = deck.render_deck(view, nonce="nonce", now=now)
    assert 'id="control-plane"' in html and 'href="/deck/roster"' in html


def _policy_machines():
    from brigade import fleet_policy

    return {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {},
        "machines": {
            "worker-idle": {"os": "linux", "node_id": NODE_A, "concurrency": 4},
            "worker-stale": {"os": "linux", "node_id": NODE_B, "concurrency": 2},
            "worker-queued": {"os": "linux", "node_id": NODE_C, "concurrency": 1},
            "worker-unknown": {"os": "linux", "node_id": "44444444-4444-4444-8444-444444444444", "concurrency": 2},
        },
        "seats": {"seat-alpha": {"provider": "provider-a", "model": "model-a-1"}},
        "consumers": {},
        "repositories": {},
        "routing": {"enabled": True},
    }


def _insert_telemetry(db, node_id, *, observed_at, status="available", running=None, load=0.1):
    conn = fleet_hub.open_db(db)
    try:
        conn.execute(
            "INSERT INTO fleet_machine_telemetry (node_id, machine, observed_at, ttl_seconds, status, load, "
            "running, active_claims, usable_seats, credential_state, observed_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                node_id,
                node_id,
                observed_at,
                300,
                status,
                load,
                json.dumps(running or {"session_ids": [], "run_ids": []}),
                "[]",
                '["seat-alpha"]',
                "ok",
                f"obs-{node_id[:8]}",
                observed_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_deck_http_renders_fresh_stale_queued_idle_and_unknown_slots(tmp_path):
    from brigade import fleet_hub_policy

    with _start_hub(tmp_path, CONFIG) as (hub, db):
        conn = fleet_hub.open_db(db)
        try:
            fleet_hub_policy.save_policy(
                conn, _policy_machines(), expected_version=1, actor="operator", reason="seed machines"
            )
            conn.commit()
        finally:
            conn.close()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        fresh = now.isoformat()
        stale = (now - timedelta(hours=2)).isoformat()
        _insert_telemetry(db, NODE_A, observed_at=fresh, running={"session_ids": [], "run_ids": []})
        _insert_telemetry(db, NODE_B, observed_at=stale, running={"session_ids": [], "run_ids": []})
        _insert_telemetry(db, NODE_C, observed_at=fresh, running={"session_ids": ["session-busy"], "run_ids": []})
        status, _headers, page = _request(hub, "GET", "/deck", headers=_bearer())
        assert status == 200, page
        assert "worker-idle" in page
        assert "idle-capacity" in page
        assert "worker-stale" in page
        assert "stale-telemetry" in page
        assert "worker-queued" in page
        assert ">0<" in page
        assert "worker-unknown" in page
        assert "unknown-telemetry" in page
        assert "ALL CLEAR" not in page or "unknown" in page.lower()


def _fake_activated_marker(db) -> int:
    """Stamp the activation marker without running ``activate_migration``."""
    conn = fleet_hub.open_db(db)
    try:
        fleet_policy_migration.ensure_schema(conn)
        conn.execute(
            "INSERT INTO fleet_policy_migration (singleton, activated, activated_at, activated_by, reason, "
            "preview_digest, policy_revision, roster_revision, preference_digest, candidate_digest) "
            "VALUES (1, 1, '2026-09-07T00:00:00+00:00', 'fixture-operator', 'fake-activated-db', "
            "'sha256:preview', 1, 1, 'sha256:pref', 'sha256:candidate')"
        )
        conn.commit()
        return int(fleet_hub_policy.current_policy(conn)["revision"])
    finally:
        conn.close()


def test_deck_http_projects_fake_activated_authority(tmp_path):
    with _start_hub(tmp_path, CONFIG) as (hub, db):
        status, _headers, staged = _request(hub, "GET", "/deck", headers=_bearer())
        assert status == 200, staged
        assert "authority staged" in staged
        assert "authority active" not in staged
        revision = _fake_activated_marker(db)
        status, _headers, page = _request(hub, "GET", "/deck", headers=_bearer())
        assert status == 200, page
        assert "authority active" in page
        assert "authority staged" not in page
        conn = fleet_hub.open_db(db)
        try:
            assert fleet_policy_migration.is_activated(conn) is True
            assert int(fleet_hub_policy.current_policy(conn)["revision"]) == revision
        finally:
            conn.close()


def test_control_plane_adapter_reads_node_id_and_tolerates_snapshots_without_it():
    snapshot = _control_plane_snapshot()
    legacy = deck.control_plane_from_snapshot(snapshot)
    assert legacy.machines[0].node_id == deck.UNKNOWN
    snapshot["machines"][0]["node_id"] = NODE_A
    assert deck.control_plane_from_snapshot(snapshot).machines[0].node_id == NODE_A
    snapshot["machines"][0]["node_id"] = None
    assert deck.control_plane_from_snapshot(snapshot).machines[0].node_id == deck.UNKNOWN
    snapshot["machines"][0]["node_id"] = "<script>alert(1)</script>"
    html = deck.render_deck(_empty_view(deck.control_plane_from_snapshot(snapshot)), nonce="nonce", now=NOW)
    assert "<script>alert(1)</script>" not in html


def test_failed_outcomes_and_rail_entries_carry_the_receipt_age(conn):
    _insert(conn, NODE_A, "failure.new", 1, "run.dispatch.failed", ts=_ts(60), repo="same-repo")
    (failed,) = _failed(conn)
    assert failed.age_seconds == 60
    stale = deck.LiveRun(
        node_id=NODE_A,
        run_id="ghost",
        repo="stale-only",
        seat="seat",
        harness="claude",
        state="run.started",
        bucket="stale",
        age_seconds=3600,
        elapsed_seconds=3600,
    )
    view = deck.build_view(
        deck.DeckConfig(stations=(deck.StationConfig(NODE_A, "Alpha", 2),)),
        live_runs=[stale],
        claims=[],
        enrolled_labels={NODE_A: "Alpha"},
        last_heard={},
        outcomes=[],
        failed_outcomes=[failed],
        observers=[],
        now=NOW,
    )
    assert [(entry.kind, entry.age_seconds) for entry in view.rail] == [("failed", 60), ("stale", 3600)]


def test_deck_http_joins_stations_to_telemetry_by_node_id(tmp_path):
    with _start_hub(tmp_path, CONFIG) as (hub, db):
        for node_id, label in ((NODE_A, "Alpha"), (NODE_B, "Beta")):
            body = {"action": "add", "node_id": node_id, "label": label}
            assert _request(hub, "POST", "/nodes", headers=_bearer(), body=body)[0] == 200
        conn = fleet_hub.open_db(db)
        try:
            fleet_hub_policy.save_policy(
                conn, _policy_machines(), expected_version=1, actor="operator", reason="seed machines"
            )
            conn.commit()
        finally:
            conn.close()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        _insert_telemetry(db, NODE_A, observed_at=now.isoformat())
        _insert_telemetry(db, NODE_B, observed_at=(now - timedelta(hours=2)).isoformat())
        status, _headers, page = _request(hub, "GET", "/deck", headers=_bearer())
        assert status == 200, page

        def card(name: str) -> str:
            found = re.search(rf'data-card="{name}">.*?class="card-value">([^<]*)<', page, re.S)
            assert found, name
            return found.group(1)

        assert card("working") == "0"
        assert card("idle") == "4"
        assert card("offline") == "1"
        rows = re.findall(r"<tr data-status=.*?</tr>", page, re.S)
        alpha = next(row for row in rows if ">Alpha<" in row)
        beta = next(row for row in rows if ">Beta<" in row)
        assert "idle-capacity" in alpha and "stale-telemetry" not in alpha
        assert "stale-telemetry" in beta and 'data-status="stale"' in beta and 'class="tag tag--warn">stale<' in beta
        alerts_page = _request(hub, "GET", "/deck/alerts", headers=_bearer())[2]
        assert 'class="attention-kind">stale telemetry</span>' in alerts_page


def test_octopool_prefers_canonical_cache_and_uncached_counters():
    from brigade import fleet_deck_activity as activity

    raw = _octopool_capture()
    raw["pool_usage"].update(cache_served_responses=11, uncached_outcomes=30, backend_requests=99)
    view = deck.DeckView(stations=(), rail=(), repos=(), outcomes=(), observers=(), octopool_observation=raw)
    html = activity.render_octopool_observation(view)
    assert "11 cache-served responses" in html and "30 uncached outcomes" in html
    assert "7 cache-served responses" not in html and "99 uncached" not in html
    assert "reads saved" not in html and "requests avoided" not in html


def test_v2_tasks_and_recent_outcomes_render_from_hub_without_managed_capacity(monkeypatch):
    from brigade import fleet_dot, fleet_deck_overview as overview
    from tests.test_fleet_dot import v2_report

    conn = sqlite3.connect(":memory:")
    fleet_hub_sessions.init_schema(conn)
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: NOW.timestamp())
    for raw in (
        v2_report(
            session_id="active-task",
            agent_label="Miller",
            observed_at=_ts(60),
            task="Check <fixture>",
            blocker="Awaiting fixture",
            progress="Testing",
        ),
        v2_report(session_id="stale-task", observed_at=_ts(1000), reported_state="running", task="Older build"),
        v2_report(
            session_id="outcome-task",
            observed_at=_ts(120),
            reported_state="completed",
            task="Review change",
            result="Reviewed fixture",
            evidence_refs=["receipt-a"],
        ),
    ):
        fleet_hub_sessions.handle_session(conn, fleet_dot.report_session(raw)["presence_payload"], caller_node=NODE_A)
    rows = deck.fetch_interactive_sessions(conn, now=NOW)
    view = deck.build_view(
        deck.DeckConfig(stations=()),
        live_runs=[],
        claims=[],
        enrolled_labels={},
        last_heard={},
        outcomes=[],
        failed_outcomes=[],
        observers=[],
        now=NOW,
        interactive_sessions=rows,
    )
    assert len(view.interactive_sessions) == 1
    assert len(view.recent_cloud_reports) == 2
    html = overview.render_overview(view, NOW, worklore_enabled=True)
    visible = _visible_text(html)
    assert "Check <fixture>" in visible and "Check &lt;fixture&gt;" in html
    assert "Miller" in visible and "Reported running" in visible
    assert "Observed 1m ago" in visible
    assert "Testing" in visible and "Awaiting fixture" not in visible and "Awaiting fixture" in html
    assert "<summary>Recent cloud reports (2)</summary>" in html
    history = html.split('<details class="activity-history">', 1)[1]
    assert "Older build" not in visible and "Older build" in history and "stale" in history
    assert "Reported completed" in history and "Reviewed fixture" in history
    assert "provider execution unverified" in visible
    about = html.split("<summary>About reports</summary>", 1)[1].split("</details>", 1)[0]
    assert "explicitly reported sessions" in about and "explicitly reported sessions" not in visible
    assert "receipt-a" not in visible and "receipt-a" in html
    assert "active-task" not in visible and "outcome-task" not in visible
    assert "Running: 0 · 0 managed jobs reported" in visible
    assert overview.summarize(view).busy == 0 and view.cloud_workers == ()
    capacity = overview.render_capacity_page(view, NOW, nonce="n", worklore_enabled=False)
    capacity = capacity.split("<main", 1)[1].split("</main>", 1)[0]
    assert 'data-activity-kind="reported"' not in capacity
    for task_fact in (
        "Check &lt;fixture&gt;",
        "Older build",
        "Reviewed fixture",
        "Recent cloud reports",
        "active-task",
    ):
        assert task_fact not in capacity
    assert "<summary>Advanced resource details</summary>" in capacity


def test_capacity_keeps_resources_visible_and_folds_provider_and_control_evidence():
    from dataclasses import replace

    from brigade import fleet_deck_overview as overview
    from tests.test_fleet_deck_overview import NOW as resource_now, _mixed_view

    worker = deck.CloudWorker(
        "example-provider",
        1,
        3,
        "open",
        (deck.CloudLease("lease-example", "example/repo", "Lease <fixture>", "running", "artifact-example"),),
    )
    view = replace(_mixed_view(), cloud_workers=(worker,), octopool_observation=_octopool_capture())
    html = overview.render_capacity_page(view, resource_now, nonce="n", worklore_enabled=False)
    html = html.split("<main", 1)[1].split("</main>", 1)[0]
    visible = _visible_text(html)
    assert "Machines (3)" in visible and "44%" in visible and "1m" in visible
    assert "2 busy, 8 idle" in html and 'data-status="stale"' in html
    assert "41 requests" in visible and "Pool-wide" in visible
    assert "example-provider" not in visible and "Lease <fixture>" not in visible
    assert "Control plane" not in visible and "No active leases." not in visible
    assert "No active provider leases reported." not in visible
    marker = '<details id="cloud-details"><summary>Advanced resource details</summary>'
    assert html.count(marker) == 1
    advanced = html.split(marker, 1)[1]
    assert "example-provider" in advanced and "Lease &lt;fixture&gt;" in advanced
    assert "lease-example" in advanced and "artifact-example" in advanced and "circuit: open" in advanced
    assert "Control plane" in advanced and "stale-telemetry" in advanced
    assert "Configured slots are not verified usable workers." in advanced
    assert "Lease <fixture>" not in html


@pytest.mark.parametrize(
    "change",
    [
        {"task": "x" * 401},
        {"reported_state": "verified"},
        {"sequence": True},
        {"evidence_refs": ["https://localhost/private"]},
    ],
)
def test_dashboard_refuses_malformed_v2_context_even_in_history(change):
    from brigade import fleet_dot, fleet_deck_overview as overview
    from tests.test_fleet_dot import v2_report

    context = dict(fleet_dot.parse_report(v2_report()).snapshot.cloud_context)
    context.update(change)
    view = deck.build_view(
        deck.DeckConfig(stations=()),
        live_runs=[],
        claims=[],
        enrolled_labels={},
        last_heard={},
        outcomes=[],
        failed_outcomes=[],
        observers=[],
        now=NOW,
        interactive_sessions=[
            {"node_id": NODE_A, "session_id": "bad", "presence_state": "stale", "cloud_context": context}
        ],
    )
    assert view.recent_cloud_reports == ()
    html = overview.render_work_lists(view)
    assert "Check build" not in html and "https://localhost" not in html


def test_recent_report_cap_is_applied_before_merging_active_presence(monkeypatch):
    from brigade import fleet_dot
    from tests.test_fleet_dot import v2_report

    conn = sqlite3.connect(":memory:")
    fleet_hub_sessions.init_schema(conn)
    monkeypatch.setattr(deck, "CLOUD_REPORT_HISTORY_LIMIT", 1)
    for received, raw in (
        (NOW.timestamp() - 1, v2_report(session_id="older-outcome", observed_at=_ts(60), reported_state="completed")),
        (NOW.timestamp(), v2_report(session_id="newer-stale-report", observed_at=_ts(1000))),
    ):
        monkeypatch.setattr(fleet_hub, "_now_epoch", lambda received=received: received)
        fleet_hub_sessions.handle_session(conn, fleet_dot.report_session(raw)["presence_payload"], caller_node=NODE_A)
    rows = deck.fetch_interactive_sessions(conn, now=NOW)
    view = deck.build_view(
        deck.DeckConfig(stations=()),
        live_runs=[],
        claims=[],
        enrolled_labels={},
        last_heard={},
        outcomes=[],
        failed_outcomes=[],
        observers=[],
        now=NOW,
        interactive_sessions=rows,
    )
    assert [row.session_id for row in view.recent_cloud_reports] == ["newer-stale-report"]


def test_terminal_reports_do_not_hide_live_presence_at_sql_cap(conn, monkeypatch):
    from brigade import fleet_dot
    from tests.test_fleet_dot import report, v2_report

    # All three older rows remain unexpired while newer outcomes fill the
    # real 500-row SQL cap. Do not shorten the cap or replace the SQL reader.
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: NOW.timestamp() - 60)
    _seed_session(conn, session_id="local-session")
    conn.execute("UPDATE interactive_sessions SET heartbeat_at = ?, started_at = ?", (_ts(60), _ts(60)))
    for raw in (
        report(session_id="v1-session", observed_at=_ts(60)),
        v2_report(session_id="running-task", observed_at=_ts(60)),
    ):
        fleet_hub_sessions.handle_session(conn, fleet_dot.report_session(raw)["presence_payload"], caller_node=NODE_A)
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: NOW.timestamp())
    for index in range(500):
        raw = v2_report(session_id=f"completed-{index:03}", observed_at=_ts(0), reported_state="completed")
        fleet_hub_sessions.handle_session(conn, fleet_dot.report_session(raw)["presence_payload"], caller_node=NODE_A)
    assert conn.execute("SELECT count(*) FROM interactive_sessions").fetchone()[0] == 503
    # Ordinary session readers retain their existing presence semantics.
    assert len(fleet_hub_sessions.list_sessions(conn, now_epoch=NOW.timestamp())) == 500
    rows = deck.fetch_interactive_sessions(conn, now=NOW)
    live = {row["session_id"] for row in rows if not str(row["session_id"]).startswith("completed-")}
    assert live == {"local-session", "v1-session", "running-task"}
    assert len(rows) == 3 + deck.CLOUD_REPORT_HISTORY_LIMIT


@pytest.mark.parametrize(
    ("state", "facts", "expected"),
    [
        ("blocked", {"progress": "Checking fixture", "blocker": "Need review", "result": "Draft ready"}, "Need review"),
        (
            "completed",
            {"progress": "Checking fixture", "blocker": "Need review", "result": "Draft ready"},
            "Draft ready",
        ),
        (
            "running",
            {"progress": "Checking fixture", "blocker": "Need review", "result": "Draft ready"},
            "Checking fixture",
        ),
        ("failed", {"result": "Build failed"}, "Build failed"),
        ("queued", {}, "No progress reported."),
        ("cancelled", {}, "No progress reported."),
        ("unknown", {}, "No progress reported."),
    ],
)
def test_reported_activity_cards_prioritize_one_fact_and_fold_provenance(state, facts, expected):
    from brigade import fleet_deck_activity as activity

    context = deck.CloudSessionContext(
        version=2,
        provider="dot",
        agent_label="Example agent",
        sequence=4,
        observed_at=NOW.isoformat(),
        source="cloud_threads",
        source_scope="authorized-visible-threads",
        coverage="explicitly-reported-sessions",
        task="Review <fixture>",
        reported_state=state,
        evidence_refs=("receipt-example",),
        **facts,
    )
    session = deck.InteractiveSession(
        NODE_A,
        "dot",
        "session-example",
        "github.com/example/project",
        "fleet",
        "project",
        "",
        None,
        (),
        False,
        cloud_context=context,
        presence_state="stale",
        observation_age_seconds=60,
    )
    view = deck.DeckView(stations=(), rail=(), repos=(), outcomes=(), observers=(), interactive_sessions=(session,))
    html = activity.render_cloud_presence(view)
    visible = _visible_text(html)
    assert 'data-activity-kind="reported"' in html
    assert f"Reported {state}" in visible and "stale" in visible
    assert "Review <fixture>" in visible and "Review &lt;fixture&gt;" in html
    assert "<fixture>" not in html
    assert visible.count("Example agent") == 1
    assert expected in visible
    for fact in facts.values():
        assert fact in html
        assert (fact in visible) == (fact == expected)
    assert "session-example" not in visible and "session-example" in html
    assert "receipt-example" not in visible and "receipt-example" in html
    assert NOW.isoformat() not in visible and NOW.isoformat() in html
    assert "Source: cloud_threads" in html and "Source: cloud_threads" not in visible
    assert visible.count("Self-reported task state; provider execution unverified.") == 1


def test_recent_cloud_outcomes_are_closed_with_count_and_keep_stale_and_cap_details():
    from brigade import fleet_deck_activity as activity

    context = deck.CloudSessionContext(
        version=2,
        provider="dot",
        agent_label="Example agent",
        sequence=1,
        observed_at=NOW.isoformat(),
        source="cloud_threads",
        source_scope="authorized-visible-threads",
        coverage="explicitly-reported-sessions",
        task="Older fixture",
        reported_state="completed",
        result="Reviewed fixture",
    )
    session = deck.InteractiveSession(
        NODE_A,
        "dot",
        "old-session",
        "",
        "fleet",
        "",
        "",
        None,
        (),
        False,
        cloud_context=context,
        presence_state="stale",
        observation_age_seconds=1000,
    )
    view = deck.DeckView(
        stations=(),
        rail=(),
        repos=(),
        outcomes=(),
        observers=(),
        recent_cloud_reports=(session,) * deck.CLOUD_REPORT_HISTORY_LIMIT,
    )
    html = activity.render_cloud_report_history(view)
    assert html.startswith("<details") and " open" not in html.split(">", 1)[0]
    assert f"<summary>Recent cloud reports ({deck.CLOUD_REPORT_HISTORY_LIMIT})</summary>" in html
    assert "Older fixture" not in _visible_text(html)
    assert "Older fixture" in html and "Reviewed fixture" in html and "stale" in html
    assert "Recent report list may be capped." in html
    assert "Outcomes do not complete leases." in html
