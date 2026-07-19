#!/usr/bin/env python3
"""Run the versioned synthetic corpus against GraphTrail and optional adapters."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any


ROOT = Path(__file__).resolve().parent
CORPUS_PATH = ROOT / "corpus.v1.json"
FIXTURE_ROOT = ROOT / "fixtures" / "polyglot"
FIXTURE_SETUP_TIMEOUT_SECONDS = 30.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graphtrail", default="target/release/graphtrail")
    parser.add_argument(
        "--comparator",
        action="append",
        default=[],
        metavar="NAME=COMMAND",
        help="optional stdin/stdout adapter; repeat for more implementations",
    )
    parser.add_argument("--case", action="append", dest="cases", default=[])
    parser.add_argument("--output", default="-", help="result JSON path or - for stdout")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=120.0,
        help="per-command timeout (default: 120)",
    )
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args()


def load_corpus() -> dict[str, Any]:
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    if corpus.get("corpus_schema_version") != 1:
        raise ValueError("unsupported corpus_schema_version")
    if corpus.get("result_schema_version") != 1:
        raise ValueError("unsupported result_schema_version")
    if not isinstance(corpus.get("response_budget_chars"), int):
        raise ValueError("response_budget_chars must be an integer")
    cases = corpus.get("cases", [])
    ids = [case.get("id") for case in cases]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("case ids must be non-empty and unique")
    for case in cases:
        mode = case.get("graphtrail_mode")
        changed_files = case.get("changed_files", [])
        if mode not in {"context", "affected", "doctor"}:
            raise ValueError(f"unsupported graphtrail_mode for {case['id']}: {mode}")
        if (mode == "affected") != bool(changed_files):
            raise ValueError(
                f"{case['id']} must provide changed_files only for affected mode"
            )
        task = case.get("task", "").lower()
        labels = [*case.get("expected_files", []), *case.get("expected_tokens", [])]
        disclosed_setup = json.dumps(
            {
                "protocol_version": 1,
                "case_id": case.get("id"),
                "behavior": case.get("behavior"),
                "fixture_variant": case.get("fixture_variant"),
                "response_budget_chars": corpus.get("response_budget_chars"),
                "branch_transition": (
                    {"from": "main", "to": "drift"}
                    if case.get("fixture_variant") == "drift"
                    else None
                ),
            },
            sort_keys=True,
        ).lower()
        leaked = [
            label
            for label in labels
            if label.lower() in task or label.lower() in disclosed_setup
        ]
        if leaked:
            raise ValueError(
                f"{case['id']} leaks correctness labels in disclosed inputs: {leaked}"
            )
    return corpus


def split_command(raw_command: str, windows: bool) -> list[str]:
    command = shlex.split(raw_command, posix=not windows)
    if windows:
        command = [
            token[1:-1]
            if len(token) >= 2
            and token[0] == token[-1]
            and token[0] in {'"', "'"}
            else token
            for token in command
        ]
    return command


def parse_comparators(
    raw_values: list[str], windows: bool | None = None
) -> list[tuple[str, list[str]]]:
    parsed = []
    names = {"graphtrail"}
    windows = os.name == "nt" if windows is None else windows
    for raw in raw_values:
        if "=" not in raw:
            raise ValueError(f"comparator must be NAME=COMMAND: {raw}")
        name, raw_command = raw.split("=", 1)
        name = name.strip()
        command = split_command(raw_command, windows)
        if not name or not command or name in names:
            raise ValueError(f"invalid or duplicate comparator: {raw}")
        names.add(name)
        parsed.append((name, command))
    return parsed


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=FIXTURE_SETUP_TIMEOUT_SECONDS,
    )


def overlay(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)


def prepare_repo(destination: Path, fixture_variant: str) -> None:
    overlay(FIXTURE_ROOT / "base", destination)
    git(destination, "init", "-q", "-b", "main")
    git(
        destination,
        "config",
        "user.name",
        "Corpus Runner",
    )
    git(
        destination,
        "config",
        "user.email",
        "corpus@example.invalid",
    )
    git(destination, "add", ".")
    git(
        destination,
        "commit",
        "-q",
        "-m",
        "base fixture",
    )
    if fixture_variant == "drift":
        git(
            destination,
            "switch",
            "-q",
            "-c",
            "drift",
        )
        git(
            destination,
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "drift fixture",
        )
        git(destination, "switch", "-q", "main")


def process_rss_kib(pid: int) -> int | None:
    status = Path(f"/proc/{pid}/status")
    try:
        for line in status.read_text(encoding="utf-8").splitlines():
            if line.startswith(("VmHWM:", "VmRSS:")):
                return int(line.split()[1])
    except (FileNotFoundError, PermissionError, ValueError):
        return None
    return None


def kill_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return

    try:
        killed = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        )
        if killed.returncode != 0 and process.poll() is None:
            process.kill()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        process.kill()


def run_command(
    command: list[str], cwd: Path, timeout_seconds: float, stdin: str | None = None
) -> tuple[int, str, str, float, int | None]:
    started = time.perf_counter()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name == "posix",
    )
    peak = [process_rss_kib(process.pid)]
    finished = threading.Event()

    def sample_memory() -> None:
        while not finished.wait(0.005):
            sample = process_rss_kib(process.pid)
            if sample is not None and (peak[0] is None or sample > peak[0]):
                peak[0] = sample

    monitor = threading.Thread(target=sample_memory, daemon=True)
    monitor.start()
    try:
        stdout, stderr = process.communicate(stdin, timeout=timeout_seconds)
        return_code = process.returncode
    except subprocess.TimeoutExpired as timeout:
        kill_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=1.0)
        except subprocess.TimeoutExpired:
            stdout = timeout.stdout or ""
            stderr = timeout.stderr or ""
            if process.poll() is None:
                process.kill()
        message = f"timed out after {timeout_seconds:g} seconds"
        stderr = f"{stderr.rstrip()}\n{message}".lstrip()
        return_code = 124
    finally:
        finished.set()
        monitor.join(timeout=0.05)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return return_code, stdout, stderr, elapsed_ms, peak[0]


def bounded_response(raw: str, budget: int) -> tuple[str, bool]:
    return raw[:budget], len(raw) > budget


def correctness(case: dict[str, Any], response: str) -> dict[str, Any]:
    expected_files = case.get("expected_files", [])
    expected_tokens = case.get("expected_tokens", [])
    matched_files = [path for path in expected_files if path in response]
    matched_tokens = [token for token in expected_tokens if token in response]
    missing_files = [path for path in expected_files if path not in matched_files]
    missing_tokens = [token for token in expected_tokens if token not in matched_tokens]
    total = len(expected_files) + len(expected_tokens)
    matched = len(matched_files) + len(matched_tokens)
    return {
        "passed": not missing_files and not missing_tokens,
        "score": matched / total if total else 1.0,
        "matched_files": matched_files,
        "missing_files": missing_files,
        "matched_tokens": matched_tokens,
        "missing_tokens": missing_tokens,
    }


def base_result(name: str, case: dict[str, Any]) -> dict[str, Any]:
    task = case["task"]
    return {
        "implementation": name,
        "case_id": case["id"],
        "task_sha256": hashlib.sha256(task.encode("utf-8")).hexdigest(),
    }


def failed_result(
    name: str,
    case: dict[str, Any],
    error: str,
    setup_steps: int,
    tool_calls: int,
    elapsed_ms: float,
    peak_memory_kib: int | None,
) -> dict[str, Any]:
    result = base_result(name, case)
    result.update(
        {
            "status": "failed",
            "correctness": correctness(case, ""),
            "setup_steps": setup_steps,
            "tool_calls": tool_calls,
            "response_chars": 0,
            "elapsed_ms": round(elapsed_ms, 3),
            "peak_memory_kib": peak_memory_kib,
            "truncated": False,
            "error": error,
        }
    )
    return result


def run_graphtrail(
    executable: Path,
    case: dict[str, Any],
    budget: int,
    repo: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    db = repo / ".graphtrail" / "graphtrail.db"
    setup = [str(executable), "--db", str(db), "sync", "."]
    code, _, stderr, setup_ms, setup_peak = run_command(
        setup, repo, timeout_seconds
    )
    if code != 0:
        return failed_result(
            "graphtrail", case, stderr.strip(), 1, 0, setup_ms, setup_peak
        )

    setup_steps = 1
    peaks = [value for value in [setup_peak] if value is not None]
    if case["fixture_variant"] == "drift":
        code, _, stderr, switch_ms, switch_peak = run_command(
            ["git", "switch", "-q", "drift"], repo, timeout_seconds
        )
        setup_ms += switch_ms
        setup_steps += 1
        if switch_peak is not None:
            peaks.append(switch_peak)
        if code != 0:
            return failed_result(
                "graphtrail",
                case,
                stderr.strip(),
                setup_steps,
                0,
                setup_ms,
                max(peaks) if peaks else None,
            )

    mode = case["graphtrail_mode"]
    if mode == "context":
        query_command = [
            str(executable),
            "--db",
            str(db),
            "context",
            case["task"],
            "--limit",
            "12",
            "--json",
        ]
    elif mode == "affected":
        query_command = [
            str(executable),
            "--db",
            str(db),
            "affected",
            *case["changed_files"],
            "--json",
        ]
    elif mode == "doctor":
        query_command = [str(executable), "--db", str(db), "doctor", ".", "--json"]
    else:
        raise AssertionError(f"validated unsupported graphtrail_mode: {mode}")

    code, stdout, stderr, query_ms, peak = run_command(
        query_command, repo, timeout_seconds
    )
    if peak is not None:
        peaks.append(peak)
    accepted_codes = {0, 1} if mode == "doctor" else {0}
    if code not in accepted_codes:
        return failed_result(
            "graphtrail",
            case,
            stderr.strip(),
            setup_steps,
            1,
            setup_ms + query_ms,
            max(peaks) if peaks else None,
        )
    if mode == "context":
        try:
            payload = json.loads(stdout)
            payload.pop("task", None)
            stdout = json.dumps(payload, sort_keys=True)
        except (AttributeError, json.JSONDecodeError) as error:
            return failed_result(
                "graphtrail",
                case,
                f"invalid GraphTrail context output: {error}",
                setup_steps,
                1,
                setup_ms + query_ms,
                max(peaks) if peaks else None,
            )
    elif mode == "doctor":
        try:
            payload = json.loads(stdout)
            drifted = payload["branch"]["drifted"]
            if not isinstance(drifted, bool):
                raise TypeError("branch.drifted must be a boolean")
            observation = "index mismatch" if drifted else "index current"
            stdout = f"{observation}\n{json.dumps(payload, sort_keys=True)}"
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            return failed_result(
                "graphtrail",
                case,
                f"invalid GraphTrail doctor output: {error}",
                setup_steps,
                1,
                setup_ms + query_ms,
                max(peaks) if peaks else None,
            )
    response, truncated = bounded_response(stdout, budget)
    result = base_result("graphtrail", case)
    result.update(
        {
            "status": "completed",
            "correctness": correctness(case, response),
            "setup_steps": setup_steps,
            "tool_calls": 1,
            "response_chars": len(response),
            "elapsed_ms": round(setup_ms + query_ms, 3),
            "peak_memory_kib": max(peaks) if peaks else None,
            "truncated": truncated,
        }
    )
    return result


def run_comparator(
    name: str,
    command: list[str],
    case: dict[str, Any],
    budget: int,
    repo: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    request = {
        "protocol_version": 1,
        "case_id": case["id"],
        "task": case["task"],
        "behavior": case["behavior"],
        "fixture_variant": case["fixture_variant"],
        "response_budget_chars": budget,
        "branch_transition": (
            {"from": "main", "to": "drift"}
            if case["fixture_variant"] == "drift"
            else None
        ),
    }
    code, stdout, stderr, elapsed_ms, peak = run_command(
        command, repo, timeout_seconds, json.dumps(request) + "\n"
    )
    if code != 0:
        return failed_result(name, case, stderr.strip(), 0, 1, elapsed_ms, peak)
    try:
        payload = json.loads(stdout)
        raw_response = payload["response"]
        files = payload.get("files", [])
        setup_steps = payload["setup_steps"]
        tool_calls = payload["tool_calls"]
        if (
            not isinstance(raw_response, str)
            or not isinstance(files, list)
            or not all(isinstance(path, str) for path in files)
        ):
            raise TypeError("response must be a string and files must be a string array")
        if not isinstance(setup_steps, int) or not isinstance(tool_calls, int):
            raise TypeError("setup_steps and tool_calls must be integers")
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        return failed_result(
            name,
            case,
            f"invalid comparator output: {error}",
            0,
            1,
            elapsed_ms,
            peak,
        )

    files = [path.replace("\\", "/") for path in files]
    combined_response = "\n".join([*files, raw_response])
    response, truncated = bounded_response(combined_response, budget)
    result = base_result(name, case)
    result.update(
        {
            "status": "completed",
            "correctness": correctness(case, response),
            "setup_steps": setup_steps,
            "tool_calls": tool_calls,
            "response_chars": len(response),
            "elapsed_ms": round(elapsed_ms, 3),
            "peak_memory_kib": peak,
            "truncated": truncated,
        }
    )
    return result


def write_result(result: dict[str, Any], output: str) -> None:
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if output == "-":
        print(text, end="")
    else:
        Path(output).write_text(text, encoding="utf-8")


def main() -> int:
    args = parse_args()
    corpus = load_corpus()
    if args.timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    comparators = parse_comparators(args.comparator)
    if len(args.cases) != len(set(args.cases)):
        raise ValueError("requested case ids must be unique")
    selected = [
        case
        for case in corpus["cases"]
        if not args.cases or case["id"] in args.cases
    ]
    if len(selected) != (len(args.cases) if args.cases else len(corpus["cases"])):
        raise ValueError("one or more requested case ids do not exist")
    if args.validate_only:
        print(f"validated {len(selected)} cases from corpus schema v1")
        return 0

    graphtrail = Path(args.graphtrail).resolve()
    if not graphtrail.is_file() or not os.access(graphtrail, os.X_OK):
        raise ValueError(f"GraphTrail executable is unavailable: {graphtrail}")

    budget = corpus["response_budget_chars"]
    results = []
    for case in selected:
        with tempfile.TemporaryDirectory(prefix="graphtrail-cross-tool-") as raw_temp:
            repo = Path(raw_temp) / "repo"
            repo.mkdir()
            prepare_repo(repo, case["fixture_variant"])
            results.append(
                run_graphtrail(graphtrail, case, budget, repo, args.timeout_seconds)
            )
        for name, command in comparators:
            with tempfile.TemporaryDirectory(prefix="graphtrail-cross-tool-") as raw_temp:
                repo = Path(raw_temp) / "repo"
                repo.mkdir()
                prepare_repo(repo, case["fixture_variant"])
                results.append(
                    run_comparator(
                        name, command, case, budget, repo, args.timeout_seconds
                    )
                )

    report = {
        "result_schema_version": corpus["result_schema_version"],
        "corpus_schema_version": corpus["corpus_schema_version"],
        "fixture_version": corpus["fixture_version"],
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "response_budget_chars": budget,
        "results": results,
    }
    write_result(report, args.output)
    all_passed = all(
        result["status"] == "completed" and result["correctness"]["passed"]
        for result in results
    )
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
