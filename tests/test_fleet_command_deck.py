"""Tests for the fleet command deck module (config, projections, bounded reads)."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from brigade import fleet_command_deck as deck

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
    repo: str = "repo",
) -> None:
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, repo, seat, harness, state, ts, received_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            node_id,
            run_id,
            sequence,
            f"{node_id[:8]}-{run_id}-{sequence:04d}",
            repo,
            "seat",
            "claude",
            state,
            ts if ts is not None else _ts(10),
            _ts(5),
        ),
    )


@pytest.fixture()
def conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(_EVENTS_SCHEMA)
    yield conn
    conn.close()


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


def test_capped_queries_select_latest_then_limit(conn):
    seed_events(conn, terminal_then_older_live=True, extra_live=201, extra_terminal=101)
    assert len(deck.fetch_live_runs(conn, now=NOW, stale_after_seconds=1800)) == 200
    assert len(deck.fetch_outcomes(conn, outcome_window=100)) == 100
    assert all(
        row.state not in deck.TERMINAL_STATES for row in deck.fetch_live_runs(conn, now=NOW, stale_after_seconds=1800)
    )
