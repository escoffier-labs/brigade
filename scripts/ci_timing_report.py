#!/usr/bin/env python3
"""Measure CI wall time and runner time for one workflow, and test-suite size.

Timing mode reads completed runs of one workflow through the ``gh`` CLI and
writes a raw JSON report plus an optional Markdown summary. Inventory mode
(``--inventory``) records test-suite size at the checked-out commit and makes
no network calls.

Definitions (the campaign doc, docs/phase-adaptive-ci.md, repeats these):

* A run is in the window when its ``created_at`` is within ``--since`` and
  ``--until`` (inclusive, UTC) and its status is ``completed``. Cancelled and
  failed runs are included in the "all" aggregates and excluded from the
  "successful" ones.
* ``wall_minutes`` is ``run_started_at`` to ``updated_at`` of the latest
  attempt. It excludes time spent waiting in a queue before the run started.
* ``runner_minutes`` is the sum of ``completed_at - started_at`` over the
  latest attempt's jobs that were not skipped. Cancelled jobs count, because
  they held a runner.
* Percentiles use linear interpolation between order statistics (the same
  definition as numpy's default and ``statistics.quantiles(method="inclusive")``).
* A job family normalizes matrix names: a trailing integer shard index is
  dropped (``test shard (3.12, 1)`` becomes ``test shard 3.12``) and a
  free-text argument group is dropped (``install-from-source (x, --depth y)``
  becomes ``install-from-source``). Other names are kept whole.

Output holds run ids, SHAs, events, job names and numbers only. A job name
containing a retired engine name is stored with that name replaced by
``retired-name``. It records no
user names, branch names or titles. Two invocations over the same window give
identical JSON apart from ``generated_at``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
PER_PAGE = 100
SLICE_HOURS = 6
SEARCH_RESULT_CAP = 1000  # the Actions runs API stops returning rows past this
FETCH_WORKERS = 4
RATE_LIMIT_SLEEP_SECONDS = 60
_ISO = "%Y-%m-%dT%H:%M:%SZ"
# The repository's retired-name check rejects these spellings in tracked files,
# and a CI job name can still carry one. Recorded names get a neutral stand-in.
_RETIRED_NAME = re.compile(r"graph[ _-]?trail|mise[ _-]?ledger", re.IGNORECASE)
# gh failures worth a short retry: server errors and dropped HTTP/2 or TCP streams.
_TRANSIENT = re.compile(
    r"http 5\d\d|stream error|connection reset|unexpected eof|timed out|timeout|temporarily unavailable"
)


# --------------------------------------------------------------------------
# Pure helpers (unit tested)
# --------------------------------------------------------------------------


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 UTC timestamp such as ``2026-11-09T12:00:00Z``."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolation percentile, ``q`` in [0, 100]. None for no data."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def job_family(name: str) -> str:
    match = re.fullmatch(r"(.*?) \((.*)\)", name)
    if not match:
        return name
    base, group = match.group(1), match.group(2)
    parts = [part.strip() for part in group.split(",")]
    if parts[-1].isdigit():
        rest = parts[:-1]
        return f"{base} {', '.join(rest)}" if rest else base
    if " --" in group or group.startswith("--"):
        return base
    return name


def os_of(runner: str | None) -> str:
    label = (runner or "").lower()
    if label.startswith("windows"):
        return "windows"
    if label.startswith("macos"):
        return "macos"
    if label.startswith("ubuntu") or label.startswith("linux"):
        return "linux"
    if label == "self-hosted":
        return "self-hosted"
    return "other"


def record_name(name: str) -> str:
    """Job name as stored: retired engine names are replaced by ``retired-name``."""
    return _RETIRED_NAME.sub("retired-name", name)


def job_seconds(job: dict[str, Any]) -> int | None:
    """Duration of one job, or None when it did not run on a runner."""
    if job.get("conclusion") == "skipped":
        return None
    started, completed = job.get("started_at"), job.get("completed_at")
    if not started or not completed:
        return None
    return max(0, round((parse_ts(completed) - parse_ts(started)).total_seconds()))


def summarize_run(run: dict[str, Any], jobs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Reduce one API run and its jobs to the recorded fields."""
    recorded: list[dict[str, Any]] = []
    skipped = 0
    for job in jobs:
        seconds = job_seconds(job)
        if seconds is None:
            skipped += 1
            continue
        labels = job.get("labels") or []
        recorded.append(
            {
                "name": record_name(job["name"]),
                "runner": labels[0] if labels else None,
                "seconds": seconds,
                "conclusion": job.get("conclusion"),
            }
        )
    recorded.sort(key=lambda j: (j["name"], j["seconds"], str(j["conclusion"])))
    started = run.get("run_started_at") or run["created_at"]
    wall = (parse_ts(run["updated_at"]) - parse_ts(started)).total_seconds() / 60.0
    total = sum(j["seconds"] for j in recorded)
    windows = sum(j["seconds"] for j in recorded if os_of(j["runner"]) == "windows")
    return {
        "id": run["id"],
        "event": run["event"],
        "conclusion": run.get("conclusion"),
        "head_sha": run.get("head_sha"),
        "run_attempt": run.get("run_attempt", 1),
        "created_at": run["created_at"],
        "wall_minutes": round(max(0.0, wall), 3),
        "runner_minutes": round(total / 60.0, 3),
        "windows_runner_minutes": round(windows / 60.0, 3),
        "job_count": len(recorded),
        "skipped_jobs": skipped,
        "jobs": recorded,
    }


def _stats(values: Sequence[float], keys: Sequence[tuple[str, float]]) -> dict[str, Any]:
    return {name: _round(percentile(values, q)) for name, q in keys}


def aggregate_group(runs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregates for one set of runs (one event, successful or all)."""
    count = len(runs)
    wall = [r["wall_minutes"] for r in runs]
    runner = [r["runner_minutes"] for r in runs]
    windows = [r["windows_runner_minutes"] for r in runs]
    by_os: dict[str, list[float]] = {}
    for run in runs:
        totals: Counter[str] = Counter()
        for job in run["jobs"]:
            totals[os_of(job["runner"])] += job["seconds"]
        for name in ("linux", "windows", "macos", "self-hosted", "other"):
            by_os.setdefault(name, []).append(totals[name] / 60.0)
    seconds_by_family: dict[str, list[int]] = {}
    runs_with: Counter[str] = Counter()
    runner_label: dict[str, Counter[str]] = {}
    for run in runs:
        seen: set[str] = set()
        for job in run["jobs"]:
            family = job_family(job["name"])
            seconds_by_family.setdefault(family, []).append(job["seconds"])
            runner_label.setdefault(family, Counter())[str(job["runner"])] += 1
            seen.add(family)
        runs_with.update(seen)
    families = {
        family: {
            "instances": len(secs),
            "runs_with": runs_with[family],
            "ran_fraction": _round(runs_with[family] / count, 3),
            "runner": sorted(runner_label[family].items(), key=lambda kv: (-kv[1], kv[0]))[0][0],
            "median_seconds": _round(percentile(secs, 50), 1),
            "p90_seconds": _round(percentile(secs, 90), 1),
            "max_seconds": max(secs),
        }
        for family, secs in sorted(seconds_by_family.items())
    }
    return {
        "runs": count,
        "wall_minutes": {
            **_stats(wall, (("median", 50), ("p75", 75), ("p90", 90))),
            "max": _round(max(wall)) if wall else None,
        },
        "runner_minutes": {
            **_stats(runner, (("median", 50), ("p75", 75), ("p90", 90))),
            "max": _round(max(runner)) if runner else None,
            "total": _round(sum(runner)),
        },
        "windows_runner_minutes": {
            **_stats(windows, (("median", 50), ("p90", 90))),
            "total": _round(sum(windows)),
        },
        "runner_minutes_by_os_median": {name: _round(percentile(v, 50)) for name, v in sorted(by_os.items())},
        "job_count_median": _round(percentile([r["job_count"] for r in runs], 50), 1),
        "job_families": families,
    }


def aggregate(runs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregates per event, with successful-only and all-runs views."""
    events = sorted({r["event"] for r in runs})
    result: dict[str, Any] = {}
    for event in events:
        in_event = [r for r in runs if r["event"] == event]
        result[event] = {
            "successful": aggregate_group([r for r in in_event if r["conclusion"] == "success"]),
            "all": aggregate_group(in_event),
            "conclusions": dict(sorted(Counter(str(r["conclusion"]) for r in in_event).items())),
            "rerun_attempts": sum(1 for r in in_event if r["run_attempt"] > 1),
        }
    return result


# --------------------------------------------------------------------------
# GitHub access
# --------------------------------------------------------------------------


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def gh_api(path: str, *, sleep: Callable[[float], None] = time.sleep) -> Any:
    """GET one API path through gh.

    Retries once after a rate-limit response (sleeping a minute) and up to five
    times after a transient server or connection error (short backoff).
    """
    rate_limit_retried = False
    server_retries = 0
    while True:
        proc = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            return json.loads(proc.stdout)
        text = (proc.stderr + proc.stdout).lower()
        route = path.split("?")[0]
        if not rate_limit_retried and ("rate limit" in text or "http 429" in text or "secondary" in text):
            rate_limit_retried = True
            _log(f"rate limited on {route}; sleeping {RATE_LIMIT_SLEEP_SECONDS}s then retrying once")
            sleep(RATE_LIMIT_SLEEP_SECONDS)
        elif server_retries < 5 and _TRANSIENT.search(text):
            server_retries += 1
            _log(f"server error on {route}; retry {server_retries}/5")
            sleep(5 * server_retries)
        else:
            raise RuntimeError(f"gh api failed for {route}: {proc.stderr.strip() or proc.stdout.strip()}")


def window_slices(since: datetime, until: datetime) -> list[tuple[datetime, datetime]]:
    """Split a window so each query stays under the API's 1000 row cap."""
    slices = []
    cursor = since
    step = timedelta(hours=SLICE_HOURS)
    while cursor < until:
        end = min(cursor + step, until)
        slices.append((cursor, end))
        cursor = end
    return slices or [(since, until)]


def fetch_runs(repo_path: str, workflow: str, since: datetime, until: datetime) -> list[dict[str, Any]]:
    by_id: dict[int, dict[str, Any]] = {}
    for start, end in window_slices(since, until):
        created = f"{start.strftime(_ISO)}..{end.strftime(_ISO)}"
        got = 0
        for page in range(1, SEARCH_RESULT_CAP // PER_PAGE + 1):
            path = (
                f"{repo_path}/actions/workflows/{workflow}/runs"
                f"?created={created}&status=completed&per_page={PER_PAGE}&page={page}"
            )
            rows = gh_api(path).get("workflow_runs", [])
            got += len(rows)
            for row in rows:
                by_id[row["id"]] = row
            if len(rows) < PER_PAGE:
                break
        if got >= SEARCH_RESULT_CAP:
            _log(f"warning: slice {created} hit the {SEARCH_RESULT_CAP} row cap; runs may be missing")
        _log(f"listed {len(by_id)} runs through {end.strftime(_ISO)}")
    return [by_id[i] for i in sorted(by_id) if since <= parse_ts(by_id[i]["created_at"]) <= until]


def fetch_jobs(repo_path: str, run_id: int) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    page = 1
    while True:
        data = gh_api(f"{repo_path}/actions/runs/{run_id}/jobs?filter=latest&per_page={PER_PAGE}&page={page}")
        batch = data.get("jobs", [])
        jobs.extend(batch)
        if len(batch) < PER_PAGE or len(jobs) >= data.get("total_count", 0):
            return jobs
        page += 1


def collect_runs(repo_path: str, workflow: str, since: datetime, until: datetime) -> list[dict[str, Any]]:
    listed = fetch_runs(repo_path, workflow, since, until)
    total = len(listed)
    _log(f"fetching jobs for {total} runs")
    done = 0

    def one(run: dict[str, Any]) -> dict[str, Any]:
        return summarize_run(run, fetch_jobs(repo_path, run["id"]))

    summaries = []
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        for summary in pool.map(one, listed):
            summaries.append(summary)
            done += 1
            if done % 25 == 0 or done == total:
                _log(f"jobs {done}/{total}")
    return sorted(summaries, key=lambda r: (r["created_at"], r["id"]))


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def dumps(value: Any, level: int = 0) -> str:
    """Deterministic JSON: sorted keys, small scalar-only objects on one line."""
    pad = "  " * (level + 1)
    end = "  " * level
    if isinstance(value, dict):
        if not value:
            return "{}"
        if len(value) <= 8 and all(not isinstance(v, (dict, list)) for v in value.values()):
            return json.dumps(value, sort_keys=True)
        items = [f"{pad}{json.dumps(k)}: {dumps(v, level + 1)}" for k, v in sorted(value.items())]
        return "{\n" + ",\n".join(items) + f"\n{end}}}"
    if isinstance(value, list):
        if not value:
            return "[]"
        if all(not isinstance(v, (dict, list)) for v in value):
            return json.dumps(value)
        return "[\n" + ",\n".join(f"{pad}{dumps(v, level + 1)}" for v in value) + f"\n{end}]"
    return json.dumps(value)


def build_report(
    runs: Sequence[dict[str, Any]], *, workflow: str, since: str, until: str, label: str | None, repo: str
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime(_ISO),
        "repo": repo,
        "workflow": workflow,
        "window": {"since": since, "until": until},
        "label": label,
        "run_count": len(runs),
        "aggregates": aggregate(runs),
        "runs": list(runs),
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def render_markdown(report: dict[str, Any], *, top_families: int = 12) -> str:
    window = report["window"]
    lines = [
        f"# CI timing: {report['workflow']}",
        "",
        f"- Window (run created_at, UTC): {window['since']} to {window['until']}",
        f"- Runs: {report['run_count']} completed",
    ]
    if report.get("label"):
        lines.append(f"- Label: {report['label']}")
    lines += ["- Generated: " + report["generated_at"], ""]
    for event, data in report["aggregates"].items():
        lines += [f"## {event}", "", "Conclusions: " + ", ".join(f"{k} {v}" for k, v in data["conclusions"].items())]
        lines += [f"Runs with a re-run attempt: {data['rerun_attempts']}", ""]
        lines += [
            "| runs | n | wall min median | p75 | p90 | max | runner min median | p90 | Windows runner min median | p90 |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for view in ("successful", "all"):
            g = data[view]
            w, r, x = g["wall_minutes"], g["runner_minutes"], g["windows_runner_minutes"]
            cells = [
                view,
                g["runs"],
                w["median"],
                w["p75"],
                w["p90"],
                w["max"],
                r["median"],
                r["p90"],
                x["median"],
                x["p90"],
            ]
            lines.append("| " + " | ".join(_fmt(c) for c in cells) + " |")
        families = data["successful"]["job_families"]
        ranked = sorted(families.items(), key=lambda kv: (-(kv[1]["p90_seconds"] or 0), kv[0]))[:top_families]
        lines += [
            "",
            f"Job families in successful runs, top {len(ranked)} by p90 seconds:",
            "",
            "| family | runner | ran in | median s | p90 s | max s |",
            "|---|---|---|---|---|---|",
        ]
        for family, f in ranked:
            lines.append(
                f"| {family} | {f['runner']} | {f['runs_with']}/{data['successful']['runs']} "
                f"| {_fmt(f['median_seconds'])} | {_fmt(f['p90_seconds'])} | {f['max_seconds']} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------

_TEST_DEF = re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+test_\w*[ \t]*\(", re.MULTILINE)
_COLLECTED = re.compile(r"(\d+)(?:/(\d+))? tests? collected")


def count_lines(path: Path) -> int:
    data = path.read_bytes()
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def parse_collect_output(text: str) -> dict[str, int]:
    """Read the item count from the last line of ``pytest --collect-only -q``."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("collect-only output is empty")
    match = _COLLECTED.search(lines[-1])
    if not match:
        raise ValueError(f"last line is not a pytest collection summary: {lines[-1]!r}")
    selected = int(match.group(1))
    total = int(match.group(2)) if match.group(2) else selected
    return {"collected": selected, "collected_total": total}


def collect_inventory(root: Path, collect_text: str | None) -> dict[str, Any]:
    tests_dir, src_dir = root / "tests", root / "src" / "brigade"
    test_files = sorted(tests_dir.rglob("test_*.py"))
    test_loc = sum(count_lines(p) for p in tests_dir.rglob("*.py"))
    src_loc = sum(count_lines(p) for p in src_dir.rglob("*.py"))
    defs = sum(len(_TEST_DEF.findall(p.read_text(encoding="utf-8", errors="replace"))) for p in test_files)
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    inventory: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime(_ISO),
        "commit": commit,
        "test_files": len(test_files),
        "test_defs": defs,
        "test_loc": test_loc,
        "src_loc": src_loc,
        "test_to_src_loc_ratio": round(test_loc / src_loc, 3) if src_loc else None,
        "loc_definition": "physical lines of every .py file under tests/ and src/brigade/",
    }
    if collect_text is not None:
        inventory.update(parse_collect_output(collect_text))
    return inventory


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workflow", default="ci.yml", help="workflow file name (default ci.yml)")
    p.add_argument("--since", help="window start, ISO UTC, filters run created_at")
    p.add_argument("--until", help="window end, ISO UTC, filters run created_at")
    p.add_argument("--output", required=True, help="path of the JSON report")
    p.add_argument("--markdown", help="path of the Markdown summary (timing mode)")
    p.add_argument("--label", help="free-text label stored in the report")
    p.add_argument("--aggregates-only", action="store_true", help="omit the per-run list from the JSON (smaller file)")
    p.add_argument("--repo", help="owner/name; default is the gh working-directory repo")
    p.add_argument("--inventory", action="store_true", help="record test-suite size instead of CI timings")
    p.add_argument("--collect-output", help="saved `pytest --collect-only -q` output (inventory mode)")
    p.add_argument("--root", default=".", help="checkout to measure in inventory mode")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.inventory:
        text = Path(args.collect_output).read_text(encoding="utf-8") if args.collect_output else None
        out.write_text(dumps(collect_inventory(Path(args.root), text)) + "\n", encoding="utf-8")
        _log(f"wrote {out}")
        return 0
    if not args.since or not args.until:
        build_parser().error("--since and --until are required in timing mode")
    since, until = parse_ts(args.since), parse_ts(args.until)
    if since >= until:
        build_parser().error("--since must be before --until")
    repo_path = f"repos/{args.repo}" if args.repo else "repos/{owner}/{repo}"
    repo = (
        args.repo
        or json.loads(
            subprocess.run(
                ["gh", "repo", "view", "--json", "nameWithOwner"], capture_output=True, text=True, check=True
            ).stdout
        )["nameWithOwner"]
    )
    runs = collect_runs(repo_path, args.workflow, since, until)
    report = build_report(
        runs,
        workflow=args.workflow,
        since=since.strftime(_ISO),
        until=until.strftime(_ISO),
        label=args.label,
        repo=repo,
    )
    stored = {k: v for k, v in report.items() if k != "runs"} if args.aggregates_only else report
    out.write_text(dumps(stored) + "\n", encoding="utf-8")
    if args.markdown:
        Path(args.markdown).write_text(render_markdown(report), encoding="utf-8")
    _log(f"wrote {out} ({len(runs)} runs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
