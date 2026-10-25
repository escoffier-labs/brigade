#!/usr/bin/env python3
"""Run CI's deterministic pytest shards, then enforce combined coverage once."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def positive_setting(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def shard_count() -> int:
    return positive_setting("BRIGADE_VERIFY_SHARDS", min(8, max(1, (os.cpu_count() or 1) // 2)))


def stop_process_group(process: subprocess.Popen, *, kill: bool = False) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL if kill else signal.SIGTERM)
        elif process.poll() is None:
            if kill:
                process.kill()
            else:
                process.terminate()
    except ProcessLookupError:
        pass


def main(argv: list[str] | None = None) -> int:
    try:
        count = shard_count()
    except ValueError as exc:
        print(f"verify: {exc}", file=sys.stderr)
        return 2
    arguments = sys.argv[1:] if argv is None else argv
    processes: list[subprocess.Popen] = []
    pending: set[subprocess.Popen] = set()
    totals = dict(passed=0, failed=0, skipped=0, errors=0)
    extra_totals: dict[str, int] = {}
    collected: set[int] = set()
    selected = 0
    failed = False
    # Separate every run's data, including the combined file, even in one checkout.
    with tempfile.TemporaryDirectory(prefix="brigade-verify-") as directory:
        work = Path(directory)
        logs = []
        previous = signal.getsignal(signal.SIGTERM)

        def terminate(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, terminate)
        try:
            print(f"Full verification: {count} pytest shards", flush=True)
            for index in range(count):
                env = dict(os.environ, COVERAGE_FILE=str(work / f".coverage.shard-{index}"))
                log = (work / f"shard-{index}.log").open("w+")
                logs.append(log)
                command = [
                    sys.executable,
                    str(REPO_ROOT / "scripts/ci_pytest_shard.py"),
                    "--shard-index",
                    str(index),
                    "--shard-count",
                    str(count),
                    "--result-json",
                    str(work / f"shard-{index}.json"),
                    "--",
                    "-q",
                    f"--cov={REPO_ROOT / 'src' / 'brigade'}",
                    "--cov-report=",
                    "--cov-fail-under=0",
                    # Shards must not race on the checkout's shared .pytest_cache.
                    "-o",
                    f"cache_dir={work / f'pytest-cache-{index}'}",
                    *arguments,
                ]
                processes.append(
                    subprocess.Popen(
                        command,
                        cwd=REPO_ROOT,
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=(os.name == "posix"),
                    )
                )
                pending.add(processes[-1])
            for index, process in enumerate(processes):
                status = process.wait()
                # Do not retain a reaped leader's group id while later shards run.
                stop_process_group(process)
                stop_process_group(process, kill=True)
                pending.remove(process)
                failed |= status != 0
                label = "FAILED" if status else "Completed"
                print(f"\n{label} shard {index + 1}/{count} (exit {status})", flush=True)
                logs[index].seek(0)
                for line in logs[index]:
                    print(line, end="")
                try:
                    result = json.loads((work / f"shard-{index}.json").read_text(encoding="utf-8"))
                    collected.add(result["collected"])
                    selected += result["selected"]
                    counts = result["counts"]
                    for key in totals:
                        totals[key] += counts.get("error" if key == "errors" else key, 0)
                    for key in (
                        "xfailed",
                        "xpassed",
                        "subtests passed",
                        "subtests failed",
                        "subtests skipped",
                        "subtests xfailed",
                    ):
                        extra_totals[key] = extra_totals.get(key, 0) + counts.get(key, 0)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    print(f"Shard {index + 1}/{count}: invalid or missing results: {exc}")
                    failed = True
            print(
                "\nAggregate: {passed} passed, {failed} failed, {skipped} skipped, {errors} errors".format(**totals),
                flush=True,
            )
            for key, value in extra_totals.items():
                if value:
                    print(f"{value} {key}")
            print(
                f"Selection: {selected} selected across {count} shards; collection counts: {sorted(collected)}",
                flush=True,
            )
            if collected != {selected}:
                print("Shard selection does not match the unsharded collection", file=sys.stderr)
                failed = True
            if failed:
                return 1
            env = dict(os.environ, COVERAGE_FILE=str(work / ".coverage"))
            combined = subprocess.run([sys.executable, "-m", "coverage", "combine", str(work)], cwd=REPO_ROOT, env=env)
            if combined.returncode:
                return combined.returncode
            print("Combined coverage (floor 78%)", flush=True)
            return subprocess.run(
                [sys.executable, "-m", "coverage", "report", "--fail-under=78"], cwd=REPO_ROOT, env=env
            ).returncode
        except KeyboardInterrupt:
            return 130
        finally:
            # A second cancellation must not interrupt bounded cleanup.
            previous_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            # Do not leave shard workers holding resources after cancellation or launch failure.
            for process in pending:
                stop_process_group(process)
            for process in pending:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    stop_process_group(process, kill=True)
                    process.wait()
                # The shard may have exited before a child. Reap the entire group.
                stop_process_group(process, kill=True)
            for log in logs:
                log.close()
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
