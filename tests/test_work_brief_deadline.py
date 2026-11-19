"""Work brief section deadlines: a stalled provider must degrade, never hang."""

from __future__ import annotations

import json
import threading
import time

from brigade import cloud_tracker, memory_cmd, work_cmd


def _stalled_health(event: threading.Event, *, run_seconds: float = 15.0):
    """Block like a provider waiting on an unreachable socket; release on timeout."""

    def health(target):
        event.wait(run_seconds)
        raise AssertionError("stalled health provider returned instead of timing out")

    return health


def test_work_brief_degrades_a_stalled_section_within_its_deadline(tmp_path, monkeypatch, capsys):
    event = threading.Event()
    monkeypatch.setattr(memory_cmd, "health", _stalled_health(event))
    monkeypatch.setenv("BRIGADE_WORK_BRIEF_SECTION_TIMEOUT", "1")
    monkeypatch.setenv("BRIGADE_WORK_BRIEF_BUDGET", "20")

    started = time.monotonic()
    try:
        assert work_cmd.brief(target=tmp_path) == 0
    finally:
        event.set()
    elapsed = time.monotonic() - started

    out = capsys.readouterr().out
    # The deadline is injectable, so the assertion stays off the wall clock. The
    # stalled provider would otherwise hold the brief for fifteen seconds.
    assert elapsed < 10, out
    assert "work brief:" in out
    assert "dogfood_ready:" in out
    assert "suggested_command:" in out
    assert "status_section_issues: 1" in out
    assert "work_brief_section:memory-care-health timed out after 1" in out


def test_work_brief_stops_calling_sections_after_its_total_budget(tmp_path, monkeypatch, capsys):
    first = threading.Event()
    second = threading.Event()
    monkeypatch.setattr(memory_cmd, "health", _stalled_health(first))
    # The stalled section consumes the whole budget, so later sections are
    # skipped instead of starting another provider call.
    monkeypatch.setenv("BRIGADE_WORK_BRIEF_SECTION_TIMEOUT", "2")
    monkeypatch.setenv("BRIGADE_WORK_BRIEF_BUDGET", "1")

    calls: list[str] = []

    def notifications_health(target):
        calls.append("notifications")
        second.wait(15)
        raise AssertionError("budget-exhausted section should not run")

    from brigade import notifications_cmd

    monkeypatch.setattr(notifications_cmd, "health", notifications_health)

    try:
        assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    finally:
        first.set()
        second.set()

    payload = json.loads(capsys.readouterr().out)
    checks = payload["status_section_checks"]
    memory_check = next(check for check in checks if check["name"] == "work_brief_section:memory-care-health")
    assert memory_check["status"] == "warn"
    assert "timed out" in memory_check["detail"]
    notifications_check = next(check for check in checks if check["name"] == "work_brief_section:notifications")
    assert notifications_check["status"] == "warn"
    assert "skipped after the 1s brief budget" in notifications_check["detail"]
    assert payload["status_section_issue_count"] >= 2
    assert calls == []


def test_brief_cloud_observation_keeps_one_github_deadline(tmp_path, monkeypatch):
    calls: list[float] = []

    def slow_run_text(command, *, cwd=None, timeout=30.0):
        calls.append(timeout)
        time.sleep(0.2)
        return 1, "", "timed out"

    monkeypatch.setattr(cloud_tracker, "_run_text", slow_run_text)

    started = time.monotonic()
    result = cloud_tracker.observe_github(tmp_path, budget_seconds=0.3)
    elapsed = time.monotonic() - started

    assert result["branches"] == []
    assert result["prs"] == []
    assert calls, "observe_github should still attempt the first GitHub read"
    assert max(calls) <= 0.3
    assert elapsed < 1.0
