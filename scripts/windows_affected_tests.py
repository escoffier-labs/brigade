#!/usr/bin/env python3
"""Select Windows tests from a base/head diff, using the checked-out head tree.

Directly changed tests and static import matches are never capped. Shared
infrastructure (including test helpers and source data) adds 24 tests ordered by SHA256 of their portable relative name,
then name. This stable sample avoids the alphabetic bias of the first 24 files
without turning infrastructure-only PRs into another full nightly run. Dynamic
imports and transitive dependencies remain the nightly's responsibility.

A large selection is split across up to MAX_SHARDS hosted Windows runners. The
split only moves files between runners. Every selected file still runs exactly
once. Weights come from measured per-file seconds in scripts/windows_test_weights.json
and affect balance and run order only, never which files run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Sequence

# The workflow runs this file directly, while tests import it as a module.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Re-exported: scripts/pr_tier_selection.py and the tests import these names from here.
from scripts.ci_selection_core import discover_tests, imports_changed, module_name  # noqa: F401

BROAD_SAMPLE_SIZE = 24
SHARED_PATHS = {
    "pyproject.toml",
    "scripts/windows_pytest.py",
    "scripts/windows_pytest_diagnostics.py",
    "scripts/windows_affected_tests.py",
    "scripts/ci_selection_core.py",
    "tests/windows_pytest_allowlist.json",
    "src/brigade/__init__.py",
    ".github/workflows/ci.yml",
}
# Driver changes exercise the hub files affected by slow FULL flushes.
DRIVER_PATH = "scripts/windows_pytest.py"
FLUSH_TESTS = (
    "test_fleet_policy_page.py",
    "test_fleet_roster_page.py",
    "test_fleet_message_grants.py",
    "test_fleet_model_roster.py",
    "test_fleet_enrollment.py",
    "test_fleet_dashboard.py",
    "test_fleet_claim_generations.py",
    "test_fleet_client_message_credentials.py",
)

# Sharding. A shard is one hosted Windows runner with its own cores.
# Serial files run alone, so they cost wall time. Parallel files overlap at the
# effective concurrency measured on 18 large PR runs (5.2 of 6 workers).
# Keep SERIAL_FILES equal to the --serial arguments in ci.yml.
SERIAL_FILES = frozenset({"test_runs_serve.py", "test_aboyeur.py"})
PARALLEL_SPEEDUP = 5.2
SHARD_TARGET_SECONDS = 480
MAX_SHARDS = 3
DEFAULT_WEIGHTS = Path(__file__).resolve().with_name("windows_test_weights.json")
WEIGHT_THRESHOLD_SECONDS = 10


def select_tests(repo: Path, changed: Sequence[str]) -> list[str]:
    files = discover_tests(repo)
    changed_set = set(changed)
    selected = {name for name in files if f"tests/{name}" in changed_set}
    modules = {module for path in changed if (module := module_name(path)) is not None}
    if modules:
        selected.update(
            name for name, path in files.items() if imports_changed(path.read_text(encoding="utf-8"), modules)
        )
    shared = changed_set & SHARED_PATHS or any(
        PurePosixPath(path).name == "conftest.py"
        or (path.startswith("tests/") and not (PurePosixPath(path).name.startswith("test_") and path.endswith(".py")))
        or (path.startswith("src/brigade/") and not path.endswith(".py"))
        for path in changed
    )
    if shared:
        sampled = sorted(files, key=lambda name: (hashlib.sha256(name.encode()).hexdigest(), name))
        selected.update(sampled[:BROAD_SAMPLE_SIZE])
    if DRIVER_PATH in changed_set:
        selected.update(name for name in FLUSH_TESTS if name in files)
    return sorted(selected)


def load_weights(path: Path) -> tuple[dict[str, float], float]:
    """Return measured seconds per test file and the default for unlisted files."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        seconds = {str(name): float(value) for name, value in payload["seconds"].items()}
        return seconds, float(payload["default_seconds"])
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        # A missing or damaged table only costs balance, never coverage.
        return {}, 5.0


def shard_count(costs: dict[str, float]) -> int:
    return max(1, min(MAX_SHARDS, math.ceil(sum(costs.values()) / SHARD_TARGET_SECONDS)))


def plan_shards(selected: Sequence[str], weights: dict[str, float], default: float) -> list[list[str]]:
    """Split selected files across runners, balancing estimated wall seconds.

    Longest-processing-time assignment. Each shard lists its heaviest files
    first, which is the order the driver starts them with --keep-order.
    """
    if not selected:
        return []
    seconds = {name: weights.get(name, default) for name in selected}
    cost = {name: seconds[name] if name in SERIAL_FILES else seconds[name] / PARALLEL_SPEEDUP for name in selected}
    # Never more shards than files: an empty shard would fail the driver.
    count = min(shard_count(cost), len(selected))
    loads = [0.0] * count
    shards: list[list[str]] = [[] for _ in range(count)]
    for name in sorted(selected, key=lambda name: (-cost[name], name)):
        index = min(range(count), key=lambda i: (loads[i], i))
        loads[index] += cost[name]
        shards[index].append(name)
    return [sorted(shard, key=lambda name: (-seconds[name], name)) for shard in shards]


def build_weights(summaries: Sequence[Path], threshold: float = WEIGHT_THRESHOLD_SECONDS) -> dict[str, object]:
    """Mean measured seconds per file from driver summary.json records."""
    observed: dict[str, list[float]] = {}
    for path in summaries:
        for row in json.loads(path.read_text(encoding="utf-8"))["results"]:
            if row["status"] in {"passed", "failed", "timeout"}:
                observed.setdefault(row["name"], []).append(float(row["seconds"]))
    means = {name: statistics.mean(values) for name, values in observed.items()}
    light = [value for value in means.values() if value < threshold]
    return {
        "schema": "brigade.windows_test_weights.v1",
        "default_seconds": round(statistics.mean(light), 1) if light else 5.0,
        "seconds": {name: round(value) for name, value in sorted(means.items()) if value >= threshold},
    }


def refresh_weights(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="windows_affected_tests.py weights", description=build_weights.__doc__)
    parser.add_argument("summaries", type=Path, nargs="+", help="driver summary.json records")
    parser.add_argument("--output", type=Path, default=DEFAULT_WEIGHTS)
    args = parser.parse_args(argv)
    payload = build_weights(args.summaries)
    args.output.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    print(f"windows test weights: files={len(payload['seconds'])} default={payload['default_seconds']}")  # type: ignore[arg-type]
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "weights":
        return refresh_weights(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--weights", type=Path, default=DEFAULT_WEIGHTS)
    parser.add_argument(
        "--plan",
        type=Path,
        help="also write the shard plan as JSON: {count, matrix: {include: [{shard, total, files}]}}",
    )
    args = parser.parse_args(argv)
    # --no-renames makes moved tests additions and removals, with no rename threshold ambiguity.
    diff = subprocess.run(
        ["git", "-C", str(args.repo), "diff", "--no-renames", "--name-only", "-z", args.base, args.head, "--"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    selected = select_tests(args.repo, [path for path in diff.stdout.split("\0") if path])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(name + "\n" for name in selected), encoding="utf-8")
    print(f"windows affected tests: files={len(selected)}")
    for name in selected:
        print(name)
    if args.plan is not None:
        weights, default = load_weights(args.weights)
        shards = plan_shards(selected, weights, default)
        plan = {
            "count": len(selected),
            "matrix": {
                "include": [
                    {"shard": index, "total": max(1, len(shards)), "files": "".join(name + "\n" for name in shard)}
                    # An empty selection keeps one inert entry so the matrix never expands to nothing.
                    for index, shard in enumerate(shards or [[]], 1)
                ]
            },
        }
        args.plan.parent.mkdir(parents=True, exist_ok=True)
        args.plan.write_text(json.dumps(plan), encoding="utf-8")
        print(f"windows affected shards: {len(shards)} sizes={[len(shard) for shard in shards]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
