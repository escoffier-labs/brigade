#!/usr/bin/env python3
"""Measure disposable SQLite WAL databases without changing product settings."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import sqlite3
import statistics
import subprocess
import tempfile
import time
from pathlib import Path


def connect(path: Path, synchronous: str = "FULL") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    conn.execute(f"PRAGMA synchronous={synchronous}")
    assert conn.execute("PRAGMA synchronous").fetchone() == ({"FULL": 2, "NORMAL": 1, "OFF": 0}[synchronous],)
    # Keep automatic checkpoints out of the commit timing, including larger N.
    conn.execute("PRAGMA wal_autocheckpoint=0")
    return conn


def commit_samples(path: Path, mode: str, count: int) -> list[float]:
    conn = connect(path, mode)
    try:
        conn.execute("CREATE TABLE probe (value INTEGER)")
        conn.commit()
        samples = []
        for value in range(count):
            conn.execute("INSERT INTO probe VALUES (?)", (value,))
            started = time.perf_counter_ns()
            conn.commit()
            samples.append((time.perf_counter_ns() - started) / 1_000_000)
        assert conn.execute("SELECT count(*) FROM probe").fetchone() == (count,)
        return samples
    finally:
        conn.close()


def connection_samples(path: Path, count: int, *, idle: bool, pending_wal: bool) -> list[float]:
    seed = connect(path)
    seed.execute("CREATE TABLE probe (value INTEGER)")
    seed.execute("INSERT INTO probe VALUES (0)")
    seed.commit()
    seed.close()
    keeper = connect(path) if idle else None
    try:
        samples = []
        for value in range(count):
            if pending_wal:
                conn = connect(path)
                conn.execute("UPDATE probe SET value=?", (value + 1,))
                conn.commit()
                assert Path(str(path) + "-wal").stat().st_size > 0
                # Only close is timed here. Commit/setup above are excluded.
                started = time.perf_counter_ns()
                conn.close()
            else:
                # This separate metric includes open, read and close of a clean DB.
                started = time.perf_counter_ns()
                conn = sqlite3.connect(path)
                try:
                    assert conn.execute("SELECT value FROM probe").fetchone() == (0,)
                finally:
                    conn.close()
            samples.append((time.perf_counter_ns() - started) / 1_000_000)
        return samples
    finally:
        if keeper is not None:
            keeper.close()


def defender_status() -> dict:
    if os.name != "nt":
        return {"unavailable": "not Windows"}
    command = """
    $ErrorActionPreference = 'Stop'
    try {
      $status = Get-MpComputerStatus | Select-Object AMServiceEnabled, AntivirusEnabled,
        RealTimeProtectionEnabled, BehaviorMonitorEnabled, IoavProtectionEnabled,
        OnAccessProtectionEnabled, IsTamperProtected, AMProductVersion, AntivirusSignatureVersion
      $preferences = Get-MpPreference | Select-Object ExclusionPath, DisableRealtimeMonitoring
      @{ status = $status; preferences = $preferences } | ConvertTo-Json -Depth 4 -Compress
    } catch { @{ unavailable = $_.Exception.Message } | ConvertTo-Json -Compress }
    """
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"unavailable": str(exc)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be positive")

    metadata = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "image_os": os.environ.get("ImageOS"),
        "image_version": os.environ.get("ImageVersion"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "head_sha": os.environ.get("GITHUB_SHA"),
        "pytest_temp": tempfile.gettempdir(),
        "workspace": str(args.workspace.resolve()),
        "defender": defender_status(),
    }
    lines = [
        "## SQLite flush probe",
        "",
        "Metadata: `" + json.dumps(metadata, sort_keys=True) + "`",
        "",
        "Times in ms. Each row has N successful operations. p99 uses nearest rank.",
        "Commit times exclude INSERT. WAL-close times exclude UPDATE/commit.",
        "Read cycles use a clean DB. WAL-close cycles have one pending write each.",
        "All close measurements use FULL. Idle connections hold no read transaction.",
        "",
        "| Root | Operation | N | p50 | p99 | Max |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    print("\n".join(lines), flush=True)
    rows = []
    for label, root in (("pytest-temp", Path(tempfile.gettempdir())), ("workspace", args.workspace.resolve())):
        with tempfile.TemporaryDirectory(prefix="brigade-sqlite-probe-", dir=root) as directory:
            target = Path(directory)
            cases = [(f"commit {mode}", mode, False, False) for mode in ("FULL", "NORMAL", "OFF")]
            cases += [
                (f"{'WAL close' if pending else 'open/read/close'} idle={idle}", None, idle, pending)
                for pending in (False, True)
                for idle in (False, True)
            ]
            for index, (operation, mode, idle, pending) in enumerate(cases):
                path = target / f"probe-{index}.db"
                samples = (
                    commit_samples(path, mode, args.samples)
                    if mode is not None
                    else connection_samples(path, args.samples, idle=idle, pending_wal=pending)
                )
                ordered = sorted(samples)
                row = {
                    "root": label,
                    "path": str(root),
                    "operation": operation,
                    "n": len(samples),
                    "p50_ms": statistics.median(samples),
                    "p99_ms": ordered[math.ceil(0.99 * len(samples)) - 1],
                    "max_ms": max(samples),
                    "samples_ms": samples,
                }
                rows.append(row)
                line = (
                    f"| {label} | {operation} | {len(samples)} | {row['p50_ms']:.3f} | "
                    f"{row['p99_ms']:.3f} | {row['max_ms']:.3f} |"
                )
                lines.append(line)
                print(line, flush=True)
        print(f"{label} path: {root}", flush=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"metadata": metadata, "rows": rows}, indent=2) + "\n", encoding="utf-8")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
