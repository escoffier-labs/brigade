"""Tests for the codex app-server JSON-RPC client against the fake server."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from brigade import codex_appserver

FAKE = [sys.executable, str(Path(__file__).parent / "fake_appserver.py")]


def _server():
    return codex_appserver.AppServer(argv=FAKE)


def test_happy_turn_returns_complete():
    with _server() as server:
        thread = server.start_thread(cwd=Path("/tmp"))
        result = thread.run_turn("say hi", timeout=10.0)
    assert result.ok
    assert result.status == "complete"
    assert result.text == "result for: say hi"
    assert result.thread_id == "t-1"


def test_events_are_streamed_without_deltas():
    events: list[dict] = []
    with _server() as server:
        thread = server.start_thread(cwd=Path("/tmp"))
        thread.run_turn("NOISE say hi", timeout=10.0, on_event=events.append)
    methods = [e["method"] for e in events]
    assert "turn/started" in methods
    assert "item/started" in methods
    assert "item/completed" in methods
    assert "turn/completed" in methods
    assert "totally/unknown" in methods  # unknown methods logged, not fatal
    assert not any("delta" in m for m in methods)


def test_timeout_interrupts_and_salvages_partial_text():
    with _server() as server:
        thread = server.start_thread(cwd=Path("/tmp"))
        result = thread.run_turn("HANG forever", timeout=0.5)
    assert not result.ok
    assert result.status == "interrupted"
    assert result.text == "partial answer"
    assert "timeout" in result.detail


def test_approval_requests_are_auto_declined():
    with _server() as server:
        thread = server.start_thread(cwd=Path("/tmp"))
        result = thread.run_turn("APPROVAL needed", timeout=10.0)
    assert result.ok
    assert result.text == "approval:decline"


def test_server_death_mid_turn_fails_not_hangs():
    with _server() as server:
        thread = server.start_thread(cwd=Path("/tmp"))
        result = thread.run_turn("DIE now", timeout=10.0)
    assert not result.ok
    assert result.status == "failed"
    assert "app-server exited" in result.detail


def test_resume_thread_reuses_id():
    with _server() as server:
        thread = server.resume_thread("t-99", cwd=Path("/tmp"))
        result = thread.run_turn("continue", timeout=10.0)
    assert result.ok
    assert result.thread_id == "t-99"


def test_spawn_failure_raises():
    with pytest.raises(codex_appserver.AppServerError):
        codex_appserver.AppServer(argv=["/nonexistent-binary-xyz"]).start()
