"""Deterministic alarm delivery around a bounded status section."""

import signal

import pytest

from brigade import cli
from brigade.daily_cmd import config


@pytest.fixture
def alarm(monkeypatch):
    previous_handler = object()
    state = {"handler": previous_handler, "timers": []}
    monkeypatch.setattr(config.signal, "SIGALRM", 14, raising=False)
    monkeypatch.setattr(config.signal, "ITIMER_REAL", 0, raising=False)
    monkeypatch.setattr(config.signal, "getsignal", lambda _: previous_handler, raising=False)
    monkeypatch.setattr(config.signal, "getitimer", lambda _: (12.0, 3.0), raising=False)

    def install(_signum, handler):
        state["handler"] = handler

    def timer(_kind, seconds, interval=0):
        state["timers"].append((seconds, interval))
        if seconds == 0:
            state["handler"](signal.SIGALRM, None)

    monkeypatch.setattr(config.signal, "signal", install)
    monkeypatch.setattr(config.signal, "setitimer", timer, raising=False)
    yield state
    assert state["handler"] is previous_handler
    assert state["timers"][-2:] == [(0, 0), (12.0, 3.0)]


@pytest.mark.parametrize("outcome", ["success", "error", "timeout", "propagate"])
def test_late_alarm_is_ignored_after_section_finishes(monkeypatch, alarm, outcome):
    finished = False
    original_monotonic = config.time.monotonic

    def monotonic():
        if finished:
            alarm["handler"](signal.SIGALRM, None)
        return original_monotonic()

    def section():
        nonlocal finished
        try:
            if outcome == "timeout":
                alarm["handler"](signal.SIGALRM, None)
            if outcome in {"error", "propagate"}:
                raise ValueError("section failed")
            return "section result"
        finally:
            finished = True

    monkeypatch.setattr(config.time, "monotonic", monotonic)
    if outcome == "propagate":
        with pytest.raises(ValueError, match="section failed"):
            config._bounded_status_call("alarm", section, "fallback", raise_types=(ValueError,))
        return
    result, check = config._bounded_status_call("alarm", section, "fallback", timeout_seconds=1)
    assert result == ("section result" if outcome == "success" else "fallback")
    assert check["status"] == ("ok" if outcome == "success" else "warn")
    if outcome == "timeout":
        assert check["detail"] == "timed out after 1s"
    elif outcome == "error":
        assert "ValueError: section failed" in check["detail"]


def test_daily_status_cli_ignores_alarm_during_result_reporting(tmp_path, monkeypatch, capsys, alarm):
    original_check = config._status_section_check
    deliveries = []

    def check(*args, **kwargs):
        alarm["handler"](signal.SIGALRM, None)
        deliveries.append(args[0])
        return original_check(*args, **kwargs)

    monkeypatch.setattr(config, "_status_section_check", check)
    assert cli.main(["daily", "status", "--target", str(tmp_path), "--json"]) == 0
    assert deliveries
    assert '"status_section_checks"' in capsys.readouterr().out
