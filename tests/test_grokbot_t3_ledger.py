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
    assert ids == ["done-unnotified", "running"]


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


def test_pending_notifications_puts_terminal_rows_first_then_least_recently_polled(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3")
    _record(ledger, "old-running", state="running")
    _record(ledger, "new-running", state="running")
    _record(ledger, "done", state="succeeded")
    ledger.mark_polled("old-running")
    ids = [row["request_id"] for row in ledger.pending_notifications()]
    assert ids == ["done", "new-running", "old-running"]


def test_legacy_state_revision_migrates_and_survives_reopen(tmp_path):
    import sqlite3

    path = tmp_path / "ledger.sqlite3"
    ledger = Ledger(path)
    _record(ledger, "r1", state="running")
    ledger.close()
    with sqlite3.connect(path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(requests)")]
        if "state_revision" in columns:
            connection.execute("ALTER TABLE requests DROP COLUMN state_revision")
    ledger = Ledger(path)
    assert ledger.get("r1")["state_revision"] == 0
    assert ledger.update_state("r1", "running", T0, expected_revision=0)
    ledger.close()
    ledger = Ledger(path)
    assert ledger.get("r1")["state_revision"] == 1
    assert not ledger.update_state("r1", "succeeded", T0, expected_revision=0)
    assert ledger.get("r1")["last_state"] == "running"
    assert ledger.get("r1")["state_revision"] == 1
    assert ledger.update_state("r1", "succeeded", T0, expected_revision=1)
    assert ledger.get("r1")["state_revision"] == 2
    ledger.close()


def test_record_submit_converts_the_reservation_atomically(tmp_path):
    import sqlite3

    import pytest

    ledger = Ledger(tmp_path / "t3" / "ledger.sqlite3")
    reservation = ledger.record_in_doubt("Builder", T0)
    ledger._conn.execute(
        "CREATE TRIGGER fail_release BEFORE DELETE ON in_doubt BEGIN SELECT RAISE(ABORT, 'release failed'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        ledger.record_submit(
            request_id="r1",
            caller="Builder",
            repository="brigade",
            host="host-a",
            seat="dsflash",
            title="t",
            prompt_sha256="0" * 64,
            state="dispatching",
            now=T0,
            release_reservation=reservation,
        )
    # The insert rolled back with the failed release: the slot is counted once, as the reservation.
    assert ledger.get("r1") is None
    assert ledger.open_count(T0) == 1


def test_intake_claim_is_exclusive_and_releasable_across_connections(tmp_path):
    import sqlite3

    path = tmp_path / "state" / "ledger.sqlite3"
    first = Ledger(path)
    second = Ledger(path)
    assert first.claim_intake("brigade#42", T0) is True
    assert second.claim_intake("brigade#42", T0) is False
    first.attach_intake("brigade#42", "r1")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM intake_claims").fetchall() == [
            ("brigade#42", "r1", T0.isoformat(timespec="seconds"))
        ]
    second.release_intake("brigade#42")
    assert first.claim_intake("brigade#42", T0) is True
    first.close()
    second.close()
