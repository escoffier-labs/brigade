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
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

_STREAM_ENCODING = "utf-8"
MAX_CAPTURE_BYTES = 1_048_576
# Child output is drained beyond the retained capture cap. This limits the
# untrusted transport stream without treating transient output as retained
# memory.
MAX_STREAM_BYTES = MAX_CAPTURE_BYTES * 16
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
# Forced nonzero exit for a terminated post-exit process group: the direct
# child's zero must never stand once its output pipes were cut off mid-stream.
_INCOMPLETE_GROUP_EXIT_CODE = 125


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


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_CLASS = 9
_WINDOWS_CREATE_SUSPENDED = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_WINDOWS_DETACHED_PROCESS = 0x00000008
_WINDOWS_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
# CreateProcess refuses CREATE_BREAKAWAY_FROM_JOB with ERROR_ACCESS_DENIED when
# the ambient job object was not opened with JOB_OBJECT_LIMIT_BREAKAWAY_OK.
_WINDOWS_BREAKAWAY_DENIED_WINERROR = 5
DETACH_MODE_POSIX_SESSION = "posix-new-session"
DETACH_MODE_WINDOWS_JOB_BREAKAWAY = "windows-detached-new-group-job-breakaway"
DETACH_MODE_WINDOWS_NO_BREAKAWAY = "windows-detached-new-group"
DETACH_MODE_INHERITED = "inherited"


class DetachedLaunchError(RuntimeError):
    """A requested durable detach cannot be guaranteed on this platform."""


class _WindowsChildJob:
    """Owned kill-on-close job object binding one launched child process tree."""

    def __init__(self, kernel32: Any, handle: int) -> None:
        self._kernel32 = kernel32
        self._handle = handle

    def assign(self, process_handle: int) -> bool:
        """Bind the direct child so every descendant joins the owned tree."""
        return bool(self._kernel32.AssignProcessToJobObject(self._handle, process_handle))

    def resume(self, pid: int) -> bool:
        """Resume a CREATE_SUSPENDED child's main thread once it is job-assigned."""
        return _resume_windows_main_thread(self._kernel32, pid)

    def terminate(self) -> bool:
        """Terminate every process in the owned tree, including escaped descendants."""
        return bool(self._kernel32.TerminateJobObject(self._handle, _INCOMPLETE_GROUP_EXIT_CODE))

    def close(self) -> None:
        """Release the kill-on-close handle once the tree is reaped."""
        self._kernel32.CloseHandle(self._handle)


class _WindowsJobLaunchFailure(RuntimeError):
    """Typed failure to create or bind the owned job object around one launch."""


def _create_windows_child_job() -> _WindowsChildJob | None:
    """Create a kill-on-close job object on Windows; return None when unavailable."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        loader_factory = getattr(ctypes, "WinDLL", None)
        if loader_factory is None:
            return None
        kernel32 = loader_factory("kernel32", use_last_error=True)
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_CLASS,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            kernel32.CloseHandle(handle)
            return None
        return _WindowsChildJob(kernel32, handle)
    except Exception:
        return None


def _resume_windows_main_thread(kernel32: Any, pid: int) -> bool:
    """Resume the suspended main thread of ``pid``; False when it cannot be resumed.

    A CREATE_SUSPENDED child stays frozen until its first thread is resumed,
    so the job assignment in ``run`` closes the grandchild-spawn window. The
    thread handle is resolved through the tool-help snapshot because Popen
    closes its own main-thread handle immediately after CreateProcess.
    """
    import ctypes

    snapshot_factory = getattr(kernel32, "CreateToolhelp32Snapshot", None)
    open_thread = getattr(kernel32, "OpenThread", None)
    resume_thread = getattr(kernel32, "ResumeThread", None)
    if snapshot_factory is None or open_thread is None or resume_thread is None:
        return False
    try:
        th32cs_snapthread = 0x00000004
        snapshot_factory.restype = ctypes.c_void_p
        snapshot = snapshot_factory(th32cs_snapthread, 0)
        invalid_handle = ctypes.c_void_p(-1).value
        if snapshot is None or snapshot == invalid_handle:
            return False

        class _ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.c_uint32),
                ("cntUsage", ctypes.c_uint32),
                ("th32ThreadID", ctypes.c_uint32),
                ("th32OwnerProcessID", ctypes.c_uint32),
                ("tpBasePri", ctypes.c_long),
                ("tpDeltaPri", ctypes.c_long),
                ("dwFlags", ctypes.c_uint32),
            ]

        entry = _ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        thread_id: int | None = None
        if kernel32.Thread32First(snapshot, ctypes.byref(entry)):
            while True:
                if entry.th32OwnerProcessID == pid:
                    thread_id = int(entry.th32ThreadID)
                    break
                if not kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                    break
        if thread_id is None:
            return False
        thread = open_thread(_THREAD_SUSPEND_RESUME, False, thread_id)
        if not thread:
            return False
        try:
            # ResumeThread returns the previous suspend count; loop until the
            # count reaches zero instead of assuming one suspend. A return of
            # 0xFFFFFFFF is (DWORD)-1, i.e. failure: report it so the caller
            # terminates the still-suspended child rather than leaving it
            # frozen on its job assignment until timeout.
            for _ in range(32):
                previous = resume_thread(thread)
                if previous == 0xFFFFFFFF:
                    return False
                if previous == 0:
                    break
        finally:
            kernel32.CloseHandle(thread)
        return True
    except Exception:
        return False
    finally:
        try:
            kernel32.CloseHandle(snapshot)
        except Exception:
            pass


def _bind_suspended_windows_child(
    job: _WindowsChildJob,
    process: subprocess.Popen[bytes],
    argv: List[str],
) -> str | None:
    """Assign the still-suspended child to the pre-created job and resume it.

    Returns ``None`` on success. On failure the suspended child never ran any
    user code: terminate it directly, release the job handle, and return the
    typed launch-failure message instead of silently falling back to taskkill.
    """
    process_handle = getattr(process, "_handle", None)
    assigned = isinstance(process_handle, int) and job.assign(process_handle)
    reason: str | None = None
    if not assigned:
        reason = "owned job object assignment failed"
    elif not job.resume(process.pid):
        reason = "suspended child could not be resumed"
    if reason is None:
        return None
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=_TIMED_OUT_DRAIN_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        job.close()
    except Exception:
        pass
    command = ntpath.basename(argv[0]) if argv else "command"
    return f"command launch failed: {command} ({reason}); suspended child terminated"


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
    stream_limit_exceeded: bool = False
    incomplete_process_group: bool = False
    descendants_reaped: bool = False
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
    stream_limit_exceeded: bool = False,
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
        stream_limit_exceeded=stream_limit_exceeded,
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


TRUNCATION_MARKER = "\n[... output truncated ...]\n"
_TRUNCATION_MARKER = TRUNCATION_MARKER


def bound_text_tail(text: str, max_bytes: int | None = None) -> str:
    """Return the final ``max_bytes`` of UTF-8, cut on a character boundary."""

    limit = MAX_CAPTURE_BYTES if max_bytes is None else max_bytes
    raw = text.encode(_STREAM_ENCODING)
    if len(raw) <= limit:
        return text
    if limit <= 0:
        return ""
    return raw[-limit:].decode(_STREAM_ENCODING, errors="ignore")


def bound_text_ends(text: str, max_bytes: int | None = None) -> str:
    """Bound UTF-8 text while preserving both its beginning and final bytes."""

    limit = MAX_CAPTURE_BYTES if max_bytes is None else max_bytes
    raw = text.encode(_STREAM_ENCODING)
    if len(raw) <= limit:
        return text
    marker = _TRUNCATION_MARKER.encode(_STREAM_ENCODING)
    if limit <= len(marker):
        return raw[-limit:].decode(_STREAM_ENCODING, errors="ignore")
    retained = limit - len(marker)
    head_bytes = retained // 2
    tail_bytes = retained - head_bytes
    head = raw[:head_bytes].decode(_STREAM_ENCODING, errors="ignore")
    tail = raw[-tail_bytes:].decode(_STREAM_ENCODING, errors="ignore")
    return head + _TRUNCATION_MARKER + tail


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
        self.observed = 0
        self.overflowed = False

    @property
    def max_bytes(self) -> int:
        return MAX_CAPTURE_BYTES if self._max_bytes is None else self._max_bytes

    def try_add(self, n: int) -> bool:
        """Accept all ``n`` bytes, or reject and trip overflow."""

        if n <= 0:
            return not self.overflowed
        with self._lock:
            self.observed += n
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
            self.observed += n
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

    def observe(self, n: int) -> bool:
        """Charge ``n`` streamed bytes; False once the ceiling is crossed.

        Unlike ``try_add``, the count keeps advancing past the ceiling so the
        run record can report the volume the child actually produced (#1144).
        """

        if n <= 0:
            return not self.overflowed
        with self._lock:
            self.observed += n
            if self.used + n > self.max_bytes:
                self.overflowed = True
                return False
            self.used += n
            return True


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


def _process_group_kwargs(*, suspend: bool = False) -> dict[str, Any]:
    if os.name == "posix":
        return {"start_new_session": True}
    if os.name == "nt":
        flags = _WINDOWS_NEW_PROCESS_GROUP
        if suspend:
            # The child stays frozen until its job assignment completes, so no
            # grandchild can spawn outside the owned kill-on-close tree.
            flags |= _WINDOWS_CREATE_SUSPENDED
        return {"creationflags": flags}
    return {}


def process_group_kwargs() -> dict[str, Any]:
    """Public launcher kwargs so callers start children in their own group."""
    return _process_group_kwargs()


def detached_launch_kwargs(*, job_breakaway: bool = True) -> dict[str, Any]:
    """Launcher kwargs for a child that must outlive its parent's session.

    ``process_group_kwargs`` only isolates signal delivery while the parent
    lives. Detaching needs more: the child must survive the parent's session,
    controlling terminal, console, and job object going away.
    """

    if os.name == "posix":
        # setsid() drops the controlling terminal, so the SIGHUP an SSH
        # disconnect delivers to the session stops at the parent.
        return {"start_new_session": True}
    if os.name == "nt":
        # start_new_session is accepted and then silently discarded on Windows
        # (CPython binds it as unused_start_new_session in the Windows
        # _execute_child). Without explicit creation flags the child keeps the
        # parent's console, so the ConPTY teardown that follows an SSH session
        # exit sends it CTRL_CLOSE_EVENT and kills it. DETACHED_PROCESS gives
        # it no console at all, CREATE_NEW_PROCESS_GROUP keeps console control
        # events off it, and CREATE_BREAKAWAY_FROM_JOB escapes a kill-on-close
        # job object when the ambient job permits breakaway.
        flags = _WINDOWS_DETACHED_PROCESS | _WINDOWS_NEW_PROCESS_GROUP
        if job_breakaway:
            flags |= _WINDOWS_CREATE_BREAKAWAY_FROM_JOB
        return {"creationflags": flags}
    return {}


def _is_windows_breakaway_denied(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) == _WINDOWS_BREAKAWAY_DENIED_WINERROR


def spawn_detached(
    argv: Sequence[str],
    *,
    cwd: Path | str | None = None,
    stdin: Any = subprocess.DEVNULL,
    stdout: Any = subprocess.DEVNULL,
    stderr: Any = subprocess.STDOUT,
    popen: Any = None,
) -> tuple[subprocess.Popen[Any], str]:
    """Start ``argv`` so it survives the parent's session, console, and job.

    Returns the process and a short, secret-free mode label naming the detach
    mechanism actually used, so a run receipt or log can say why a child did or
    did not outlive its parent without echoing argv or environment.
    """

    launcher = subprocess.Popen if popen is None else popen

    def launch(job_breakaway: bool) -> subprocess.Popen[Any]:
        return launcher(  # type: ignore[no-any-return]
            argv,
            cwd=cwd,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            **detached_launch_kwargs(job_breakaway=job_breakaway),
        )

    if os.name == "nt":
        try:
            return launch(True), DETACH_MODE_WINDOWS_JOB_BREAKAWAY
        except OSError as exc:
            if not _is_windows_breakaway_denied(exc):
                raise
        raise DetachedLaunchError(
            "Windows refused CREATE_BREAKAWAY_FROM_JOB; detached runs cannot safely survive this session's "
            "kill-on-close job. Run outside the restricted job or ask the administrator to allow breakaway."
        )
    if os.name == "posix":
        return launch(True), DETACH_MODE_POSIX_SESSION
    return launch(True), DETACH_MODE_INHERITED


def terminate_process_tree(
    process: subprocess.Popen[bytes],
    *,
    terminate_grace: float = 0.5,
    kill_grace: float = 0.5,
) -> None:
    """Kill a child and its whole process group via the shared cleanup path."""
    _terminate_processes((process,), terminate_grace=terminate_grace, kill_grace=kill_grace)


class _BoundedCollector:
    """Drain a bounded transport stream while retaining its head and tail."""

    def __init__(self, max_bytes: int | None = None, stream_bytes: int | None = None) -> None:
        # Resolved at construction, not at def time, so the caps stay a single
        # source of truth that tests and future config can actually move.
        self.max_bytes = MAX_CAPTURE_BYTES if max_bytes is None else max_bytes
        self.stream_bytes = MAX_STREAM_BYTES if stream_bytes is None else stream_bytes
        self._lock = threading.Lock()
        self._head = {"stdout": bytearray(), "stderr": bytearray()}
        self._tail: deque[tuple[str, bytes]] = deque()
        self._head_bytes = 0
        self._tail_bytes = 0
        self._marker = _TRUNCATION_MARKER.encode(_STREAM_ENCODING)
        # Head and tail split the retention cap, so a truncated capture keeps
        # both how the output started and how it ended (#1144).
        self._head_limit = self.max_bytes // 2
        self._tail_limit = max(0, self.max_bytes - self._head_limit - len(self._marker))
        self.stdout_bytes = 0
        self.stderr_bytes = 0
        self.overflowed = False
        self.stream_limit_exceeded = False
        self.overflow = threading.Event()
        self.wake = threading.Event()

    def _append_tail(self, stream: str, data: bytes) -> None:
        if not data or self._tail_limit <= 0:
            return
        self._tail.append((stream, data))
        self._tail_bytes += len(data)
        while self._tail_bytes > self._tail_limit:
            old_stream, old = self._tail[0]
            excess = self._tail_bytes - self._tail_limit
            if len(old) <= excess:
                self._tail.popleft()
                self._tail_bytes -= len(old)
            else:
                self._tail[0] = (old_stream, old[excess:])
                self._tail_bytes -= excess

    def feed(self, stream: str, data: bytes) -> bool:
        """Drain data. False means the stream ceiling, not retention, was hit."""

        if not data:
            return True
        with self._lock:
            if self.stream_limit_exceeded:
                return False
            if stream == "stdout":
                self.stdout_bytes += len(data)
            else:
                self.stderr_bytes += len(data)
            if self.stdout_bytes + self.stderr_bytes > self.stream_bytes:
                self.stream_limit_exceeded = True
                self.overflowed = True
                self.overflow.set()
                self.wake.set()
                return False
            head_remaining = self._head_limit - self._head_bytes
            if head_remaining > 0:
                accepted = data[:head_remaining]
                self._head[stream].extend(accepted)
                self._head_bytes += len(accepted)
                data = data[len(accepted) :]
            if data:
                self.overflowed = True
                self._append_tail(stream, data)
            return True

    def snapshot(self) -> tuple[bytes, bytes]:
        with self._lock:
            retained = {name: bytearray(value) for name, value in self._head.items()}
            if self.overflowed:
                marker_stream = (
                    "stdout" if retained["stdout"] or any(n == "stdout" for n, _ in self._tail) else "stderr"
                )
                retained[marker_stream].extend(self._marker)
            for stream, data in self._tail:
                retained[stream].extend(data)
            return bytes(retained["stdout"]), bytes(retained["stderr"])


def _stop_child(
    process: subprocess.Popen[bytes],
    process_registry: ProcessRegistry | None,
    child_job: _WindowsChildJob | None = None,
) -> None:
    if child_job is not None and child_job.terminate():
        # The job owns the whole tree; give the pipes a beat to drain closed.
        try:
            process.wait(timeout=_TIMED_OUT_DRAIN_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        return
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


def _collection_complete(
    process: subprocess.Popen[bytes],
    threads: list[threading.Thread],
    collector: _BoundedCollector,
) -> bool:
    return collector.stream_limit_exceeded or (process.poll() is not None and not _readers_alive(threads))


def _collect_process_output(
    process: subprocess.Popen[bytes],
    *,
    timeout: float,
    stdin: bytes | None,
    process_registry: ProcessRegistry | None,
    child_job: _WindowsChildJob | None = None,
    supervise_group: bool = False,
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
        finally:
            collector.wake.set()

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

    def watch_process() -> None:
        try:
            process.wait()
        finally:
            collector.wake.set()

    process_watcher = threading.Thread(target=watch_process, daemon=True)
    process_watcher.start()

    timed_out = False
    incomplete_group = False
    descendants_reaped = False
    cleanup_error: str | None = None

    def bounded_stop() -> None:
        """Stop the group; a termination or wait failure must never bypass Result."""

        nonlocal cleanup_error
        try:
            _stop_child(process, process_registry, child_job)
        except Exception as exc:
            if cleanup_error is None:
                cleanup_error = f"{type(exc).__name__}: {exc}"

    deadline = time.monotonic() + max(timeout, 0.0)
    exited_at: float | None = None
    try:
        while True:
            if _collection_complete(process, threads, collector):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            wait_for = remaining
            if process.poll() is not None:
                if exited_at is None:
                    exited_at = time.monotonic()
                # The direct child is gone but a descendant still holds an
                # output pipe open. Drain briefly, then reap the whole group
                # instead of blocking until the timeout.
                drain_left = _TIMED_OUT_DRAIN_SECONDS - (time.monotonic() - exited_at)
                if drain_left <= 0:
                    incomplete_group = True
                    break
                wait_for = min(wait_for, drain_left)
            # Clear-check-wait: avoid missing a wake that arrives between the
            # completeness check above and the blocking wait below.
            collector.wake.clear()
            if _collection_complete(process, threads, collector):
                break
            collector.wake.wait(timeout=wait_for)
    except BaseException:
        try:
            _stop_child(process, process_registry, child_job)
        except Exception:
            pass
        raise

    if supervise_group and not timed_out and not incomplete_group and process.poll() is not None:
        # Supervised runs (scanner launches): once the direct child is gone,
        # reap the owned group even when every capture pipe closed cleanly, so
        # an escaped descendant cannot outlive the run while holding workspace
        # locks. The direct child's exit status stands.
        bounded_stop()
        descendants_reaped = True

    if timed_out or collector.stream_limit_exceeded or incomplete_group:
        bounded_stop()

    _join_readers(threads, _TIMED_OUT_DRAIN_SECONDS)
    if stdin_thread is not None:
        stdin_thread.join(timeout=_TIMED_OUT_DRAIN_SECONDS)
    if _readers_alive(threads):
        bounded_stop()
        _join_readers(threads, _TIMED_OUT_DRAIN_SECONDS)
        if stdin_thread is not None:
            stdin_thread.join(timeout=_TIMED_OUT_DRAIN_SECONDS)

    if process.poll() is None:
        bounded_stop()
        try:
            process.wait(timeout=_TIMED_OUT_DRAIN_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass

    stdout_bytes, stderr_bytes = collector.snapshot()
    code = process.returncode
    if timed_out:
        code = 124
    elif code is None:
        code = -1
    if incomplete_group and code == 0:
        code = _INCOMPLETE_GROUP_EXIT_CODE
    if cleanup_error is not None:
        # Group termination failed, so the tree's fate is unknown: fail closed
        # instead of letting a zero exit stand for a possibly-alive group.
        if code == 0:
            code = _INCOMPLETE_GROUP_EXIT_CODE
        incomplete_group = True

    result = _result_from_output(
        code=code,
        stdout=stdout_bytes,
        stderr=stderr_bytes,
        stdout_bytes=collector.stdout_bytes,
        stderr_bytes=collector.stderr_bytes,
        output_limit_exceeded=collector.overflowed,
        stream_limit_exceeded=collector.stream_limit_exceeded,
    )
    result.incomplete_process_group = incomplete_group
    result.descendants_reaped = descendants_reaped
    extras: list[str] = []
    if collector.stream_limit_exceeded:
        extras.append(f"combined output exceeded {MAX_STREAM_BYTES} stream byte limit; child terminated")
    elif collector.overflowed:
        extras.append(f"combined output exceeded {MAX_CAPTURE_BYTES} byte capture limit; output truncated")
    if timed_out:
        extras.append(f"timeout after {timeout}s")
    if incomplete_group:
        extras.append(
            "process group kept the output pipes open after child exit; output incomplete and group terminated"
        )
    if cleanup_error is not None:
        extras.append(f"process group cleanup failed ({cleanup_error}); output may be incomplete")
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
            stream_limit_exceeded=result.stream_limit_exceeded,
            incomplete_process_group=result.incomplete_process_group,
            descendants_reaped=result.descendants_reaped,
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
    supervise_group: bool = False,
) -> Result:
    windows_launch = os.name == "nt"
    child_job: _WindowsChildJob | None = None
    if windows_launch:
        # The job must exist before the child can run a single instruction.
        child_job = _create_windows_child_job()
        if child_job is None:
            command = ntpath.basename(args[0]) if args else "command"
            return Result(
                code=126,
                stdout="",
                stderr=f"command launch failed: {command} (owned process-tree job object is unavailable)",
            )
    try:
        popen_kwargs: dict[str, Any] = dict(_process_group_kwargs(suspend=windows_launch))
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
            env=env,
            cwd=cwd,
            shell=False,
            **popen_kwargs,
        )
    except OSError as exc:
        code, message = _launch_failure(args, exc)
        if child_job is not None:
            try:
                child_job.close()
            except Exception:
                pass
        return Result(code=code, stdout="", stderr=message)

    if child_job is not None:
        bind_error = _bind_suspended_windows_child(child_job, process, args)
        if bind_error is not None:
            return Result(code=126, stdout="", stderr=bind_error)
    if process_registry is not None:
        process_registry.register(process)
    try:
        return _collect_process_output(
            process,
            timeout=timeout,
            stdin=stdin,
            process_registry=process_registry,
            child_job=child_job,
            supervise_group=supervise_group,
        )
    finally:
        if child_job is not None:
            try:
                child_job.close()
            except Exception:
                pass
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
