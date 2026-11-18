"""Idle policy sessions are swept on the next policy write, and only those.

Real Hub tables from ``fleet_hub.init_db`` and the real ``fleet_hub_policy``
writers. Ages are set by rewriting stored timestamps, never by sleeping.
The audit trail (``fleet_policy_acknowledged_history`` and
``fleet_policy_revisions``) must survive every sweep.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from brigade import fleet_hub, fleet_hub_policy, fleet_policy
from brigade.fleet_hub import FleetHubError

NODE_A = "11111111-1111-4111-8111-111111111111"
CONSUMER = "t3-fleet"
REPO = "repo/public"


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        fleet_hub_policy.save_policy(connection, _document(), expected_version=1, actor="tester", reason="install")
        yield connection
    finally:
        connection.close()


def _document() -> dict[str, Any]:
    return {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {"roles": {"impl": "seat-alpha"}, "data": {"allow_training": False}},
        "machines": {"worker-linux-1": {"os": "linux", "node_id": NODE_A, "concurrency": 2}},
        "seats": {"seat-alpha": {"provider": "provider-a", "model": "model-a-1"}},
        "consumers": {CONSUMER: {"reload": "refreshable"}},
        "repositories": {REPO: {"privacy": "public"}},
    }


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _context_hash(name: str) -> str:
    return "sha256:" + hashlib.sha256(name.encode("utf-8")).hexdigest()


def _pending(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    current = fleet_hub_policy.current_policy(conn)
    return fleet_hub_policy.record_pending_policy(
        conn,
        session_id=name,
        consumer=CONSUMER,
        node_id=NODE_A,
        repo_identity=REPO,
        origin=CONSUMER,
        revision=current["revision"],
        digest=current["digest"],
        overrides={},
        override_reason=None,
        effective={"roles": {"impl": "seat-alpha"}},
        sources={"defaults": "base"},
        selected={},
        context_hash=_context_hash(name),
    )


def _acknowledged_session(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    """Prepare then acknowledge: one row in each of the three session tables plus history."""
    pending = _pending(conn, name)
    fleet_hub_policy.record_acknowledged_policy(
        conn, pending, node_id=NODE_A, expected_context_hash=_context_hash(name)
    )
    return pending


def _receipt_session(conn: sqlite3.Connection, name: str) -> None:
    """A legacy-style receipt row with no pending or acknowledged copy."""
    fleet_hub_policy.record_session_policy(
        conn,
        session_id=name,
        consumer=CONSUMER,
        node_id=NODE_A,
        repo_identity=REPO,
        revision=None,
        digest=None,
        source="test",
    )


def _age(conn: sqlite3.Connection, pending_key: str, name: str, days: float) -> None:
    """Make every timestamp of one acknowledged session ``days`` old."""
    stamp = _ago(days)
    conn.execute(
        "UPDATE fleet_policy_sessions SET loaded_at=?, updated_at=?, refresh_requested_at=NULL, "
        "refresh_updated_at=NULL WHERE session_id IN (?, ?)",
        (stamp, stamp, pending_key, name),
    )
    conn.execute(
        "UPDATE fleet_policy_pending SET created_at=?, updated_at=? WHERE pending_key=?", (stamp, stamp, pending_key)
    )
    conn.execute(
        "UPDATE fleet_policy_acknowledged SET loaded_at=?, acknowledged_at=? WHERE receipt_key=?",
        (stamp, stamp, pending_key),
    )
    conn.commit()


def _age_receipt(conn: sqlite3.Connection, name: str, days: float) -> None:
    stamp = _ago(days)
    conn.execute("UPDATE fleet_policy_sessions SET loaded_at=?, updated_at=? WHERE session_id=?", (stamp, stamp, name))
    conn.commit()


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = (
        "fleet_policy_sessions",
        "fleet_policy_pending",
        "fleet_policy_acknowledged",
        "fleet_policy_acknowledged_history",
        "fleet_policy_revisions",
    )
    return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}


def _save(conn: sqlite3.Connection) -> None:
    version = fleet_hub_policy.current_policy(conn)["revision"]
    document = _document()
    document["machines"]["worker-linux-1"]["concurrency"] = 2 + version % 2
    fleet_hub_policy.save_policy(conn, document, expected_version=version, actor="tester", reason="touch")


def test_idle_session_is_removed_from_all_three_tables_on_the_next_save(conn):
    pending = _acknowledged_session(conn, "idle")
    keep = _acknowledged_session(conn, "active")
    _age(conn, pending["pending_key"], "idle", 30)
    before = _counts(conn)
    assert (before["fleet_policy_sessions"], before["fleet_policy_pending"], before["fleet_policy_acknowledged"]) == (
        2,
        2,
        2,
    )

    _save(conn)

    after = _counts(conn)
    assert (after["fleet_policy_sessions"], after["fleet_policy_pending"], after["fleet_policy_acknowledged"]) == (
        1,
        1,
        1,
    )
    for table, column in (
        ("fleet_policy_sessions", "session_id"),
        ("fleet_policy_pending", "pending_key"),
        ("fleet_policy_acknowledged", "receipt_key"),
    ):
        survivors = [row[0] for row in conn.execute(f"SELECT {column} FROM {table}")]
        assert survivors == [keep["pending_key"]]


def test_idle_session_is_removed_on_the_next_session_record(conn):
    pending = _acknowledged_session(conn, "idle")
    _receipt_session(conn, "old-receipt")
    _age(conn, pending["pending_key"], "idle", 30)
    _age_receipt(conn, "old-receipt", 30)

    _receipt_session(conn, "fresh-receipt")

    assert [row[0] for row in conn.execute("SELECT session_id FROM fleet_policy_sessions")] == ["fresh-receipt"]
    assert _counts(conn)["fleet_policy_pending"] == 0
    assert _counts(conn)["fleet_policy_acknowledged"] == 0


def test_the_cutoff_is_fourteen_days(conn):
    assert fleet_hub_policy.SESSION_RETENTION == timedelta(days=14)
    pending = _acknowledged_session(conn, "edge")
    _age(conn, pending["pending_key"], "edge", 13)
    _save(conn)
    assert _counts(conn)["fleet_policy_sessions"] == 1
    _age(conn, pending["pending_key"], "edge", 15)
    _save(conn)
    assert _counts(conn)["fleet_policy_sessions"] == 0


@pytest.mark.parametrize(
    "recent_update",
    [
        "UPDATE fleet_policy_sessions SET refresh_requested_at='{now}'",
        "UPDATE fleet_policy_sessions SET refresh_updated_at='{now}'",
        "UPDATE fleet_policy_sessions SET updated_at='{now}'",
        "UPDATE fleet_policy_sessions SET loaded_at='{now}'",
        "UPDATE fleet_policy_pending SET updated_at='{now}'",
        "UPDATE fleet_policy_pending SET created_at='{now}'",
        "UPDATE fleet_policy_acknowledged SET acknowledged_at='{now}'",
        "UPDATE fleet_policy_acknowledged SET loaded_at='{now}'",
    ],
)
def test_one_recent_timestamp_anywhere_keeps_the_whole_session(conn, recent_update):
    pending = _acknowledged_session(conn, "mixed")
    _age(conn, pending["pending_key"], "mixed", 30)
    conn.execute(recent_update.format(now=_ago(1)))
    conn.commit()

    _save(conn)

    counts = _counts(conn)
    assert (counts["fleet_policy_sessions"], counts["fleet_policy_pending"], counts["fleet_policy_acknowledged"]) == (
        1,
        1,
        1,
    )


def test_audit_trail_survives_the_sweep(conn):
    pending = _acknowledged_session(conn, "idle")
    _age(conn, pending["pending_key"], "idle", 30)
    history_before = conn.execute("SELECT * FROM fleet_policy_acknowledged_history ORDER BY history_id").fetchall()
    revisions_before = conn.execute("SELECT * FROM fleet_policy_revisions ORDER BY revision").fetchall()
    assert history_before

    _save(conn)

    assert (
        conn.execute("SELECT * FROM fleet_policy_acknowledged_history ORDER BY history_id").fetchall() == history_before
    )
    revisions_after = conn.execute("SELECT * FROM fleet_policy_revisions ORDER BY revision").fetchall()
    assert revisions_after[: len(revisions_before)] == revisions_before
    assert len(revisions_after) == len(revisions_before) + 1
    assert _counts(conn)["fleet_policy_sessions"] == 0


def test_each_write_sweeps_at_most_the_limit_oldest_first(conn, monkeypatch):
    for index in range(5):
        _receipt_session(conn, f"receipt-{index}")
    for index in range(5):
        _age_receipt(conn, f"receipt-{index}", 40 - index)  # receipt-0 is the oldest
    monkeypatch.setattr(fleet_hub_policy, "SESSION_SWEEP_LIMIT", 2)

    _save(conn)
    assert sorted(row[0] for row in conn.execute("SELECT session_id FROM fleet_policy_sessions")) == [
        "receipt-2",
        "receipt-3",
        "receipt-4",
    ]
    _save(conn)
    assert sorted(row[0] for row in conn.execute("SELECT session_id FROM fleet_policy_sessions")) == ["receipt-4"]


def test_sweep_limit_default_is_bounded(conn):
    assert fleet_hub_policy.SESSION_SWEEP_LIMIT == 500
    for index in range(3):
        _receipt_session(conn, f"receipt-{index}")
    for index in range(3):
        _age_receipt(conn, f"receipt-{index}", 30)
    assert fleet_hub_policy.sweep_idle_sessions(conn, limit=0) == 0
    assert _counts(conn)["fleet_policy_sessions"] == 3


def test_unparseable_or_naive_timestamps_keep_the_session(conn):
    bad = _acknowledged_session(conn, "bad-stamp")
    naive = _acknowledged_session(conn, "naive-stamp")
    _age(conn, bad["pending_key"], "bad-stamp", 30)
    _age(conn, naive["pending_key"], "naive-stamp", 30)
    conn.execute("UPDATE fleet_policy_pending SET updated_at='not-a-time' WHERE pending_key=?", (bad["pending_key"],))
    conn.execute(
        "UPDATE fleet_policy_acknowledged SET loaded_at='2020-01-01T00:00:00' WHERE receipt_key=?",
        (naive["pending_key"],),
    )
    conn.commit()

    _save(conn)

    kept = sorted(row[0] for row in conn.execute("SELECT session_id FROM fleet_policy_sessions"))
    assert kept == sorted([bad["pending_key"], naive["pending_key"]])
    assert _counts(conn)["fleet_policy_pending"] == 2
    assert _counts(conn)["fleet_policy_acknowledged"] == 2


def test_sweep_uses_the_injected_clock(conn):
    pending = _acknowledged_session(conn, "fresh-now")
    later = datetime.now(timezone.utc) + timedelta(days=15)
    assert fleet_hub_policy.sweep_idle_sessions(conn, now=datetime.now(timezone.utc)) == 0
    assert fleet_hub_policy.sweep_idle_sessions(conn, now=later) == 1
    assert pending["pending_key"] not in [
        row[0] for row in conn.execute("SELECT session_id FROM fleet_policy_sessions")
    ]


def _refusal(call) -> tuple[type[BaseException], str]:
    with pytest.raises(FleetHubError) as caught:
        call()
    return type(caught.value), str(caught.value)


def test_a_swept_session_is_refused_like_an_unknown_one(conn):
    pending = _acknowledged_session(conn, "swept")
    prepared = _pending(conn, "swept-prepared")
    _age(conn, pending["pending_key"], "swept", 30)
    _age(conn, prepared["pending_key"], "swept-prepared", 30)
    stale_pending = dict(prepared)
    _save(conn)  # sweeps both and moves the revision
    assert _counts(conn)["fleet_policy_sessions"] == 0

    identity = {"consumer": CONSUMER, "repo_identity": REPO, "node_id": NODE_A}
    for name, key in (("swept", pending["pending_key"]), ("swept-prepared", prepared["pending_key"])):
        assert fleet_hub_policy.find_loaded_session(conn, session_id=name, **identity) is None
        assert fleet_hub_policy.find_loaded_session(conn, session_id=key, **identity) is None
        assert fleet_hub_policy.find_pending_policy(conn, session_id=name, **identity) is None
        assert fleet_hub_policy.find_acknowledged_policy(conn, session_id=name, **identity) is None
    assert fleet_hub_policy.list_session_states(conn) == []

    # The refusals match what a session the Hub never saw gets.
    def gap(call_for) -> list[tuple[type[BaseException], str]]:
        return [_refusal(lambda: call_for("swept")), _refusal(lambda: call_for("never-seen"))]

    for call_for in (
        lambda name: fleet_hub_policy.get_acknowledged_policy(conn, session_id=name, **identity),
        lambda name: fleet_hub_policy.get_pending_policy(conn, session_id=name, **identity),
        lambda name: fleet_hub_policy.request_session_refresh(conn, name, actor="tester"),
        lambda name: fleet_hub_policy.acknowledge_session_refresh(conn, name, node_id=NODE_A, state="failed"),
    ):
        swept_error, unknown_error = gap(call_for)
        assert swept_error[0] is unknown_error[0]
        assert swept_error[1].replace("swept", "X") == unknown_error[1].replace("never-seen", "X")

    # A consumer that still holds the old prepare cannot acknowledge it: it must prepare again.
    swept_ack = _refusal(
        lambda: fleet_hub_policy.record_acknowledged_policy(
            conn, stale_pending, node_id=NODE_A, expected_context_hash=_context_hash("swept-prepared")
        )
    )
    never_seen = dict(stale_pending, session_id="never-seen", pending_key=None)
    unknown_ack = _refusal(
        lambda: fleet_hub_policy.record_acknowledged_policy(
            conn, never_seen, node_id=NODE_A, expected_context_hash=_context_hash("never-seen")
        )
    )
    assert swept_ack[0] is unknown_ack[0]
    assert "has no pending receipt" in swept_ack[1] and "has no pending receipt" in unknown_ack[1]
