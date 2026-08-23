"""Run external tool CLIs and capture their results. No tool is imported in-process."""

from __future__ import annotations

import errno
import json
import ntpath
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

_STREAM_ENCODING = "utf-8"
MAX_CAPTURE_BYTES = 1_048_576
_READ_CHUNK_BYTES = 4096
_UNSUPPORTED_WINDOWS_SUFFIXES: dict[str, str] = {
    ".ps1": "PowerShell script",
    ".vbs": "VBScript",
    ".js": "JavaScript file",
    ".py": "Python script",
    ".jar": "Java archive",
    ".msi": "Windows installer",
}
_WINDOWS_BATCH_SUFFIXES = frozenset({".cmd", ".bat"})
_WINDOWS_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
_TIMED_OUT_DRAIN_SECONDS = 0.5


class ProcessRegistry:
    """Own cancellable subprocess groups for one worker dispatch."""

    def __init__(self, *, terminate_grace: float = 0.5, kill_grace: float = 0.5) -> None:
        self._terminate_grace = terminate_grace
        self._kill_grace = kill_grace
        self._lock = threading.Lock()
        self._processes: dict[subprocess.Popen[bytes], str | None] = {}
        self._canceled = False

    def register(self, process: subprocess.Popen[bytes], *, seat: str | None = None) -> None:
        with self._lock:
            if not self._canceled:
                self._processes[process] = seat
                return
        _terminate_processes(
            (process,),
            terminate_grace=self._terminate_grace,
            kill_grace=self._kill_grace,
        )

    def unregister(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._processes.pop(process, None)

    def for_seat(self, seat: str) -> _SeatProcessRegistry:
        return _SeatProcessRegistry(self, seat)

    def cancel(self) -> tuple[ProcessCancelOutcome, ...]:
        with self._lock:
            self._canceled = True
            processes = tuple(self._processes.items())
        try:
            _terminate_processes(
                tuple(process for process, _seat in processes),
                terminate_grace=self._terminate_grace,
                kill_grace=self._kill_grace,
            )
        except Exception:
            return tuple(ProcessCancelOutcome(seat, "error") for _process, seat in processes)
        return tuple(
            ProcessCancelOutcome(seat, "interrupted" if process.poll() is not None else "still_active")
            for process, seat in processes
        )

    def terminate(self, process: subprocess.Popen[bytes]) -> None:
        _terminate_processes(
            (process,),
            terminate_grace=self._terminate_grace,
            kill_grace=self._kill_grace,
        )


@dataclass(frozen=True)
class ProcessCancelOutcome:
    """Safe observed state after a cancellation request for one registered process."""

    seat: str | None
    result: str


class _SeatProcessRegistry:
    """Label process registrations with a Brigade seat without changing adapters."""

    def __init__(self, parent: ProcessRegistry, seat: str) -> None:
        self._parent = parent
        self._seat = seat

    def register(self, process: subprocess.Popen[bytes]) -> None:
        self._parent.register(process, seat=self._seat)

    def unregister(self, process: subprocess.Popen[bytes]) -> None:
        self._parent.unregister(process)

    def cancel(self) -> tuple[ProcessCancelOutcome, ...]:
        return self._parent.cancel()

    def terminate(self, process: subprocess.Popen[bytes]) -> None:
        self._parent.terminate(process)


def _signal_process_group(process: subprocess.Popen[bytes], sig: int) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, sig)
        elif sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except OSError:
        pass


def _terminate_windows_process_tree(process: subprocess.Popen[bytes], *, timeout: float) -> None:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=max(timeout, 0.1),
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def _wait_for_processes(processes: tuple[subprocess.Popen[bytes], ...], timeout: float) -> None:
    deadline = time.monotonic() + max(timeout, 0.0)
    while any(process.poll() is None for process in processes):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.01, remaining))


def _terminate_processes(
    processes: tuple[subprocess.Popen[bytes], ...],
    *,
    terminate_grace: float,
    kill_grace: float,
) -> None:
    if not processes:
        return
    if os.name == "nt":
        timeout = terminate_grace + kill_grace
        for process in processes:
            _terminate_windows_process_tree(process, timeout=timeout)
        _wait_for_processes(processes, kill_grace)
        return
    for process in processes:
        _signal_process_group(process, signal.SIGTERM)
    _wait_for_processes(processes, terminate_grace)
    for process in processes:
        _signal_process_group(process, signal.SIGKILL)
    _wait_for_processes(processes, kill_grace)


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str
    stdout_decode_error: str | None = None
    stderr_decode_error: str | None = None
    output_limit_exceeded: bool = False
    stdout_bytes: int = 0
    stderr_bytes: int = 0

    @property
    def decode_failed(self) -> bool:
        return self.stdout_decode_error is not None or self.stderr_decode_error is not None

    @property
    def decode_failure_detail(self) -> str:
        return self.stderr_decode_error or self.stdout_decode_error or "child output is not valid UTF-8"

    @property
    def total_bytes(self) -> int:
        return self.stdout_bytes + self.stderr_bytes

    def json(self) -> Optional[object]:
        try:
            return json.loads(self.stdout)
        except (json.JSONDecodeError, ValueError):
            return None


def _decode_stream(value: str | bytes | None, *, stream: str) -> tuple[str, str | None]:
    if value is None:
        return "", None
    if isinstance(value, str):
        return value, None
    try:
        return value.decode(_STREAM_ENCODING), None
    except UnicodeDecodeError as exc:
        replaced = value.decode(_STREAM_ENCODING, errors="replace")
        return replaced, f"child {stream} is not valid UTF-8 ({_STREAM_ENCODING}): {exc}"


def _decoded_output(stdout: str | bytes | None, stderr: str | bytes | None) -> tuple[str, str, str | None, str | None]:
    out_text, out_error = _decode_stream(stdout, stream="stdout")
    err_text, err_error = _decode_stream(stderr, stream="stderr")
    return out_text, err_text, out_error, err_error


def _observed_bytes(value: str | bytes | None, decoded: str) -> int:
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if isinstance(value, str):
        return len(value.encode(_STREAM_ENCODING))
    return len(decoded.encode(_STREAM_ENCODING))


def _result_from_output(
    *,
    code: int,
    stdout: str | bytes | None,
    stderr: str | bytes | None,
    stdout_bytes: int | None = None,
    stderr_bytes: int | None = None,
    output_limit_exceeded: bool = False,
) -> Result:
    out_text, err_text, stdout_error, stderr_error = _decoded_output(stdout, stderr)
    for stream_error in (stdout_error, stderr_error):
        if stream_error is not None:
            if err_text and not err_text.endswith("\n"):
                err_text += "\n"
            err_text += stream_error
    return Result(
        code=code,
        stdout=out_text,
        stderr=err_text,
        stdout_decode_error=stdout_error,
        stderr_decode_error=stderr_error,
        output_limit_exceeded=output_limit_exceeded,
        stdout_bytes=_observed_bytes(stdout, out_text) if stdout_bytes is None else stdout_bytes,
        stderr_bytes=_observed_bytes(stderr, err_text) if stderr_bytes is None else stderr_bytes,
    )


def bound_text(text: str, max_bytes: int = MAX_CAPTURE_BYTES) -> str:
    """Return ``text`` truncated to at most ``max_bytes`` of UTF-8."""

    raw = text.encode(_STREAM_ENCODING)
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode(_STREAM_ENCODING, errors="ignore")


def _process_group_kwargs() -> dict[str, Any]:
    if os.name == "posix":
        return {"start_new_session": True}
    if os.name == "nt":
        return {"creationflags": _WINDOWS_NEW_PROCESS_GROUP}
    return {}


class _BoundedCollector:
    """Retain combined stdout/stderr up to ``MAX_CAPTURE_BYTES``."""

    def __init__(self, max_bytes: int = MAX_CAPTURE_BYTES) -> None:
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._buffers = {"stdout": bytearray(), "stderr": bytearray()}
        self.stdout_bytes = 0
        self.stderr_bytes = 0
        self.overflowed = False
        self.overflow = threading.Event()

    def feed(self, stream: str, data: bytes) -> bool:
        """Retain bytes under the combined cap. Return False after overflow."""

        if not data:
            return not self.overflowed
        with self._lock:
            if self.overflowed:
                return False
            remaining = self.max_bytes - (self.stdout_bytes + self.stderr_bytes)
            if remaining <= 0:
                self.overflowed = True
                self.overflow.set()
                return False
            accepted = data[:remaining]
            self._buffers[stream].extend(accepted)
            if stream == "stdout":
                self.stdout_bytes += len(accepted)
            else:
                self.stderr_bytes += len(accepted)
            if len(data) > remaining:
                self.overflowed = True
                self.overflow.set()
                return False
            return True

    def snapshot(self) -> tuple[bytes, bytes]:
        with self._lock:
            return bytes(self._buffers["stdout"]), bytes(self._buffers["stderr"])


def _stop_child(process: subprocess.Popen[bytes], process_registry: ProcessRegistry | None) -> None:
    if process_registry is not None:
        process_registry.terminate(process)
        return
    _terminate_processes((process,), terminate_grace=0.5, kill_grace=0.5)


def _write_stdin(process: subprocess.Popen[bytes], stdin: bytes | None) -> None:
    if process.stdin is None:
        return
    try:
        if stdin is not None:
            process.stdin.write(stdin)
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass


def _join_readers(threads: list[threading.Thread], timeout: float) -> None:
    deadline = time.monotonic() + max(timeout, 0.0)
    for reader in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        reader.join(timeout=remaining)


def _readers_alive(threads: list[threading.Thread]) -> bool:
    return any(reader.is_alive() for reader in threads)


def _collect_process_output(
    process: subprocess.Popen[bytes],
    *,
    timeout: float,
    stdin: bytes | None,
    process_registry: ProcessRegistry | None,
) -> Result:
    collector = _BoundedCollector()

    def read_stream(stream: Any, name: str) -> None:
        try:
            while True:
                chunk = stream.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                if not collector.feed(name, chunk):
                    break
        except OSError:
            pass

    threads: list[threading.Thread] = []
    for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        if stream is None:
            continue
        reader = threading.Thread(target=read_stream, args=(stream, name), daemon=True)
        reader.start()
        threads.append(reader)

    _write_stdin(process, stdin)

    timed_out = False
    deadline = time.monotonic() + max(timeout, 0.0)
    try:
        while True:
            if collector.overflowed:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            process_exited = process.poll() is not None
            if process_exited and not _readers_alive(threads):
                break
            collector.overflow.wait(timeout=min(0.05, remaining))
    except BaseException:
        _stop_child(process, process_registry)
        raise

    if timed_out or collector.overflowed:
        _stop_child(process, process_registry)

    _join_readers(threads, _TIMED_OUT_DRAIN_SECONDS)
    if _readers_alive(threads):
        _stop_child(process, process_registry)
        _join_readers(threads, _TIMED_OUT_DRAIN_SECONDS)

    if process.poll() is None:
        _stop_child(process, process_registry)
        try:
            process.wait(timeout=_TIMED_OUT_DRAIN_SECONDS)
        except subprocess.TimeoutExpired:
            pass

    stdout_bytes, stderr_bytes = collector.snapshot()
    code = process.returncode
    if timed_out:
        code = 124
    elif code is None:
        code = -1

    result = _result_from_output(
        code=code,
        stdout=stdout_bytes,
        stderr=stderr_bytes,
        stdout_bytes=collector.stdout_bytes,
        stderr_bytes=collector.stderr_bytes,
        output_limit_exceeded=collector.overflowed,
    )
    extras: list[str] = []
    if collector.overflowed:
        extras.append(f"combined output exceeded {MAX_CAPTURE_BYTES} byte limit")
    if timed_out:
        extras.append(f"timeout after {timeout}s")
    if not extras:
        return result
    stderr = result.stderr
    if stderr and not stderr.endswith("\n"):
        stderr += "\n"
    return Result(
        code=result.code,
        stdout=result.stdout,
        stderr=stderr + "\n".join(extras),
        stdout_decode_error=result.stdout_decode_error,
        stderr_decode_error=result.stderr_decode_error,
        output_limit_exceeded=result.output_limit_exceeded,
        stdout_bytes=result.stdout_bytes,
        stderr_bytes=result.stderr_bytes,
    )


@dataclass(frozen=True)
class ExecutableIdentity:
    """Resolved adapter executable identity safe for public diagnostics."""

    command: str
    path: str | None
    kind: str
    runnable: bool
    detail: str


def _native_executable_remediation(command: str) -> str:
    return f"add the native {command} executable directory to PATH instead of the shim"


def _looks_like_windows_pe_executable(path: Path) -> bool:
    """Return True when path begins with a Windows PE executable signature."""

    try:
        with path.open("rb") as handle:
            if handle.read(2) != b"MZ":
                return False
            handle.seek(0x3C)
            pe_offset_bytes = handle.read(4)
            if len(pe_offset_bytes) != 4:
                return False
            pe_offset = int.from_bytes(pe_offset_bytes, "little")
            handle.seek(pe_offset)
            return handle.read(4) == b"PE\0\0"
    except OSError:
        return False


def _windows_executable_kind(path: Path, *, raw_path: str, command: str) -> tuple[str, bool, str]:
    suffix = ntpath.splitext(raw_path)[1].lower()
    basename = ntpath.splitext(ntpath.basename(raw_path))[0] or path.name
    if suffix == ".exe":
        return "exe", True, f"{basename} resolves to a supported Windows exe executable"
    if suffix in _WINDOWS_BATCH_SUFFIXES:
        shim_kind = suffix[1:]
        return (
            shim_kind,
            False,
            (
                f"{basename} resolves to an unsupported Windows {shim_kind} shim; "
                f"{_native_executable_remediation(command)}"
            ),
        )
    if suffix in _UNSUPPORTED_WINDOWS_SUFFIXES:
        shim = _UNSUPPORTED_WINDOWS_SUFFIXES[suffix]
        return (
            f"unsupported{suffix}",
            False,
            (f"{basename} resolves to an unsupported Windows {shim}; {_native_executable_remediation(command)}"),
        )
    if suffix:
        return (
            "unsupported",
            False,
            (
                f"{basename} resolves to an unsupported Windows executable kind ({suffix}); "
                f"add the native {command} executable directory to PATH"
            ),
        )
    if _looks_like_windows_pe_executable(path):
        return "native", True, f"{basename} resolves to a supported Windows native executable"
    return (
        "npm-shim",
        False,
        (f"{basename} resolves to an unsupported Windows npm shim; {_native_executable_remediation(command)}"),
    )


def _posix_executable_kind(command: str) -> tuple[str, bool, str]:
    return "native", True, f"{command} is available on PATH"


def which(cmd: str, path: str | None = None) -> Optional[str]:
    return shutil.which(cmd, path=path)


def resolve_executable(command: str, path: str | None = None) -> ExecutableIdentity:
    """Resolve a command name once for detection and dispatch.

    Public diagnostics intentionally omit user-specific absolute paths.
    """

    resolved_path = which(command) if path is None else which(command, path=path)
    if resolved_path is None:
        return ExecutableIdentity(
            command=command,
            path=None,
            kind="missing",
            runnable=False,
            detail=f"{command} is not on PATH",
        )

    resolved = Path(resolved_path)
    if sys.platform == "win32":
        kind, runnable, detail = _windows_executable_kind(resolved, raw_path=resolved_path, command=command)
    else:
        kind, runnable, detail = _posix_executable_kind(command)

    return ExecutableIdentity(
        command=command,
        path=resolved_path,
        kind=kind,
        runnable=runnable,
        detail=detail,
    )


def _launch_failure(argv: List[str], exc: OSError) -> tuple[int, str]:
    command = ntpath.basename(argv[0]) if argv else "command"
    if isinstance(exc, FileNotFoundError):
        return 127, f"command not found: {command}"
    if isinstance(exc, PermissionError) or exc.errno in {errno.EACCES, errno.EPERM}:
        return 126, f"command permission denied: {command}"
    if exc.errno == errno.ENOEXEC:
        return 126, f"command has invalid executable format: {command}"
    return 126, f"command launch failed: {command}"


def run(
    args: List[str],
    timeout: float = 30.0,
    env: Optional[dict] = None,
    cwd: Optional[Path] = None,
    stdin: bytes | None = None,
    process_registry: ProcessRegistry | None = None,
) -> Result:
    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
            env=env,
            cwd=cwd,
            shell=False,
            **_process_group_kwargs(),
        )
    except OSError as exc:
        code, message = _launch_failure(args, exc)
        return Result(code=code, stdout="", stderr=message)

    if process_registry is not None:
        process_registry.register(process)
    try:
        return _collect_process_output(
            process,
            timeout=timeout,
            stdin=stdin,
            process_registry=process_registry,
        )
    finally:
        if process_registry is not None:
            process_registry.unregister(process)
        for stream_name in ("stdout", "stderr", "stdin"):
            stream = getattr(process, stream_name, None)
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
