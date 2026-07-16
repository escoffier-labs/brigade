"""Regression tests for the issue 266 benchmark harness."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from scripts import benchmark_issue266_work_status_memory as benchmark
from tests.issue266_fixture import FLEET_SWEEP_HISTORY_COUNT


def _observed_result(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "argv": [sys.executable, "-c", "pass"],
        "exit_code": 0,
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "wall_seconds": 0.01,
        "peak_rss_kib": 1024,
        "peak_rss_mib": 1.0,
        "rss_sampling_supported": True,
        "timed_out": False,
    }
    result.update(overrides)
    return result


def test_proc_peak_rss_marks_unavailable_sampling(monkeypatch):
    def missing_status(_self: Path) -> str:
        raise FileNotFoundError

    monkeypatch.setattr(Path, "read_text", missing_status)

    assert benchmark._proc_peak_rss_kib(999_999) is None


def test_run_command_does_not_pass_memory_budget_without_rss_sampling(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "_proc_peak_rss_kib", lambda _pid: None)

    observed = benchmark._run_command(
        [sys.executable, "-c", "pass"],
        cwd=tmp_path,
        wall_seconds_max=1.0,
    )
    result = benchmark._budget_result("work_brief", observed, benchmark.DEFAULT_BUDGETS["work_brief"])

    assert observed["rss_sampling_supported"] is False
    assert observed["peak_rss_mib"] is None
    assert result["checks"]["peak_rss_mib"] is False
    assert result["passed"] is False


def test_run_command_drains_large_output_without_pipe_deadlock(tmp_path):
    observed = benchmark._run_command(
        [
            sys.executable,
            "-c",
            "import os; os.write(1, b'x' * 200_000); os.write(2, b'y' * 200_000)",
        ],
        cwd=tmp_path,
        wall_seconds_max=2.0,
    )

    assert observed["exit_code"] == 0
    assert observed["timed_out"] is False
    assert observed["stdout_bytes"] == 200_000
    assert observed["stderr_bytes"] == 200_000


def test_run_command_enforces_wall_time_deadline(tmp_path):
    observed = benchmark._run_command(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        cwd=tmp_path,
        wall_seconds_max=0.05,
    )

    assert observed["timed_out"] is True
    assert observed["exit_code"] != 0
    assert observed["wall_seconds"] < 1.0


def test_benchmark_defaults_to_representative_sweep_history(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(benchmark, "_build_workspace", lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr(benchmark, "_run_command", lambda *_args, **_kwargs: _observed_result())
    monkeypatch.setattr(sys, "argv", ["benchmark_issue266_work_status_memory.py"])

    assert benchmark.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["fixture"]["sweep_history_count"] == FLEET_SWEEP_HISTORY_COUNT
