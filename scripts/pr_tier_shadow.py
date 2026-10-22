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
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

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
    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary_path).open("a", encoding="utf-8") as stream:
            stream.write(summary)


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
            outcome["phases"].append({"when": report.when, "outcome": report.outcome, "duration": report.duration})
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
                    "test_duration_seconds": sum(phase["duration"] for item in outcomes for phase in item["phases"]),
                },
            )

    os.chdir(repo)
    return int(pytest.main(["-q", "-rA", "-o", "addopts=", "--", *selected], plugins=[Results()]))


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
    match = re.match(r"^(FAILED|ERROR) (tests/.*)$", line)
    if not match:
        if line.startswith(("FAILED ", "ERROR ")):
            raise ValueError("unparseable failure outcome")
        return None
    # Pytest's separator can also occur inside parametrized IDs.
    depth = 0
    text = match[2]
    for position, char in enumerate(text):
        if depth == 0 and text[position:].startswith(" - "):
            return match[1], text[:position]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
    if depth:
        raise ValueError("unparseable parametrized nodeid")
    return match[1], text


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
    failed: list[str] = []
    errored: list[str] = []
    all_failures: set[str] = set()
    summaries: list[dict] = []
    for line in lines[start + 1 :]:
        if "ERROR collecting" in line or "Interrupted:" in line or "INTERNALERROR" in line:
            raise ValueError("collection, interrupt or internal error")
        outcome = outcome_nodeid(line)
        if outcome:
            (failed if outcome[0] == "FAILED" else errored).append(outcome[1])
        summary = SUMMARY.search(line)
        if summary:
            counts = {name: int(count) for count, name in COUNTS.findall(line)}
            if (
                not counts
                or counts.get("failed", 0) != len(failed)
                or counts.get("errors", counts.get("error", 0)) != len(errored)
            ):
                raise ValueError("summary failures lack attributable nodeids")
            outcomes = sum(
                counts.get(name, 0) for name in ("passed", "failed", "skipped", "errors", "error", "xfailed", "xpassed")
            )
            if not summaries and (outcomes < selected or (selected == 0 and not counts.get("deselected"))):
                raise ValueError("summary does not account for selected tests")
            all_failures.update(failed + errored)
            summaries.append({"counts": counts, "elapsed_seconds": float(summary[1])})
            failed, errored = [], []
    if not summaries or failed or errored:
        raise ValueError("missing valid terminal pytest summary")
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


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api_get(path: str) -> bytes:
    """GET only. Never forward the API token to the signed log-download host."""
    token = os.environ.get("GH_TOKEN", "")
    request = Request(
        "https://api.github.com" + path,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=60) as response:
            return response.read()
    except HTTPError as exc:
        if exc.code != 302 or not path.endswith("/logs"):
            # The error text intentionally omits signed URLs and response bodies.
            raise OSError(f"GitHub API HTTP {exc.code}") from None
        location = exc.headers.get("Location", "")
        if urlparse(location).scheme != "https":
            raise OSError("invalid log-download redirect") from None
        with urlopen(Request(location), timeout=60) as response:
            return response.read()


def fetch_evidence(
    repository: str, run_id: int, attempt: int, output: Path
) -> tuple[dict, list[dict], dict[int, str], list[str]]:
    run: dict = {}
    jobs: list[dict] = []
    logs: dict[int, str] = {}
    errors: list[str] = []
    output.mkdir(parents=True, exist_ok=True)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or run_id < 1 or attempt < 1:
        return run, jobs, logs, ["invalid repository, run ID or attempt"]
    prefix = f"/repos/{repository}/actions/runs/{run_id}/attempts/{attempt}"
    try:
        run = json.loads(api_get(prefix))
        write_json(output / "workflow-run.json", run)
        for page in range(1, 21):
            batch = json.loads(api_get(f"{prefix}/jobs?per_page=100&page={page}"))
            write_json(output / f"jobs-page-{page}.json", batch)
            jobs.extend(batch["jobs"])
            if len(jobs) == batch["total_count"]:
                break
            if not batch["jobs"] or len(jobs) > batch["total_count"]:
                raise ValueError("inconsistent jobs pagination")
        else:
            raise ValueError("jobs pagination exceeded 20 pages")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(f"exact attempt metadata unavailable: {type(exc).__name__}")
    for job in jobs:
        if job.get("name") not in SHARD_NAMES:
            continue
        try:
            job_id = int(job["id"])
            text = api_get(f"/repos/{repository}/actions/jobs/{job_id}/logs").decode("utf-8-sig")
            (output / f"job-{job_id}.log").write_text(text, encoding="utf-8")
            logs[job_id] = text
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"{job.get('name')}: log unavailable: {type(exc).__name__}")
    write_json(output / "fetch-errors.json", {"errors": errors})
    return run, jobs, logs, errors


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


def finalize(output: Path) -> int:
    if not (output / "report.json").is_file():
        guard(output)
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    if report["trial_status"] != "active":
        retain_report(output, report)
        return 0
    try:
        identity = report["identity"]
        selected = json.loads((output / "pytest-results.json").read_text(encoding="utf-8"))
        run, jobs, logs, errors = fetch_evidence(
            os.environ["GITHUB_REPOSITORY"], identity["run_id"], identity["run_attempt"], output / "full"
        )
        report["comparison"] = compare(identity, run, jobs, logs, selected, errors=errors)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        report["comparison"] = incomplete(f"comparison unavailable: {type(exc).__name__}")
    retain_report(output, report)
    return 0 if report["comparison"]["status"] == "complete" else 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("guard", "run", "finalize", "pytest"))
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--now", type=parse_time, help="UTC clock override for offline reproduction")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if args.command == "guard":
        guard(output, now=args.now)
        return 0
    if args.command == "run":
        return run_shadow(args.repo, output, args.now)
    if args.command == "finalize":
        return finalize(output)
    selected = json.loads((output / "pytest-input.json").read_text(encoding="utf-8"))["selected"]
    return pytest_worker(args.repo.resolve(), output, selected)


if __name__ == "__main__":
    raise SystemExit(main())
