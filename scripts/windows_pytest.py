#!/usr/bin/env python3
"""Run each Windows pytest file in a separate, bounded process."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, CancelledError, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import Callable, Sequence


CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CONSOLE_INTERRUPT = 0xC000013A
CLEANUP_TIMEOUT_SECONDS = 15
PROCESS_POLL_SECONDS = 1
GIT_TIMEOUT_SECONDS = 5
DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_JOB_TIMEOUT_SECONDS = 3300
DEFAULT_WORKERS = 6
INITIAL_ALLOWLIST_ENTRIES = 169
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWLIST = REPO_ROOT / "tests" / "windows_pytest_allowlist.json"


@dataclass(frozen=True)
class FileResult:
    name: str
    status: str
    returncode: int | None
    seconds: float
    log: str


def is_windows() -> bool:
    return os.name == "nt"


def install_console_handler() -> None:
    """Keep Ctrl+C directed at the driver from terminating the parent."""
    if not ctypes.windll.kernel32.SetConsoleCtrlHandler(None, True):
        raise RuntimeError("SetConsoleCtrlHandler(None, True) failed")


def portable_test_name(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or any(part in {".", ".."} or part.endswith(":") for part in path.parts)
        or path.as_posix() != value
        or not path.name.startswith("test_")
        or path.suffix != ".py"
    ):
        raise ValueError(f"allowlist entry must be a portable pytest file name: {value!r}")
    return path.as_posix()


def load_allowlist(path: Path) -> set[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("allowlist must be a JSON object")
    entries: set[str] = set()
    for key in ("known_failures", "known_timeouts"):
        values = payload.get(key)
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError(f"allowlist {key} must be a list of file names")
        entries.update(portable_test_name(value) for value in values)
    return entries


def discover_files(repo: Path) -> list[str]:
    tests = repo / "tests"
    return sorted(path.relative_to(tests).as_posix() for path in tests.rglob("test_*.py") if path.is_file())


def validate_allowlist(allowlist: set[str], files: list[str]) -> None:
    stale = sorted(allowlist.difference(files))
    if stale:
        raise ValueError("allowlist entries not discovered: " + ", ".join(stale))


def check_allowlist_ratchet(current: set[str], baseline: set[str] | None) -> None:
    if baseline is None:
        if len(current) > INITIAL_ALLOWLIST_ENTRIES:
            raise ValueError(f"initial allowlist exceeds {INITIAL_ALLOWLIST_ENTRIES} historical entries")
        return
    added = sorted(current.difference(baseline))
    if added:
        raise ValueError("allowlist expanded: " + ", ".join(added))


def load_allowlist_from_ref(repo: Path, ref: str, allowlist: Path) -> set[str] | None:
    relative = allowlist.resolve().relative_to(repo.resolve()).as_posix()
    verified = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"],
        capture_output=True,
        check=False,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if verified.returncode != 0:
        raise RuntimeError(f"base ref is unavailable: {ref}")
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{relative}"],
        capture_output=True,
        check=False,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        listed = subprocess.run(
            ["git", "-C", str(repo), "ls-tree", "--name-only", ref, "--", relative],
            capture_output=True,
            check=False,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        if listed.returncode != 0 or listed.stdout.strip():
            raise RuntimeError(f"unable to read base allowlist: {ref}")
        return None
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise ValueError("base allowlist must be a JSON object")
    entries: set[str] = set()
    for key in ("known_failures", "known_timeouts"):
        values = payload.get(key)
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError(f"base allowlist {key} must be a list of file names")
        entries.update(portable_test_name(value) for value in values)
    return entries


def enclosing_checkout(path: Path) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=False,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def make_temp_root(repo: Path) -> Path:
    created = Path(tempfile.mkdtemp(prefix="brigade-windows-pytest-"))
    try:
        root = created.resolve()
        checkout = enclosing_checkout(root)
        if (
            not root.is_dir()
            or root.is_relative_to(repo.resolve())
            or (checkout is not None and root.is_relative_to(checkout))
        ):
            raise RuntimeError("per-file basetemp must be pre-created outside the checkout")
        return root
    except BaseException:
        if created.is_symlink():
            created.unlink(missing_ok=True)
        else:
            shutil.rmtree(created, ignore_errors=True)
        raise


def _status(returncode: int) -> str:
    if returncode == 0:
        return "passed"
    if returncode & 0xFFFFFFFF == CONSOLE_INTERRUPT:
        return "console-interrupt"
    return "failed"


def _child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["BRIGADE_EXTRAS"] = "0"
    environment["BRIGADE_NO_UPDATE_CHECK"] = "1"
    return environment


def _kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=CLEANUP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.wait(timeout=CLEANUP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        pass


class ProcessTracker:
    def __init__(self) -> None:
        self._lock = Lock()
        self._processes: dict[int, subprocess.Popen[bytes]] = {}
        self._closing = False

    def add(self, process: subprocess.Popen[bytes]) -> bool:
        with self._lock:
            if not self._closing:
                self._processes[process.pid] = process
                return True
        _kill_process_tree(process)
        return False

    def is_closing(self) -> bool:
        with self._lock:
            return self._closing

    def discard(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._processes.pop(process.pid, None)

    def kill_all(self) -> None:
        with self._lock:
            self._closing = True
            processes = list(self._processes.values())
            self._processes.clear()
        for process in processes:
            _kill_process_tree(process)


def _wait_for_process(
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: int,
    tracker: ProcessTracker | None,
    clock: Callable[[], float] = time.monotonic,
) -> int | None:
    deadline = clock() + timeout_seconds
    while True:
        if tracker is not None and tracker.is_closing():
            _kill_process_tree(process)
            return None
        remaining = deadline - clock()
        if remaining <= 0:
            _kill_process_tree(process)
            return None
        try:
            return process.wait(timeout=min(remaining, PROCESS_POLL_SECONDS))
        except subprocess.TimeoutExpired:
            continue


def run_file(
    name: str,
    *,
    repo: Path,
    python: Path,
    output_dir: Path,
    temp_root: Path,
    timeout_seconds: int,
    tracker: ProcessTracker | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FileResult:
    if timeout_seconds < 1:
        raise ValueError("timeout must be positive")
    started = clock()
    output_dir.mkdir(parents=True, exist_ok=True)
    log = output_dir / "logs" / f"{name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    basetemp = temp_root / name.removesuffix(".py")
    basetemp.mkdir(parents=True, exist_ok=False)
    argv = [
        str(python),
        "-m",
        "pytest",
        "-q",
        "-ra",
        "--tb=line",
        "-p",
        "no:cacheprovider",
        "-p",
        "no:xdist",
        "-o",
        "addopts=",
        f"--basetemp={basetemp}",
        f"tests/{name}",
    ]
    try:
        with log.open("wb") as stream:
            process = subprocess.Popen(
                argv,
                cwd=repo,
                env=_child_environment(),
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
            )
            if tracker is not None:
                if not tracker.add(process):
                    return _deadline_failure(name, output_dir)
            try:
                returncode = _wait_for_process(process, timeout_seconds=timeout_seconds, tracker=tracker, clock=clock)
            finally:
                if tracker is not None:
                    tracker.discard(process)
            if returncode is None:
                return FileResult(name, "timeout", None, clock() - started, log.relative_to(output_dir).as_posix())
    except OSError as exc:
        log.write_text(f"launch failure: {exc!r}\n", encoding="utf-8")
        return FileResult(name, "launch-failure", None, clock() - started, log.relative_to(output_dir).as_posix())
    return FileResult(name, _status(returncode), returncode, clock() - started, log.relative_to(output_dir).as_posix())


def _launch_failure(name: str, output_dir: Path, error: Exception) -> FileResult:
    log = output_dir / "logs" / f"{name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"launch failure: {error!r}\n", encoding="utf-8")
    return FileResult(name, "launch-failure", None, 0.0, log.relative_to(output_dir).as_posix())


def _deadline_failure(name: str, output_dir: Path) -> FileResult:
    log = output_dir / "logs" / f"{name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("driver aggregate deadline exceeded\n", encoding="utf-8")
    return FileResult(name, "deadline-exceeded", None, 0.0, log.relative_to(output_dir).as_posix())


def result_from_future(future: Future[FileResult], name: str, output_dir: Path) -> FileResult:
    if future.cancelled():
        return _launch_failure(name, output_dir, RuntimeError("worker cancelled"))
    try:
        return future.result()
    except CancelledError:
        return _launch_failure(name, output_dir, RuntimeError("worker cancelled"))


def run_files(
    *,
    files: list[str],
    serial: set[str],
    repo: Path,
    python: Path,
    output_dir: Path,
    temp_root: Path,
    workers: int,
    timeout_seconds: int,
    deadline: float,
    on_result: Callable[[list[FileResult]], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[FileResult]:
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if not files:
        raise RuntimeError("no test files discovered")
    parallel = [name for name in files if name not in serial]
    serial_files = [name for name in files if name in serial]
    tracker = ProcessTracker()
    results: list[FileResult] = []

    def runner(name: str) -> FileResult:
        return run_file(
            name,
            repo=repo,
            python=python,
            output_dir=output_dir,
            temp_root=temp_root,
            timeout_seconds=timeout_seconds,
            tracker=tracker,
            clock=clock,
        )

    def safe_runner(name: str) -> FileResult:
        try:
            return runner(name)
        except Exception as exc:  # noqa: BLE001 - each file must yield a failure row.
            return _launch_failure(name, output_dir, exc)

    def record(result: FileResult) -> None:
        results.append(result)
        if on_result is not None:
            on_result(sorted(results, key=lambda item: item.name))

    parallel_pending = parallel
    serial_pending = serial_files
    serial_phase = False
    active: dict[Future[FileResult], str] = {}
    executor = ThreadPoolExecutor(max_workers=workers)
    expired = False
    cleanup_required = False
    try:
        while parallel_pending or serial_pending or active:
            if not active and not parallel_pending and serial_pending:
                serial_phase = True
            pending = serial_pending if serial_phase else parallel_pending
            limit = 1 if serial_phase else workers
            while pending and len(active) < limit and clock() < deadline:
                name = pending.pop(0)
                active[executor.submit(safe_runner, name)] = name
            if pending and clock() >= deadline:
                expired = True
                break
            if not active:
                continue
            remaining = deadline - clock()
            if remaining <= 0:
                expired = True
                break
            done, _ = wait(active, timeout=remaining, return_when=FIRST_COMPLETED)
            if not done:
                expired = True
                break
            for future in done:
                name = active.pop(future)
                record(result_from_future(future, name, output_dir))
    except BaseException:
        cleanup_required = True
        raise
    finally:
        if expired or cleanup_required:
            active_names = list(active.values())
            for future in active:
                future.cancel()
            tracker.kill_all()
            if expired:
                for name in [*active_names, *parallel_pending, *serial_pending]:
                    record(_deadline_failure(name, output_dir))
        executor.shutdown(wait=False, cancel_futures=expired or cleanup_required)
    return sorted(results, key=lambda result: result.name)


def regressions(results: list[FileResult], allowlist: set[str]) -> list[FileResult]:
    infra_errors = {"console-interrupt", "deadline-exceeded", "launch-failure"}
    return [
        result
        for result in results
        if result.status != "passed" and (result.status in infra_errors or result.name not in allowlist)
    ]


def write_record(path: Path, *, results: list[FileResult], driver_error: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "schema": "brigade.windows_pytest.v1",
        "results": [asdict(result) for result in results],
    }
    if driver_error is not None:
        payload["driver_error"] = driver_error
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument(
        "--record", type=Path, required=True, help="write measured results only; never changes the allowlist"
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--job-timeout", type=int, default=DEFAULT_JOB_TIMEOUT_SECONDS)
    parser.add_argument("--base-ref", help="compare the allowlist with this pull request base commit")
    parser.add_argument("--serial", action="append", default=[], metavar="TEST_FILE")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    results: list[FileResult] = []
    partial_results: list[FileResult] = []
    temp_root: Path | None = None
    driver_error: str | None = None

    def record_progress(snapshot: list[FileResult]) -> None:
        partial_results[:] = snapshot
        write_record(args.record, results=snapshot)

    try:
        if not is_windows():
            raise RuntimeError("windows_pytest.py must run on Windows")
        repo = args.repo.resolve()
        allowlist = load_allowlist(args.allowlist)
        serial = {portable_test_name(name) for name in args.serial}
        install_console_handler()
        if args.timeout < 1:
            raise ValueError("timeout must be positive")
        if args.job_timeout < 1:
            raise ValueError("job timeout must be positive")
        files = discover_files(repo)
        validate_allowlist(allowlist, files)
        if args.base_ref:
            check_allowlist_ratchet(allowlist, load_allowlist_from_ref(repo, args.base_ref, args.allowlist))
        temp_root = make_temp_root(repo)
        try:
            results = run_files(
                files=files,
                serial=serial,
                repo=repo,
                python=args.python,
                output_dir=args.record.parent,
                temp_root=temp_root,
                workers=args.workers,
                timeout_seconds=args.timeout,
                deadline=time.monotonic() + args.job_timeout,
                on_result=record_progress,
            )
        finally:
            if temp_root is not None:
                shutil.rmtree(temp_root, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 - record any ordinary driver failure before exiting.
        results = results or partial_results
        driver_error = str(exc)

    try:
        write_record(args.record, results=results, driver_error=driver_error)
    except OSError as exc:
        print(f"windows pytest driver error: unable to write record: {exc}", file=sys.stderr)
        return 2
    if driver_error is not None:
        print(f"windows pytest driver error: {driver_error}", file=sys.stderr)
        return 2
    failures = regressions(results, allowlist)
    removal_candidates = [result.name for result in results if result.status == "passed" and result.name in allowlist]
    print(
        f"windows pytest: files={len(results)} regressions={len(failures)} removal_candidates={len(removal_candidates)}"
    )
    if removal_candidates:
        print("allowlist removal candidates: " + ", ".join(removal_candidates))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
