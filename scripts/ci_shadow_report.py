#!/usr/bin/env python3
"""Join the shadow selection plan with shard junit results (SHADOW MODE).

Writes `shadow-selection.json`: what each selection policy would have run, which
test files actually failed, and which of those failures fell outside the policy's
selection (an "escape"). It reads only CI outputs and never fails the workflow:
every problem is recorded in the artifact instead, and the exit status is always
0. An incomplete observation is marked `status: incomplete` and is never counted
as a clean pass by `ci_shadow_evaluate.py`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Sequence

# The workflow runs this file directly, while tests import it as a module.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.ci_test_selection import POLICIES, policy_files

SCHEMA_VERSION = 1


def parse_junit(path: Path) -> tuple[dict[str, float], dict[str, list[str]], int, int]:
    """Return ({file: seconds}, {file: failing node ids}, unattributed failures, tests)."""
    seconds: dict[str, float] = {}
    failing: dict[str, list[str]] = {}
    unattributed = tests = 0
    for _, element in ET.iterparse(path, events=("end",)):
        if element.tag != "testcase":
            continue
        tests += 1
        name = (element.get("file") or "").removeprefix("./")
        duration = float(element.get("time") or 0)
        bad = any(child.tag in ("failure", "error") for child in element)
        if not name:
            unattributed += bad
        else:
            seconds[name] = seconds.get(name, 0.0) + duration
            if bad:
                node = f"{name}::{element.get('classname') or ''}::{element.get('name') or ''}"
                failing.setdefault(name, []).append(node)
        element.clear()  # failure bodies carry tracebacks; do not hold them
    return seconds, failing, unattributed, tests


def build_report(plan_path: Path | None, junit_dir: Path, shards_expected: int, shards_result: str) -> dict:
    report: dict = {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "errors": [],
        "shards": {"expected": shards_expected, "found": 0, "job_result": shards_result},
    }
    errors: list[str] = report["errors"]
    plan: dict = {}
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path else {}
        if not plan:
            raise ValueError("plan missing")
    except (OSError, ValueError) as exc:
        errors.append(f"plan unavailable: {type(exc).__name__}: {exc}")
    seconds: dict[str, float] = {}
    failing: dict[str, list[str]] = {}
    unattributed = tests = 0
    files = sorted(junit_dir.rglob("*.xml")) if junit_dir.is_dir() else []
    for path in files:
        try:
            s, f, u, n = parse_junit(path)
        except (OSError, ET.ParseError, ValueError) as exc:
            errors.append(f"junit unreadable: {path.name}: {type(exc).__name__}")
            continue
        report["shards"]["found"] += 1
        for name, value in s.items():
            seconds[name] = seconds.get(name, 0.0) + value
        for name, nodes in f.items():
            failing.setdefault(name, []).extend(nodes)
        unattributed += u
        tests += n
    if report["shards"]["found"] != shards_expected:
        errors.append(f"expected {shards_expected} junit files, found {report['shards']['found']}")
    if unattributed:
        errors.append(f"{unattributed} failing testcases had no file attribute")
    if shards_result == "failure" and not failing and not unattributed:
        errors.append("shard job failed but junit shows no failing test")
    reasons = sorted({t["reason"] for t in plan.get("full_triggers", [])})
    report.update(
        mode=plan.get("mode", "unknown"),
        full_reasons=reasons,
        # Unmapped paths are the main lever for widening coverage, so keep the paths themselves.
        full_paths=sorted({t["path"] for t in plan.get("full_triggers", []) if t.get("path") not in ("*", "")}),
        changed_count=len(plan.get("changed", [])),
        test_files_total=plan.get("test_files_total", 0),
        tests_run=tests,
        total_test_seconds=round(sum(seconds.values()), 3),
        failing_files=sorted(failing),
        failing_test_count=sum(len(v) for v in failing.values()),
        unattributed_failures=unattributed,
        file_seconds={k: round(v, 3) for k, v in sorted(seconds.items())},
    )
    if plan.get("error"):
        errors.append(f"selector error: {plan['error']}")
    policies = {}
    if plan:
        for policy in POLICIES:
            selected = policy_files(plan, policy)
            escaped = sorted(set(failing) - selected)
            policies[policy] = {
                "selected_files": len(selected),
                "selected_fraction": round(len(selected) / max(1, plan.get("test_files_total", 0)), 4),
                "selected_seconds": round(sum(v for k, v in seconds.items() if k in selected), 3),
                "escape_files": escaped,
                "escape_nodeids": [node for name in escaped for node in failing[name]],
            }
    report["policies"] = policies
    if errors:
        report["status"] = "incomplete"
    return report


def summary_line(report: dict) -> str:
    parts = [
        f"{name} {p['selected_files']}/{report['test_files_total']} escapes={len(p['escape_files'])}"
        for name, p in report["policies"].items()
    ]
    return (
        f"shadow selection: status={report['status']} mode={report['mode']} "
        f"failing_files={len(report['failing_files'])} | "
        + " | ".join(parts)
        + (f" | errors={len(report['errors'])}" if report["errors"] else "")
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--junit-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--shards-result", default=os.environ.get("SHARDS_RESULT", "unknown"))
    args = parser.parse_args(argv)
    try:
        report = build_report(args.plan, args.junit_dir, args.shards, args.shards_result)
    except Exception as exc:  # noqa: BLE001 - never fail the workflow; record the failure instead
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "error",
            "errors": [f"report failed: {type(exc).__name__}: {exc}"],
            "mode": "unknown",
            "policies": {},
            "failing_files": [],
            "test_files_total": 0,
        }
    for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA"):
        report[key.removeprefix("GITHUB_").lower()] = os.environ.get(key, "")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    line = summary_line(report) if report["policies"] else f"shadow selection: status={report['status']}"
    print(line)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
