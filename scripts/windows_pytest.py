#!/usr/bin/env python3
"""Run each Windows pytest file in a separate, bounded process."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, CancelledError, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence


if __package__:
    from .windows_job import (
        AGGREGATE_DEADLINE,
        DRIVER_ABORT,
        FILE_TIMEOUT,
        REASONS,
        CleanupError,
        LaunchClosed,
        ProcessTracker,
        console_handler,
        launch_process,
    )
else:
    from windows_job import (
        AGGREGATE_DEADLINE,
        DRIVER_ABORT,
        FILE_TIMEOUT,
        REASONS,
        CleanupError,
        LaunchClosed,
        ProcessTracker,
        console_handler,
        launch_process,
    )
CONSOLE_INTERRUPT = 0xC000013A
CLEANUP_TIMEOUT_SECONDS = 15
PROCESS_POLL_SECONDS = 1
GIT_TIMEOUT_SECONDS = 5
DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_JOB_TIMEOUT_SECONDS = 3300
DEFAULT_STARTUP_RESERVE_SECONDS = 120
DEFAULT_FINALIZE_RESERVE_SECONDS = 300
REPLACE_ATTEMPTS = 4
DEFAULT_WORKERS = 6
INITIAL_ALLOWLIST_ENTRIES = 182
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWLIST = REPO_ROOT / "tests" / "windows_pytest_allowlist.json"


@dataclass(frozen=True)
class FileResult:
    name: str
    status: str
    returncode: int | None
    seconds: float
    log: str
    diagnostic: str | None = None
    cleanup_error: str | None = None


def is_windows() -> bool:
    return os.name == "nt"


def install_console_handler() -> None:
    """Keep Ctrl+C directed at the driver from terminating the parent."""
    console_handler()


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


def _cleanup_path(root: Path, path: Path) -> os.stat_result:
    """Reject lexical escapes and links/reparse points, including ancestors."""
    if not path.is_relative_to(root) or ".." in path.parts:
        raise ValueError("temporary cleanup path escapes owned root")
    info = os.lstat(path)
    for current in (path, *path.parents):
        metadata = info if current == path else os.lstat(current)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ValueError("temporary cleanup path contains a link or reparse point")
    return info


def _remove_owned_temp_root(root: Path) -> None:
    """Strict removal after shutdown, with one Windows read-only unlink retry."""
    root = root.absolute()
    _cleanup_path(root, root)
    # No worker is alive here. Check traversal before rmtree, including 3.10.
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in (*dirs, *files):
            _cleanup_path(root, Path(directory) / name)

    def onerror(function, filename, exc_info):
        error = exc_info[1]
        if (
            not is_windows()
            or (function is not os.unlink and function is not os.remove)
            or not isinstance(error, PermissionError)
            or getattr(error, "winerror", None) != 5
        ):
            raise error
        path = Path(filename).absolute()
        info = _cleanup_path(root, path)
        if not stat.S_ISREG(info.st_mode) or not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_READONLY:
            raise error
        # On Windows chmod changes only FILE_ATTRIBUTE_READONLY, not ACLs.
        os.chmod(path, info.st_mode | stat.S_IWRITE)
        after = _cleanup_path(root, path)
        if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino) or not stat.S_ISREG(after.st_mode):
            raise ValueError("temporary cleanup file changed during read-only repair")
        # Never execute the callback-supplied function. Retry our unlink once.
        os.unlink(path)

    shutil.rmtree(root, onerror=onerror)


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


class CleanupUnconfirmed(CleanupError):
    """Job emptiness or worker finalization could not be confirmed."""


@dataclass
class _CleanupDeadline:
    deadline: float | None = None

    def get(self, tracker: ProcessTracker) -> float:
        if self.deadline is None:
            self.deadline = time.monotonic() + CLEANUP_TIMEOUT_SECONDS
        self.deadline = tracker.cleanup_deadline(self.deadline)
        return self.deadline


def _wait_for_process(
    process,
    *,
    timeout_seconds: int,
    tracker: ProcessTracker,
    cleanup: _CleanupDeadline,
    clock: Callable[[], float] = time.monotonic,
    aggregate_deadline: float | None = None,
) -> int:
    file_deadline = clock() + timeout_seconds
    deadline = min(file_deadline, aggregate_deadline) if aggregate_deadline is not None else file_deadline
    while True:
        remaining = deadline - clock()
        if remaining <= 0 or tracker.is_closing():
            cause = (
                tracker.closing_cause
                if tracker.is_closing()
                else (
                    AGGREGATE_DEADLINE
                    if aggregate_deadline is not None and aggregate_deadline <= file_deadline
                    else FILE_TIMEOUT
                )
            )
            cleanup.get(tracker)
            process.terminate(cause)
            try:
                return process.wait(timeout=max(0.0, cleanup.get(tracker) - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise CleanupUnconfirmed("cleanup unconfirmed: root process wait expired") from None
        try:
            # A natural code retains its meaning even when shutdown races this wait.
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
    deadline: float | None = None,
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
    tracker = tracker if tracker is not None else ProcessTracker()
    returncode = None
    termination_status = None
    cleanup = _CleanupDeadline()
    process = None
    if deadline is not None and clock() >= deadline:
        return _deadline_failure(name, output_dir, unstarted=True)
    try:
        with log.open("wb") as stream:
            process = launch_process(
                python=python.absolute(),
                args=argv[1:],
                cwd=repo.resolve(),
                env=_child_environment(),
                log=stream,
                tracker=tracker,
                deadline=deadline,
                clock=clock,
            )
            try:
                returncode = _wait_for_process(
                    process,
                    timeout_seconds=timeout_seconds,
                    tracker=tracker,
                    cleanup=cleanup,
                    clock=clock,
                    aggregate_deadline=deadline,
                )
                # Natural finalization can itself record DRIVER_ABORT for descendants.
                if process.cause == returncode:
                    termination_status = REASONS.get(returncode)
            finally:
                try:
                    process.finish(
                        deadline=cleanup.get(tracker),
                        natural=returncode is not None and termination_status is None,
                    )
                finally:
                    process.close()
    except LaunchClosed:
        return _deadline_failure(
            name,
            output_dir,
            unstarted=True,
            cause=tracker.closing_cause if tracker.is_closing() else AGGREGATE_DEADLINE,
        )
    except CleanupError as exc:
        if returncode is not None and termination_status is None and _status(returncode) == "failed":
            return FileResult(
                name,
                "failed",
                returncode,
                clock() - started,
                log.relative_to(output_dir).as_posix(),
                str(exc),
                cleanup_error=str(exc),
            )
        return FileResult(
            name, "cleanup-unconfirmed", None, clock() - started, log.relative_to(output_dir).as_posix(), str(exc)
        )
    except OSError as exc:
        return FileResult(
            name,
            "launch-failure",
            None,
            clock() - started,
            log.relative_to(output_dir).as_posix(),
            f"launch failure: {exc!r}",
        )
    status = termination_status or _status(returncode)
    diagnostic = None
    if termination_status is not None:
        diagnostic = f"job termination: {status}"
    if process.token.leaked_descendants:
        diagnostic = f"{diagnostic + '; ' if diagnostic else ''}leaked_descendants={process.token.leaked_descendants}"
    return FileResult(name, status, returncode, clock() - started, log.relative_to(output_dir).as_posix(), diagnostic)


def _launch_failure(name: str, output_dir: Path, error: Exception) -> FileResult:
    log = output_dir / "logs" / f"{name}.log"
    return FileResult(
        name, "launch-failure", None, 0.0, log.relative_to(output_dir).as_posix(), f"launch failure: {error!r}"
    )


def _deadline_failure(
    name: str, output_dir: Path, *, unstarted: bool = False, cause: int = AGGREGATE_DEADLINE
) -> FileResult:
    log = output_dir / "logs" / f"{name}.log"
    return FileResult(
        name,
        "unstarted" if unstarted else REASONS[cause],
        None,
        0.0,
        log.relative_to(output_dir).as_posix(),
        "driver aggregate deadline exceeded" if cause == AGGREGATE_DEADLINE else "driver abort",
    )


def result_from_future(
    future: Future[FileResult], name: str, output_dir: Path, *, cause: int = AGGREGATE_DEADLINE
) -> FileResult:
    if future.cancelled():
        return _deadline_failure(name, output_dir, unstarted=True, cause=cause)
    try:
        return future.result()
    except CancelledError:
        return _deadline_failure(name, output_dir, unstarted=True, cause=cause)


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
    validate_allowlist(serial, files)
    parallel = [name for name in files if name not in serial]
    serial_files = [name for name in files if name in serial]
    tracker = ProcessTracker()
    results: list[FileResult] = []

    def runner(name: str) -> FileResult:
        if tracker.is_closing() or clock() >= deadline:
            return _deadline_failure(
                name,
                output_dir,
                unstarted=True,
                cause=tracker.closing_cause if tracker.is_closing() else AGGREGATE_DEADLINE,
            )
        return run_file(
            name,
            repo=repo,
            python=python,
            output_dir=output_dir,
            temp_root=temp_root,
            timeout_seconds=timeout_seconds,
            tracker=tracker,
            clock=clock,
            deadline=deadline,
        )

    def safe_runner(name: str) -> FileResult:
        try:
            return runner(name)
        except CleanupUnconfirmed as exc:
            return FileResult(name, "cleanup-unconfirmed", None, 0.0, f"logs/{name}.log", str(exc))
        except Exception as exc:  # noqa: BLE001 - each file must yield a failure row.
            return _launch_failure(name, output_dir, exc)

    def record(result: FileResult) -> None:
        results.append(result)
        if on_result is not None:
            on_result(sorted(results, key=lambda item: item.name))

    parallel_pending = parallel
    serial_pending = serial_files
    serial_phase = bool(serial_pending)
    active: dict[Future[FileResult], str] = {}
    executor = ThreadPoolExecutor(max_workers=workers)
    expired = False
    driver_error: BaseException | None = None
    try:
        while parallel_pending or serial_pending or active:
            # Drain the isolated phase completely before submitting parallel work.
            if serial_phase and not active and not serial_pending:
                serial_phase = False
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
                name = active[future]
                result = result_from_future(future, name, output_dir)
                active.pop(future)
                record(result)
                if results[-1].status == "cleanup-unconfirmed" or results[-1].cleanup_error is not None:
                    raise CleanupUnconfirmed("cleanup unconfirmed; retaining temporary target")
    except BaseException as exc:
        driver_error = exc
    finally:
        cleanup_error: BaseException | None = None
        if expired or driver_error is not None:
            cause = AGGREGATE_DEADLINE if expired else DRIVER_ABORT
            for future in active:
                future.cancel()
            cleanup_deadline = time.monotonic() + CLEANUP_TIMEOUT_SECONDS
            try:
                tracker.kill_all(cause=cause, deadline=cleanup_deadline)
            except BaseException as exc:
                cleanup_error = exc
                if isinstance(exc, KeyboardInterrupt):
                    driver_error = driver_error or exc
            for future in active:
                if not future.cancelled():
                    try:
                        future.result(timeout=max(0.0, cleanup_deadline - time.monotonic()))
                    except BaseException as exc:
                        # A user interrupt during cleanup keeps its interrupt status.
                        if isinstance(exc, KeyboardInterrupt):
                            driver_error = driver_error or exc
            # Workers can retain failed closes after the first ownership snapshot.
            # Drain those handles within the same shutdown cause and deadline.
            if tracker._entries:
                try:
                    tracker.kill_all(cause=cause, deadline=cleanup_deadline)
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
                    if isinstance(exc, KeyboardInterrupt):
                        driver_error = driver_error or exc
            for future, name in active.items():
                if future.done():
                    try:
                        results.append(result_from_future(future, name, output_dir, cause=cause))
                    except BaseException as exc:
                        driver_error = driver_error or exc
                        results.append(_deadline_failure(name, output_dir, cause=cause))
                else:
                    row = _deadline_failure(name, output_dir, cause=cause)
                    results.append(replace(row, cleanup_error="worker unfinished after cleanup deadline"))
            accounted = {row.name for row in results}
            results.extend(
                _deadline_failure(name, output_dir, unstarted=True, cause=cause)
                for name in files
                if name not in accounted
            )
            if cleanup_error or any(not future.done() for future in active):
                # Preserve the triggering exception, including KeyboardInterrupt.
                driver_error = driver_error or CleanupUnconfirmed(
                    f"cleanup unconfirmed; retained temporary target: {temp_root}"
                )
                if cleanup_error is not None:
                    active_names = set(active.values())
                    results[:] = [
                        replace(
                            row,
                            cleanup_error=(
                                f"{row.cleanup_error + '; ' if row.cleanup_error else ''}"
                                f"driver cleanup failed: {cleanup_error}"
                            ),
                        )
                        if row.name in active_names
                        else row
                        for row in results
                    ]
        try:
            executor.shutdown(wait=False, cancel_futures=expired or driver_error is not None)
        except BaseException as exc:
            driver_error = driver_error or exc

    snapshot = sorted(results, key=lambda result: result.name)
    # Real results publish incrementally above. Terminal rows publish as one batch,
    # after executor cleanup, and an abort never retries a failed progress callback.
    if expired and driver_error is None and on_result is not None:
        try:
            on_result(snapshot)
        except BaseException as exc:
            driver_error = exc
    if driver_error is not None:
        driver_error.windows_pytest_results = snapshot
        driver_error.windows_pytest_deadline_exceeded = expired
        raise driver_error
    return snapshot


def regressions(results: list[FileResult], allowlist: set[str]) -> list[FileResult]:
    return [result for result in results if result.status in {"failed", "timeout"} and result.name not in allowlist]


def infrastructure_results(results: list[FileResult]) -> list[FileResult]:
    return [
        result
        for result in results
        if result.status not in {"passed", "failed", "timeout"} or result.cleanup_error is not None
    ]


def write_record(
    path: Path,
    *,
    results: list[FileResult],
    driver_error: str | None = None,
    expected_files: list[str] | None = None,
    status: str = "running",
    retained_temp_root: Path | None = None,
    aggregate_deadline_exceeded: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "schema": "brigade.windows_pytest.v2",
        "status": status,
        "aggregate_deadline_exceeded": aggregate_deadline_exceeded,
        "expected_files": expected_files,
        "expected_count": None if expected_files is None else len(expected_files),
        "completed_count": sum(result.status in {"passed", "failed", "timeout"} for result in results),
        "accounted_count": len(results),
        "results": [asdict(result) for result in results],
    }
    if driver_error is not None:
        payload["driver_error"] = driver_error
    if retained_temp_root is not None:
        payload["retained_temp_root"] = str(retained_temp_root)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
    parser.add_argument("--job-started-at", type=float, help="wall-clock epoch captured before CI checkout/setup")
    parser.add_argument("--job-limit", type=float, default=3600)
    parser.add_argument("--startup-reserve", type=float, default=DEFAULT_STARTUP_RESERVE_SECONDS)
    parser.add_argument("--finalize-reserve", type=float, default=DEFAULT_FINALIZE_RESERVE_SECONDS)
    parser.add_argument("--base-ref", help="compare the allowlist with this pull request base commit")
    parser.add_argument("--serial", action="append", default=[], metavar="TEST_FILE")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    entered_monotonic = time.monotonic()
    entered_wall = time.time()
    args = parse_args(sys.argv[1:] if argv is None else argv)
    results: list[FileResult] = []
    partial_results: list[FileResult] = []
    temp_root: Path | None = None
    driver_error: str | None = None
    files: list[str] | None = None
    allowlist: set[str] = set()
    status = "running"
    aggregate_deadline_exceeded = False

    def record_progress(snapshot: list[FileResult]) -> None:
        partial_results[:] = snapshot
        write_record(args.record, results=snapshot, expected_files=files)

    try:
        if args.job_timeout < 1:
            raise ValueError("job timeout must be positive")
        if not math.isfinite(args.job_limit) or args.job_limit <= 0:
            raise ValueError("job limit must be finite and positive")
        if any(not math.isfinite(value) or value < 0 for value in (args.startup_reserve, args.finalize_reserve)):
            raise ValueError("budget reserves must be finite and nonnegative")
        allowance = float(args.job_timeout)
        if args.job_started_at is not None:
            if not math.isfinite(args.job_started_at) or not 0 <= args.job_started_at <= entered_wall:
                raise ValueError("job started epoch must be finite, nonnegative and not in the future")
            allowance = min(
                allowance,
                args.job_limit - (entered_wall - args.job_started_at) - args.startup_reserve - args.finalize_reserve,
            )
        deadline = entered_monotonic + allowance
        write_record(args.record, results=[])
        if not is_windows():
            raise RuntimeError("windows_pytest.py must run on Windows")
        repo = args.repo.resolve()
        allowlist = load_allowlist(args.allowlist)
        selectors = [portable_test_name(name) for name in args.serial]
        if len(selectors) != len(set(selectors)):
            raise ValueError("duplicate serial selectors")
        serial = set(selectors)
        install_console_handler()
        if args.timeout < 1:
            raise ValueError("timeout must be positive")
        if args.workers < 1:
            raise ValueError("workers must be at least 1")
        files = discover_files(repo)
        validate_allowlist(allowlist, files)
        unknown_serial = sorted(serial.difference(files))
        if unknown_serial:
            raise ValueError("serial selectors not discovered: " + ", ".join(unknown_serial))
        record_progress([])
        if args.base_ref:
            check_allowlist_ratchet(allowlist, load_allowlist_from_ref(repo, args.base_ref, args.allowlist))
        if time.monotonic() >= deadline:
            aggregate_deadline_exceeded = True
            results = [_deadline_failure(name, args.record.parent, unstarted=True) for name in files]
        else:
            temp_root = make_temp_root(repo)
            if not files:
                _remove_owned_temp_root(temp_root)
                raise RuntimeError("no test files discovered")
            results = run_files(
                files=files,
                serial=serial,
                repo=repo,
                python=args.python,
                output_dir=args.record.parent,
                temp_root=temp_root,
                workers=args.workers,
                timeout_seconds=args.timeout,
                deadline=deadline,
                on_result=record_progress,
            )
            aggregate_deadline_exceeded = time.monotonic() >= deadline
            if any(result.status == "cleanup-unconfirmed" or result.cleanup_error is not None for result in results):
                raise CleanupUnconfirmed("cleanup unconfirmed; retaining temporary target")
            # run_files returns only after its worker writers have finished.
            _remove_owned_temp_root(temp_root)
        if not files:
            raise RuntimeError("no test files discovered")
        status = (
            "complete"
            if {result.name for result in results} == set(files)
            and not infrastructure_results(results)
            and not aggregate_deadline_exceeded
            else "incomplete"
        )
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - persist partial coverage on driver failures.
        results = getattr(exc, "windows_pytest_results", results or partial_results)
        aggregate_deadline_exceeded = getattr(exc, "windows_pytest_deadline_exceeded", aggregate_deadline_exceeded)
        driver_error = str(exc) or type(exc).__name__
        status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "error"

    try:
        write_record(
            args.record,
            results=results,
            driver_error=driver_error,
            expected_files=files,
            status=status,
            retained_temp_root=temp_root if temp_root is not None and temp_root.exists() else None,
            aggregate_deadline_exceeded=aggregate_deadline_exceeded,
        )
    except OSError as exc:
        print(f"windows pytest driver error: unable to write record: {exc}", file=sys.stderr)
        return 2
    failures = regressions(results, allowlist)
    infra = infrastructure_results(results)
    missing_count = len(set(files or []).difference(result.name for result in results))
    allowlisted_failures = [
        result for result in results if result.status in {"failed", "timeout"} and result.name in allowlist
    ]
    removal_candidates = [result.name for result in results if result.status == "passed" and result.name in allowlist]
    print(
        f"windows pytest: files={len(results)} regressions={len(failures)} removal_candidates={len(removal_candidates)} "
        f"infrastructure_incomplete={len(infra) + missing_count + int(aggregate_deadline_exceeded)} "
        f"allowlisted_failures={len(allowlisted_failures)} "
        f"driver_errors={int(driver_error is not None)} status={status}"
    )
    if removal_candidates:
        print("allowlist removal candidates: " + ", ".join(removal_candidates))
    if driver_error is not None:
        print(f"windows pytest driver error: {driver_error}", file=sys.stderr)
        return 2
    return 1 if failures or status != "complete" else 0


if __name__ == "__main__":
    raise SystemExit(main())
