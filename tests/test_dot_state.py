"""Compatibility and private-state regressions ported from the bridge."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brigade.dot.contracts import DotError
from brigade.dot.state import BridgeState, MAX_ARG_SUMMARY, summarize_arguments

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
REQUEST = "request-example"


def rows(state: BridgeState) -> list[dict]:
    with state.transaction() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM call_log ORDER BY id")]


def test_upgrade_is_additive_and_preserves_existing_rows(dot_private_dir: Path) -> None:
    with BridgeState(dot_private_dir) as state:
        with state.transaction() as connection:
            connection.execute(
                "INSERT INTO inbox(created_at,sender,body,subject) VALUES (?,?,?,?)",
                (NOW.isoformat(), "agent-example", "keep me", "s1"),
            )
            connection.execute(
                "INSERT INTO replies(msg_id,created_at,body) VALUES (?,?,?)", (1, NOW.isoformat(), "answer")
            )
            connection.execute(
                "INSERT INTO subscriptions(id,owner,name,arguments,url,secret,created) VALUES (?,?,?,?,?,?,?)",
                (
                    "sub-example",
                    "owner-example",
                    "fleet.inbox.message",
                    "{}",
                    "https://example.com/callback",
                    "fixture-secret",
                    NOW.timestamp(),
                ),
            )
            connection.execute(
                "INSERT INTO event_queue VALUES (?,?,?,?,?,?)",
                ("evt-example", "fleet.inbox.message", "{}", "normal", NOW.timestamp(), "inbox:1"),
            )
        operation = state.begin_operation("fleet_submit", {"request_id": REQUEST}, clock=lambda: NOW)
        with state.transaction() as connection:
            connection.execute("DROP TABLE call_log")
            before = {
                table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("inbox", "replies", "subscriptions", "operations", "event_queue")
            }
    with BridgeState(dot_private_dir) as state:
        with state.transaction() as connection:
            after = {
                table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in before
            }
            assert connection.execute("SELECT COUNT(*) FROM call_log").fetchone()[0] == 0
        assert before == after
        assert operation


def test_log_call_sanitizes_every_column(dot_private_dir: Path) -> None:
    with BridgeState(dot_private_dir) as state:
        state.log_call("fleet_status", True, 12.7, request_id=REQUEST, arg_summary="x" * 500, clock=lambda: NOW)
        state.log_call("bad name; drop", False, -5, request_id="--not valid", error_code="e" * 100, clock=lambda: NOW)
        first, second = rows(state)
    assert (first["tool"], first["ok"], first["duration_ms"], first["request_id"]) == ("fleet_status", 1, 12, REQUEST)
    assert len(first["arg_summary"]) == MAX_ARG_SUMMARY
    assert (second["tool"], second["ok"], second["duration_ms"], second["request_id"]) == ("unknown", 0, 0, None)
    assert len(second["error_code"]) == 64


def test_prune_keeps_thirty_days(dot_private_dir: Path) -> None:
    with BridgeState(dot_private_dir) as state:
        for days in (40, 31, 29, 0):
            state.log_call("inbox_list", True, 1, clock=lambda days=days: NOW - timedelta(days=days))
        assert state.prune_call_log(clock=lambda: NOW) == 2
        assert len(rows(state)) == 2


def test_summary_keeps_ids_and_limits_never_bodies() -> None:
    summary = summarize_arguments(
        {
            "repository": "brigade",
            "title": "SECRET-TITLE",
            "prompt": "SECRET-PROMPT",
            "message": "SECRET-MSG",
            "body": "SECRET-BODY",
            "query": "SECRET-QUERY",
            "content": "# Memory Handoff\nSECRET",
            "answers": {"q": "SECRET-ANS"},
            "override_reason": "SECRET-REASON",
            "cursor": "SECRETCURSOR",
            "request_id": REQUEST,
            "limit": 5,
            "ids": list(range(1, 15)),
            "include_output": True,
            "host": "has space",
        }
    )
    assert summary is not None
    for secret in ("SECRET", "Memory Handoff"):
        assert secret not in summary
    assert "request_id=" + REQUEST in summary
    assert "limit=5" in summary
    assert "ids=1,2,3,4,5,6,7,8,9,10,..." in summary
    assert "host=?" in summary
    assert len(summary) <= MAX_ARG_SUMMARY
    assert summarize_arguments({}) is None
    assert "ghp_" + "A" * 36 not in (summarize_arguments({"name": "ghp_" + "A" * 36}) or "")


def test_unsafe_directory_database_and_ancestors_rejected(dot_private_dir: Path) -> None:
    with BridgeState(dot_private_dir):
        pass
    parent = dot_private_dir.parent
    link = parent / "link"
    link.symlink_to(dot_private_dir, target_is_directory=True)
    for path in (link, link / "child"):
        with pytest.raises(DotError, match="^unsafe_state$"):
            BridgeState(path)
    unsafe = parent / "unsafe"
    unsafe.mkdir(mode=0o755)
    with pytest.raises(DotError, match="^unsafe_state$"):
        BridgeState(unsafe)
    database = dot_private_dir / "bridge.sqlite3"
    database.chmod(0o644)
    with pytest.raises(DotError, match="^unsafe_state$"):
        BridgeState(dot_private_dir)
    database.chmod(0o600)
    database.unlink()
    database.symlink_to(parent / "outside")
    with pytest.raises(DotError, match="^unsafe_state$"):
        BridgeState(dot_private_dir)


def test_fresh_database_owns_all_bridge_and_event_tables(dot_private_dir: Path) -> None:
    with BridgeState(dot_private_dir) as state:
        with state.transaction() as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "inbox",
        "replies",
        "operations",
        "call_log",
        "hub_inbox",
        "hub_state",
        "subscriptions",
        "event_queue",
        "event_deliveries",
        "wake_attempts",
        "tracked_requests",
    } <= tables


def test_private_directory_rejects_overflow_uid_ancestor(dot_private_dir, monkeypatch):
    """Namespace overflow ownership must fail closed, even for systemd sandboxes."""
    import os
    import stat

    from brigade.dot.safe_state import private_directory

    real_fstat = os.fstat
    ancestors = []

    def overflow_ancestor(descriptor):
        info = real_fstat(descriptor)
        values = list(info)
        values[0] = stat.S_IFDIR | 0o755  # Isolate ownership from ancestor write permissions.
        values[4] = 65534
        ancestors.append(descriptor)
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", overflow_ancestor)
    with pytest.raises(DotError, match="^unsafe_state$"):
        private_directory(dot_private_dir)
    assert len(ancestors) == 1
