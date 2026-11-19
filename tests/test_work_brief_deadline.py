"""Work brief section deadlines: a stalled provider must degrade, never hang."""

from __future__ import annotations

import json
import threading
import time

import pytest

from brigade import cli, cloud_tracker, memory_cmd, work_cmd


def _stalled_health(event: threading.Event, *, run_seconds: float = 15.0):
    """Block like a provider waiting on an unreachable socket; release on timeout."""

    def health(target):
        event.wait(run_seconds)
        raise AssertionError("stalled health provider returned instead of timing out")

    return health


def _brief_env(monkeypatch, *, section: str = "1", budget: str = "20") -> None:
    monkeypatch.setenv("BRIGADE_WORK_BRIEF_SECTION_TIMEOUT", section)
    monkeypatch.setenv("BRIGADE_WORK_BRIEF_BUDGET", budget)


def test_work_brief_degrades_a_stalled_section_within_its_deadline(tmp_path, monkeypatch, capsys):
    event = threading.Event()
    monkeypatch.setattr(memory_cmd, "health", _stalled_health(event))
    _brief_env(monkeypatch)

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
    assert "work_brief_section:memory-care-health timed out after 1s" in out


@pytest.mark.parametrize("threaded", [False, True])
def test_work_brief_times_out_a_provider_that_swallows_exceptions(tmp_path, monkeypatch, capsys, threaded):
    """A provider's own ``except Exception`` must not eat the section deadline."""
    from brigade.daily_cmd import config

    if threaded:
        monkeypatch.delattr(config.signal, "SIGALRM", raising=False)

    event = threading.Event()

    def stalled_github(target, **_kwargs):
        try:
            event.wait(15)
        except Exception:  # mirrors providers that degrade their own transport errors
            pass
        return {"branches": [], "prs": []}

    monkeypatch.setattr(cloud_tracker, "observe_github", stalled_github)
    _brief_env(monkeypatch)

    started = time.monotonic()
    try:
        assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    finally:
        event.set()
    elapsed = time.monotonic() - started

    payload = json.loads(capsys.readouterr().out)
    check = next(c for c in payload["status_section_checks"] if c["name"] == "work_brief_section:cloud-tracker")
    assert check["status"] == "warn"
    assert "timed out after 1s" in check["detail"]
    assert "cloud_tracker" not in payload
    assert elapsed < 10


def test_work_brief_stops_calling_sections_after_its_total_budget(tmp_path, monkeypatch, capsys):
    first = threading.Event()
    second = threading.Event()
    monkeypatch.setattr(memory_cmd, "health", _stalled_health(first))
    # The stalled section consumes the whole budget, so later sections are
    # skipped instead of starting another provider call.
    _brief_env(monkeypatch, section="2", budget="1")

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


def test_work_brief_bounds_patrol_and_work_mode_pin(tmp_path, monkeypatch, capsys):
    from brigade import runs_patrol, work_mode

    patrol_event = threading.Event()
    pin_event = threading.Event()

    def stalled_patrol(*_args, **_kwargs):
        patrol_event.wait(15)
        return None

    def stalled_pin(*_args, **_kwargs):
        pin_event.wait(15)
        return None

    monkeypatch.setattr(runs_patrol, "brief_summary", stalled_patrol)
    monkeypatch.setattr(work_mode, "pin_explicit_mode", stalled_pin)
    _brief_env(monkeypatch)

    started = time.monotonic()
    try:
        assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    finally:
        patrol_event.set()
        pin_event.set()
    elapsed = time.monotonic() - started

    payload = json.loads(capsys.readouterr().out)
    checks = {check["name"]: check for check in payload["status_section_checks"]}
    for name in ("work_brief_section:patrol", "work_brief_section:work-mode-pin"):
        assert checks[name]["status"] == "warn"
        assert "timed out after 1s" in checks[name]["detail"]
    # A timed-out patrol must be named in the checks, not serialize as a
    # healthy-looking "no local lanes" payload.
    assert "patrol" not in payload
    assert elapsed < 10


def test_work_brief_json_omits_a_timed_out_section(tmp_path, monkeypatch, capsys):
    from brigade import handoff_cmd, pantry_cmd

    handoff_event = threading.Event()
    pantry_event = threading.Event()

    def stalled_handoffs(*_args, **_kwargs):
        handoff_event.wait(15)
        return []

    def stalled_pantry(*_args, **_kwargs):
        pantry_event.wait(15)
        return {}

    monkeypatch.setattr(handoff_cmd, "collect_issues", stalled_handoffs)
    monkeypatch.setattr(pantry_cmd, "status_payload", stalled_pantry)
    _brief_env(monkeypatch)

    try:
        assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    finally:
        handoff_event.set()
        pantry_event.set()

    payload = json.loads(capsys.readouterr().out)
    checks = {check["name"]: check for check in payload["status_section_checks"]}
    assert checks["work_brief_section:handoff-issues"]["status"] == "warn"
    assert checks["work_brief_section:pantry"]["status"] == "warn"
    # A timed-out handoff check must not serialize as a healthy zero count.
    assert "handoff_issues" not in payload
    assert "pantry" not in payload


def test_work_brief_fails_closed_on_corrupt_task_ledger(tmp_path, capsys):
    tasks_path = tmp_path / ".brigade" / "work" / "tasks.json"
    tasks_path.parent.mkdir(parents=True)
    tasks_path.write_text("{not valid json")

    assert cli.main(["work", "brief", "--target", str(tmp_path)]) == 2
    assert "task ledger is corrupt" in capsys.readouterr().err


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
    assert max(calls) <= 0.3 + 1e-6
    # Each read sleeps at least 0.2 s, so a shared 0.3 s budget allows at most two reads; an unbudgeted
    # observation would make every read. The wall-clock bound is loose for slow and coarse-clock runners.
    assert len(calls) <= 2
    assert elapsed < 5.0
