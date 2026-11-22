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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional

from .proc_group import drain_supported, observe_leader_exit, wait_for_group
from .proc_windows_job import (
    INCOMPLETE_GROUP_EXIT_CODE as _INCOMPLETE_GROUP_EXIT_CODE,
    JobObject as OwnedJob,
    _resume_windows_main_thread as _resume_windows_main_thread,
    abort_windows_launch as _abort_windows_launch,
    bind_suspended_windows_child as _bind_suspended_windows_child,
    create_windows_child_job as _create_windows_child_job,
    register_owned_child,
    terminate_windows_child as _terminate_windows_child,
    terminate_windows_process_tree as _terminate_windows_process_tree,
)

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
# Bound for a supervised run's descendants to finish after the direct child
# exits before the owned group is reaped (see group_drain_seconds in run()).
GROUP_DRAIN_SECONDS = 5.0


def check_cancellation(process_registry: ProcessRegistry | None) -> None:
    """Honor cancellation when supported by a registry's additive interface."""
    check = getattr(process_registry, "check_cancellation", None)
    if check is not None:
        check()


def _check_launch_allowed(process_registry: ProcessRegistry | None) -> None:
    gate = getattr(process_registry, "ensure_launch_allowed", None)
    if gate is not None:
        gate()


class ProcessCancelledError(RuntimeError):
    """A canceled non-claim registry refused a new worker launch."""


@dataclass(frozen=True)
class RegisteredChild:
    """One child, its dispatch identity, and the job that owns its tree."""

    seat: str | None = None
    job: OwnedJob | None = None
    task_key: tuple[str, str] | None = None


class ProcessRegistry:
    """Own cancellable subprocess groups for one worker dispatch."""

    def __init__(
        self,
        *,
        terminate_grace: float = 0.5,
        kill_grace: float = 0.5,
        check_cancellation: Callable[[], None] | None = None,
    ) -> None:
        self._terminate_grace = terminate_grace
        self._kill_grace = kill_grace
        self._check_cancellation = check_cancellation
        self._lock = threading.Lock()
        self._cancel_gate = threading.Lock()
        self._processes: dict[subprocess.Popen[bytes], RegisteredChild] = {}
        self._cancel_outcomes: dict[subprocess.Popen[bytes], ProcessCancelOutcome] = {}
        self._task_cancels: dict[tuple[str, str], dict[subprocess.Popen[bytes], ProcessCancelOutcome]] = {}
        self._cancellation_group_count = 0
        self._canceled = False

    def check_cancellation(self) -> None:
        """Honor the owner's signal without replacing an internal dispatch error."""
        if self._check_cancellation is not None:
            self._check_cancellation()

    def ensure_launch_allowed(self) -> None:
        self.check_cancellation()
        with self._lock:
            canceled = self._canceled
        if canceled:
            raise ProcessCancelledError("worker dispatch canceled")

    @property
    def cancellation_timeout(self) -> float:
        """Bound serial Windows tree cleanup as well as POSIX group cleanup."""
        with self._lock:
            # In-flight batch members may unregister before their outcomes land.
            groups = max(self._cancellation_group_count, len(self._processes.keys() | self._cancel_outcomes.keys()))
        return max(1, groups) * (self._terminate_grace + self._kill_grace) + 2.0

    @property
    def cancellation_outcomes(self) -> tuple[ProcessCancelOutcome, ...]:
        with self._lock:
            observed = dict(self._cancel_outcomes)
            for process, outcome in observed.items():
                if outcome.result == "still_active" and process.poll() is not None:
                    observed[process] = ProcessCancelOutcome(outcome.seat, "interrupted")
            for process, registered in self._processes.items():
                if process not in observed:
                    observed[process] = ProcessCancelOutcome(
                        registered.seat, "interrupted" if process.poll() is not None else "still_active"
                    )
            return tuple(observed.values())

    def _cancel_processes(
        self,
        processes: tuple[tuple[subprocess.Popen[bytes], RegisteredChild], ...],
        *,
        task_key: tuple[str, str] | None = None,
    ) -> None:
        if not processes:
            return
        jobs = {process: registered.job for process, registered in processes if registered.job is not None}
        try:
            _terminate_processes(
                tuple(process for process, _registered in processes),
                terminate_grace=self._terminate_grace,
                kill_grace=self._kill_grace,
                jobs=jobs,
            )
            outcomes = tuple(
                (
                    process,
                    ProcessCancelOutcome(
                        registered.seat, "interrupted" if process.poll() is not None else "still_active"
                    ),
                )
                for process, registered in processes
            )
        except Exception:
            outcomes = tuple(
                (process, ProcessCancelOutcome(registered.seat, "error")) for process, registered in processes
            )
        with self._lock:
            recorded = self._cancel_outcomes if task_key is None else self._task_cancels[task_key]
            for process, outcome in outcomes:
                previous = recorded.get(process)
                if previous is None or previous.result != "error":
                    recorded[process] = outcome

    def register(
        self,
        process: subprocess.Popen[bytes],
        *,
        seat: str | None = None,
        job: OwnedJob | None = None,
        task_key: tuple[str, str] | None = None,
    ) -> None:
        """Track one child, keeping the owned job that contains its Windows tree."""
        registered = RegisteredChild(seat, job, task_key)
        with self._lock:
            self._processes[process] = registered
            task_canceled = task_key is not None and task_key in self._task_cancels
            canceled = self._canceled or task_canceled
        if canceled:
            with self._cancel_gate:
                with self._lock:
                    key = task_key if task_canceled else None
                    recorded = self._cancel_outcomes if key is None else self._task_cancels[key]
                    pending = process not in recorded
                if pending:
                    self._cancel_processes(((process, registered),), task_key=key)

    def register_with_job(self, process: subprocess.Popen[bytes], job: OwnedJob) -> None:
        """Register a child together with the job object that owns its tree."""
        self.register(process, job=job)

    def unregister(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            child = self._processes.get(process)
            if child is None or child.task_key not in self._task_cancels:
                self._processes.pop(process, None)
                return
        # Keep the launcher-owned job open until the cancellation snapshot is
        # done with it. The launcher closes the job after unregister returns.
        with self._cancel_gate, self._lock:
            self._processes.pop(process, None)

    def for_seat(self, seat: str) -> _SeatProcessRegistry:
        return _SeatProcessRegistry(self, seat)

    def for_task(self, task_id: str, attempt_id: str, seat: str) -> _SeatProcessRegistry:
        return _SeatProcessRegistry(self, seat, (task_id, attempt_id))

    def _check_task_cancellation(self, key: tuple[str, str] | None) -> None:
        with self._lock:
            if key is not None and key in self._task_cancels:
                raise ProcessCancelledError("worker task canceled")

    def task_cancellation_outcomes(self, task_id: str, attempt_id: str) -> tuple[ProcessCancelOutcome, ...]:
        with self._lock:
            return tuple(self._task_cancels.get((task_id, attempt_id), {}).values())

    def cancel_task(self, task_id: str, attempt_id: str) -> tuple[ProcessCancelOutcome, ...]:
        key = (task_id, attempt_id)
        with self._cancel_gate:
            with self._lock:
                if key in self._task_cancels:
                    return tuple(self._task_cancels[key].values())
                self._task_cancels[key] = {}
                processes = tuple((p, child) for p, child in self._processes.items() if child.task_key == key)
            self._cancel_processes(processes, task_key=key)
            return self.task_cancellation_outcomes(task_id, attempt_id)

    def cancel(self) -> tuple[ProcessCancelOutcome, ...]:
        with self._cancel_gate:
            with self._lock:
                self._canceled = True
                processes = tuple(
                    (process, registered)
                    for process, registered in self._processes.items()
                    if process not in self._cancel_outcomes
                    or (self._cancel_outcomes[process].result != "interrupted" and process.poll() is None)
                )
                self._cancellation_group_count = max(self._cancellation_group_count, len(processes))
            self._cancel_processes(processes)
            return self.cancellation_outcomes

    def terminate(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            registered = self._processes.get(process)
        jobs = {process: registered.job} if registered is not None and registered.job is not None else {}
        _terminate_processes(
            (process,),
            terminate_grace=self._terminate_grace,
            kill_grace=self._kill_grace,
            jobs=jobs,
        )


@dataclass(frozen=True)
class ProcessCancelOutcome:
    """Safe observed state after a cancellation request for one registered process."""

    seat: str | None
    result: str


class _SeatProcessRegistry:
    """Label process registrations with a Brigade seat without changing adapters."""

    def __init__(self, parent: ProcessRegistry, seat: str, task_key: tuple[str, str] | None = None) -> None:
        self._parent = parent
        self._seat = seat
        self._task_key = task_key

    def check_cancellation(self) -> None:
        self._parent.check_cancellation()
        self._parent._check_task_cancellation(self._task_key)

    def ensure_launch_allowed(self) -> None:
        self._parent.ensure_launch_allowed()
        self._parent._check_task_cancellation(self._task_key)

    def register(self, process: subprocess.Popen[bytes]) -> None:
        self._parent.register(process, seat=self._seat, task_key=self._task_key)

    def register_with_job(self, process: subprocess.Popen[bytes], job: OwnedJob) -> None:
        self._parent.register(process, seat=self._seat, job=job, task_key=self._task_key)

    def unregister(self, process: subprocess.Popen[bytes]) -> None:
        self._parent.unregister(process)

    def cancel(self) -> tuple[ProcessCancelOutcome, ...]:
        if self._task_key is not None:
            return self._parent.cancel_task(*self._task_key)
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


def process_group_id(pid: int) -> int | None:
    """Return the process group id for ``pid``, or None where unavailable.

    Windows has no ``os.getpgid``. Returns None there (and when the pid has
    already been reaped) so callers fall back to the Windows job-object or
    taskkill termination path in this module instead of raising
    ``AttributeError``.
    """
    getpgid = getattr(os, "getpgid", None)
    if getpgid is None:
        return None
    try:
        return int(getpgid(pid))
    except OSError:
        return None


_WINDOWS_CREATE_SUSPENDED = 0x00000004
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


def create_owned_windows_job() -> OwnedJob | None:
    """Create the kill-on-close job object that contains one Windows child tree."""
    return _create_windows_child_job()


def bind_owned_windows_child(
    job: OwnedJob,
    process: subprocess.Popen[bytes],
    argv: List[str],
) -> str | None:
    """Assign a suspended Windows child to its owned job, then resume it."""
    return _bind_suspended_windows_child(job, process, argv)


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
    jobs: Mapping[subprocess.Popen[bytes], OwnedJob] | None = None,
) -> None:
    if not processes:
        return
    if os.name == "nt":
        timeout = terminate_grace + kill_grace
        owned = jobs or {}
        for process in processes:
            try:
                _terminate_windows_child(
                    process,
                    timeout=timeout,
                    job=owned.get(process),
                    taskkill=_terminate_windows_process_tree,
                )
            except KeyboardInterrupt:
                try:
                    process.kill()
                except (OSError, KeyboardInterrupt):
                    pass
        try:
            _wait_for_processes(processes, kill_grace)
        except KeyboardInterrupt:
            pass
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
            # Latch: once the ceiling is crossed this never reports True again,
            # or a later small write would wave the reader past an overflow.
            if self.overflowed or self.used + n > self.max_bytes:
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


def suspended_process_group_kwargs() -> dict[str, Any]:
    """Launcher kwargs that start a Windows child frozen for job assignment."""
    return _process_group_kwargs(suspend=True)


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
    child_job: OwnedJob | None = None,
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
    leader_gone: Callable[[], bool],
    threads: list[threading.Thread],
    collector: _BoundedCollector,
) -> bool:
    return collector.stream_limit_exceeded or (leader_gone() and not _readers_alive(threads))


def _join_started_threads_after_exception(threads: list[threading.Thread], timeout: float) -> None:
    """Attempt every already-started cleanup join under one shared deadline."""
    deadline = time.monotonic() + max(timeout, 0.0)
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            thread.join(timeout=remaining)
        except BaseException:
            pass


def _cleanup_launch_exception(
    process: subprocess.Popen[bytes],
    process_registry: ProcessRegistry | None,
    child_job: OwnedJob | None,
    started_threads: list[threading.Thread],
) -> None:
    """Best-effort bounded cleanup while preserving a launch setup exception."""
    try:
        _stop_child(process, process_registry, child_job)
    except BaseException:
        pass
    try:
        process.wait(timeout=_TIMED_OUT_DRAIN_SECONDS)
    except BaseException:
        pass
    _join_started_threads_after_exception(started_threads, _TIMED_OUT_DRAIN_SECONDS)


def _release_launch_resources(
    process: subprocess.Popen[bytes],
    process_registry: ProcessRegistry | None,
    child_job: OwnedJob | None,
    registration_attempted: bool,
) -> BaseException | None:
    """Release launcher-owned resources without letting one release skip another."""
    cleanup_exception: BaseException | None = None

    def record_cleanup_exception(exc: BaseException) -> None:
        nonlocal cleanup_exception
        if cleanup_exception is None:
            cleanup_exception = exc

    if registration_attempted and process_registry is not None:
        try:
            process_registry.unregister(process)
        except BaseException as exc:
            record_cleanup_exception(exc)
    if child_job is not None:
        try:
            child_job.close()
        except Exception:
            pass
        except BaseException as exc:
            record_cleanup_exception(exc)
    for stream_name in ("stdout", "stderr", "stdin"):
        stream = getattr(process, stream_name, None)
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
            except BaseException as exc:
                record_cleanup_exception(exc)
    return cleanup_exception


def _collect_process_output(
    process: subprocess.Popen[bytes],
    *,
    timeout: float,
    stdin: bytes | None,
    process_registry: ProcessRegistry | None,
    child_job: OwnedJob | None = None,
    supervise_group: bool = False,
    group_drain_seconds: float = 0.0,
    started_threads: list[threading.Thread] | None = None,
) -> Result:
    collector = _BoundedCollector()
    # Drain mode keeps the leader's pid and group id reserved until the whole
    # group is gone, so a recycled id can never be probed or signalled (#2286).
    drain_seconds = group_drain_seconds if drain_supported() else 0.0
    leader_exited = threading.Event() if drain_seconds > 0 else None

    def leader_gone() -> bool:
        return leader_exited.is_set() if leader_exited is not None else process.poll() is not None

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
    if started_threads is None:
        started_threads = []
    for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        if stream is None:
            continue
        reader = threading.Thread(target=read_stream, args=(stream, name), daemon=True)
        reader.start()
        threads.append(reader)
        started_threads.append(reader)

    stdin_thread: threading.Thread | None = None
    if process.stdin is not None:
        stdin_thread = threading.Thread(target=_write_stdin, args=(process, stdin), daemon=True)
        stdin_thread.start()
        started_threads.append(stdin_thread)

    def watch_process() -> None:
        try:
            if leader_exited is None:
                process.wait()
            else:
                observe_leader_exit(process)
                leader_exited.set()
        finally:
            collector.wake.set()

    process_watcher = threading.Thread(target=watch_process, daemon=True)
    process_watcher.start()
    started_threads.append(process_watcher)

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
    while True:
        if _collection_complete(leader_gone, threads, collector):
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        wait_for = remaining
        if leader_gone():
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
        if _collection_complete(leader_gone, threads, collector):
            break
        collector.wake.wait(timeout=wait_for)

    if supervise_group and not timed_out and not incomplete_group and leader_gone():
        # Supervised runs (scanner launches): once the direct child is gone,
        # reap the owned group even when every capture pipe closed cleanly, so
        # an escaped descendant cannot outlive the run holding workspace locks.
        # A draining run lets descendants finish the run's own output, bounded
        # by the remaining timeout; survivors were killed mid-work and fail it.
        drain_budget = min(drain_seconds, max(deadline - time.monotonic(), 0.0))
        survivors = wait_for_group(process, drain_budget) if drain_seconds > 0 else True
        if survivors:
            bounded_stop()
        # Either the group was observed empty or it was just stopped.
        descendants_reaped = True
        incomplete_group = survivors and drain_seconds > 0

    if (timed_out or collector.stream_limit_exceeded or incomplete_group) and not descendants_reaped:
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
        extras.append("process group did not finish cleanly after child exit; output incomplete and group terminated")
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
    group_drain_seconds: float = 0.0,
) -> Result:
    return _launch_and_collect(
        args,
        subprocess.PIPE,
        timeout=timeout,
        env=env,
        cwd=cwd,
        stdin=stdin,
        process_registry=process_registry,
        supervise_group=supervise_group,
        group_drain_seconds=group_drain_seconds,
    )


def run_held(
    args: List[str],
    held_fd: int,
    *,
    timeout: float = 30.0,
    env: Optional[dict] = None,
    cwd: Optional[Path] = None,
    stdin: bytes | None = None,
    process_registry: ProcessRegistry | None = None,
    supervise_group: bool = False,
) -> Result:
    """Run the native executable open behind ``held_fd``, never whatever a path names at spawn time.

    ``args[0]`` is only the display name. The child inherits ``held_fd`` and
    nothing else (``run`` itself passes no descriptors), and starts as the
    small :data:`_HELD_LAUNCHER`, which marks the descriptor close-on-exec and
    execs it with ``fexecve`` (``execveat`` with ``AT_EMPTY_PATH``). The kernel
    resolves the open file before it closes close-on-exec descriptors, so the
    held image runs and neither it nor anything it starts keeps the
    descriptor. Timeout, tree termination and output bounds match :func:`run`.
    Platforms without the mechanism raise ``OSError``.

    Only native binaries run this way. A ``#!`` script needs its descriptor
    after the exec, because the interpreter reopens it by path, so the kernel
    refuses it with ``ENOENT`` and the call returns code 126 without running
    anything. The launcher passes on the exact environment the call was given
    and restores the signal dispositions Python ignores at startup.

    ``held_fd`` must be 3 or above (see :func:`high_fd`), so it is never the
    child's stdin, stdout or stderr. ``supervise_group`` reaps the child's
    group after it exits, as for :func:`run`.
    """
    if held_fd < HELD_FD_MIN:
        raise OSError("a held executable descriptor must be 3 or above")
    if not held_exec_supported():
        raise OSError("launching a held executable descriptor is unavailable on this platform")
    return _launch_and_collect(
        held_argv(held_fd, args),
        subprocess.PIPE,
        timeout=timeout,
        env=env,
        cwd=cwd,
        stdin=stdin,
        process_registry=process_registry,
        supervise_group=supervise_group,
        pass_fds=(held_fd,),
    )


HELD_FD_MIN = 3


def held_argv(held_fd: int, args: Sequence[str]) -> List[str]:
    """The command that execs the native executable open behind ``held_fd`` as ``args``.

    For a caller that streams the child with its own ``Popen``: pass ``pass_fds=(held_fd,)``
    and an environment built for the child. ``run_held`` uses the same command.
    """
    return [sys.executable, "-I", "-S", "-c", _HELD_LAUNCHER, str(held_fd), *args]


# Runs as ``python -I -S -c _HELD_LAUNCHER <fd> <argv...>`` in the child of
# :func:`run_held`, then replaces itself with the held executable. Python's
# startup may add LC_CTYPE (locale coercion) and ignores SIGPIPE and SIGXFSZ,
# so the environment is read back from the kernel's copy and those signals are
# reset before the exec.
_HELD_LAUNCHER = r"""
import errno, os, signal, sys
for name in ("SIGPIPE", "SIGXFSZ"):
    if hasattr(signal, name):
        signal.signal(getattr(signal, name), signal.SIG_DFL)
fd, argv = int(sys.argv[1]), sys.argv[2:]
try:
    with open("/proc/self/environ", "rb") as handle:
        env = dict(entry.partition(b"=")[::2] for entry in handle.read().split(b"\0") if entry.find(b"=") > 0)
    os.set_inheritable(fd, False)
    os.execve(fd, argv, env)
except OSError as exc:
    reason = "not a native binary this system runs" if exc.errno == errno.ENOENT else exc.strerror
    os.write(2, ("command launch failed: %s (%s)\n" % (os.path.basename(argv[0]), reason)).encode())
    os._exit(126)
"""


def high_fd(fd: int) -> int:
    """The same open file at descriptor 3 or above, closing ``fd`` when it had to move.

    A descriptor opened while 0, 1 or 2 was closed lands in that slot, and a
    child would then receive it as a standard stream. ``F_DUPFD_CLOEXEC``
    copies it to the lowest free slot from 3 up, close-on-exec.
    """
    if fd >= HELD_FD_MIN:
        return fd
    moved = -1
    try:
        import fcntl

        moved = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, HELD_FD_MIN)
    finally:
        try:
            os.close(fd)
        except OSError:
            # The copy is unreachable if the original cannot be closed, so drop it too.
            if moved >= 0:
                os.close(moved)
            raise
    return moved


def held_exec_supported() -> bool:
    """Whether :func:`run_held` can exec a held descriptor here (Linux ``fexecve`` and ``/proc``)."""
    return (
        sys.platform.startswith("linux")
        and os.path.isdir("/proc/self/fd")
        and os.execve in os.supports_fd
        and bool(sys.executable)
    )


def run_to_file(
    args: List[str],
    stdout_file: Any,
    *,
    timeout: float = 30.0,
    env: Optional[dict] = None,
    cwd: Optional[Path] = None,
    process_registry: ProcessRegistry | None = None,
) -> Result:
    """Run ``args`` with stdout redirected to an already-open binary file.

    Some agent CLIs truncate piped stdout (``cursor-agent models`` stops at
    8 KiB on a pipe), so their probes need a regular file. Timeout and tree
    termination match :func:`run`: the call returns exit 124 with ``timeout
    after Ns`` in stderr, and the whole process tree is reaped through the
    owned job object on Windows or the process group on POSIX. The caller owns
    ``stdout_file`` and reads the captured bytes back after the call; the
    returned :class:`Result` carries an empty stdout with the captured stderr.
    """
    return _launch_and_collect(
        args,
        stdout_file,
        timeout=timeout,
        env=env,
        cwd=cwd,
        stdin=None,
        process_registry=process_registry,
        supervise_group=False,
    )


@dataclass(frozen=True)
class StreamedResult:
    """A child run whose stdout the caller consumed in bounded chunks."""

    code: int
    stderr: str
    stdout_bytes: int = 0
    stopped: bool = False
    timed_out: bool = False


def run_streaming(
    args: List[str],
    consumer: Callable[[bytes], bool],
    *,
    timeout: float = 30.0,
    env: Optional[dict] = None,
    cwd: Optional[Path] = None,
) -> StreamedResult:
    """Stream a child's stdout to ``consumer`` in bounded chunks.

    The consumer sees at most ``_READ_CHUNK_BYTES`` per call and returns
    ``False`` to stop reading; the child is then terminated and reaped, so it
    cannot keep writing past the caller's decision. Nothing is buffered here:
    the caller owns every byte it is handed. A consumer that raises stops the
    child and the exception is re-raised.
    """
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
        return StreamedResult(code=code, stderr=message)

    stopped = threading.Event()
    seen: dict[str, Any] = {"bytes": 0, "error": None}
    stderr_budget = ByteBudget()
    stderr_buf = bytearray()

    def read_stdout() -> None:
        stdout = process.stdout
        assert stdout is not None
        try:
            while True:
                chunk = stdout.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                seen["bytes"] += len(chunk)
                if not consumer(chunk):
                    stopped.set()
                    break
        except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread
            seen["error"] = exc
            stopped.set()

    def drain_stderr() -> None:
        stderr = process.stderr
        assert stderr is not None
        try:
            while True:
                chunk = stderr.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                if stderr_budget.try_add(len(chunk)):
                    stderr_buf.extend(chunk)
        except OSError:
            pass

    readers = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=drain_stderr, daemon=True)]
    for reader in readers:
        reader.start()
    timed_out = False
    deadline = time.monotonic() + max(timeout, 0.0)
    try:
        while not stopped.wait(timeout=0.02):
            if process.poll() is not None and not _readers_alive(readers):
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
    finally:
        if timed_out or stopped.is_set() or process.poll() is None:
            _stop_child(process, None)
        _join_readers(readers, _TIMED_OUT_DRAIN_SECONDS)
        if _readers_alive(readers):
            _stop_child(process, None)
            _join_readers(readers, _TIMED_OUT_DRAIN_SECONDS)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    if seen["error"] is not None:
        raise seen["error"]
    err_text, _err_error = _decode_stream(bytes(stderr_buf), stream="stderr")
    if timed_out:
        err_text = (err_text + "\n" if err_text else "") + f"timeout after {timeout}s"
    if stderr_budget.overflowed:
        err_text = (
            err_text + "\n" if err_text else ""
        ) + f"stderr capture exceeded {stderr_budget.max_bytes} byte limit"
    code = 124 if timed_out else (process.returncode if process.returncode is not None else -1)
    return StreamedResult(
        code=code,
        stderr=err_text,
        stdout_bytes=seen["bytes"],
        stopped=stopped.is_set(),
        timed_out=timed_out,
    )


def _launch_and_collect(
    args: List[str],
    stdout_target: Any,
    *,
    timeout: float = 30.0,
    env: Optional[dict] = None,
    cwd: Optional[Path] = None,
    stdin: bytes | None = None,
    process_registry: ProcessRegistry | None = None,
    supervise_group: bool = False,
    group_drain_seconds: float = 0.0,
    pass_fds: tuple[int, ...] = (),
) -> Result:
    _check_launch_allowed(process_registry)
    windows_launch = os.name == "nt"
    child_job: OwnedJob | None = None
    process: subprocess.Popen[bytes] | None = None
    started_threads: list[threading.Thread] = []
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
        if pass_fds:
            popen_kwargs["pass_fds"] = pass_fds
        process = subprocess.Popen(
            args,
            stdout=stdout_target,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
            env=env,
            cwd=cwd,
            shell=False,
            **popen_kwargs,
        )
        if child_job is not None:
            bind_error = _bind_suspended_windows_child(child_job, process, args)
            if bind_error is not None:
                # The binder killed the suspended child and closed its job.
                process = None
                return Result(code=126, stdout="", stderr=bind_error)
    except OSError as exc:
        _abort_windows_launch(process, child_job)
        code, message = _launch_failure(args, exc)
        return Result(code=code, stdout="", stderr=message)
    except BaseException:
        # A ValueError, TypeError, or interrupt after the job existed must not
        # leak the handle or leave a suspended child behind.
        _abort_windows_launch(process, child_job)
        raise
    assert process is not None
    registration_attempted = False
    primary_exception = False
    try:
        if process_registry is not None:
            registration_attempted = True
            if child_job is None:
                process_registry.register(process)
            else:
                # The registry keeps the job handle so cancellation never has
                # to resolve a pid that Windows may have recycled (#1744).
                register_owned_child(process_registry, process, child_job)
            _check_launch_allowed(process_registry)
        return _collect_process_output(
            process,
            timeout=timeout,
            stdin=stdin,
            process_registry=process_registry,
            child_job=child_job,
            supervise_group=supervise_group,
            group_drain_seconds=group_drain_seconds,
            started_threads=started_threads,
        )
    except BaseException:
        primary_exception = True
        _cleanup_launch_exception(process, process_registry, child_job, started_threads)
        raise
    finally:
        cleanup_exception = _release_launch_resources(
            process,
            process_registry,
            child_job,
            registration_attempted,
        )
        if not primary_exception and cleanup_exception is not None:
            raise cleanup_exception


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
