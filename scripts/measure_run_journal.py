#!/usr/bin/env python3
"""Measure journal-authority append latency across N real run directories.

Drives each run directory through the real Brigade journal-authority path
(``run_lifecycle`` + ``run_checkpoint`` under a real ``runguard.run_lock``),
records per-append wall-clock latency with ``time.perf_counter_ns``, and
reports deterministic integer percentiles plus environment metadata.

This is a measurement harness only: it carries no acceptance threshold and
no pass/fail verdict. Standard library plus the in-repo ``brigade`` package;
no third-party runtime dependencies.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from brigade import run_checkpoint, run_lifecycle, runguard

_MEASURED_STATUS = "planning"

_MOUNTPOINT_ESCAPE_RE = re.compile(r"\\([0-7]{3})")


def _bootstrap_run(run_dir: Path) -> bytes:
    """Write the minimal canonical run.json carrying both durable request fields.

    Returns the writer-canonical base bytes (the same bytes on disk), which the
    base-stripped checkpoint path requires to be canonical and to carry both
    durable request fields with no journal-derived metadata fields.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    base = {
        "lifecycle_journal_requested": True,
        "run_journal_authority_requested": True,
        "status": "started",
        "task": "journal-authority measurement",
    }
    base_bytes = (json.dumps(base, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (run_dir / "run.json").write_bytes(base_bytes)
    return base_bytes


def _measure_run(run_dir: Path, workspace: Path) -> dict[str, Any]:
    """Drive one run directory through the journal-authority append path.

    Bootstraps with both durable request fields, activates journal authority
    under a real ``runguard.run_lock``, appends one base-stripped checkpoint
    event and one mapped lifecycle status event, timing each append call with
    ``time.perf_counter_ns``.
    """
    base_bytes = _bootstrap_run(run_dir)
    latencies: list[int] = []
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        start = time.perf_counter_ns()
        run_checkpoint.write_checkpoint(
            run_dir,
            base_bytes,
            workspace=workspace,
            paired_event_type=run_lifecycle.STATUS_EVENT_TYPE[_MEASURED_STATUS],
            body_kind=run_checkpoint._BODY_KIND_BASE_STRIPPED,
        )
        latencies.append(time.perf_counter_ns() - start)
        start = time.perf_counter_ns()
        run_lifecycle.record_lifecycle_transition(
            run_dir,
            status=_MEASURED_STATUS,
            workspace=workspace,
        )
        latencies.append(time.perf_counter_ns() - start)
    journal_bytes = (run_dir / "events" / "lifecycle.jsonl").stat().st_size
    return {"journal_bytes": journal_bytes, "append_latencies_ns": latencies}


def _percentile_nearest_rank(sorted_values: list[int], percentile: float) -> int:
    """Deterministic nearest-rank percentile on an ascending-sorted nonempty list.

    rank = ceil(p/100 * n), clamped to [1, n]; the value at rank-1 is returned.
    """
    n = len(sorted_values)
    rank = math.ceil(percentile / 100 * n)
    index = max(0, min(n - 1, rank - 1))
    return int(sorted_values[index])


def _decode_mountpoint(raw: str) -> str:
    """Decode mountinfo octal escapes (e.g. ``\\040`` for space) in a mountpoint."""
    return _MOUNTPOINT_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 8)), raw)


def _filesystem_type(root: Path) -> str:
    """Filesystem type hosting ``root``, from /proc/self/mountinfo.

    Picks the deepest mount whose mountpoint is a path-prefix of the resolved
    root directory. Returns "unknown" when mountinfo is absent or unmatched.
    """
    mountinfo = Path("/proc/self/mountinfo")
    try:
        lines = mountinfo.read_text().splitlines()
    except OSError:
        return "unknown"
    resolved = root.expanduser().resolve()
    best_fs: str | None = None
    best_depth = -1
    for line in lines:
        fields, separator, tail = line.partition(" - ")
        if not separator:
            continue
        parts = fields.split()
        if len(parts) < 5:
            continue
        mountpoint = Path(_decode_mountpoint(parts[4]))
        try:
            resolved.relative_to(mountpoint)
        except ValueError:
            continue
        depth = len(mountpoint.parts)
        if depth > best_depth:
            best_depth = depth
            best_fs = tail.split(maxsplit=1)[0] if tail.split() else None
    return best_fs if best_fs is not None else "unknown"


def _directory_fsync_supported(root: Path) -> bool:
    """True when a directory under ``root`` can be opened and fsynced."""
    probe = Path(tempfile.mkdtemp(prefix="dir-fsync-probe-", dir=root))
    try:
        fd = os.open(probe, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        except OSError:
            return False
        finally:
            os.close(fd)
        return True
    except OSError:
        return False
    finally:
        shutil.rmtree(probe, ignore_errors=True)


def _environment(root: Path) -> dict[str, Any]:
    return {
        "python_version": sys.version,
        "platform": platform.platform(),
        "filesystem_type": _filesystem_type(root),
        "directory_fsync_supported": _directory_fsync_supported(root),
    }


def measure_runs(runs: int, root: Path) -> dict[str, Any]:
    """Drive ``runs`` run directories under ``root`` through journal authority.

    Returns a deterministic-schema report dict with keys ``runs``,
    ``per_run``, ``aggregate``, and ``environment``. No pass/fail verdict or
    acceptance threshold is included.
    """
    if isinstance(runs, bool) or not isinstance(runs, int):
        raise TypeError("runs must be an integer")
    if runs <= 0:
        raise ValueError("runs must be a positive integer")
    if not isinstance(root, Path):
        raise TypeError("root must be a pathlib.Path")
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    per_run: list[dict[str, Any]] = []
    all_latencies: list[int] = []
    with tempfile.TemporaryDirectory(prefix="brigade-run-journal-measure-", dir=root) as staging:
        staging_root = Path(staging)
        workspace = staging_root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        for index in range(runs):
            entry = _measure_run(staging_root / "runs" / f"run-{index:05d}", workspace)
            per_run.append(entry)
            all_latencies.extend(entry["append_latencies_ns"])

    ordered = sorted(all_latencies)
    aggregate = {
        "p50_ns": _percentile_nearest_rank(ordered, 50),
        "p95_ns": _percentile_nearest_rank(ordered, 95),
        "p99_ns": _percentile_nearest_rank(ordered, 99),
        "max_ns": int(ordered[-1]),
    }
    return {
        "runs": runs,
        "per_run": per_run,
        "aggregate": aggregate,
        "environment": _environment(root),
    }


def write_report(report: dict[str, Any], output: Path) -> None:
    """Write ``report`` as canonical JSON (indent=2, sort_keys) plus newline."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=1000, help="number of run directories to measure")
    parser.add_argument("--output", required=True, help="exact file path for the JSON report")
    args = parser.parse_args(argv)
    if args.runs <= 0:
        parser.error("--runs must be a positive integer")
    report = measure_runs(args.runs, root=Path.cwd())
    write_report(report, Path(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
