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
