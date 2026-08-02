from __future__ import annotations

import subprocess
import sys

from tests import repeat_watch_approval_test as repeat_runner


def test_repeat_runner_applies_timeout_to_every_iteration(monkeypatch, capsys):
    calls: list[tuple[list[str], bool, int]] = []

    def completed(command, *, check, timeout):
        calls.append((command, check, timeout))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(repeat_runner, "REPEAT_COUNT", 2)
    monkeypatch.setattr(repeat_runner.subprocess, "run", completed)

    assert repeat_runner.main() == 0
    assert calls == [
        (
            [sys.executable, "-m", "pytest", "-q", repeat_runner.TEST_NODE],
            False,
            repeat_runner.ITERATION_TIMEOUT_SECONDS,
        ),
        (
            [sys.executable, "-m", "pytest", "-q", repeat_runner.TEST_NODE],
            False,
            repeat_runner.ITERATION_TIMEOUT_SECONDS,
        ),
    ]
    assert capsys.readouterr().out == "repeat_runs: 2 passed\n"


def test_repeat_runner_reports_timeout_without_starting_another_iteration(monkeypatch, capsys):
    calls = 0

    def timeout(command, *, check, timeout):
        nonlocal calls
        calls += 1
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(repeat_runner, "REPEAT_COUNT", 3)
    monkeypatch.setattr(repeat_runner.subprocess, "run", timeout)

    assert repeat_runner.main() == 124
    assert calls == 1
    assert capsys.readouterr().err == (
        f"repeat_runs: timed out on iteration 1/3 after {repeat_runner.ITERATION_TIMEOUT_SECONDS}s\n"
    )
