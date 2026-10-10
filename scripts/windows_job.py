"""Atomic Windows test containment. No Win32 library is loaded at import time.

The tracker alone owns jobs. Launch and every operation on a job handle are
serialized by one process-wide lifecycle lock. Process handles belong to workers.
"""

from __future__ import annotations

import ctypes as C
import math
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import BinaryIO

DWORD = C.c_uint32
BOOL = C.c_int32
HANDLE = C.c_void_p
SIZE_T = C.c_size_t
LPWSTR = C.POINTER(C.c_uint16)

FILE_TIMEOUT = 0xE1551001
AGGREGATE_DEADLINE = 0xE1551002
DRIVER_ABORT = 0xE1551003
REASONS = {FILE_TIMEOUT: "timeout", AGGREGATE_DEADLINE: "deadline-exceeded", DRIVER_ABORT: "driver-abort"}
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF
HANDLE_FLAG_INHERIT = 1
DUPLICATE_SAME_ACCESS = 2
JOB_OBJECT_QUERY = 4
SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 1
KILL_ON_JOB_CLOSE = 0x2000
JOB_LIST = 0x0002000D
HANDLE_LIST = 0x00020002
CREATION_FLAGS = 0x00080000 | 0x00000400 | 0x00000200 | 0x08000000
_LIFECYCLE = RLock()


class SECURITY_ATTRIBUTES(C.Structure):
    _fields_ = [("nLength", DWORD), ("lpSecurityDescriptor", HANDLE), ("bInheritHandle", BOOL)]


class STARTUPINFOW(C.Structure):
    _fields_ = [
        ("cb", DWORD),
        ("lpReserved", LPWSTR),
        ("lpDesktop", LPWSTR),
        ("lpTitle", LPWSTR),
        ("dwX", DWORD),
        ("dwY", DWORD),
        ("dwXSize", DWORD),
        ("dwYSize", DWORD),
        ("dwXCountChars", DWORD),
        ("dwYCountChars", DWORD),
        ("dwFillAttribute", DWORD),
        ("dwFlags", DWORD),
        ("wShowWindow", C.c_uint16),
        ("cbReserved2", C.c_uint16),
        ("lpReserved2", HANDLE),
        ("hStdInput", HANDLE),
        ("hStdOutput", HANDLE),
        ("hStdError", HANDLE),
    ]


class STARTUPINFOEXW(C.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", HANDLE)]


class PROCESS_INFORMATION(C.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE), ("dwProcessId", DWORD), ("dwThreadId", DWORD)]


class BASIC_LIMIT(C.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", C.c_int64),
        ("PerJobUserTimeLimit", C.c_int64),
        ("LimitFlags", DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", DWORD),
        ("Affinity", SIZE_T),
        ("PriorityClass", DWORD),
        ("SchedulingClass", DWORD),
    ]


class IO_COUNTERS(C.Structure):
    _fields_ = [
        (name, C.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class EXTENDED_LIMIT(C.Structure):
    _fields_ = [
        ("BasicLimitInformation", BASIC_LIMIT),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    ]


class BASIC_ACCOUNTING(C.Structure):
    _fields_ = [
        ("TotalUserTime", C.c_int64),
        ("TotalKernelTime", C.c_int64),
        ("ThisPeriodTotalUserTime", C.c_int64),
        ("ThisPeriodTotalKernelTime", C.c_int64),
        ("TotalPageFaultCount", DWORD),
        ("TotalProcesses", DWORD),
        ("ActiveProcesses", DWORD),
        ("TotalTerminatedProcesses", DWORD),
    ]


# This table also covers the native identity/handle probes. No ctypes defaults.
SIGNATURES = {
    "CreateJobObjectW": ([C.POINTER(SECURITY_ATTRIBUTES), LPWSTR], HANDLE),
    "SetHandleInformation": ([HANDLE, DWORD, DWORD], BOOL),
    "SetInformationJobObject": ([HANDLE, C.c_int32, HANDLE, DWORD], BOOL),
    "QueryInformationJobObject": ([HANDLE, C.c_int32, HANDLE, DWORD, C.POINTER(DWORD)], BOOL),
    "TerminateJobObject": ([HANDLE, DWORD], BOOL),
    "CloseHandle": ([HANDLE], BOOL),
    "GetCurrentProcess": ([], HANDLE),
    "DuplicateHandle": ([HANDLE, HANDLE, HANDLE, C.POINTER(HANDLE), DWORD, BOOL, DWORD], BOOL),
    "CreateFileW": ([LPWSTR, DWORD, DWORD, C.POINTER(SECURITY_ATTRIBUTES), DWORD, DWORD, HANDLE], HANDLE),
    "InitializeProcThreadAttributeList": ([HANDLE, DWORD, DWORD, C.POINTER(SIZE_T)], BOOL),
    "UpdateProcThreadAttribute": ([HANDLE, DWORD, SIZE_T, HANDLE, SIZE_T, HANDLE, C.POINTER(SIZE_T)], BOOL),
    "DeleteProcThreadAttributeList": ([HANDLE], None),
    "CreateProcessW": (
        [
            LPWSTR,
            LPWSTR,
            C.POINTER(SECURITY_ATTRIBUTES),
            C.POINTER(SECURITY_ATTRIBUTES),
            BOOL,
            DWORD,
            HANDLE,
            LPWSTR,
            C.POINTER(STARTUPINFOEXW),
            C.POINTER(PROCESS_INFORMATION),
        ],
        BOOL,
    ),
    "WaitForSingleObject": ([HANDLE, DWORD], DWORD),
    "GetExitCodeProcess": ([HANDLE, C.POINTER(DWORD)], BOOL),
    "SetConsoleCtrlHandler": ([HANDLE, BOOL], BOOL),
    "OpenProcess": ([DWORD, BOOL, DWORD], HANDLE),
    "TerminateProcess": ([HANDLE, DWORD], BOOL),
    "IsProcessInJob": ([HANDLE, HANDLE, C.POINTER(BOOL)], BOOL),
    "GetProcessHandleCount": ([HANDLE, C.POINTER(DWORD)], BOOL),
    "CreatePipe": ([C.POINTER(HANDLE), C.POINTER(HANDLE), C.POINTER(SECURITY_ATTRIBUTES), DWORD], BOOL),
    "PeekNamedPipe": ([HANDLE, HANDLE, DWORD, C.POINTER(DWORD), C.POINTER(DWORD), C.POINTER(DWORD)], BOOL),
}


class JobError(OSError):
    """A kernel failure, with the original last-error value preserved."""

    def __init__(self, operation: str, winerror: int = 0):
        self.operation = operation
        super().__init__(winerror, f"{operation} failed (winerror={winerror})")
        self.winerror = winerror


class LaunchError(JobError):
    """Launch failed closed before user code could run."""


class CleanupError(JobError):
    """Containment cleanup is unconfirmed. Retain the temporary target."""


class LaunchClosed(LaunchError):
    """Shutdown has linearized before launch."""


def wide(value: str):
    if "\0" in value:
        raise ValueError("embedded NUL")
    data = value.encode("utf-16-le") + b"\0\0"
    return (C.c_uint16 * (len(data) // 2)).from_buffer_copy(data)


def command_buffer(python: Path, args: list[str]):
    if not python.is_absolute():
        raise ValueError("interpreter must be absolute")
    argv = [str(python), *args]
    if any("\0" in value for value in argv):
        raise ValueError("embedded NUL in argv")
    command = subprocess.list2cmdline(argv)
    if len(command.encode("utf-16-le")) // 2 + 1 > 32767:
        raise ValueError("command exceeds 32767 UTF-16 units including terminator")
    return wide(command)


def environment_buffer(environment: dict[str, str]):
    folded = set()
    rows = []
    for name, value in environment.items():
        if not name or "=" in name or "\0" in name or "\0" in value:
            raise ValueError("invalid environment name or value")
        key = name.upper()
        if key in folded:
            raise ValueError("duplicate case-insensitive environment name")
        folded.add(key)
        rows.append((key, name + "=" + value))
    # UTF-16 ordinal ordering is independent of the host locale.
    rows.sort(
        key=lambda row: tuple(
            int.from_bytes(row[0].encode("utf-16-le")[i : i + 2], "little")
            for i in range(0, len(row[0].encode("utf-16-le")), 2)
        )
    )
    data = ("\0".join(row[1] for row in rows) + "\0\0").encode("utf-16-le")
    return (C.c_uint16 * (len(data) // 2)).from_buffer_copy(data)


class KernelAPI:
    def __init__(self, kernel=None):
        if kernel is None:
            if os.name != "nt":
                raise LaunchError("Windows Job Objects require Windows 10 or newer")
            kernel = C.WinDLL("kernel32", use_last_error=True)
        for name, (args, result) in SIGNATURES.items():
            function = getattr(kernel, name)
            function.argtypes = args
            function.restype = result
            setattr(self, name, function)

    def last_error(self):
        return C.get_last_error()

    def error(self, operation: str, kind=JobError):
        return kind(operation, self.last_error())

    def log_handle(self, stream: BinaryIO):
        import msvcrt

        return msvcrt.get_osfhandle(stream.fileno())


def console_handler() -> None:
    api = KernelAPI()
    if not api.SetConsoleCtrlHandler(None, True):
        raise api.error("SetConsoleCtrlHandler", LaunchError)


@dataclass(eq=False)
class _Entry:
    job: int | None
    process_handle: int | None = None
    pending_handles: dict[int, str] = field(default_factory=dict)
    cause: int | None = None
    error: CleanupError | None = None
    leaked_descendants: int = 0


class ProcessTracker:
    def __init__(self, api=None, *, clock=time.monotonic, pause=time.sleep):
        self._api = api
        self._entries: set[_Entry] = set()
        self._closing = False
        self._closing_cause = DRIVER_ABORT
        self._cleanup_deadline: float | None = None
        self.clock = clock
        self.pause = pause

    @property
    def api(self):
        if self._api is None:
            self._api = KernelAPI()
        return self._api

    def is_closing(self):
        with _LIFECYCLE:
            return self._closing

    @property
    def closing_cause(self):
        with _LIFECYCLE:
            return self._closing_cause

    def close(self):
        with _LIFECYCLE:
            self._closing = True

    def cleanup_deadline(self, proposed: float):
        with _LIFECYCLE:
            return min(proposed, self._cleanup_deadline) if self._cleanup_deadline is not None else proposed

    def _terminate(self, entry, cause):
        if entry.job is not None and entry.cause is None:
            entry.cause = cause
            if not self.api.TerminateJobObject(entry.job, cause):
                entry.error = self.api.error("TerminateJobObject", CleanupError)

    def terminate(self, entry, cause):
        if cause not in REASONS:
            raise ValueError("unknown termination cause")
        with _LIFECYCLE:
            self._terminate(entry, cause)
            if entry.error:
                raise entry.error

    def _close(self, entry):
        if entry.job is not None:
            if self.api.CloseHandle(entry.job):
                entry.job = None
            else:
                entry.error = self.api.error("CloseHandle(job)", CleanupError)
        for handle, operation in list(entry.pending_handles.items()):
            if self.api.CloseHandle(handle):
                del entry.pending_handles[handle]
                if handle == entry.process_handle:
                    entry.process_handle = None
            else:
                entry.error = self.api.error(operation, CleanupError)
        if entry.job is None and not entry.pending_handles:
            self._entries.discard(entry)

    def _active(self, entry):
        info = BASIC_ACCOUNTING()
        if not self.api.QueryInformationJobObject(entry.job, 1, C.byref(info), C.sizeof(info), None):
            raise self.api.error("QueryInformationJobObject", CleanupError)
        return info.ActiveProcesses

    def finish(self, entry, *, deadline, cause=DRIVER_ABORT, natural=False):
        with _LIFECYCLE:
            # A reported close failure is retryable while ownership is retained.
            if entry.error is not None and entry.error.operation.startswith("CloseHandle("):
                entry.error = None
            if natural and entry.job is not None:
                try:
                    entry.leaked_descendants = self._active(entry)
                except CleanupError as exc:
                    entry.error = exc
            self._terminate(entry, cause)
        try:
            while True:
                with _LIFECYCLE:
                    if entry.error:
                        raise entry.error
                    if entry.job is None:
                        return
                    if self._active(entry) == 0:
                        return
                    remaining = self.cleanup_deadline(deadline) - self.clock()
                    if remaining <= 0:
                        raise CleanupError("cleanup unconfirmed: ActiveProcesses did not reach zero")
                self.pause(min(0.01, remaining))
        except JobError as exc:
            with _LIFECYCLE:
                entry.error = exc
            raise
        finally:
            with _LIFECYCLE:
                self._close(entry)
                if entry.error:
                    raise entry.error

    def kill_all(self, *, cause=AGGREGATE_DEADLINE, deadline=None):
        if cause not in REASONS:
            raise ValueError("unknown termination cause")
        if deadline is None:
            deadline = self.clock() + 15
        with _LIFECYCLE:
            if not self._closing:
                self._closing_cause = cause
            self._closing = True
            self._cleanup_deadline = self.cleanup_deadline(deadline)
            entries = list(self._entries)
            # Initiate termination of EVERY job before waiting for ANY job.
            for entry in entries:
                self._terminate(entry, cause)
        errors = []
        for entry in entries:
            try:
                self.finish(entry, deadline=deadline, cause=cause)
            except JobError as exc:
                errors.append(exc)
        if errors:
            raise errors[0]


class LaunchedProcess:
    def __init__(self, tracker, entry, handle, pid):
        self.tracker = tracker
        self.token = entry
        entry.process_handle = handle
        self.pid = pid

    @property
    def handle(self):
        with _LIFECYCLE:
            return self.token.process_handle

    @property
    def cause(self):
        with _LIFECYCLE:
            return self.token.cause

    def wait(self, timeout):
        milliseconds = min(0xFFFFFFFE, max(0, math.ceil(timeout * 1000)))
        result = self.tracker.api.WaitForSingleObject(self.handle, milliseconds)
        if result == WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(str(self.pid), timeout)
        if result != WAIT_OBJECT_0:
            raise self.tracker.api.error("WaitForSingleObject", CleanupError)
        code = DWORD()
        if not self.tracker.api.GetExitCodeProcess(self.handle, C.byref(code)):
            raise self.tracker.api.error("GetExitCodeProcess", CleanupError)
        return code.value

    def terminate(self, cause):
        self.tracker.terminate(self.token, cause)

    def finish(self, *, deadline, natural=False):
        self.tracker.finish(self.token, deadline=deadline, natural=natural)

    def close(self):
        with _LIFECYCLE:
            handle = self.token.process_handle
            if handle is not None:
                if not self.tracker.api.CloseHandle(handle):
                    # finish() may already have removed the closed job's entry.
                    self.token.pending_handles[handle] = "CloseHandle(process)"
                    self.tracker._entries.add(self.token)
                    raise self.tracker.api.error("CloseHandle(process)", CleanupError)
                self.token.process_handle = None
                self.token.pending_handles.pop(handle, None)
                if self.token.job is None and not self.token.pending_handles:
                    self.tracker._entries.discard(self.token)


def launch_process(
    *,
    python: Path,
    args: list[str],
    cwd: Path,
    env: dict[str, str],
    log: BinaryIO,
    tracker: ProcessTracker,
    deadline: float | None = None,
    clock=time.monotonic,
) -> LaunchedProcess:
    command = command_buffer(python, args)
    application = wide(str(python))
    directory = wide(str(cwd))
    if not cwd.is_absolute():
        raise ValueError("cwd must be absolute")
    environment = environment_buffer(env)
    with _LIFECYCLE:
        if tracker._closing:
            raise LaunchClosed("tracker closing")
        if deadline is not None and clock() >= deadline:
            raise LaunchClosed("launch budget exhausted")
        api = tracker.api
        entry = _Entry(None)
        temporary = []
        attributes = None
        initialized = False
        pi = PROCESS_INFORMATION()
        launch_error = None
        cleanup_error = None
        try:
            entry.job = api.CreateJobObjectW(None, None)
            if not entry.job:
                raise api.error("CreateJobObjectW", LaunchError)
            tracker._entries.add(entry)
            if not api.SetHandleInformation(entry.job, HANDLE_FLAG_INHERIT, 0):
                raise api.error("SetHandleInformation", LaunchError)
            limits = EXTENDED_LIMIT()
            limits.BasicLimitInformation.LimitFlags = KILL_ON_JOB_CLOSE
            if not api.SetInformationJobObject(entry.job, 9, C.byref(limits), C.sizeof(limits)):
                raise api.error("SetInformationJobObject", LaunchError)
            duplicate = HANDLE()
            current = api.GetCurrentProcess()
            if not api.DuplicateHandle(
                current, api.log_handle(log), current, C.byref(duplicate), 0, True, DUPLICATE_SAME_ACCESS
            ):
                raise api.error("DuplicateHandle", LaunchError)
            temporary.append(duplicate.value)
            security = SECURITY_ATTRIBUTES(C.sizeof(SECURITY_ATTRIBUTES), None, True)
            stdin = api.CreateFileW(wide("NUL"), 0x80000000, 3, C.byref(security), 3, 0, None)
            if stdin in (None, 0, C.c_void_p(-1).value):
                raise api.error("CreateFileW(NUL)", LaunchError)
            temporary.append(stdin)
            size = SIZE_T()
            # The sizing call must fail with ERROR_INSUFFICIENT_BUFFER.
            sized = api.InitializeProcThreadAttributeList(None, 2, 0, C.byref(size))
            if sized or not size.value or api.last_error() != 122:
                raise api.error("InitializeProcThreadAttributeList(size)", LaunchError)
            attributes = C.create_string_buffer(size.value)
            if not api.InitializeProcThreadAttributeList(attributes, 2, 0, C.byref(size)):
                raise api.error("InitializeProcThreadAttributeList", LaunchError)
            initialized = True
            jobs = (HANDLE * 1)(entry.job)
            handles = (HANDLE * len(set(temporary)))(*dict.fromkeys(temporary))
            for attribute, array in ((JOB_LIST, jobs), (HANDLE_LIST, handles)):
                if not api.UpdateProcThreadAttribute(attributes, 0, attribute, array, C.sizeof(array), None, None):
                    raise api.error("UpdateProcThreadAttribute", LaunchError)
            startup = STARTUPINFOEXW()
            startup.StartupInfo.cb = C.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x100
            startup.StartupInfo.hStdInput = stdin
            startup.StartupInfo.hStdOutput = startup.StartupInfo.hStdError = duplicate.value
            startup.lpAttributeList = C.cast(attributes, HANDLE)
            if deadline is not None and clock() >= deadline:
                raise LaunchClosed("launch setup exhausted budget")
            if not api.CreateProcessW(
                application,
                command,
                None,
                None,
                True,
                CREATION_FLAGS,
                environment,
                directory,
                C.byref(startup),
                C.byref(pi),
            ):
                raise api.error("CreateProcessW", LaunchError)
        except BaseException as exc:
            launch_error = exc
        finally:
            try:
                # Keep attributes, jobs, handles and buffers alive through deletion.
                if initialized:
                    api.DeleteProcThreadAttributeList(attributes)
                for handle in [pi.hThread, *temporary]:
                    if handle and not api.CloseHandle(handle):
                        cleanup_error = api.error("CloseHandle(launch temporary)", CleanupError)
                        entry.pending_handles[handle] = "CloseHandle(launch temporary)"
                if launch_error or cleanup_error:
                    tracker._terminate(entry, DRIVER_ABORT)
                    if pi.hProcess and not api.CloseHandle(pi.hProcess):
                        cleanup_error = api.error("CloseHandle(process)", CleanupError)
                        entry.pending_handles[pi.hProcess] = "CloseHandle(process)"
                    # Failed closes remain owned for the driver's shutdown retry.
                    pending = entry.pending_handles
                    entry.pending_handles = {}
                    tracker._close(entry)
                    entry.pending_handles = pending
                    if entry.pending_handles:
                        tracker._entries.add(entry)
                    cleanup_error = cleanup_error or entry.error
                if cleanup_error:
                    raise cleanup_error
                if launch_error:
                    raise launch_error
            finally:
                # Like an except target, release aliases that would retain this frame.
                launch_error = cleanup_error = entry.error = None
        return LaunchedProcess(tracker, entry, pi.hProcess, pi.dwProcessId)
