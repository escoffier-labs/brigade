"""t3-dispatch SQLite ledger."""

from __future__ import annotations

import os
import stat
from datetime import datetime, timedelta, timezone

from brigade.grokbot_t3.ledger import Ledger

T0 = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def _record(ledger, request_id, *, caller="Builder", state="dispatching", at=T0):
    ledger.record_submit(
        request_id=request_id,
        caller=caller,
        repository="brigade",
        host="host-a",
        seat="dsflash",
        title="[grokbot:Builder] t",
        prompt_sha256="a" * 64,
        state=state,
        now=at,
    )


def test_file_is_owner_only_and_round_trips(tmp_path):
    path = tmp_path / "state" / "ledger.sqlite3"
    ledger = Ledger(path)
    _record(ledger, "r1")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    row = ledger.get("r1")
    assert row["caller"] == "Builder" and row["last_state"] == "dispatching" and row["notified_state"] is None
    assert ledger.get("missing") is None


def test_open_count_and_daily_count(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3")
    _record(ledger, "r1")
    _record(ledger, "r2", state="succeeded")
    _record(ledger, "r3", at=T0 - timedelta(days=1))
    assert ledger.open_count() == 2
    assert ledger.submits_since(T0.replace(hour=0)) == 2


def test_state_change_clears_notification(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3")
    _record(ledger, "r1", state="needs_attention")
    ledger.mark_notified("r1", "needs_attention", T0)
    assert ledger.get("r1")["notified_state"] == "needs_attention"
    ledger.update_state("r1", "needs_attention", T0)
    assert ledger.get("r1")["notified_state"] == "needs_attention"
    ledger.update_state("r1", "running", T0)
    assert ledger.get("r1")["notified_state"] is None


def test_pending_notifications(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3")
    _record(ledger, "running", state="running")
    _record(ledger, "done-unnotified", state="succeeded")
    _record(ledger, "done-notified", state="failed")
    ledger.mark_notified("done-notified", "failed", T0)
    ids = [row["request_id"] for row in ledger.pending_notifications()]
    assert ids == ["running", "done-unnotified"]


def test_recent_projection(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3")
    _record(ledger, "r1", caller="Builder", at=T0)
    _record(ledger, "r2", caller="Other", at=T0 + timedelta(minutes=1))
    rows = ledger.recent(None, 10)
    assert [row["request_id"] for row in rows] == ["r2", "r1"]
    assert set(rows[0]) == {
        "request_id",
        "caller",
        "repository",
        "host",
        "seat",
        "title",
        "state",
        "created_at",
        "updated_at",
    }
    assert [row["request_id"] for row in ledger.recent("Builder", 10)] == ["r1"]


def test_existing_requests_seed_attempt_count_once(tmp_path):
    import sqlite3

    path = tmp_path / "ledger.sqlite3"
    ledger = Ledger(path)
    _record(ledger, "old")
    ledger.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE IF EXISTS attempts")
    ledger = Ledger(path)
    assert ledger.attempts_since(T0.replace(hour=0)) == 1
    ledger.record_attempt(T0)
    ledger.close()
    ledger = Ledger(path)
    assert ledger.attempts_since(T0.replace(hour=0)) == 2
    ledger.close()


def test_existing_parent_mode_is_preserved(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o750)
    ledger = Ledger(parent / "ledger.sqlite3")
    assert stat.S_IMODE(parent.stat().st_mode) == 0o750
    ledger.close()


def test_existing_writable_parent_is_rejected_without_chmod(tmp_path):
    import pytest
    from brigade.grokbot_t3.contracts import T3DispatchError

    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o770)
    with pytest.raises(T3DispatchError, match="Dispatch is unavailable"):
        Ledger(parent / "ledger.sqlite3")
    assert stat.S_IMODE(parent.stat().st_mode) == 0o770
    assert not (parent / "ledger.sqlite3").exists()


def test_existing_parent_must_be_owned_by_current_user(tmp_path, monkeypatch):
    import pytest
    from brigade.grokbot_t3.contracts import T3DispatchError

    monkeypatch.setattr(os, "getuid", lambda: tmp_path.stat().st_uid + 1)
    with pytest.raises(T3DispatchError, match="Dispatch is unavailable"):
        Ledger(tmp_path / "ledger.sqlite3")


def test_update_state_resets_status_failures_even_without_a_transition(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    _record(ledger, "r1", state="running")
    ledger.record_status_poll("r1", failed=True)
    ledger.update_state("r1", "running", T0)
    assert ledger.get("r1")["status_failures"] == 0


def test_stale_notification_cannot_mark_a_newer_state(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    _record(ledger, "r1", state="needs_attention")
    ledger.update_state("r1", "running", T0)
    ledger.mark_notified("r1", "needs_attention", T0)
    assert ledger.get("r1")["notified_state"] is None
    assert ledger.get("r1")["notified_at"] is None
    ledger.update_state("r1", "needs_attention", T0)
    assert [row["request_id"] for row in ledger.pending_notifications()] == ["r1"]


def test_existing_delivery_identifiers_are_hashed_once_on_open(tmp_path):
    import hashlib
    import json
    import sqlite3

    path = tmp_path / "ledger.sqlite3"
    ledger = Ledger(path)
    _record(ledger, "r1", state="succeeded")
    ledger.close()
    url = "https://example.com/wake/opaque-token"
    other_url = "https://example.com/other"
    already_hashed = "a" * 64
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE requests SET delivered_targets = ? WHERE request_id = ?",
            (json.dumps([json.dumps([url, "/srv/keys/wake"]), other_url, already_hashed]), "r1"),
        )
    expected = {hashlib.sha256(value.encode()).hexdigest() for value in (url, other_url)} | {already_hashed}
    for _ in range(2):
        ledger = Ledger(path)
        stored = ledger.get("r1")["delivered_targets"]
        assert set(json.loads(stored)) == expected
        assert "https://" not in stored and "/srv/keys/wake" not in stored
        ledger.close()


def test_legacy_requests_default_to_default_interaction_mode(tmp_path):
    import sqlite3

    path = tmp_path / "ledger.sqlite3"
    ledger = Ledger(path)
    _record(ledger, "r1")
    ledger.close()
    with sqlite3.connect(path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(requests)")]
        if "interaction_mode" in columns:
            connection.execute("ALTER TABLE requests DROP COLUMN interaction_mode")
    ledger = Ledger(path)
    assert ledger.get("r1")["interaction_mode"] == "default"
    ledger.close()
