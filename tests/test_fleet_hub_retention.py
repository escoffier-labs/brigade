"""Bounded retention sweeps for the Hub event journal, sessions and leases."""

from __future__ import annotations

import sqlite3

import pytest

from brigade import fleet_hub, fleet_hub_retention as retention

NOW = 1_800_000_000.0
DAY = 86_400
OLD = retention._iso(NOW - 91 * DAY)
YOUNG = retention._iso(NOW - 89 * DAY)


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "hub.db")
    yield connection
    connection.close()


def _event(conn: sqlite3.Connection, run_id: str, sequence: int, received_at: str, node: str = "n1") -> None:
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, state, ts, received_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (node, run_id, sequence, f"d{sequence}", "run.created", received_at, received_at),
    )
    conn.commit()


def _session(
    conn: sqlite3.Connection,
    session_id: str,
    heartbeat_at: str,
    *,
    state: str = "ended",
) -> None:
    conn.execute(
        "INSERT INTO interactive_sessions (node_id, harness, session_id, repo_identity, identity_scope, "
        "repo_label, checkout_path, dirty_paths_json, dirty_truncated, state, started_at, heartbeat_at, "
        "ttl_seconds, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "n1",
            "claude",
            session_id,
            "example/repo",
            "fleet",
            "example/repo",
            "/tmp/repo",
            "[]",
            0,
            state,
            heartbeat_at,
            heartbeat_at,
            3600,
            NOW + 3600,
        ),
    )
    conn.commit()


def _lease(
    conn: sqlite3.Connection,
    lease_id: str,
    *,
    expires_at: float,
    released_at: str | None,
) -> None:
    conn.execute(
        "INSERT INTO cloud_leases (lease_id, provider, owner_node, holder_token, state, admitted_at, "
        "renewed_at, ttl_seconds, expires_at, released_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            lease_id,
            "example-cloud",
            "n1",
            "holder",
            "released" if released_at else "bound",
            OLD,
            OLD,
            3600,
            expires_at,
            released_at,
        ),
    )
    conn.commit()


def _runs(conn: sqlite3.Connection) -> set[tuple[str, int]]:
    return {(row[0], row[1]) for row in conn.execute("SELECT run_id, sequence FROM events")}


def test_events_delete_old_rows_keep_young_and_latest(conn):
    _event(conn, "dead-run", 1, OLD)
    _event(conn, "dead-run", 2, OLD)
    _event(conn, "young-run", 1, YOUNG)
    _event(conn, "live-old-run", 1, OLD)
    _event(conn, "mixed-run", 1, OLD)
    _event(conn, "mixed-run", 2, YOUNG)

    assert retention.sweep_events(conn, now=NOW) == 2
    assert _runs(conn) == {
        ("dead-run", 2),
        ("young-run", 1),
        ("live-old-run", 1),
        ("mixed-run", 2),
    }


def test_events_batch_limit_and_second_run(conn):
    for run in range(5):
        _event(conn, f"run-{run}", 1, OLD)
        _event(conn, f"run-{run}", 2, OLD)

    assert retention.sweep_events(conn, now=NOW, limit=3) == 3
    assert retention.sweep_events(conn, now=NOW, limit=3) == 2
    assert retention.sweep_events(conn, now=NOW, limit=3) == 0
    assert _runs(conn) == {(f"run-{run}", 2) for run in range(5)}


def test_events_empty_table_is_a_noop(conn):
    assert retention.sweep_events(conn, now=NOW) == 0


def test_sessions_delete_old_heartbeats_and_keep_young(conn):
    _session(conn, "old", OLD)
    _session(conn, "young", YOUNG)

    assert retention.sweep_interactive_sessions(conn, now=NOW) == 1
    assert [row[0] for row in conn.execute("SELECT session_id FROM interactive_sessions")] == ["young"]


def test_sessions_batch_limit_and_second_run(conn):
    for index in range(5):
        _session(conn, f"session-{index}", OLD)

    assert retention.sweep_interactive_sessions(conn, now=NOW, limit=3) == 3
    assert retention.sweep_interactive_sessions(conn, now=NOW, limit=3) == 2
    assert retention.sweep_interactive_sessions(conn, now=NOW, limit=3) == 0
    assert conn.execute("SELECT COUNT(*) FROM interactive_sessions").fetchone()[0] == 0


def test_sessions_empty_table_is_a_noop(conn):
    assert retention.sweep_interactive_sessions(conn, now=NOW) == 0


def test_leases_delete_released_and_expired_but_never_live(conn):
    _lease(conn, "released-old", expires_at=NOW - 91 * DAY, released_at=OLD)
    _lease(conn, "expired-old", expires_at=NOW - 91 * DAY, released_at=None)
    _lease(conn, "released-young", expires_at=NOW + 3600, released_at=YOUNG)
    _lease(conn, "live-old", expires_at=NOW + 3600, released_at=None)

    assert retention.sweep_cloud_leases(conn, now=NOW) == 2
    assert {row[0] for row in conn.execute("SELECT lease_id FROM cloud_leases")} == {
        "released-young",
        "live-old",
    }


def test_leases_batch_limit_and_second_run(conn):
    for index in range(5):
        _lease(conn, f"lease-{index}", expires_at=NOW - 91 * DAY, released_at=OLD)
    _lease(conn, "live-old", expires_at=NOW + 3600, released_at=None)

    assert retention.sweep_cloud_leases(conn, now=NOW, limit=3) == 3
    assert retention.sweep_cloud_leases(conn, now=NOW, limit=3) == 2
    assert retention.sweep_cloud_leases(conn, now=NOW, limit=3) == 0
    assert [row[0] for row in conn.execute("SELECT lease_id FROM cloud_leases")] == ["live-old"]


def test_leases_empty_table_is_a_noop(conn):
    assert retention.sweep_cloud_leases(conn, now=NOW) == 0


def test_sweep_retention_reports_each_table(conn):
    _event(conn, "dead-run", 1, OLD)
    _event(conn, "dead-run", 2, OLD)
    _session(conn, "old", OLD)
    _lease(conn, "released-old", expires_at=NOW - 91 * DAY, released_at=OLD)

    assert retention.sweep_retention(conn, now=NOW) == {
        "events": 1,
        "interactive_sessions": 1,
        "cloud_leases": 1,
    }
