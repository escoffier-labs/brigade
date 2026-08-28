"""Bounded Backup Steward subprocess runner and process limiter."""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from weakref import WeakKeyDictionary

from .. import proc
from .contracts import BackupError
from .runtime_config import backup_adapter_environment

EXEC_MIN_TIMEOUT_MS = 250
EXEC_MAX_TIMEOUT_MS = 30_000
EXEC_MIN_OUTPUT_BYTES = 1_024
EXEC_MAX_OUTPUT_BYTES = 262_144
EXEC_DEFAULT_OUTPUT_BYTES = 65_536
BACKUP_DEFAULT_MAX_CONCURRENT_PROCESSES = 4
BACKUP_MAX_CONCURRENT_PROCESSES_LIMIT = 8
_READ_CHUNK_BYTES = 4096
_CHILD_CLEANUP_SECONDS = 0.5
_CHILD_FAILURES: WeakKeyDictionary[BackupError, tuple[int, str, str]] = WeakKeyDictionary()


class ChildTimeout(Exception):
    """Internal marker for a timed-out child process."""


class ChildOversize(Exception):
    """Internal marker for stdout/stderr that exceeded the output cap."""


class ChildFailure(Exception):
    """Internal child exit with privately retained bounded output."""

    def __init__(self, exit_code: int, stdout: bytes, stderr: bytes):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        super().__init__("child failed")


@dataclass(frozen=True)
class ExecRequest:
    file: str
    args: tuple[str, ...]
    cwd: str
    timeout_ms: int
    max_buffer_bytes: int
    env: Mapping[str, str]
    stdin: bytes | None = None


@dataclass(frozen=True)
class PrivateExecResult:
    stdout: str


Runner = Callable[[ExecRequest], PrivateExecResult]


class BackupProcessLimiter:
    def __init__(self, max_concurrent: int):
        if type(max_concurrent) is not int or not 1 <= max_concurrent <= BACKUP_MAX_CONCURRENT_PROCESSES_LIMIT:
            raise BackupError("invalid_request", "Backup runtime configuration is invalid")
        self._max = max_concurrent
        self._active = 0
        self._condition = threading.Condition()

    def run(self, work: Callable[[], Any]) -> Any:
        with self._condition:
            while self._active >= self._max:
                self._condition.wait()
            self._active += 1
        try:
            return work()
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify()


def create_process_limiter(max_concurrent: int = BACKUP_DEFAULT_MAX_CONCURRENT_PROCESSES) -> BackupProcessLimiter:
    return BackupProcessLimiter(max_concurrent)


def _bounded_int(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def child_failure_output(error: BackupError) -> tuple[int, str, str] | None:
    return _CHILD_FAILURES.get(error)


def run_exec(request: ExecRequest, *, runner: Runner | None = None) -> PrivateExecResult:
    if not _bounded_int(request.timeout_ms, EXEC_MIN_TIMEOUT_MS, EXEC_MAX_TIMEOUT_MS):
        raise BackupError("invalid_request", "Backup execution request is invalid")
    if not _bounded_int(request.max_buffer_bytes, EXEC_MIN_OUTPUT_BYTES, EXEC_MAX_OUTPUT_BYTES):
        raise BackupError("invalid_request", "Backup execution request is invalid")
    prepared = ExecRequest(
        file=request.file,
        args=tuple(request.args),
        cwd=request.cwd,
        timeout_ms=request.timeout_ms,
        max_buffer_bytes=request.max_buffer_bytes,
        env=backup_adapter_environment(request.env),
        stdin=request.stdin,
    )
    try:
        return (runner or _subprocess_runner)(prepared)
    except ChildTimeout:
        raise BackupError("timeout", "Backup observation timed out") from None
    except ChildOversize:
        raise BackupError("protocol_error", "Backup observation was invalid") from None
    except ChildFailure as exc:
        error = BackupError("unavailable", "Backup observation is unavailable")
        _CHILD_FAILURES[error] = (
            exc.exit_code,
            _decode_bounded(exc.stdout, request.max_buffer_bytes),
            _decode_bounded(exc.stderr, request.max_buffer_bytes),
        )
        raise error from None
    except BackupError:
        raise
    except Exception:
        raise BackupError("unavailable", "Backup observation is unavailable") from None


def _close_pipe(stream: Any) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except OSError:
        pass


def _write_child_stdin(process: subprocess.Popen[bytes], payload: bytes | None) -> None:
    if process.stdin is None:
        return
    try:
        if payload:
            process.stdin.write(payload)
            process.stdin.flush()
    except OSError:
        pass
    finally:
        _close_pipe(process.stdin)


def _reap_child_group(process: subprocess.Popen[bytes]) -> None:
    proc.terminate_process_tree(process, terminate_grace=0.2, kill_grace=0.2)
    for stream in (process.stdin, process.stdout, process.stderr):
        _close_pipe(stream)


def _join_bounded(threads: tuple[threading.Thread, ...], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        thread.join(timeout=remaining)


def _read_chunk(stream: Any) -> bytes:
    try:
        return stream.read(_READ_CHUNK_BYTES) or b""
    except (OSError, ValueError):
        return b""


def _decode_bounded(data: bytes, maximum: int) -> str:
    return data[:maximum].decode("utf-8", errors="replace")


def _subprocess_runner(request: ExecRequest) -> PrivateExecResult:
    try:
        process = subprocess.Popen(
            [request.file, *request.args],
            cwd=request.cwd,
            env=dict(request.env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            **proc.process_group_kwargs(),
        )
    except OSError as exc:
        raise ChildFailure(-1, b"", b"") from exc

    cap = request.max_buffer_bytes
    stdout = bytearray()
    stderr = bytearray()
    overflowed = threading.Event()
    wake = threading.Event()

    def read_into(stream: Any, buf: bytearray) -> None:
        try:
            while True:
                chunk = _read_chunk(stream)
                if not chunk:
                    return
                if overflowed.is_set() or len(buf) + len(chunk) > cap:
                    overflowed.set()
                    return
                buf.extend(chunk)
        except (OSError, ValueError):
            return
        finally:
            wake.set()

    readers = (
        threading.Thread(target=read_into, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=read_into, args=(process.stderr, stderr), daemon=True),
    )
    for reader in readers:
        reader.start()
    stdin_thread = threading.Thread(target=_write_child_stdin, args=(process, request.stdin), daemon=True)
    stdin_thread.start()

    deadline = time.monotonic() + (request.timeout_ms / 1000)
    timed_out = False
    child_exited_at: float | None = None
    try:
        while True:
            readers_alive = any(reader.is_alive() for reader in readers)
            if overflowed.is_set() or (process.poll() is not None and not readers_alive):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            wait_for = remaining
            if process.poll() is not None:
                if child_exited_at is None:
                    child_exited_at = time.monotonic()
                drain_left = _CHILD_CLEANUP_SECONDS - (time.monotonic() - child_exited_at)
                if drain_left <= 0:
                    timed_out = True
                    break
                wait_for = min(wait_for, drain_left)
            wake.clear()
            readers_alive = any(reader.is_alive() for reader in readers)
            if overflowed.is_set() or (process.poll() is not None and not readers_alive):
                break
            wake.wait(timeout=wait_for)
    finally:
        if timed_out or overflowed.is_set():
            _reap_child_group(process)
        _join_bounded((*readers, stdin_thread), _CHILD_CLEANUP_SECONDS)
        if process.poll() is None:
            _reap_child_group(process)
            try:
                process.wait(timeout=_CHILD_CLEANUP_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                pass
        _close_pipe(process.stdout)
        _close_pipe(process.stderr)

    if overflowed.is_set():
        raise ChildOversize()
    if timed_out:
        raise ChildTimeout()
    if process.returncode:
        raise ChildFailure(int(process.returncode), bytes(stdout), bytes(stderr))
    return PrivateExecResult(stdout=_decode_bounded(bytes(stdout), cap))
