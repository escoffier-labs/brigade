"""Tests for the fleet command deck module (config, projections, bounded reads)."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import http.client
import threading
import time
from contextlib import contextmanager

import pytest

from brigade import fleet_command_deck as deck
from brigade import fleet_hub
from brigade import fleet_hub_status
from brigade import fleet_hub_sessions

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
    assert "Interactive sessions" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert view.stations[0].busy == 0


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
    html = deck.render_repos(view, nonce="nonce", now=now)
    assert ">brigade<" in html
    assert session_row.target in html
    assert html.count("! overlap") == 1


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
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data, headers=headers or {})
    response = conn.getresponse()
    text = response.read().decode("utf-8")
    result = (response.status, {k.lower(): v for k, v in response.getheaders()}, text)
    conn.close()
    return result


def _bearer(extra: dict | None = None) -> dict:
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json", **(extra or {})}


def _login_cookie(hub, path: str = "/") -> str:
    status, headers, _text = _request(hub, "GET", f"{path}?token={TOKEN}")
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
        # Root is the deck itself, so href="/" is the valid self-link here; what
        # must not exist is a legacy board served from root.
        assert '<a href="/">deck</a>' in root
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
        assert ".stations { grid-template-columns: 1fr; }" in body


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
        assert body.index(NODE_B[:12]) < body.index("Alpha")
        # Unnamed station falls back to the enrolled label then node id prefix.
        heading = f'title="{NODE_B}"'
        assert heading in body and "BravoBox" in body
        assert "not enrolled" not in body
        # Capacity and busy counts.
        assert "1/1 busy" in body and "1/2 busy" in body
        # Terminal-only timeline, bounded by outcome_window.
        assert "Recent outcomes" in body
        assert body.count("run.completed") <= 2


def test_collision_markers_and_observers_and_expired_claims(tmp_path):
    with _start_hub(tmp_path, CONFIG) as (hub, _db):
        for node_id, label in ((NODE_A, "Rocinante"), (NODE_B, "Shadowfax")):
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
        _seed_claim(hub, "shared", node=NODE_A)
        _seed_claim(hub, "gone", node=NODE_A, ttl=1)
        _seed_run(hub, NODE_C, "obs", "run.created", repo="observer-repo")
        time.sleep(1.5)
        _status, _headers, deck_body = _request(hub, "GET", "/deck", headers=_bearer())
        assert deck_body.count("! collision") >= 1
        assert "collision" in deck_body.split('id="rail"')[1].split("</section>")[0]
        rail = deck_body.split('id="rail"')[1].split("</section>")[0]
        assert "shared" in rail and "held by Rocinante" in rail
        assert f"held by {NODE_A}" not in rail
        _status, _headers, repos_body = _request(hub, "GET", "/deck/repos", headers=_bearer())
        first_row = repos_body.split("<tbody>")[1].split("<tr>")[1]
        assert "! collision" in first_row and "shared" in first_row
        # Expired claims are absent.
        assert "gone" not in repos_body
        # Observers are excluded from station totals.
        assert "observer-repo" not in deck_body.split('id="rail"')[1]
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
        assert "Cloud workers" in body
        assert "<h3>cursor</h3>" in body and ">1/3<" in body
        assert "closed" in body
        assert "<h3>codex</h3>" in body and ">0/2<" in body and "No active leases." in body
        assert "1/4 busy" in body
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
        assert "<h3>grok-bot</h3>" in body and ">1/64<" in body
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
            assert '<meta name="theme-color" content="#111617"' in body
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
