#!/usr/bin/env python3
"""Bounded, non-gating PR tier shadow with retained exact-attempt evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Sequence

# The workflow executes this file directly, while tests import it as a module.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.pr_tier_selection import MANIFEST, select_tests

TRIAL_START = datetime(2026, 10, 23, tzinfo=timezone.utc)
TRIAL_EXPIRY = datetime(2026, 11, 6, tzinfo=timezone.utc)
SHARD_NAMES = [f"test shard (3.12, {index})" for index in range(4)]
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\S+\s+")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
SUMMARY = re.compile(
    r"^=*\s*\d+ (?:subtests )?[a-z]+(?:, \d+ (?:subtests )?[a-z]+)* in (\d+(?:\.\d+)?)s(?:\s+\([\d:]+\))?\s*=*\s*$"
)
COUNTS = re.compile(r"(\d+) (passed|failed|skipped|error|errors|xfailed|xpassed|deselected|warnings?|rerun)\b")


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def incomplete(*errors: str) -> dict:
    return {
        "status": "incomplete",
        "errors": list(errors),
        "full_only_candidates": [],
        "full_only_candidate_count": None,
        "confirmed_real_miss_count": None,
    }


def retain_report(output: Path, report: dict) -> None:
    write_json(output / "report.json", report)
    comparison = report["comparison"]
    candidates = comparison["full_only_candidates"]
    count = comparison["full_only_candidate_count"]
    lines = [
        "# PR test tier shadow",
        "",
        f"Trial: {report['trial_status']}",
        f"Window: {TRIAL_START.isoformat()} inclusive to {TRIAL_EXPIRY.isoformat()} exclusive.",
        f"Comparison: {comparison['status']}",
        f"Full-only UNCONFIRMED candidate count: {count if count is not None else 'unknown'}.",
        "Confirmed real miss count: unknown. Reproduce candidates at the exact merge SHA and base before classification.",
    ]
    if identity := report.get("identity"):
        lines += ["", "```json", json.dumps(identity, indent=2, sort_keys=True), "```"]
    if timing := comparison.get("timing"):
        lines += ["", "Timing (seconds):", "```json", json.dumps(timing, indent=2, sort_keys=True), "```"]
    if candidates:
        lines += ["", "Unconfirmed candidates:", "```", *candidates, "```"]
    if errors := comparison.get("errors"):
        lines += ["", "Evidence gaps:", "```", *errors, "```"]
    lines += [
        "",
        "Full logs provide pytest elapsed time, not summed testcase durations.",
        "Job wall time includes installation and other steps. Shards run in parallel.",
        "This observation does not change required CI checks or authorize rollout.",
        "",
    ]
    summary = "\n".join(lines)
    (output / "summary.md").write_text(summary, encoding="utf-8")


def publish_summary(output: Path) -> None:
    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary_path).open("a", encoding="utf-8") as stream:
            stream.write((output / "summary.md").read_text(encoding="utf-8"))


def guard(output: Path, *, now: datetime | None = None) -> dict:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("trial clock requires an explicit timezone")
    status = "not-started" if instant < TRIAL_START else "expired" if instant >= TRIAL_EXPIRY else "active"
    report = {
        "schema_version": 1,
        "trial_status": status,
        "observed_at": instant.isoformat(),
        "trial_start": TRIAL_START.isoformat(),
        "trial_expiry": TRIAL_EXPIRY.isoformat(),
        "comparison": incomplete("selected tests and exact full-shard evidence not yet available"),
    }
    retain_report(output, report)
    if output_path := os.environ.get("GITHUB_OUTPUT"):
        with Path(output_path).open("a", encoding="utf-8") as stream:
            stream.write(f"active={'true' if status == 'active' else 'false'}\n")
    return report


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True, timeout=60).strip()


def merge_identity(repo: Path) -> dict:
    sha = git(repo, "rev-parse", "HEAD")
    if sha != os.environ.get("GITHUB_SHA"):
        raise ValueError("checkout differs from workflow merge SHA")
    parents = git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()
    if len(parents) != 3 or parents[2] != os.environ.get("PR_HEAD_SHA"):
        raise ValueError("checkout is not a PR merge with the event head as its second parent")
    changed = subprocess.check_output(
        ["git", "-C", str(repo), "diff", "--no-renames", "--name-only", "-z", parents[1], sha, "--"],
        text=True,
        timeout=60,
    )
    return {
        "sha": sha,
        "base": parents[1],
        "pr_head": parents[2],
        "changed": sorted(path for path in changed.split("\0") if path),
    }


def pytest_worker(repo: Path, output: Path, selected: Sequence[str]) -> int:
    """The child process records actual reports, including setup/teardown errors."""
    os.environ["COVERAGE_CORE"] = "sysmon"
    os.environ["COVERAGE_FILE"] = str(output.resolve() / ".coverage")
    import pytest

    class Results:
        def __init__(self) -> None:
            self.outcomes: dict[str, dict] = {}
            self.collection_errors: list[str] = []
            self.finished: set[str] = set()

        def pytest_collectreport(self, report) -> None:
            if report.failed:
                self.collection_errors.append(report.nodeid)

        def pytest_runtest_logreport(self, report) -> None:
            outcome = self.outcomes.setdefault(report.nodeid, {"nodeid": report.nodeid, "phases": []})
            outcome["phases"].append(
                {
                    "when": report.when,
                    "outcome": report.outcome,
                    "duration": report.duration,
                    "subtest": isinstance(report, getattr(pytest, "SubtestReport", ())),
                }
            )
            if report.when == "teardown":
                self.finished.add(report.nodeid)

        @pytest.hookimpl(trylast=True)
        def pytest_sessionfinish(self, session, exitstatus) -> None:
            outcomes = [self.outcomes[nodeid] for nodeid in sorted(self.outcomes)]
            write_json(
                output / "pytest-results.json",
                {
                    "complete": (
                        int(exitstatus) in (0, 1)
                        and not self.collection_errors
                        and session.testscollected > 0
                        and len(self.finished) == session.testscollected
                    ),
                    "exit_code": int(exitstatus),
                    "collected": session.testscollected,
                    "failed_nodeids": [
                        item["nodeid"]
                        for item in outcomes
                        if any(phase["outcome"] == "failed" for phase in item["phases"])
                    ],
                    "collection_errors": self.collection_errors,
                    "outcomes": outcomes,
                    # A parent's call already includes time spent inside its subtests.
                    "test_duration_seconds": sum(
                        phase["duration"] for item in outcomes for phase in item["phases"] if not phase["subtest"]
                    ),
                },
            )

    os.chdir(repo)
    return int(
        pytest.main(
            ["-q", "-rA", "-o", "addopts=", "--cov=brigade", "--cov-report=", "--cov-fail-under=0", "--", *selected],
            plugins=[Results()],
        )
    )


def run_pytest(repo: Path, selected: Sequence[str], output: Path) -> dict:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "pytest-input.json", {"selected": list(selected)})
    result = {
        "complete": False,
        "failed_nodeids": [],
        "exit_code": None,
        "test_duration_seconds": None,
        "collection_errors": [],
        "outcomes": [],
    }
    (output / "pytest-results.json").unlink(missing_ok=True)
    if not selected:
        result["error"] = "empty selection; pytest discovery was not started"
        result["wall_seconds"] = 0.0
        (output / "pytest.log").write_text(result["error"] + "\n", encoding="utf-8")
        write_json(output / "pytest-results.json", result)
        return result
    started = time.monotonic()
    env = dict(os.environ)
    env.update(COVERAGE_CORE="sysmon", COVERAGE_FILE=str(output / ".coverage"))
    # No Actions API credentials go to the selected test process.
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        env.pop(key, None)
    with (output / "pytest.log").open("w", encoding="utf-8") as log:
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "pytest",
                    "--repo",
                    str(repo.resolve()),
                    "--output",
                    str(output),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                timeout=3600,
            )
            if (output / "pytest-results.json").is_file():
                result = json.loads((output / "pytest-results.json").read_text(encoding="utf-8"))
            if proc.returncode != result["exit_code"]:
                result["complete"] = False
            result["exit_code"] = proc.returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["error"] = type(exc).__name__
    result["wall_seconds"] = time.monotonic() - started
    write_json(output / "pytest-results.json", result)
    return result


def outcome_nodeid(line: str) -> tuple[str, str] | None:
    # Pytest 9 emits each subtest failure as well as its parent failure.
    match = re.match(r"^(FAILED|ERROR|SUBFAILED.*?) (tests/.*)$", line)
    if not match:
        if line.startswith(("FAILED ", "ERROR ", "SUBFAILED")):
            raise ValueError("unparseable failure outcome")
        return None
    # Pytest's separator can also occur inside parametrized IDs.
    depth = 0
    text = match[2]
    for position, char in enumerate(text):
        if depth == 0 and text[position:].startswith(" - "):
            return "ERROR" if match[1] == "ERROR" else "FAILED", text[:position]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
    if depth:
        raise ValueError("unparseable parametrized nodeid")
    return "ERROR" if match[1] == "ERROR" else "FAILED", text


def parse_full_log(text: str, index: int, sha: str) -> dict:
    """Attribute every pytest summary, retaining primary shard time separately."""
    lines = [TIMESTAMP.sub("", ANSI.sub("", line)).strip() for line in text.splitlines()]
    checkouts = [
        lines[i + 1]
        for i, line in enumerate(lines[:-1])
        if re.search(r"\bgit log -1 --format=(?:%H|['\"]%H['\"])", line)
    ]
    if checkouts != [sha]:
        raise ValueError("missing or mismatched checkout merge SHA")
    markers = [
        (i, re.fullmatch(r"CI shard (\d+)/4: selected (\d+) of (\d+) tests", line))
        for i, line in enumerate(lines)
        if line.startswith("CI shard ")
    ]
    if len(markers) != 1 or markers[0][1] is None or int(markers[0][1][1]) != index + 1:
        raise ValueError("missing or mismatched shard marker")
    start, marker = markers[0]
    assert marker is not None
    selected = int(marker[2])
    all_failures: set[str] = set()
    summaries: list[dict] = []
    groups = [line for line in lines[:start] if line.startswith("##[group]")]
    if not groups or "Run python scripts/ci_pytest_shard.py " not in groups[-1]:
        raise ValueError("missing primary pytest execution step")
    # Actions starts each step with a group. Use those boundaries to attribute
    # terminal output to the shard or the known shard-zero auxiliary invocation.
    frames: list[list[str]] = [[]]
    active = True
    for line in lines[start + 1 :]:
        if re.fullmatch(r"##\[error\]Process completed with exit code \d+\.", line) or line == "Post job cleanup.":
            active = False
        elif line.startswith("##[group]"):
            active = line.startswith("##[group]Run output=$(python -m pytest tests/test_grokbot_mcp.py ")
            if active:
                frames.append([])
        elif active:
            if line and not line.startswith(("##[endgroup]", "##[error]")):
                frames[-1].append(line)
        elif SUMMARY.fullmatch(line) or line.startswith(("FAILED ", "ERROR", "SUBFAILED", "INTERNALERROR")):
            raise ValueError("pytest output outside an attributable execution step")
    for frame in frames:
        summary = SUMMARY.fullmatch(frame[-1]) if frame else None
        if summary is None:
            raise ValueError("missing valid terminal pytest summary")
        headers = [i for i, line in enumerate(frame[:-1]) if re.fullmatch(r"=+ short test summary info =+", line)]
        if not headers and any(re.match(r"-+ Captured (?:stdout|stderr|log) ", line) for line in frame):
            raise ValueError("captured output lacks a final short test summary")
        # Captured text and tracebacks precede the real short summary. A passing
        # run can omit that section, but must still have one terminal summary.
        section = frame[headers[-1] + 1 : -1] if headers else frame[:-1]
        if any(SUMMARY.fullmatch(line) for line in section):
            raise ValueError("ambiguous pytest summaries")
        failed: list[str] = []
        errored: list[str] = []
        subfailed_count = 0
        if headers:
            for line in section:
                outcome = outcome_nodeid(line)
                if outcome:
                    (failed if outcome[0] == "FAILED" else errored).append(outcome[1])
                    subfailed_count += int(line.startswith("SUBFAILED"))
        counts = {name: int(count) for count, name in COUNTS.findall(frame[-1])}
        if (
            not counts
            or counts.get("failed", 0) != len(failed)
            or counts.get("errors", counts.get("error", 0)) != len(errored)
        ):
            raise ValueError("summary failures lack attributable nodeids")
        outcomes = (
            sum(
                counts.get(name, 0) for name in ("passed", "failed", "skipped", "errors", "error", "xfailed", "xpassed")
            )
            - subfailed_count
        )
        if not summaries and (outcomes < selected or (selected == 0 and not counts.get("deselected"))):
            raise ValueError("summary does not account for selected tests")
        all_failures.update(failed + errored)
        summaries.append({"counts": counts, "elapsed_seconds": float(summary[1])})
    primary_counts = summaries[0]["counts"]
    primary_failed = any(primary_counts.get(name, 0) for name in ("failed", "error", "errors"))
    if index == 0 and not primary_failed and len(summaries) < 2:
        raise ValueError("missing shard zero auxiliary pytest summary")
    return {
        "failed_nodeids": sorted(all_failures),
        "counts": summaries[0]["counts"],
        "selected": selected,
        "pytest_elapsed_seconds": summaries[0]["elapsed_seconds"],
        "auxiliary_pytest_elapsed_seconds": [item["elapsed_seconds"] for item in summaries[1:]],
        "checkout_sha": sha,
    }


def parse_time(value: str) -> datetime:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return instant


def compare(
    identity: dict, run: dict, jobs: list[dict], logs: dict[int, str], selected: dict, *, errors: Sequence[str] = ()
) -> dict:
    problems = list(errors)
    if run.get("id") != identity["run_id"] or run.get("run_attempt") != identity["run_attempt"]:
        problems.append("workflow run ID or attempt mismatch")
    # PR Actions metadata may identify the PR head. Each full log must separately
    # prove that checkout tested the exact merge SHA, as the local shadow does.
    if run.get("head_sha") not in {identity["sha"], identity["pr_head"]} or run.get("event") != "pull_request":
        problems.append("workflow SHA or event mismatch")
    if not selected.get("complete") or selected.get("exit_code") not in (0, 1):
        problems.append("selected pytest evidence incomplete")
    elif (selected["exit_code"] == 1) != bool(selected.get("failed_nodeids")):
        problems.append("selected pytest exit and failure outcomes disagree")
    parsed = []
    full_failures: set[str] = set()
    starts, ends, walls = [], [], []
    for index, name in enumerate(SHARD_NAMES):
        matches = [job for job in jobs if job.get("name") == name]
        if len(matches) != 1:
            problems.append(f"{name}: expected exactly one completed job, found {len(matches)}")
            continue
        job = matches[0]
        # Missing job.run_attempt is allowed only because the jobs were fetched
        # from the explicit /attempts/N/jobs endpoint, never the latest endpoint.
        if (
            job.get("run_id") != identity["run_id"]
            or job.get("run_attempt", identity["run_attempt"]) != identity["run_attempt"]
            or job.get("head_sha") != run.get("head_sha")
        ):
            problems.append(f"{name}: job run, attempt or SHA mismatch")
        if job.get("status") != "completed" or job.get("conclusion") not in ("success", "failure"):
            problems.append(f"{name}: job is {job.get('status')}/{job.get('conclusion')}")
        try:
            start, end = parse_time(job["started_at"]), parse_time(job["completed_at"])
            if end < start:
                raise ValueError("negative job duration")
            starts.append(start)
            ends.append(end)
            walls.append((end - start).total_seconds())
            evidence = parse_full_log(logs[job["id"]], index, identity["sha"])
            if job.get("conclusion") == "failure" and not evidence["failed_nodeids"]:
                raise ValueError("failed job has no attributable pytest failure")
            full_failures.update(evidence["failed_nodeids"])
            parsed.append({"job_id": job["id"], "name": name, **evidence})
        except (KeyError, ValueError, TypeError) as exc:
            problems.append(f"{name}: {exc}")
    candidates = sorted(full_failures - set(selected.get("failed_nodeids", [])))
    result = incomplete(*problems)
    result.update(
        {
            "status": "incomplete" if problems else "complete",
            "full_only_candidates": candidates,
            "full_only_candidate_count": None if problems else len(candidates),
            "full_shards": parsed,
            "timing": {
                "selected_pytest_wall_seconds": selected.get("wall_seconds"),
                "selected_test_duration_seconds": selected.get("test_duration_seconds"),
                "full_pytest_elapsed_seconds_sum": sum(item["pytest_elapsed_seconds"] for item in parsed)
                if len(parsed) == 4
                else None,
                "full_job_wall_seconds_sum": sum(walls) if len(walls) == 4 else None,
                "full_parallel_wall_seconds": (max(ends) - min(starts)).total_seconds() if len(starts) == 4 else None,
                "full_test_duration_seconds": None,
                "historical_estimate_seconds": None,
            },
        }
    )
    return result


def run_shadow(repo: Path, output: Path, now: datetime | None) -> int:
    report = guard(output, now=now)
    if report["trial_status"] != "active":
        return 0
    try:
        identity = merge_identity(repo)
        identity.update(run_id=int(os.environ["GITHUB_RUN_ID"]), run_attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]))
        report["identity"] = identity
        write_json(output / "identity.json", identity)
        selection = select_tests(repo, identity["changed"])
        write_json(output / "selection.json", selection)
        shutil.copyfile(MANIFEST, output / "pr_test_tiers.json")
        (output / "selected.txt").write_text("".join(path + "\n" for path in selection["selected"]), encoding="utf-8")
        # Persist identity/selection before tests so interruptions leave evidence.
        retain_report(output, report)
        result = run_pytest(repo, selection["selected"], output)
        report["selected_pytest"] = result
        retain_report(output, report)
        return int(result["exit_code"] or (0 if result["complete"] else 2))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        report["comparison"] = incomplete(f"shadow execution unavailable: {type(exc).__name__}: {exc}")
        retain_report(output, report)
        return 2


def load_evidence(output: Path) -> tuple[dict, list[dict], dict[int, str], list[str]]:
    """Read an exact-attempt cache only. Missing/partial fetches remain gaps."""
    errors: list[str] = []

    def read_json(name: str, expected: type):
        try:
            value = json.loads((output / name).read_text(encoding="utf-8"))
            if not isinstance(value, expected):
                raise ValueError("invalid cached JSON shape")
            return value
        except (OSError, ValueError) as exc:
            errors.append(f"cached {name} unavailable: {type(exc).__name__}")
            return expected()

    run = read_json("workflow-run.json", dict)
    fetched = read_json("fetch-errors.json", dict)
    if not isinstance(fetched.get("errors"), list) or any(not isinstance(item, str) for item in fetched["errors"]):
        errors.append("cached fetch-errors.json lacks a valid fetch result")
    else:
        errors.extend(fetched["errors"])
    jobs: list[dict] = []
    logs: dict[int, str] = {}
    total = None
    for page in range(1, 21):
        batch = read_json(f"jobs-page-{page}.json", dict)
        if (
            not isinstance(batch.get("jobs"), list)
            or any(not isinstance(job, dict) for job in batch["jobs"])
            or type(batch.get("total_count")) is not int
            or batch["total_count"] < 0
            or (total is not None and total != batch["total_count"])
        ):
            errors.append("invalid cached jobs pagination")
            break
        total = batch["total_count"]
        jobs.extend(batch["jobs"])
        if len(jobs) == total:
            break
        if not batch["jobs"] or len(jobs) > total:
            errors.append("inconsistent cached jobs pagination")
            break
    else:
        errors.append("cached jobs pagination exceeded 20 pages")
    for job in jobs:
        if job.get("name") not in SHARD_NAMES:
            continue
        job_id = job.get("id")
        if type(job_id) is not int or job_id < 1:
            errors.append("invalid cached job ID")
            continue
        try:
            logs[job_id] = (output / f"job-{job_id}.log").read_text(encoding="utf-8-sig")
        except (OSError, ValueError) as exc:
            errors.append(f"{job['name']}: cached log unavailable: {type(exc).__name__}")
    return run, jobs, logs, errors


def finalize(output: Path, full_evidence: Path | None = None) -> int:
    # Retain the raw prefetch even if checkout, installation or pytest failed.
    cached = output / "full"
    copy_errors: list[str] = []
    if full_evidence is not None and full_evidence.absolute() != cached.absolute():
        try:
            if output.is_symlink():
                raise OSError("shadow output directory is a symlink")
            if cached.is_symlink() or (cached.exists() and not cached.is_dir()):
                cached.unlink()
            elif cached.exists():
                shutil.rmtree(cached)
            shutil.copytree(full_evidence, cached, symlinks=True)
        except OSError as exc:
            copy_errors.append(f"full evidence cache unavailable: {type(exc).__name__}")
    # Read the explicit source, so older retained output cannot fill its gaps.
    run, jobs, logs, errors = load_evidence(full_evidence if full_evidence is not None else cached)
    errors.extend(copy_errors)
    if not (output / "report.json").is_file():
        guard(output)
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    if report["trial_status"] != "active":
        retain_report(output, report)
        publish_summary(output)
        return 0
    try:
        identity = report["identity"]
        selected = json.loads((output / "pytest-results.json").read_text(encoding="utf-8"))
        report["comparison"] = compare(identity, run, jobs, logs, selected, errors=errors)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        report["comparison"] = incomplete(*errors, f"comparison unavailable: {type(exc).__name__}")
    retain_report(output, report)
    publish_summary(output)
    return 0 if report["comparison"]["status"] == "complete" else 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("guard", "run", "finalize", "pytest"))
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--now", type=parse_time, help="UTC clock override for offline reproduction")
    parser.add_argument(
        "--full-evidence", type=Path, help="prefetched exact-attempt directory; finalize never uses the network"
    )
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if args.command == "guard":
        guard(output, now=args.now)
        return 0
    if args.command == "run":
        return run_shadow(args.repo, output, args.now)
    if args.command == "finalize":
        return finalize(output, args.full_evidence)
    selected = json.loads((output / "pytest-input.json").read_text(encoding="utf-8"))["selected"]
    return pytest_worker(args.repo.resolve(), output, selected)


if __name__ == "__main__":
    raise SystemExit(main())
