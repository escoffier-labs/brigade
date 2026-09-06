"""Throttled Hub presence refresh for live Claude sessions (heartbeat module)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade.claude_hooks import envelope, heartbeat
from brigade.fleet_session_presence import DEFAULT_TTL_SECONDS

T0 = 1_700_000_000.0
SESSION = "session-heartbeat-1"


def _capture_presence(monkeypatch, *, reason: str | None = None) -> list[tuple[str, str]]:
    """Record every emit_presence call instead of reaching the Hub."""
    calls: list[tuple[str, str]] = []

    def emit(event: str, target: Path, session_id: str) -> str | None:
        calls.append((event, session_id))
        return reason

    monkeypatch.setattr(heartbeat.presence, "emit_presence", emit)
    return calls


def test_interval_is_one_third_of_the_hub_ttl():
    """The refresh cadence must leave room for two missed beats inside the TTL."""
    assert heartbeat.HEARTBEAT_INTERVAL_SECONDS == DEFAULT_TTL_SECONDS // 3


def test_prompt_refresh_is_throttled_to_one_publish_per_interval(tmp_path: Path, monkeypatch):
    calls = _capture_presence(monkeypatch)
    interval = heartbeat.HEARTBEAT_INTERVAL_SECONDS

    heartbeat.on_event("UserPromptSubmit", tmp_path, SESSION, now=T0)
    heartbeat.on_event("UserPromptSubmit", tmp_path, SESSION, now=T0 + interval - 1)
    assert calls == [("UserPromptSubmit", SESSION)]

    heartbeat.on_event("UserPromptSubmit", tmp_path, SESSION, now=T0 + interval)
    assert calls == [("UserPromptSubmit", SESSION), ("UserPromptSubmit", SESSION)]


def test_tool_use_shares_the_prompt_throttle_and_other_events_never_publish(tmp_path: Path, monkeypatch):
    calls = _capture_presence(monkeypatch)
    interval = heartbeat.HEARTBEAT_INTERVAL_SECONDS

    heartbeat.on_event("UserPromptSubmit", tmp_path, SESSION, now=T0)
    heartbeat.on_event("PreToolUse", tmp_path, SESSION, now=T0 + 1)
    assert calls == [("UserPromptSubmit", SESSION)]

    heartbeat.on_event("PreToolUse", tmp_path, SESSION, now=T0 + interval)
    assert calls[-1] == ("PreToolUse", SESSION)

    # Stop fires once per assistant turn and PostToolUse follows every tool call;
    # neither carries a presence effect through the heartbeat path.
    heartbeat.on_event("Stop", tmp_path, SESSION, now=T0 + 10 * interval)
    heartbeat.on_event("PostToolUse", tmp_path, SESSION, now=T0 + 10 * interval)
    assert len(calls) == 2


def test_sessions_are_throttled_independently(tmp_path: Path, monkeypatch):
    calls = _capture_presence(monkeypatch)

    heartbeat.on_event("UserPromptSubmit", tmp_path, "sess-a", now=T0)
    heartbeat.on_event("UserPromptSubmit", tmp_path, "sess-b", now=T0)

    assert calls == [("UserPromptSubmit", "sess-a"), ("UserPromptSubmit", "sess-b")]


def test_session_start_only_stamps_and_suppresses_the_next_prompt(tmp_path: Path, monkeypatch):
    """handle_payload already published the SessionStart row; do not publish twice."""
    calls = _capture_presence(monkeypatch)

    heartbeat.on_event("SessionStart", tmp_path, SESSION, now=T0)
    assert calls == []
    assert heartbeat.last_refresh(tmp_path, SESSION) == T0

    heartbeat.on_event("UserPromptSubmit", tmp_path, SESSION, now=T0 + 1)
    assert calls == []


def test_session_end_publishes_end_and_clears_the_stamp(tmp_path: Path, monkeypatch):
    calls = _capture_presence(monkeypatch)
    heartbeat.on_event("UserPromptSubmit", tmp_path, SESSION, now=T0)
    assert heartbeat.state_path(tmp_path, SESSION).is_file()

    heartbeat.on_event("SessionEnd", tmp_path, SESSION, now=T0 + 1)

    assert calls[-1] == ("SessionEnd", SESSION)
    assert not heartbeat.state_path(tmp_path, SESSION).exists()
    assert heartbeat.last_refresh(tmp_path, SESSION) is None


def test_session_end_publishes_even_when_no_stamp_exists(tmp_path: Path, monkeypatch):
    calls = _capture_presence(monkeypatch)

    heartbeat.on_event("SessionEnd", tmp_path, SESSION, now=T0)

    assert calls == [("SessionEnd", SESSION)]


def test_hub_failure_never_escapes_and_still_consumes_the_interval(tmp_path: Path, monkeypatch):
    """A raising Hub write must not break the hook, and must not retry per prompt."""
    calls: list[str] = []

    def boom(event: str, target: Path, session_id: str) -> str | None:
        calls.append(event)
        raise RuntimeError("hub unreachable")

    monkeypatch.setattr(heartbeat.presence, "emit_presence", boom)

    assert heartbeat.on_event("UserPromptSubmit", tmp_path, SESSION, now=T0) is None
    assert heartbeat.on_event("UserPromptSubmit", tmp_path, SESSION, now=T0 + 1) is None
    assert heartbeat.on_event("SessionEnd", tmp_path, SESSION, now=T0 + 2) is None

    # One prompt call inside the interval, plus the unconditional SessionEnd.
    assert calls == ["UserPromptSubmit", "SessionEnd"]


def test_unpublished_hub_write_is_logged_not_raised(tmp_path: Path, monkeypatch):
    _capture_presence(monkeypatch, reason="unpublished")

    heartbeat.on_event("UserPromptSubmit", tmp_path, SESSION, now=T0)

    log = envelope.log_path(tmp_path).read_text(encoding="utf-8")
    assert "fleet presence refresh failed: unpublished" in log
    assert "UserPromptSubmit" in log


def test_stamp_lives_under_the_hook_state_dir_and_is_bounded_json(tmp_path: Path, monkeypatch):
    _capture_presence(monkeypatch)

    heartbeat.on_event("UserPromptSubmit", tmp_path, SESSION, now=T0)

    path = heartbeat.state_path(tmp_path, SESSION)
    assert path.parent == envelope.hooks_state_root(tmp_path) / heartbeat.PRESENCE_DIRNAME
    assert path.parent.parent == tmp_path.resolve() / ".brigade" / "work" / "claude-hooks"
    raw = path.read_text(encoding="utf-8")
    assert len(raw) < heartbeat.MAX_STATE_BYTES
    assert json.loads(raw) == {"last_refresh_at": T0}

    # One stamp per session; a second refresh rewrites it rather than growing it.
    heartbeat.on_event("UserPromptSubmit", tmp_path, SESSION, now=T0 + heartbeat.HEARTBEAT_INTERVAL_SECONDS)
    assert len(list(path.parent.iterdir())) == 1
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "last_refresh_at": T0 + heartbeat.HEARTBEAT_INTERVAL_SECONDS
    }


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps({"last_refresh_at": "soon"}),
        json.dumps({"last_refresh_at": True}),
        json.dumps(["last_refresh_at", 1]),
        json.dumps({"last_refresh_at": 1.0}) + "x" * heartbeat.MAX_STATE_BYTES,
    ],
)
def test_unusable_stamp_reads_as_due_rather_than_pinning_the_session_off(tmp_path: Path, raw: str):
    path = heartbeat.state_path(tmp_path, SESSION)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")

    assert heartbeat.last_refresh(tmp_path, SESSION) is None
    assert heartbeat.due(tmp_path, SESSION, now=T0) is True


def test_backwards_clock_counts_as_due(tmp_path: Path):
    heartbeat.record_refresh(tmp_path, SESSION, now=T0 + 10_000)

    assert heartbeat.due(tmp_path, SESSION, now=T0) is True


def test_clear_on_a_missing_stamp_is_silent(tmp_path: Path):
    assert heartbeat.clear(tmp_path, SESSION) is None
