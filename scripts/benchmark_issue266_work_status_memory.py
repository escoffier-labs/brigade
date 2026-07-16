#!/usr/bin/env python3
"""Benchmark work-brief and daily-status memory for issue 266."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TESTS_DIR = _REPO_ROOT / "tests"
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from issue266_fixture import (  # noqa: E402
    FLEET_REPO_COUNT,
    OPERATOR_REPORT_HISTORY_COUNT,
    build_daily_status_workspace,
)

DEFAULT_BUDGETS = {
    "work_brief": {
        "exit_code": 0,
        "peak_rss_mib_max": 200.0,
        "stdout_bytes_max": 2_000_000,
        "wall_seconds_max": 8.0,
    },
    "daily_status": {
        "exit_code": 0,
        "peak_rss_mib_max": 180.0,
        "stdout_bytes_max": 1_500_000,
        "wall_seconds_max": 6.0,
    },
}


def _python_argv() -> list[str]:
    return [sys.executable, "-m", "brigade"]


def _proc_peak_rss_kib(pid: int) -> int:
    status_path = Path(f"/proc/{pid}/status")
    if not status_path.is_file():
        return 0
    peak_kib = 0
    for line in status_path.read_text().splitlines():
        if line.startswith(("VmRSS:", "VmHWM:")):
            peak_kib = max(peak_kib, int(line.split()[1]))
    return peak_kib


def _run_command(argv: list[str], *, cwd: Path) -> dict[str, object]:
    started = time.perf_counter()
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    peak_rss_kib = 0
    while proc.poll() is None:
        peak_rss_kib = max(peak_rss_kib, _proc_peak_rss_kib(proc.pid))
        time.sleep(0.005)
    peak_rss_kib = max(peak_rss_kib, _proc_peak_rss_kib(proc.pid))
    stdout, stderr = proc.communicate()
    wall_seconds = round(time.perf_counter() - started, 3)
    return {
        "argv": argv,
        "exit_code": proc.returncode,
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "wall_seconds": wall_seconds,
        "peak_rss_kib": peak_rss_kib,
        "peak_rss_mib": round(peak_rss_kib / 1024, 2),
    }


def _budget_result(name: str, observed: dict[str, object], budget: dict[str, object]) -> dict[str, object]:
    checks = {
        "exit_code": observed["exit_code"] == budget["exit_code"],
        "peak_rss_mib": float(observed["peak_rss_mib"]) <= float(budget["peak_rss_mib_max"]),
        "stdout_bytes": int(observed["stdout_bytes"]) <= int(budget["stdout_bytes_max"]),
        "wall_seconds": float(observed["wall_seconds"]) <= float(budget["wall_seconds_max"]),
    }
    return {
        "name": name,
        "budget": budget,
        "observed": {
            "exit_code": observed["exit_code"],
            "peak_rss_mib": observed["peak_rss_mib"],
            "stdout_bytes": observed["stdout_bytes"],
            "wall_seconds": observed["wall_seconds"],
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def _build_workspace(
    workspace: Path | None,
    *,
    repo_count: int,
    report_count: int,
    sweep_history_count: int,
    artifact_padding_bytes: int,
) -> Path:
    if workspace is None:
        workspace = Path(tempfile.mkdtemp(prefix="brigade-issue266-"))
    build_daily_status_workspace(
        workspace,
        repo_count=repo_count,
        report_count=report_count,
        sweep_history_count=sweep_history_count,
        artifact_padding_bytes=artifact_padding_bytes,
    )
    return workspace


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=None, help="Existing or new workspace path.")
    parser.add_argument(
        "--results-out",
        type=Path,
        default=None,
        help="Write JSON results to this durable path.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional alias for --results-out.",
    )
    parser.add_argument("--phase", default="local", help="Phase label stored in the output JSON.")
    parser.add_argument("--repo-count", type=int, default=FLEET_REPO_COUNT)
    parser.add_argument("--report-count", type=int, default=OPERATOR_REPORT_HISTORY_COUNT)
    parser.add_argument("--sweep-history-count", type=int, default=1)
    parser.add_argument("--artifact-padding-bytes", type=int, default=0)
    parser.add_argument(
        "--work-brief-peak-rss-mib-max",
        type=float,
        default=DEFAULT_BUDGETS["work_brief"]["peak_rss_mib_max"],
    )
    parser.add_argument(
        "--daily-status-peak-rss-mib-max",
        type=float,
        default=DEFAULT_BUDGETS["daily_status"]["peak_rss_mib_max"],
    )
    args = parser.parse_args()

    workspace = _build_workspace(
        args.workspace,
        repo_count=args.repo_count,
        report_count=args.report_count,
        sweep_history_count=args.sweep_history_count,
        artifact_padding_bytes=args.artifact_padding_bytes,
    )

    brigade = _python_argv()
    commands = {
        "work_brief": _run_command(
            [*brigade, "work", "brief", "--target", str(workspace), "--json"],
            cwd=_REPO_ROOT,
        ),
        "daily_status": _run_command(
            [*brigade, "daily", "status", "--target", str(workspace), "--json"],
            cwd=_REPO_ROOT,
        ),
    }

    budgets = {
        "work_brief": {
            **DEFAULT_BUDGETS["work_brief"],
            "peak_rss_mib_max": args.work_brief_peak_rss_mib_max,
        },
        "daily_status": {
            **DEFAULT_BUDGETS["daily_status"],
            "peak_rss_mib_max": args.daily_status_peak_rss_mib_max,
        },
    }
    budget_results = [
        _budget_result(name, commands[name], budgets[name])
        for name in ("work_brief", "daily_status")
    ]
    payload = {
        "issue": 266,
        "phase": args.phase,
        "passed": all(item["passed"] for item in budget_results),
        "default_budgets": DEFAULT_BUDGETS,
        "fixture": {
            "fixture_kind": "issue-266-synthetic-fleet",
            "workspace": str(workspace),
            "fleet_repo_count": args.repo_count,
            "operator_report_history_count": args.report_count,
            "sweep_history_count": args.sweep_history_count,
            "artifact_padding_bytes": args.artifact_padding_bytes,
        },
        "commands": commands,
        "budget_results": budget_results,
    }

    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    results_path = args.results_out if args.results_out is not None else args.output
    if results_path is not None:
        results_path.parent.mkdir(parents=True, exist_ok=True)
        results_path.write_text(encoded)
    print(encoded, end="")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
