"""Owned Windows job objects for registered Brigade child processes (#1744).

``ProcessRegistry`` keeps the kill-on-close job handle beside each registered
``Popen``. Cancellation terminates that job, which owns the whole tree, instead
of looking up a numeric pid. A pid is safe to pass to ``taskkill`` only while
its leader is observed to be alive, because Windows recycles pids as soon as a
process exits. Without an owned job an exited leader is therefore skipped and
the skip is logged.

The one remaining pid lookup is a live leader with no owned job: the caller
polls it and then runs ``taskkill``. That window is bounded, because ``Popen``
holds the process handle and Windows cannot reuse a pid while a handle to that
process object is open. Once ``poll`` observes the exit, Windows closes the
handle, so the pid can be reused. That is why an exited leader is never passed
to ``taskkill``.

POSIX behavior stays in ``brigade.proc``. That module imports this one and this
module imports nothing from it, so the two cannot form a cycle.
"""

from __future__ import annotations

import logging
import ntpath
import os
import subprocess
import threading
from typing import Any, Callable, List, Protocol

_LOG = logging.getLogger(__name__)

# Forced nonzero exit for a terminated owned tree: the direct child's zero
# must never stand once its output pipes were cut off mid-stream.
INCOMPLETE_GROUP_EXIT_CODE = 125
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_CLASS = 9
_THREAD_SUSPEND_RESUME = 0x0002
_BIND_DRAIN_SECONDS = 0.5

WindowsTreeTerminator = Callable[..., None]


class JobObject(Protocol):
    """The owned-job surface a process registration keeps and termination uses."""

    def assign(self, process_handle: int) -> bool: ...

    def resume(self, pid: int) -> bool: ...

    def terminate(self) -> bool: ...

    def close(self) -> None: ...


class OwnedWindowsJob:
    """One kill-on-close job object, safe to terminate and close concurrently.

    Cancellation and launcher cleanup can race. The lock makes ``terminate``
    and ``close`` mutually exclusive, and ``close`` on a kill-on-close job
    already kills the owned tree, so a ``terminate`` that loses the race is a
    no-op rather than a call against a recycled handle.
    """

    def __init__(self, kernel32: Any, handle: int) -> None:
        self._kernel32 = kernel32
        self._handle = handle
        self._lock = threading.Lock()
        self._closed = False

    def assign(self, process_handle: int) -> bool:
        """Bind the direct child so every descendant joins the owned tree."""
        with self._lock:
            if self._closed:
                return False
            return bool(self._kernel32.AssignProcessToJobObject(self._handle, process_handle))

    def resume(self, pid: int) -> bool:
        """Resume a CREATE_SUSPENDED child's main thread once it is job-assigned."""
        with self._lock:
            if self._closed:
                return False
            return _resume_windows_main_thread(self._kernel32, pid)

    def terminate(self) -> bool:
        """Terminate every process in the owned tree, including escaped descendants."""
        with self._lock:
            if self._closed:
                # Closing a kill-on-close job already killed every member.
                return False
            return bool(self._kernel32.TerminateJobObject(self._handle, INCOMPLETE_GROUP_EXIT_CODE))

    def close(self) -> None:
        """Release the kill-on-close handle once the tree is no longer needed."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._kernel32.CloseHandle(self._handle)


def create_windows_child_job() -> OwnedWindowsJob | None:
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
        return OwnedWindowsJob(kernel32, handle)
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


def bind_suspended_windows_child(
    job: JobObject,
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
        process.wait(timeout=_BIND_DRAIN_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        job.close()
    except Exception:
        pass
    command = ntpath.basename(argv[0]) if argv else "command"
    return f"command launch failed: {command} ({reason}); suspended child terminated"


def leader_exited(process: subprocess.Popen[bytes]) -> bool:
    """Whether the registered leader is observed to have exited.

    An OSError is treated as exited, because an unproven pid must never be
    handed to ``taskkill``. A KeyboardInterrupt is left to propagate: the
    caller falls back to the owned process handle instead of taskkilling a pid
    whose state was never observed.
    """
    try:
        return process.poll() is not None
    except OSError:
        return True


def _log_skipped_taskkill(process: subprocess.Popen[bytes]) -> None:
    _LOG.warning(
        "skipped taskkill for exited child pid %s: no owned job object proves the pid still belongs to Brigade",
        getattr(process, "pid", "unknown"),
    )


def terminate_windows_process_tree(process: subprocess.Popen[bytes], *, timeout: float) -> None:
    """Terminate a live Windows child's tree by pid, refusing an exited leader."""
    if leader_exited(process):
        _log_skipped_taskkill(process)
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=max(timeout, 0.1),
        )
    except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt):
        pass
    if not leader_exited(process):
        try:
            process.kill()
        except (OSError, KeyboardInterrupt):
            pass


def terminate_windows_child(
    process: subprocess.Popen[bytes],
    *,
    timeout: float,
    job: JobObject | None = None,
    taskkill: WindowsTreeTerminator = terminate_windows_process_tree,
) -> None:
    """Terminate one registered Windows child, preferring its owned job handle.

    An owned job kills the whole tree without resolving a pid, so it stays
    correct after the leader exits. Without a job the caller gets the guarded
    ``taskkill`` fallback, which skips an exited leader instead of risking a
    recycled pid.
    """
    if job is None:
        taskkill(process, timeout=timeout)
        return
    try:
        terminated = bool(job.terminate())
    except Exception:
        _LOG.warning("owned job termination raised for a registered child; using its process handle")
        terminated = False
    if terminated:
        return
    if leader_exited(process):
        return
    # The job was already closed (kill-on-close reaped the tree) or refused.
    # The Popen handle, unlike the pid, still names this child.
    try:
        process.kill()
    except (OSError, KeyboardInterrupt):
        pass


def abort_windows_launch(
    process: subprocess.Popen[bytes] | None,
    job: JobObject | None,
) -> None:
    """Release a launch that failed after its owned job existed.

    Kills any spawned child through its own handle, then closes the job, which
    also reaps descendants that joined it. Used when Popen or the job binding
    raises something the launcher does not turn into a ``Result``.
    """

    if process is not None:
        # Cleanup must not replace the exception that brought us here.
        try:
            process.kill()
        except BaseException:
            pass
        try:
            process.wait(timeout=_BIND_DRAIN_SECONDS)
        except BaseException:
            pass
    if job is not None:
        try:
            job.close()
        except BaseException:
            pass


def register_owned_child(
    registry: Any,
    process: subprocess.Popen[bytes],
    job: JobObject,
) -> None:
    """Register a child together with the job that owns its tree when supported.

    A registry without ``register_with_job`` still records the child, but its
    cancellation falls back to the guarded pid path, so an exited leader is
    skipped instead of being killed through a possibly recycled pid.
    """
    attach = getattr(registry, "register_with_job", None)
    if attach is None:
        registry.register(process)
        return
    attach(process, job)
