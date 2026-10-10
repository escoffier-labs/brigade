"""Injected kernel contracts and separately selected, real Windows acceptance."""

from __future__ import annotations

import ast
import ctypes as C
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

from scripts import windows_job as job
from scripts import windows_pytest as driver


def dereference(pointer, kind):
    return C.cast(pointer, C.POINTER(kind)).contents


def decode(buffer):
    return bytes(buffer).decode("utf-16-le")


class FakeAPI:
    """Simulated kernel boundary, with observable handle ownership and failures."""

    def __init__(self, failure=None):
        self.events = []
        self.failure = failure
        self.next_handle = 100
        self.live = set()
        self.active = {}
        self.exitcode = 0
        self.wait_result = job.WAIT_OBJECT_0
        self.attributes = {}
        self.launches = []

    def last_error(self):
        return 122

    def error(self, operation, kind=job.JobError):
        return kind(operation, 5)

    def record(self, name, *args):
        self.events.append((name, *args))
        return self.failure != name

    def handle(self):
        self.next_handle += 1
        self.live.add(self.next_handle)
        return self.next_handle

    def log_handle(self, stream):
        return 77

    def CreateJobObjectW(self, security, name):
        assert security is None and name is None
        if not self.record("CreateJobObjectW"):
            return None
        handle = self.handle()
        self.active[handle] = 0
        return handle

    def SetHandleInformation(self, handle, flags, value):
        assert flags == 1 and value == 0
        return self.record("SetHandleInformation", handle)

    def SetInformationJobObject(self, handle, kind, info, size):
        assert kind == 9 and size == C.sizeof(job.EXTENDED_LIMIT)
        assert dereference(info, job.EXTENDED_LIMIT).BasicLimitInformation.LimitFlags == 0x2000
        return self.record("SetInformationJobObject", handle)

    def GetCurrentProcess(self):
        return C.c_void_p(-1).value

    def DuplicateHandle(self, source_process, source, dest_process, output, access, inherit, options):
        assert source == 77 and inherit and options == 2
        if not self.record("DuplicateHandle"):
            return False
        dereference(output, job.HANDLE).value = self.handle()
        return True

    def CreateFileW(self, name, access, share, security, creation, flags, template):
        assert decode(name) == "NUL\0"
        assert dereference(security, job.SECURITY_ATTRIBUTES).bInheritHandle
        return self.handle() if self.record("CreateFileW") else C.c_void_p(-1).value

    def InitializeProcThreadAttributeList(self, storage, count, flags, size):
        assert count == 2
        if storage is None:
            dereference(size, job.SIZE_T).value = 128
            return False
        return self.record("InitializeProcThreadAttributeList")

    def UpdateProcThreadAttribute(self, storage, flags, attribute, array, size, previous, returned):
        values = list(array)
        self.attributes[attribute] = values
        return self.record(f"Update:{attribute}", *values)

    def DeleteProcThreadAttributeList(self, storage):
        self.record("DeleteProcThreadAttributeList")

    def CreateProcessW(self, app, cmd, pa, ta, inherit, flags, env, cwd, startup, process):
        assert inherit and flags == job.CREATION_FLAGS
        info = dereference(startup, job.STARTUPINFOEXW)
        assert info.StartupInfo.cb == C.sizeof(job.STARTUPINFOEXW)
        assert info.StartupInfo.hStdOutput == info.StartupInfo.hStdError
        assert set(self.attributes[job.HANDLE_LIST]) == {info.StartupInfo.hStdInput, info.StartupInfo.hStdOutput}
        assert self.attributes[job.JOB_LIST][0] not in self.attributes[job.HANDLE_LIST]
        self.launches.append((decode(app), decode(cmd), decode(env), decode(cwd)))
        if not self.record("CreateProcessW"):
            return False
        pi = dereference(process, job.PROCESS_INFORMATION)
        pi.hProcess = self.handle()
        pi.hThread = self.handle()
        pi.dwProcessId = 123
        return True

    def CloseHandle(self, handle):
        assert handle in self.live, f"double close: {handle}"
        if not self.record("CloseHandle", handle):
            return False
        self.live.remove(handle)
        return True

    def TerminateJobObject(self, handle, cause):
        assert handle in self.live
        if not self.record("TerminateJobObject", handle, cause):
            return False
        self.exitcode = cause
        return True

    def QueryInformationJobObject(self, handle, kind, info, size, returned):
        assert handle in self.live
        dereference(info, job.BASIC_ACCOUNTING).ActiveProcesses = self.active[handle]
        return self.record("QueryInformationJobObject", handle)

    def WaitForSingleObject(self, handle, timeout):
        assert handle in self.live
        self.record("WaitForSingleObject", handle, timeout)
        return self.wait_result

    def GetExitCodeProcess(self, handle, code):
        assert handle in self.live
        dereference(code, job.DWORD).value = self.exitcode
        return self.record("GetExitCodeProcess")


def launch(tmp_path, tracker, args=None, python=None, *, env=None):
    with (tmp_path / "log").open("ab") as log:
        return job.launch_process(
            python=python or Path(sys.executable).absolute(),
            args=args or [],
            cwd=tmp_path,
            env=dict(os.environ) if env is None else env,
            log=log,
            tracker=tracker,
        )


def test_windows_width_layout_and_every_kernel_signature():
    if C.sizeof(C.c_void_p) == 8:
        assert [
            C.sizeof(kind)
            for kind in (job.STARTUPINFOW, job.STARTUPINFOEXW, job.PROCESS_INFORMATION, job.EXTENDED_LIMIT)
        ] == [104, 112, 24, 144]
    assert C.sizeof(job.DWORD) == 4
    library = SimpleNamespace(**{name: lambda *args: None for name in job.SIGNATURES})
    api = job.KernelAPI(library)
    for name, (arguments, result) in job.SIGNATURES.items():
        assert getattr(api, name).argtypes == arguments
        assert getattr(api, name).restype is result
    assert api.CreateProcessW.argtypes[-1] == C.POINTER(job.PROCESS_INFORMATION)
    assert api.CreateJobObjectW.restype is C.c_void_p
    assert api.CloseHandle.argtypes == [C.c_void_p]


def test_atomic_launch_minimal_inheritance_buffers_and_single_ownership(tmp_path):
    api = FakeAPI()
    tracker = job.ProcessTracker(api)
    process = launch(tmp_path, tracker, ["-c", 'print("hello space")', "😀"], env={"z": "last", "A": "first"})
    assert (
        api.launches[0][1]
        == subprocess.list2cmdline([str(Path(sys.executable).absolute()), "-c", 'print("hello space")', "😀"]) + "\0"
    )
    assert api.launches[0][2] == "A=first\0z=last\0\0"
    names = [event[0] for event in api.events]
    assert names.index(f"Update:{job.JOB_LIST}") < names.index("CreateProcessW")
    assert names.index(f"Update:{job.HANDLE_LIST}") < names.index("CreateProcessW")
    assert names.index("CreateProcessW") < names.index("DeleteProcThreadAttributeList")
    assert api.live == {process.handle, process.token.job}
    process.finish(deadline=time.monotonic() + 1, natural=True)
    process.finish(deadline=time.monotonic() + 1)
    tracker.kill_all()
    process.close()
    process.close()
    assert not api.live
    assert len([event for event in api.events if event[0] == "TerminateJobObject"]) == 1


@pytest.mark.parametrize(
    "failure",
    [
        "CreateJobObjectW",
        "SetHandleInformation",
        "SetInformationJobObject",
        "DuplicateHandle",
        "CreateFileW",
        "InitializeProcThreadAttributeList",
        f"Update:{job.JOB_LIST}",
        f"Update:{job.HANDLE_LIST}",
        "CreateProcessW",
    ],
)
def test_launch_failures_close_all_handles_without_executing(tmp_path, failure):
    api = FakeAPI(failure)
    tracker = job.ProcessTracker(api)
    with pytest.raises(job.LaunchError) as caught:
        launch(tmp_path, tracker)
    assert caught.value.winerror == 5
    assert not api.live and not tracker._entries
    if failure != "CreateProcessW":
        assert not api.launches
    deleted = sum(event[0] == "DeleteProcThreadAttributeList" for event in api.events)
    assert deleted == int(failure in {f"Update:{job.JOB_LIST}", f"Update:{job.HANDLE_LIST}", "CreateProcessW"})


@pytest.mark.parametrize(
    "env", [{"": "v"}, {"=C:": "v"}, {"a=b": "v"}, {"a\0": "v"}, {"a": "v\0"}, {"Path": "a", "PATH": "b"}]
)
def test_invalid_environment_rejected_before_any_launch(env):
    with pytest.raises(ValueError):
        job.environment_buffer(env)


def test_attribute_setup_consumes_same_launch_budget_without_running_code(tmp_path):
    api = FakeAPI()
    tracker = job.ProcessTracker(api)
    ticks = iter([0.0, 1.0])
    with (tmp_path / "log").open("wb") as log, pytest.raises(job.LaunchClosed, match="setup"):
        job.launch_process(
            python=Path(sys.executable).resolve(),
            args=[],
            cwd=tmp_path,
            env={},
            log=log,
            tracker=tracker,
            deadline=0.5,
            clock=lambda: next(ticks),
        )
    assert not api.launches and not api.live


def test_utf16_boundaries_sorting_and_no_truncation():
    assert decode(job.environment_buffer({})) == "\0\0"
    assert decode(job.environment_buffer({"z": "😀", "á": "é", "A": "v"})) == "A=v\0z=😀\0á=é\0\0"
    python = Path(sys.executable).resolve()
    overhead = len(subprocess.list2cmdline([str(python), "x"]).encode("utf-16-le")) // 2
    value = "x" * (32767 - overhead)
    assert len(bytes(job.command_buffer(python, [value]))) == 32767 * 2
    for args in ([value + "x"], [value[:-1] + "😀"], ["a\0b"]):
        with pytest.raises(ValueError):
            job.command_buffer(python, args)
    with pytest.raises(ValueError):
        job.command_buffer(Path("python.exe"), [])


@pytest.mark.parametrize(
    "operation",
    ["WaitForSingleObject", "GetExitCodeProcess", "QueryInformationJobObject", "TerminateJobObject", "CloseHandle"],
)
def test_kernel_failures_are_typed_and_cleanup_overrides_success(tmp_path, operation):
    api = FakeAPI()
    tracker = job.ProcessTracker(api)
    process = launch(tmp_path, tracker)
    api.failure = operation
    if operation == "WaitForSingleObject":
        api.wait_result = job.WAIT_FAILED
    try:
        with pytest.raises(job.CleanupError) as caught:
            if operation in {"WaitForSingleObject", "GetExitCodeProcess"}:
                process.wait(1)
            else:
                process.finish(deadline=time.monotonic() + 1, natural=True)
        assert caught.value.winerror == 5
    finally:
        api.failure = None
        if process.token.job is not None:
            process.finish(deadline=time.monotonic() + 1)
        process.close()
    assert not api.live


def test_shared_cleanup_deadline_terminates_all_before_any_wait_and_closes_unconfirmed_jobs(tmp_path):
    clock = [0.0]
    api = FakeAPI()
    tracker = job.ProcessTracker(
        api, clock=lambda: clock[0], pause=lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    processes = [launch(tmp_path, tracker) for _ in range(6)]
    for process in processes:
        api.active[process.token.job] = 1
    api.events.clear()
    with pytest.raises(job.CleanupError, match="ActiveProcesses"):
        tracker.kill_all(deadline=0.03)
    assert clock[0] == pytest.approx(0.03)
    assert [event[0] for event in api.events[:6]] == ["TerminateJobObject"] * 6
    assert not tracker._entries
    for process in processes:
        process.close()
    assert not api.live


@pytest.mark.parametrize("cause", [job.FILE_TIMEOUT, job.AGGREGATE_DEADLINE, job.DRIVER_ABORT])
def test_first_termination_cause_is_immutable_with_natural_exit_preserved(tmp_path, cause):
    api = FakeAPI()
    tracker = job.ProcessTracker(api)
    process = launch(tmp_path, tracker)
    process.terminate(cause)
    for other in job.REASONS:
        process.terminate(other)
    assert process.cause == cause and process.wait(1) == cause
    api.exitcode = 7  # Root completed naturally while termination was initiated.
    assert process.wait(1) == 7
    process.finish(deadline=time.monotonic() + 1, natural=True)
    process.close()
    assert sum(event[0] == "TerminateJobObject" for event in api.events) == 1


def test_launch_shutdown_waits_for_held_lifecycle_lock(tmp_path):
    api = FakeAPI()
    tracker = job.ProcessTracker(api)
    reached = threading.Event()
    release = threading.Event()
    closing = threading.Event()
    original = api.CreateProcessW
    api.CreateProcessW = lambda *args: (reached.set(), release.wait(2), original(*args))[-1]
    processes = []
    errors = []

    def worker():
        try:
            processes.append(launch(tmp_path, tracker))
        except BaseException as exc:
            errors.append(exc)

    def shutdown():
        closing.set()
        tracker.kill_all(deadline=time.monotonic() + 2)

    first = threading.Thread(target=worker)
    second = threading.Thread(target=worker)
    kill = threading.Thread(target=shutdown)
    first.start()
    assert reached.wait(1)
    kill.start()
    assert closing.wait(1)
    second.start()
    assert not tracker._closing  # closing cannot pass the held launch mutex.
    release.set()
    for thread in (first, second, kill):
        thread.join(3)
        assert not thread.is_alive()
    assert all(isinstance(exc, job.LaunchClosed) for exc in errors)
    for process in processes:
        process.close()
    assert not api.live
    assert tracker.is_closing()
    assert len(processes) >= 1


class ProxyAPI:
    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)


@pytest.fixture
def native(tmp_path):
    if os.name != "nt":
        pytest.skip("native Windows containment requires Windows")
    api = job.KernelAPI()
    tracker = job.ProcessTracker(api)
    processes = []
    handles = []

    def start(code, *, python=None, selected_tracker=None):
        process = launch(tmp_path, selected_tracker or tracker, ["-c", code], python, env=dict(os.environ))
        processes.append(process)
        return process

    def open_process(pid):
        handle = api.OpenProcess(
            job.SYNCHRONIZE | job.PROCESS_QUERY_LIMITED_INFORMATION | job.PROCESS_TERMINATE, False, pid
        )
        assert handle, api.error("OpenProcess")
        handles.append(handle)
        assert api.WaitForSingleObject(handle, 0) == job.WAIT_TIMEOUT
        return handle

    state = SimpleNamespace(
        api=api,
        tracker=tracker,
        processes=processes,
        handles=handles,
        start=start,
        open_process=open_process,
        path=tmp_path,
    )
    try:
        yield state
    finally:
        trackers = {process.tracker for process in processes} | {tracker}
        try:
            for current in trackers:
                current.kill_all(cause=job.DRIVER_ABORT, deadline=time.monotonic() + 10)
        finally:
            for process in processes:
                process.close()
            for handle in handles:
                assert api.CloseHandle(handle)


def read_marker(path, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = path.read_text()
            if value:
                return value
        except (OSError, UnicodeError):
            pass
        time.sleep(0.01)
    pytest.fail(f"marker not received: {path.name}")


def descendant_code(marker):
    # Every descendant expires without depending on the test's cleanup.
    grandchild = f"import os,time,pathlib; pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(30)"
    return f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{grandchild!r}],creationflags=0x208)"


def in_job(native, handle, token):
    member = job.BOOL()
    assert native.api.IsProcessInJob(handle, token.job, C.byref(member)), native.api.error("IsProcessInJob")
    assert member.value == 1


def signaled(native, handle):
    assert native.api.WaitForSingleObject(handle, 10000) == job.WAIT_OBJECT_0


class TestNativeContainment:
    """CI selects this class only and rejects every selected skip."""

    @pytest.mark.parametrize("returncode", [None, 0, 7])
    def test_exited_intermediary_grandchild_remains_contained(self, native, returncode):
        intermediary_marker = native.path / "intermediary-pid"
        grandchild_marker = native.path / "grandchild-pid"
        intermediary_release = native.path / "intermediary-release"
        root_release = native.path / "root-release"
        sleeper = (
            "import os,pathlib,time; "
            f"pathlib.Path({str(grandchild_marker)!r}).write_text(str(os.getpid())); time.sleep(35)"
        )
        intermediary = (
            "import os,pathlib,subprocess,sys,time; "
            f"subprocess.Popen([sys.executable,'-c',{sleeper!r}],creationflags=0x208); "
            f"pathlib.Path({str(intermediary_marker)!r}).write_text(str(os.getpid())); "
            f"release=pathlib.Path({str(intermediary_release)!r}); deadline=time.monotonic()+30; "
            "exec('while not release.exists():\\n assert time.monotonic()<deadline\\n time.sleep(0.01)')"
        )
        root = (
            "import pathlib,subprocess,sys,time; "
            f"subprocess.Popen([sys.executable,'-c',{intermediary!r}],creationflags=0x208); "
            f"release=pathlib.Path({str(root_release)!r}); deadline=time.monotonic()+35; "
            "exec('while not release.exists():\\n assert time.monotonic()<deadline\\n time.sleep(0.01)'); "
            f"sys.exit({returncode or 0})"
        )
        process = native.start(root)
        try:
            # Markers discover PIDs. Held handles establish identity and exit.
            root_handle = native.open_process(process.pid)
            intermediary_handle = native.open_process(int(read_marker(intermediary_marker)))
            grandchild_handle = native.open_process(int(read_marker(grandchild_marker)))
            intermediary_release.touch()
            signaled(native, intermediary_handle)
            assert native.api.WaitForSingleObject(root_handle, 0) == job.WAIT_TIMEOUT
            assert native.api.WaitForSingleObject(grandchild_handle, 0) == job.WAIT_TIMEOUT
            in_job(native, grandchild_handle, process.token)
            if returncode is None:
                process.terminate(job.FILE_TIMEOUT)
                assert process.wait(10) == job.FILE_TIMEOUT
                process.finish(deadline=time.monotonic() + 10)
            else:
                root_release.touch()
                assert process.wait(10) == returncode
                process.finish(deadline=time.monotonic() + 10, natural=True)
                assert process.token.leaked_descendants >= 1
                assert process.wait(0) == returncode
            signaled(native, root_handle)
            signaled(native, grandchild_handle)
        finally:
            intermediary_release.touch()
            root_release.touch()
            native.tracker.kill_all(cause=job.DRIVER_ABORT, deadline=time.monotonic() + 10)

    @pytest.mark.parametrize("returncode", [0, 7])
    def test_normal_root_exit_cleans_orphan_and_preserves_status(self, native, returncode):
        marker = native.path / "grandchild"
        process = native.start(descendant_code(marker) + f"; sys.exit({returncode})")
        handle = native.open_process(int(read_marker(marker)))
        in_job(native, handle, process.token)
        assert process.wait(10) == returncode
        process.finish(deadline=time.monotonic() + 10, natural=True)
        assert process.token.leaked_descendants >= 1
        signaled(native, handle)
        assert process.wait(1) == returncode

    def test_first_statement_descendant_is_contained_twenty_times(self, native):
        for attempt in range(20):
            marker = native.path / f"grandchild-{attempt}"
            process = native.start(descendant_code(marker))
            handle = native.open_process(int(read_marker(marker)))
            assert process.wait(10) == 0  # Immediate parent has already exited.
            in_job(native, handle, process.token)
            process.finish(deadline=time.monotonic() + 10, natural=True)
            signaled(native, handle)
            process.close()

    @pytest.mark.parametrize("reason", [job.FILE_TIMEOUT, job.AGGREGATE_DEADLINE, job.DRIVER_ABORT])
    def test_reason_sentinels_kill_root_and_descendant(self, native, reason):
        marker = native.path / "grandchild"
        process = native.start(
            "print('pytest sentinel',flush=True); " + descendant_code(marker) + "; import time; time.sleep(30)"
        )
        child = native.open_process(process.pid)
        grand = native.open_process(int(read_marker(marker)))
        process.terminate(reason)
        for other in job.REASONS:
            process.terminate(other)
        assert process.cause == reason
        assert process.wait(10) == reason
        process.finish(deadline=time.monotonic() + 10)
        signaled(native, child)
        signaled(native, grand)
        assert b"pytest sentinel" in (native.path / "log").read_bytes()

    @pytest.mark.parametrize("behavior", ["passed", "failed", "timeout", "exception", "failed-cleanup"])
    def test_driver_status_diagnostics_and_output_survive_cleanup(self, native, behavior):
        marker = native.path / "grandchild"
        tests = native.path / "tests"
        tests.mkdir()
        body = "import time; print('pytest sentinel',flush=True); " + descendant_code(marker)
        if behavior in {"timeout", "exception"}:
            body += "; time.sleep(30)"
        elif behavior in {"failed", "failed-cleanup"}:
            body += "; assert False, 'natural test failure'"
        (tests / "test_probe.py").write_text(
            "def test_probe(capfd):\n    with capfd.disabled():\n        " + body + "\n"
        )
        tracker = native.tracker
        ready = threading.Event()
        if behavior == "exception":

            class ErrorAPI(ProxyAPI):
                def WaitForSingleObject(self, handle, timeout):
                    assert ready.wait(10)
                    C.set_last_error(5)
                    return job.WAIT_FAILED

            tracker = job.ProcessTracker(ErrorAPI(native.api))
        if behavior == "failed-cleanup":

            class CleanupFailureAPI(ProxyAPI):
                def QueryInformationJobObject(self, *args):
                    C.set_last_error(5)
                    return False

            tracker = job.ProcessTracker(CleanupFailureAPI(native.api))
        result = []
        errors = []

        def run():
            try:
                result.append(
                    driver.run_file(
                        "test_probe.py",
                        repo=native.path,
                        python=Path(sys.executable).resolve(),
                        output_dir=native.path / "out",
                        temp_root=native.path / "temps",
                        timeout_seconds=6 if behavior == "timeout" else 20,
                        tracker=tracker,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        try:
            handle = native.open_process(int(read_marker(marker)))
            ready.set()
            thread.join(25)
            assert not thread.is_alive() and not errors
            expected = (
                "cleanup-unconfirmed"
                if behavior == "exception"
                else ("failed" if behavior == "failed-cleanup" else behavior)
            )
            assert result[0].status == expected
            if behavior in {"timeout", "exception", "failed-cleanup"}:
                assert result[0].diagnostic
            if behavior == "failed-cleanup":
                assert result[0].returncode == 1
                assert driver.regressions(result, set()) == result
                assert driver.infrastructure_results(result) == result
            assert b"pytest sentinel" in (native.path / "out" / result[0].log).read_bytes()
            signaled(native, handle)
        finally:
            ready.set()
            tracker.kill_all(cause=job.DRIVER_ABORT, deadline=time.monotonic() + 10)
            thread.join(10)

    def test_abrupt_owner_death_kills_orphan_and_keeps_running_record(self, native):
        marker = native.path / "grandchild"
        record = native.path / "record.json"
        helper = Path(job.__file__).resolve().parent
        code = (
            f"import sys,time,pathlib; sys.path.insert(0,{str(helper)!r}); import windows_job as j,windows_pytest as d; "
            f"t=j.ProcessTracker(); d.write_record(pathlib.Path({str(record)!r}),results=[],expected_files=['test_probe.py']); "
            f"f=open({str(native.path / 'owner-log')!r},'wb'); "
            f"p=j.launch_process(python=pathlib.Path(sys.executable).resolve(),args=['-c',{descendant_code(marker)!r}],"
            f"cwd=pathlib.Path({str(native.path)!r}),env=dict(__import__('os').environ),log=f,tracker=t); time.sleep(30)"
        )
        owner = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            grand = native.open_process(int(read_marker(marker)))
            owner_handle = native.open_process(owner.pid)
            assert json.loads(record.read_text())["status"] == "running"
            assert native.api.TerminateProcess(owner_handle, job.DRIVER_ABORT)
            signaled(native, owner_handle)
            signaled(native, grand)
            assert owner.wait(timeout=10) != 0
            payload = json.loads(record.read_text())
            assert payload["status"] == "running" and payload["completed_count"] == 0
        finally:
            if owner.poll() is None:
                owner.kill()
            owner.communicate(timeout=10)

    def test_invalid_job_access_fails_without_uncontained_execution(self, native):
        marker = native.path / "must-not-run"
        duplicates = []

        class DeniedJobAPI(ProxyAPI):
            def UpdateProcThreadAttribute(self, storage, flags, attribute, array, size, previous, returned):
                if attribute == job.JOB_LIST:
                    duplicate = job.HANDLE()
                    current = self.GetCurrentProcess()
                    assert self.DuplicateHandle(
                        current, array[0], current, C.byref(duplicate), job.JOB_OBJECT_QUERY, False, 0
                    )
                    duplicates.append(duplicate.value)
                    # Retain the replacement attribute array through deletion.
                    self.denied_jobs = (job.HANDLE * 1)(duplicate.value)
                    array = self.denied_jobs
                return self.inner.UpdateProcThreadAttribute(storage, flags, attribute, array, size, previous, returned)

        tracker = job.ProcessTracker(DeniedJobAPI(native.api))
        try:
            with pytest.raises(job.LaunchError, match="CreateProcessW") as caught:
                launch(native.path, tracker, ["-c", f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')"])
            assert caught.value.winerror != 0
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                assert not marker.exists()
                time.sleep(0.05)
            assert not tracker._entries
        finally:
            tracker.kill_all(cause=job.DRIVER_ABORT)
            for handle in duplicates:
                assert native.api.CloseHandle(handle)

    def test_invalid_executable_and_attribute_failures_do_not_grow_handles(self, native):
        marker = native.path / "must-not-run"

        class AttributeFailure(ProxyAPI):
            def UpdateProcThreadAttribute(self, *args):
                C.set_last_error(87)
                return False

        class SetupFailure(ProxyAPI):
            def SetInformationJobObject(self, *args):
                C.set_last_error(5)
                return False

        trackers = [
            native.tracker,
            job.ProcessTracker(AttributeFailure(native.api)),
            job.ProcessTracker(SetupFailure(native.api)),
        ]

        def batch():
            for _ in range(10):
                for index, tracker in enumerate(trackers):
                    with pytest.raises(job.LaunchError):
                        launch(
                            native.path,
                            tracker,
                            ["-c", f"open({str(marker)!r},'w').write('ran')"],
                            native.path / "missing.exe" if index == 0 else Path(sys.executable).resolve(),
                        )

        def count():
            result = job.DWORD()
            assert native.api.GetProcessHandleCount(native.api.GetCurrentProcess(), C.byref(result))
            return result.value

        batch()
        before = count()
        batch()
        assert count() == before
        assert not marker.exists()

    def test_venv_redirector_and_actual_interpreter_are_in_job(self, native):
        venv = native.path / "venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, timeout=30)
        marker = native.path / "actual-pid"
        process = native.start(
            f"import os,time,pathlib; pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(30)",
            python=venv / "Scripts" / "python.exe",
        )
        actual_pid = int(read_marker(marker))
        assert actual_pid != process.pid, "redirector not exercised"
        redirector = native.open_process(process.pid)
        actual = native.open_process(actual_pid)
        in_job(native, redirector, process.token)
        in_job(native, actual, process.token)
        process.terminate(job.FILE_TIMEOUT)
        assert process.wait(10) == job.FILE_TIMEOUT
        signaled(native, redirector)
        signaled(native, actual)

    def test_unrelated_inheritable_pipe_is_excluded_and_job_not_inherited(self, native):
        read, write = job.HANDLE(), job.HANDLE()
        security = job.SECURITY_ATTRIBUTES(C.sizeof(job.SECURITY_ATTRIBUTES), None, True)
        assert native.api.CreatePipe(C.byref(read), C.byref(write), C.byref(security), 0)
        native.handles.extend([read.value, write.value])
        assert native.api.SetHandleInformation(read, 1, 0)
        process = native.start("import time; time.sleep(30)")
        child = native.open_process(process.pid)
        assert native.api.CloseHandle(write)
        native.handles.remove(write.value)
        deadline = time.monotonic() + 5
        while True:
            available = job.DWORD()
            ok = native.api.PeekNamedPipe(read, None, 0, None, C.byref(available), None)
            if not ok:
                assert C.get_last_error() == 109  # ERROR_BROKEN_PIPE is identity-safe EOF.
                break
            assert time.monotonic() < deadline, "unrelated pipe writer leaked to child"
            time.sleep(0.01)
        assert native.api.WaitForSingleObject(child, 0) == job.WAIT_TIMEOUT
        # Last job handle closure is sufficient even with the process alive.
        with job._LIFECYCLE:
            native.tracker._close(process.token)
        signaled(native, child)

    def test_breakaway_is_denied(self, native):
        marker = native.path / "breakaway"
        inner = (
            "try:\n subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)'],creationflags=0x1000000)\n"
            f"except OSError as e:\n pathlib.Path({str(marker)!r}).write_text(str(e.winerror))"
        )
        code = f"import subprocess,sys,pathlib; exec({inner!r})"
        process = native.start(code)
        assert read_marker(marker) == "5"
        assert process.wait(10) == 0

    def test_synchronized_launch_shutdown_signals_child_before_return(self, native):
        reached, release, attempted, returned = (threading.Event() for _ in range(4))

        class BarrierAPI(ProxyAPI):
            def CreateProcessW(self, *args):
                reached.set()
                assert release.wait(10)
                return self.inner.CreateProcessW(*args)

        tracker = job.ProcessTracker(BarrierAPI(native.api))
        processes, errors = [], []

        def worker():
            try:
                processes.append(native.start("import time; time.sleep(30)", selected_tracker=tracker))
            except BaseException as exc:
                errors.append(exc)

        def stop():
            attempted.set()
            try:
                tracker.kill_all(cause=job.AGGREGATE_DEADLINE, deadline=time.monotonic() + 10)
            except BaseException as exc:
                errors.append(exc)
            finally:
                returned.set()

        launcher = threading.Thread(target=worker)
        stopper = threading.Thread(target=stop)
        launcher.start()
        try:
            assert reached.wait(5)
            stopper.start()
            assert attempted.wait(5)
            assert not tracker._closing and not returned.is_set()
            release.set()
            launcher.join(10)
            stopper.join(10)
            assert returned.is_set() and not launcher.is_alive() and not stopper.is_alive() and not errors
            assert processes[0].wait(10) == job.AGGREGATE_DEADLINE
        finally:
            release.set()
            launcher.join(10)
            if stopper.ident:
                stopper.join(10)
            tracker.kill_all(cause=job.DRIVER_ABORT)

    @pytest.mark.parametrize("first", [job.FILE_TIMEOUT, job.AGGREGATE_DEADLINE, job.DRIVER_ABORT])
    def test_ordered_first_cause_is_immutable_and_natural_failure_survives(self, native, first):
        process = native.start("import time; time.sleep(30)")
        barrier = threading.Barrier(4)
        first_done = threading.Event()
        errors = []

        def terminate(reason):
            try:
                barrier.wait(5)
                if reason == first:
                    process.terminate(reason)
                    first_done.set()
                else:
                    assert first_done.wait(5)
                    process.terminate(reason)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=terminate, args=(reason,)) for reason in job.REASONS]
        for thread in threads:
            thread.start()
        barrier.wait(5)
        for thread in threads:
            thread.join(10)
            assert not thread.is_alive()
        assert not errors and process.cause == first and process.wait(10) == first
        entered, permit = threading.Event(), threading.Event()
        marker = native.path / "natural-ready"
        release = native.path / "natural-release"

        class NaturalRaceAPI(ProxyAPI):
            def TerminateJobObject(self, handle, cause):
                entered.set()
                assert permit.wait(10)
                return self.inner.TerminateJobObject(handle, cause)

        tracker = job.ProcessTracker(NaturalRaceAPI(native.api))
        code = (
            f"import pathlib,time,sys; pathlib.Path({str(marker)!r}).write_text('ready'); "
            f"release=pathlib.Path({str(release)!r}); deadline=time.monotonic()+10; "
            "exec('while not release.exists():\\n assert time.monotonic()<deadline\\n time.sleep(0.01)'); sys.exit(7)"
        )
        natural = native.start(code, selected_tracker=tracker)
        read_marker(marker)
        identity = native.open_process(natural.pid)

        def compete():
            try:
                natural.terminate(first)
            except BaseException as exc:
                errors.append(exc)

        competing = threading.Thread(target=compete)
        competing.start()
        try:
            assert entered.wait(5)
            release.write_text("exit")
            signaled(native, identity)
            # Root finishes while termination is pending; observation remains natural.
            assert natural.wait(0) == 7
            permit.set()
            competing.join(10)
            assert not competing.is_alive() and not errors
            for reason in job.REASONS:
                natural.terminate(reason)
            assert natural.wait(0) == 7
            natural.finish(deadline=time.monotonic() + 10, natural=True)
        finally:
            permit.set()
            release.touch()
            competing.join(10)

    def test_concurrent_launches_exclude_unlisted_inheritable_handles(self, native):
        import msvcrt

        barrier = threading.Barrier(3)
        errors, processes, streams, reads = [], {}, [], []
        for _ in range(2):
            read, write = job.HANDLE(), job.HANDLE()
            security = job.SECURITY_ATTRIBUTES(C.sizeof(job.SECURITY_ATTRIBUTES), None, False)
            assert native.api.CreatePipe(C.byref(read), C.byref(write), C.byref(security), 0)
            native.handles.append(read.value)
            reads.append(read.value)
            streams.append(os.fdopen(msvcrt.open_osfhandle(write.value, os.O_WRONLY), "wb"))

        def worker(index):
            try:
                barrier.wait(5)
                marker = native.path / f"ready-{index}"
                processes[index] = job.launch_process(
                    python=Path(sys.executable).resolve(),
                    args=[
                        "-c",
                        f"import pathlib,time; pathlib.Path({str(marker)!r}).write_text('ready'); time.sleep(30)",
                    ],
                    cwd=native.path,
                    env=dict(os.environ),
                    log=streams[index],
                    tracker=native.tracker,
                )
                native.processes.append(processes[index])
                read_marker(marker)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
        try:
            for thread in threads:
                thread.start()
            barrier.wait(5)
            for thread in threads:
                thread.join(20)
                assert not thread.is_alive()
            assert not errors
            for stream in streams:
                stream.close()
            for index in range(2):
                process = processes[index]
                process.terminate(job.FILE_TIMEOUT)
                assert process.wait(10) == job.FILE_TIMEOUT
                process.finish(deadline=time.monotonic() + 10)
                deadline = time.monotonic() + 5
                while native.api.PeekNamedPipe(reads[index], None, 0, None, None, None):
                    assert time.monotonic() < deadline, "other child inherited temporary pipe writer"
                    time.sleep(0.01)
                assert C.get_last_error() == 109
                if index == 0:
                    assert native.api.WaitForSingleObject(processes[1].handle, 0) == job.WAIT_TIMEOUT
        finally:
            for stream in streams:
                stream.close()
            for thread in threads:
                if thread.ident:
                    thread.join(10)

    def test_record_failure_aborts_live_workers_before_final_record(self, native):
        marker = native.path / "grandchild"
        tests = native.path / "tests"
        tests.mkdir()
        (tests / "test_slow.py").write_text(
            "def test_slow():\n    " + descendant_code(marker) + "; import time; time.sleep(30)\n"
        )
        (tests / "test_fast.py").write_text(
            "def test_fast():\n    import pathlib,time\n"
            f"    deadline=time.monotonic()+10\n    marker=pathlib.Path({str(marker)!r})\n"
            "    while not marker.exists():\n        assert time.monotonic()<deadline\n        time.sleep(0.01)\n"
        )
        record = native.path / "record.json"
        handles = []

        def progress(rows):
            handles.append(native.open_process(int(read_marker(marker))))
            driver.write_record(record, results=rows, expected_files=["test_fast.py", "test_slow.py"])
            raise OSError("record publication failed")

        with pytest.raises(OSError, match="record publication failed"):
            driver.run_files(
                files=["test_fast.py", "test_slow.py"],
                serial=set(),
                repo=native.path,
                python=Path(sys.executable).resolve(),
                output_dir=native.path / "out",
                temp_root=native.path / "temps",
                workers=2,
                timeout_seconds=20,
                deadline=time.monotonic() + 25,
                on_result=progress,
            )
        assert len(handles) == 1
        signaled(native, handles[0])
        partial = json.loads(record.read_text())
        assert partial["status"] == "running" and partial["completed_count"] == 1
        driver.write_record(record, results=[], status="error", driver_error="record publication failed")
        final = record.read_bytes()
        # Actual writers have exited before finalization, including their jobs.
        log = native.path / "out" / "logs" / "test_slow.py.log"
        log.rename(log.with_suffix(".finished"))
        assert record.read_bytes() == final

    def test_atomic_record_replacement_and_prior_json(self, native, monkeypatch):
        record = native.path / "record.json"
        for _ in range(20):
            driver.write_record(record, results=[], expected_files=["test_probe.py"])
            assert json.loads(record.read_text())["status"] == "running"
        previous = record.read_bytes()

        def denied(*args):
            raise PermissionError("busy record")

        monkeypatch.setattr(driver.os, "replace", denied)
        with pytest.raises(PermissionError, match="busy record"):
            driver.write_record(record, results=[], status="complete")
        assert record.read_bytes() == previous


def test_actual_breakaway_program_compiles_nested_windows_paths():
    class CapturedProgram(Exception):
        pass

    captured = []

    def capture(code):
        captured.append(code)
        raise CapturedProgram

    native = SimpleNamespace(path=PureWindowsPath(r"C:\Users\fake-user\probe"), start=capture)
    with pytest.raises(CapturedProgram):
        TestNativeContainment().test_breakaway_is_denied(native)

    def compile_program(source):
        compile(source, "<generated-native-probe>", "exec")
        tree = ast.parse(source)
        nested = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "exec":
                nested.append(ast.literal_eval(node.args[0]))
            if isinstance(node, ast.List):
                for left, right in zip(node.elts, node.elts[1:], strict=False):
                    if isinstance(left, ast.Constant) and left.value == "-c":
                        nested.append(ast.literal_eval(right))
        return 1 + sum(compile_program(program) for program in nested)

    assert compile_program(captured[0]) == 3


@pytest.mark.parametrize(
    ("failure", "cleanup_failures", "expected_operation"),
    [
        *[
            (
                operation,
                (),
                {
                    "CreateFileW": "CreateFileW(NUL)",
                    f"Update:{job.JOB_LIST}": "UpdateProcThreadAttribute",
                    f"Update:{job.HANDLE_LIST}": "UpdateProcThreadAttribute",
                }.get(operation, operation),
            )
            for operation in (
                "CreateJobObjectW",
                "SetHandleInformation",
                "SetInformationJobObject",
                "DuplicateHandle",
                "CreateFileW",
                "InitializeProcThreadAttributeList",
                f"Update:{job.JOB_LIST}",
                f"Update:{job.HANDLE_LIST}",
                "CreateProcessW",
            )
        ],
        (KeyboardInterrupt, (), "CreateProcessW"),
        (SystemExit, (), "CreateProcessW"),
        (None, ("temporary",), "CloseHandle(launch temporary)"),
        (None, ("temporary", "process"), "CloseHandle(process)"),
        ("CreateProcessW", ("temporary",), "CloseHandle(launch temporary)"),
        ("CreateProcessW", ("terminate",), "TerminateJobObject"),
        ("CreateProcessW", ("job",), "CloseHandle(job)"),
    ],
)
def test_failed_launch_releases_closed_log_without_gc(tmp_path, failure, cleanup_failures, expected_operation):
    import gc
    import traceback
    import weakref

    class RetentionAPI(FakeAPI):
        def __init__(self):
            super().__init__(failure if isinstance(failure, str) else None)
            self.error_ids = {}
            self.job_handle = None
            self.process_handle = None
            self.allow_cleanup = False

        def error(self, operation, kind=job.JobError):
            error = super().error(operation, kind)
            self.error_ids[operation] = id(error)
            return error

        def CreateJobObjectW(self, *args):
            self.job_handle = super().CreateJobObjectW(*args)
            return self.job_handle

        def CreateProcessW(self, *args):
            if failure in (KeyboardInterrupt, SystemExit):
                raise self.error("CreateProcessW", failure)
            result = super().CreateProcessW(*args)
            self.process_handle = dereference(args[-1], job.PROCESS_INFORMATION).hProcess
            return result

        def CloseHandle(self, handle):
            kind = "job" if handle == self.job_handle else "process" if handle == self.process_handle else "temporary"
            if kind in cleanup_failures and not self.allow_cleanup:
                self.events.append(("CloseHandle", handle))
                return False
            return super().CloseHandle(handle)

        def TerminateJobObject(self, *args):
            return super().TerminateJobObject(*args) and "terminate" not in cleanup_failures

    api = RetentionAPI()
    tracker = job.ProcessTracker(api)
    expected_type = job.CleanupError if cleanup_failures else failure if isinstance(failure, type) else job.LaunchError

    def fail_and_drop_caught_context():
        with (tmp_path / "log").open("ab") as log:
            log_ref = weakref.ref(log)
            with pytest.raises(expected_type) as caught:
                job.launch_process(
                    python=Path(sys.executable).absolute(), args=[], cwd=tmp_path, env={}, log=log, tracker=tracker
                )
        assert log.closed
        assert caught.type is expected_type
        # The original object and traceback survive cleanup and propagation.
        assert id(caught.value) == api.error_ids[expected_operation]
        if isinstance(caught.value, job.JobError):
            assert caught.value.winerror == 5
        frames = traceback.extract_tb(caught.value.__traceback__)
        assert any(frame.name == "launch_process" for frame in frames)
        if not cleanup_failures:
            assert sum(frame.name == "launch_process" for frame in frames) == 2
        if failure in (KeyboardInterrupt, SystemExit):
            assert frames[-1].name == "CreateProcessW"
        del caught
        return log_ref

    was_enabled = gc.isenabled()
    gc.disable()
    try:
        log_ref = fail_and_drop_caught_context()
        assert log_ref() is None, "failed launch retained its closed buffered log after the caught context was dropped"
        assert bool(api.live) == bool(set(cleanup_failures) - {"terminate"})
        assert bool(tracker._entries) == bool(api.live)
        closed = [event[1] for event in api.events if event[0] == "CloseHandle"]
        assert len(closed) == len(set(closed))
        api.allow_cleanup = True
        tracker.kill_all()
        assert not api.live and not tracker._entries
    finally:
        if was_enabled:
            gc.enable()


@pytest.mark.parametrize("owner", ["job", "process"])
def test_failed_close_keeps_ownership_until_successful_retry(tmp_path, owner):
    api = FakeAPI()
    tracker = job.ProcessTracker(api)
    process = launch(tmp_path, tracker)
    handle = process.token.job if owner == "job" else process.handle
    api.failure = "CloseHandle"
    with pytest.raises(job.CleanupError, match="CloseHandle"):
        if owner == "job":
            process.finish(deadline=time.monotonic() + 1)
        else:
            process.close()
    assert handle in api.live
    if owner == "job":
        assert process.token.job == handle and process.token in tracker._entries
    else:
        assert process.handle == handle
    api.failure = None
    if owner == "job":
        tracker.kill_all()
    else:
        process.close()
        process.finish(deadline=time.monotonic() + 1)
    process.close()
    assert not api.live and not tracker._entries
