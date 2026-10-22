"""A hub database created before ``fleet_events.producer`` existed repairs itself at startup.

The earlier schema-38 head shipped ``fleet_events`` without ``producer`` and never bumped
``user_version`` again, so startup treated those databases as current.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Iterator

import pytest

from brigade import fleet_hub, fleet_hub_events

_PRE_PRODUCER_TABLE = """CREATE TABLE fleet_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    subject TEXT NOT NULL, data TEXT NOT NULL, created_at REAL NOT NULL)"""


@pytest.fixture
def events_on() -> Iterator[None]:
    fleet_hub_events.configure(fleet_hub_events.EventsConfig(frozenset({"hooks.example.test"})))
    yield
    fleet_hub_events.configure(fleet_hub_events.EventsConfig())


def _pre_producer_hub(db: Path, now: float) -> None:
    """A schema-38 hub as the earlier head left it: current user_version, no producer column or index."""
    fleet_hub.init_db(db).close()
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP INDEX fleet_events_producer")
        conn.execute("DROP TABLE fleet_events")
        conn.execute(_PRE_PRODUCER_TABLE)
        sent = {"message_id": "m1", "recipient": "node-b", "sender": "agent:dot", "priority": "normal"}
        rows = [
            ("evt_1", "message.created", "node-b", json.dumps(sent)),
            ("evt_2", "message.created", "node-b", "not json"),
            ("evt_3", "claim.expired", "node-a", json.dumps({"owner_node": "node-a", "target": "t"})),
            ("evt_4", "run.failed", "node-a", json.dumps({"node_id": "node-a", "run_id": "r"})),
        ]
        conn.executemany(
            "INSERT INTO fleet_events(event_id,name,subject,data,created_at) VALUES(?,?,?,?,?)",
            [(*row, now - 60) for row in rows],
        )
        conn.execute(
            "INSERT INTO fleet_event_subscriptions(id,principal_id,name,arguments,url,secret,created_at)"
            " SELECT 'sub_' || name, 'p', name, '{}', 'https://hooks.example.test/x', 's', ? FROM"
            " (SELECT 'message.created' AS name UNION SELECT 'claim.expired' UNION SELECT 'run.failed')",
            (now,),
        )
        conn.commit()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION == 38
        assert "producer" not in {r[1] for r in conn.execute("PRAGMA table_info(fleet_events)")}
    finally:
        conn.close()


def test_pre_producer_hub_is_repaired_and_backfilled_on_startup(tmp_path: Path, events_on, monkeypatch) -> None:
    db = tmp_path / "hub.sqlite"
    now = time.time()
    _pre_producer_hub(db, now)

    conn = fleet_hub.init_db(db)
    try:
        assert "producer" in {r[1] for r in conn.execute("PRAGMA table_info(fleet_events)")}
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='fleet_events_producer'").fetchone()
        stored = dict(conn.execute("SELECT event_id, producer FROM fleet_events").fetchall())
        # A message is produced by its sender; unreadable data and node-owned events fall back to the subject.
        assert stored == {"evt_1": "agent:dot", "evt_2": "node-b", "evt_3": "node-a", "evt_4": "node-a"}

        # The backfilled rows feed the producer limiter, and new events append instead of failing on the column.
        monkeypatch.setattr(fleet_hub_events, "MAX_EVENTS_PER_PRODUCER_PER_HOUR", 2)
        message = {"id": "m2", "recipient": "node-b", "sender": "agent:dot", "priority": "normal", "created_at": "t"}
        fleet_hub_events.record_message_created(conn, message)
        fleet_hub_events.record_message_created(conn, {**message, "id": "m3"})  # over the limit for agent:dot
        fleet_hub_events.record_run_failed(conn, {"node_id": "node-c", "run_id": "r2", "exit_status": 1})
        fleet_hub_events.record_claim_expired(conn, "target", "node-c", "holder", now - 5)
        conn.commit()

        def count(name: str, producer: str) -> int:
            return conn.execute(
                "SELECT COUNT(*) FROM fleet_events WHERE name=? AND producer=?", (name, producer)
            ).fetchone()[0]

        assert count("message.created", "agent:dot") == 2, "one backfilled row plus one new; the third is dropped"
        assert count("run.failed", "node-c") == 1
        assert count("claim.expired", "node-c") == 1

        # A second startup is a no-op and leaves existing producers alone.
        before = conn.execute("SELECT seq, producer FROM fleet_events ORDER BY seq").fetchall()
    finally:
        conn.close()
    again = fleet_hub.init_db(db)
    try:
        assert again.execute("SELECT seq, producer FROM fleet_events ORDER BY seq").fetchall() == before
    finally:
        again.close()
