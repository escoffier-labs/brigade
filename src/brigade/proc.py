"""Run external tool CLIs and capture their results. No tool is imported in-process."""

from __future__ import annotations

import codecs
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
# Trusted metadata enumeration (git path listings) scales with repository
# contents, not model or tool output, so it gets its own larger streaming
# budget instead of the generic child-output cap.
MAX_DELIMITED_BYTES = MAX_CAPTURE_BYTES * 16
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
    out_text, err_text = bound_text_pair(out_text, err_text)
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


def bound_text(text: str, max_bytes: int | None = None) -> str:
    """Return ``text`` truncated to at most ``max_bytes`` of UTF-8."""

    limit = MAX_CAPTURE_BYTES if max_bytes is None else max_bytes
    raw = text.encode(_STREAM_ENCODING)
    if len(raw) <= limit:
        return text
    return raw[:limit].decode(_STREAM_ENCODING, errors="ignore")


def bound_text_pair(stdout: str, stderr: str, max_bytes: int | None = None) -> tuple[str, str]:
    """Keep combined UTF-8 of ``stdout`` + ``stderr`` at or below ``max_bytes``."""

    limit = MAX_CAPTURE_BYTES if max_bytes is None else max_bytes
    out = bound_text(stdout, limit)
    remaining = max(0, limit - len(out.encode(_STREAM_ENCODING)))
    return out, bound_text(stderr, remaining)


class ByteBudget:
    """Count retained bytes and refuse further accumulation after the cap."""

    def __init__(self, max_bytes: int | None = None) -> None:
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self.used = 0
        self.overflowed = False

    @property
    def max_bytes(self) -> int:
        return MAX_CAPTURE_BYTES if self._max_bytes is None else self._max_bytes

    def try_add(self, n: int) -> bool:
        """Accept all ``n`` bytes, or reject and trip overflow."""

        if n <= 0:
            return not self.overflowed
        with self._lock:
            if self.overflowed:
                return False
            if self.used + n > self.max_bytes:
                self.overflowed = True
                return False
            self.used += n
            return True

    def accept(self, n: int) -> int:
        """Accept as many of ``n`` bytes as remain; trip if truncated."""

        if n <= 0:
            return 0
        with self._lock:
            if self.overflowed:
                return 0
            remaining = self.max_bytes - self.used
            if remaining <= 0:
                self.overflowed = True
                return 0
            taken = n if n <= remaining else remaining
            self.used += taken
            if n > remaining:
                self.overflowed = True
            return taken


@dataclass(frozen=True)
class DelimitedResult:
    """Streaming delimiter-split child output for trusted metadata queries.

    Unlike ``Result``, items are decoded and split incrementally under a
    dedicated byte budget (``MAX_DELIMITED_BYTES``), so a large but finite
    listing succeeds without touching the generic ``MAX_CAPTURE_BYTES`` cap.
    """

    code: int
    items: list[str]
    stderr: str
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    truncated: bool = False
    timed_out: bool = False
    stderr_truncated: bool = False


class _DelimitedSplitter:
    """Incrementally decode UTF-8 text and split fields on one delimiter.

    Retains only completed fields plus the current partial field, so memory
    stays bounded by the byte budget rather than by raw child output.
    """

    def __init__(self, delimiter: bytes, max_bytes: int) -> None:
        if len(delimiter) != 1:
            raise ValueError("delimiter must be exactly one byte")
        self._delimiter_char = delimiter.decode("ascii")
        self._max_bytes = max_bytes
        self._decoder = codecs.getincrementaldecoder(_STREAM_ENCODING)("replace")
        self._pending = ""
        self.items: list[str] = []
        self.bytes_seen = 0
        self.overflowed = False

    def feed(self, chunk: bytes) -> bool:
        """Consume chunk; return False once the budget is exhausted."""

        if self.overflowed or not chunk:
            return not self.overflowed
        room = self._max_bytes - self.bytes_seen
        if len(chunk) > room:
            chunk = chunk[:room] if room > 0 else b""
            self.overflowed = True
        if not chunk:
            return False
        self.bytes_seen += len(chunk)
        text = self._decoder.decode(chunk)
        parts = (self._pending + text).split(self._delimiter_char)
        self._pending = parts.pop()
        self.items.extend(parts)
        return True

    def finish(self) -> None:
        """Flush the decoder; keep any final field lacking a trailing delimiter."""

        tail = self._pending + self._decoder.decode(b"", final=True)
        self._pending = ""
        if tail:
            self.items.append(tail)


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

    stdin_thread: threading.Thread | None = None
    if process.stdin is not None:
        stdin_thread = threading.Thread(target=_write_stdin, args=(process, stdin), daemon=True)
        stdin_thread.start()

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
    if stdin_thread is not None:
        stdin_thread.join(timeout=_TIMED_OUT_DRAIN_SECONDS)
    if _readers_alive(threads):
        _stop_child(process, process_registry)
        _join_readers(threads, _TIMED_OUT_DRAIN_SECONDS)
        if stdin_thread is not None:
            stdin_thread.join(timeout=_TIMED_OUT_DRAIN_SECONDS)

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
    if extras:
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
    return result


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


def run_delimited(
    args: List[str],
    *,
    delimiter: bytes = b"\x00",
    timeout: float = 30.0,
    env: Optional[dict] = None,
    cwd: Optional[Path] = None,
    max_bytes: Optional[int] = None,
) -> DelimitedResult:
    """Stream a delimiter-separated child listing under a dedicated budget.

    For trusted metadata enumeration (git path listings) whose output scales
    with repository contents, not with model or tool output. Output is decoded
    and split incrementally, so memory stays bounded by ``max_bytes`` plus one
    partial field instead of by the whole child stream. The generic
    ``MAX_CAPTURE_BYTES`` cap used for untrusted command output is untouched.
    ``max_bytes`` bounds raw stream bytes: retained fields are Python strings,
    so a listing of degenerately tiny fields amplifies heap use by a constant
    factor over the budget.
    """

    budget = MAX_DELIMITED_BYTES if max_bytes is None else max_bytes
    splitter = _DelimitedSplitter(delimiter=delimiter, max_bytes=budget)
    stderr_budget = ByteBudget()
    stderr_buf = bytearray()

    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=env,
            cwd=cwd,
            shell=False,
            **_process_group_kwargs(),
        )
    except OSError as exc:
        code, message = _launch_failure(args, exc)
        return DelimitedResult(code=code, items=[], stderr=message)

    overflow = threading.Event()

    def read_stdout() -> None:
        assert process.stdout is not None
        try:
            while True:
                chunk = process.stdout.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                if not splitter.feed(chunk):
                    overflow.set()
                    break
        except OSError:
            pass

    def drain_stderr() -> None:
        assert process.stderr is not None
        try:
            while True:
                chunk = process.stderr.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                # Once the budget trips, keep draining so the child cannot
                # block on a full stderr pipe; excess bytes are discarded and
                # flagged via stderr_budget.overflowed.
                if stderr_budget.try_add(len(chunk)):
                    stderr_buf.extend(chunk)
        except OSError:
            pass

    threads = [
        threading.Thread(target=read_stdout, daemon=True),
        threading.Thread(target=drain_stderr, daemon=True),
    ]
    for reader in threads:
        reader.start()

    timed_out = False
    deadline = time.monotonic() + max(timeout, 0.0)
    stopped = False
    try:
        while True:
            if splitter.overflowed:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            process_exited = process.poll() is not None
            if process_exited and not _readers_alive(threads):
                break
            overflow.wait(timeout=min(0.05, remaining))

        if timed_out or splitter.overflowed or process.poll() is None:
            _stop_child(process, None)
            stopped = True

        _join_readers(threads, _TIMED_OUT_DRAIN_SECONDS)
        if _readers_alive(threads):
            if not stopped:
                _stop_child(process, None)
                stopped = True
            _join_readers(threads, _TIMED_OUT_DRAIN_SECONDS)

        if process.poll() is None:
            if not stopped:
                _stop_child(process, None)
                stopped = True
            try:
                process.wait(timeout=_TIMED_OUT_DRAIN_SECONDS)
            except subprocess.TimeoutExpired:
                pass
    except BaseException:
        if not stopped:
            _stop_child(process, None)
        _join_readers(threads, _TIMED_OUT_DRAIN_SECONDS)
        raise
    finally:
        for stream_name in ("stdout", "stderr", "stdin"):
            stream = getattr(process, stream_name, None)
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    truncated = splitter.overflowed
    if not truncated and not timed_out:
        splitter.finish()
    code = process.returncode
    if timed_out:
        code = 124
    elif code is None:
        code = -1

    err_text, _err_error = _decode_stream(bytes(stderr_buf), stream="stderr")
    extras: list[str] = []
    if truncated:
        extras.append(f"streaming output exceeded {budget} byte delimiter-split limit")
    if timed_out:
        extras.append(f"timeout after {timeout}s")
    if stderr_budget.overflowed:
        extras.append(f"stderr capture exceeded {stderr_budget.max_bytes} byte limit")
    if extras:
        err_text = (err_text + "\n" if err_text else "") + "\n".join(extras)

    return DelimitedResult(
        code=code,
        items=splitter.items,
        stderr=err_text,
        stdout_bytes=splitter.bytes_seen,
        stderr_bytes=len(stderr_buf),
        truncated=truncated,
        timed_out=timed_out,
        stderr_truncated=stderr_budget.overflowed,
    )
