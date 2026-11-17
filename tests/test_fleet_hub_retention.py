"""Bounded retention sweeps for the Hub event journal, sessions and leases."""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from brigade import fleet_hub, fleet_hub_retention as retention

NOW = 1_800_000_000.0
DAY = 86_400
OLD = retention._iso(NOW - 91 * DAY)
YOUNG = retention._iso(NOW - 89 * DAY)
CUTOFF = retention._iso(NOW - 90 * DAY)


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "hub.db")
    yield connection
    connection.close()


def _event(
    conn: sqlite3.Connection,
    run_id: str,
    sequence: int,
    received_at: str,
    node: str = "n1",
    *,
    seat: str | None = None,
    harness: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, state, ts, received_at, seat, harness) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (node, run_id, sequence, f"d{sequence}", "run.created", received_at, received_at, seat, harness),
    )
    conn.commit()


def _session(
    conn: sqlite3.Connection,
    session_id: str,
    heartbeat_at: str,
    *,
    state: str = "ended",
) -> None:
    ttl_seconds = 3600
    # Age from the heartbeat so an old row does not claim a future expiry.
    expires_at = datetime.fromisoformat(heartbeat_at).timestamp() + ttl_seconds
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
            ttl_seconds,
            expires_at,
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
    _event(conn, "dead-run", 3, OLD)
    _event(conn, "young-run", 1, YOUNG)
    _event(conn, "live-old-run", 1, OLD)
    _event(conn, "mixed-run", 1, OLD)
    _event(conn, "mixed-run", 2, YOUNG)

    assert retention.sweep_events(conn, now=NOW) == 1
    assert _runs(conn) == {
        ("dead-run", 1),
        ("dead-run", 3),
        ("young-run", 1),
        ("live-old-run", 1),
        ("mixed-run", 1),
        ("mixed-run", 2),
    }


def test_events_keep_first_seat_and_harness_rows(conn):
    _event(conn, "worker-run", 1, OLD, seat="claude-a", harness="claude")
    _event(conn, "worker-run", 2, OLD)
    _event(conn, "worker-run", 3, OLD, seat="", harness="")
    _event(conn, "worker-run", 4, OLD)

    assert retention.sweep_events(conn, now=NOW) == 2
    assert _runs(conn) == {("worker-run", 1), ("worker-run", 4)}
    assert conn.execute("SELECT seat, harness FROM events WHERE sequence = 1").fetchone() == ("claude-a", "claude")


def test_events_first_row_carrying_the_seat_is_not_double_counted(conn):
    _event(conn, "worker-run", 1, OLD, seat="claude-a", harness="claude")
    _event(conn, "worker-run", 2, OLD)
    _event(conn, "worker-run", 3, OLD)

    assert retention.sweep_events(conn, now=NOW) == 1
    assert _runs(conn) == {("worker-run", 1), ("worker-run", 3)}


def test_events_newest_seat_row_survives_when_a_later_event_drops_it(conn):
    _event(conn, "worker-run", 1, OLD)
    _event(conn, "worker-run", 2, OLD, seat="claude-a", harness="claude")
    _event(conn, "worker-run", 3, OLD)
    _event(conn, "worker-run", 4, OLD)

    assert retention.sweep_events(conn, now=NOW) == 1
    assert _runs(conn) == {("worker-run", 1), ("worker-run", 2), ("worker-run", 4)}


def test_events_keep_the_real_seat_when_a_later_row_holds_a_padded_dash(conn):
    # /status treats " - " as a seat and the Deck does not, so both newest rows stay.
    _event(conn, "padded-run", 1, OLD)
    _event(conn, "padded-run", 2, OLD, seat="claude-a")
    _event(conn, "padded-run", 3, OLD, seat=" - ")
    _event(conn, "padded-run", 4, OLD)
    _event(conn, "padded-run", 5, OLD)

    assert retention.sweep_events(conn, now=NOW) == 1
    assert _runs(conn) == {("padded-run", 1), ("padded-run", 2), ("padded-run", 3), ("padded-run", 5)}


def test_events_sweep_keeps_rows_the_readers_project(conn):
    from datetime import timezone

    from brigade import fleet_command_deck, fleet_hub_status

    _event(conn, "worker-run", 1, OLD)
    _event(conn, "worker-run", 2, OLD, seat="claude-a", harness="claude")
    _event(conn, "worker-run", 3, OLD)
    _event(conn, "worker-run", 4, OLD)
    moment = datetime.fromtimestamp(NOW, tz=timezone.utc)

    def projected():
        entry = fleet_hub_status.latest_status(conn, include_all=True, now=moment)[0]
        started = conn.execute(fleet_hub_status._latest_sql(resolve_started_at=True)).fetchone()[-1]
        harness = fleet_command_deck._last_known_field(
            conn, node_id="n1", run_id="worker-run", column="harness", grokbot=False
        )
        return entry["seat"], started, harness

    assert projected() == ("claude-a", OLD, "claude")
    assert retention.sweep_events(conn, now=NOW) == 1
    assert projected() == ("claude-a", OLD, "claude")


def test_events_delete_step_rechecks_liveness(conn):
    _event(conn, "race-run", 1, OLD)
    _event(conn, "race-run", 2, OLD)
    key = conn.execute("SELECT rowid FROM events WHERE run_id = 'race-run' AND sequence = 1").fetchone()[0]

    # The candidate read saw a deletable row; it became live before the delete.
    conn.execute("UPDATE events SET received_at = ? WHERE rowid = ?", (YOUNG, key))
    conn.commit()

    assert retention._delete_events(conn, [key], CUTOFF) == 0
    assert _runs(conn) == {("race-run", 1), ("race-run", 2)}


def test_events_drains_every_batch_in_one_call(conn):
    for run in range(5):
        _event(conn, f"run-{run}", 1, OLD)
        _event(conn, f"run-{run}", 2, OLD)
        _event(conn, f"run-{run}", 3, OLD)

    assert retention.sweep_events(conn, now=NOW, limit=2) == 5
    assert retention.sweep_events(conn, now=NOW, limit=2) == 0
    assert _runs(conn) == {(f"run-{run}", sequence) for run in range(5) for sequence in (1, 3)}


def test_events_sweep_stops_at_batch_cap(conn):
    middle = retention.RETENTION_SWEEP_MAX_BATCHES + 1
    _event(conn, "big-run", 0, OLD)
    for sequence in range(1, middle + 1):
        _event(conn, "big-run", sequence, OLD)
    _event(conn, "big-run", middle + 1, OLD)

    assert retention.sweep_events(conn, now=NOW, limit=1) == retention.RETENTION_SWEEP_MAX_BATCHES
    assert retention.sweep_events(conn, now=NOW, limit=1) == 1
    assert retention.sweep_events(conn, now=NOW, limit=1) == 0
    assert _runs(conn) == {("big-run", 0), ("big-run", middle + 1)}


def test_events_empty_table_is_a_noop(conn):
    assert retention.sweep_events(conn, now=NOW) == 0


def test_sessions_delete_old_heartbeats_and_keep_young(conn):
    _session(conn, "old", OLD)
    _session(conn, "young", YOUNG)
    _session(conn, "recent", retention._iso(NOW - 60))

    assert retention.sweep_interactive_sessions(conn, now=NOW) == 1
    assert {row[0] for row in conn.execute("SELECT session_id FROM interactive_sessions")} == {"young", "recent"}


def test_sessions_drains_every_batch_in_one_call(conn):
    for index in range(5):
        _session(conn, f"session-{index}", OLD)

    assert retention.sweep_interactive_sessions(conn, now=NOW, limit=2) == 5
    assert retention.sweep_interactive_sessions(conn, now=NOW, limit=2) == 0
    assert conn.execute("SELECT COUNT(*) FROM interactive_sessions").fetchone()[0] == 0


def test_sessions_empty_table_is_a_noop(conn):
    assert retention.sweep_interactive_sessions(conn, now=NOW) == 0


def test_leases_delete_released_and_expired_but_never_live(conn):
    _lease(conn, "released-old", expires_at=NOW - 91 * DAY, released_at=OLD)
    _lease(conn, "expired-old", expires_at=NOW - 91 * DAY, released_at=None)
    _lease(conn, "released-young", expires_at=NOW + 3600, released_at=YOUNG)
    _lease(conn, "live-old", expires_at=NOW + 3600, released_at=None)
    _lease(conn, "expired-recently", expires_at=NOW - DAY, released_at=None)

    assert retention.sweep_cloud_leases(conn, now=NOW) == 2
    assert {row[0] for row in conn.execute("SELECT lease_id FROM cloud_leases")} == {
        "released-young",
        "live-old",
        "expired-recently",
    }


def test_leases_drains_every_batch_in_one_call(conn):
    for index in range(5):
        _lease(conn, f"lease-{index}", expires_at=NOW - 91 * DAY, released_at=OLD)
    _lease(conn, "live-old", expires_at=NOW + 3600, released_at=None)

    assert retention.sweep_cloud_leases(conn, now=NOW, limit=2) == 5
    assert retention.sweep_cloud_leases(conn, now=NOW, limit=2) == 0
    assert [row[0] for row in conn.execute("SELECT lease_id FROM cloud_leases")] == ["live-old"]


def test_leases_empty_table_is_a_noop(conn):
    assert retention.sweep_cloud_leases(conn, now=NOW) == 0


def test_sweep_retention_reports_each_table(conn):
    _event(conn, "dead-run", 1, OLD)
    _event(conn, "dead-run", 2, OLD)
    _event(conn, "dead-run", 3, OLD)
    _session(conn, "old", OLD)
    _lease(conn, "released-old", expires_at=NOW - 91 * DAY, released_at=OLD)

    assert retention.sweep_retention(conn, now=NOW) == {
        "events": 1,
        "interactive_sessions": 1,
        "cloud_leases": 1,
    }


def test_sweep_retention_isolates_a_failing_sweep(conn, monkeypatch):
    _session(conn, "old", OLD)
    _lease(conn, "released-old", expires_at=NOW - 91 * DAY, released_at=OLD)

    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(retention, "sweep_events", fail)

    assert retention.sweep_retention(conn, now=NOW) == {
        "events": 0,
        "interactive_sessions": 1,
        "cloud_leases": 1,
    }


def test_maintain_purges_sealed_content_when_a_sweep_fails(tmp_path, monkeypatch):
    from brigade import fleet_admin_stats, fleet_hub_sealed

    db_path = tmp_path / "hub.db"
    fleet_hub.init_db(db_path).close()
    calls: list[str] = []

    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(retention, "sweep_events", fail)
    monkeypatch.setattr(retention, "sweep_interactive_sessions", lambda *_a, **_k: calls.append("sessions") or 0)
    monkeypatch.setattr(retention, "sweep_cloud_leases", lambda *_a, **_k: calls.append("leases") or 0)
    monkeypatch.setattr(fleet_hub_sealed, "purge_content", lambda _conn: calls.append("purge") or 0)

    fleet_admin_stats.maintain(db_path)

    assert "purge" in calls
    assert calls[-2:] == ["sessions", "leases"]
    assert calls.index("purge") < calls.index("sessions")
