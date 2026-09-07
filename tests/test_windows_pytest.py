import json
import subprocess
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import windows_pytest


def _result(name: str, status: str) -> windows_pytest.FileResult:
    return windows_pytest.FileResult(name=name, status=status, returncode=1, seconds=1.0, log=f"logs/{name}.log")


def test_removed_allowlist_entry_makes_failure_a_regression():
    allowlist = {"test_known_failure.py"}
    results = [_result("test_known_failure.py", "failed")]

    assert windows_pytest.regressions(results, allowlist) == []
    assert windows_pytest.regressions(results, set()) == results


def test_allowlist_does_not_forgive_infrastructure_errors():
    result = _result("test_known_failure.py", "launch-failure")

    assert windows_pytest.regressions([result], {result.name}) == [result]


def test_main_records_setup_timeout_and_returns_driver_error(tmp_path, monkeypatch):
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(json.dumps({"known_failures": [], "known_timeouts": []}), encoding="utf-8")
    record = tmp_path / "record.json"
    monkeypatch.setattr(windows_pytest, "is_windows", lambda: True)
    monkeypatch.setattr(windows_pytest, "install_console_handler", lambda: None)
    monkeypatch.setattr(
        windows_pytest,
        "make_temp_root",
        lambda repo: (_ for _ in ()).throw(subprocess.TimeoutExpired("git", 5)),
    )

    assert windows_pytest.main(["--repo", str(tmp_path), "--allowlist", str(allowlist), "--record", str(record)]) == 2
    assert "timed out" in json.loads(record.read_text(encoding="utf-8"))["driver_error"]


def test_main_records_unexpected_exception_and_returns_driver_error(tmp_path, monkeypatch):
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(json.dumps({"known_failures": [], "known_timeouts": []}), encoding="utf-8")
    record = tmp_path / "record.json"
    monkeypatch.setattr(windows_pytest, "is_windows", lambda: True)
    monkeypatch.setattr(windows_pytest, "install_console_handler", lambda: None)
    monkeypatch.setattr(windows_pytest, "discover_files", lambda repo: (_ for _ in ()).throw(TypeError("bad config")))

    assert windows_pytest.main(["--repo", str(tmp_path), "--allowlist", str(allowlist), "--record", str(record)]) == 2
    assert json.loads(record.read_text(encoding="utf-8"))["driver_error"] == "bad config"


def test_main_returns_driver_error_when_record_cannot_be_written(tmp_path, monkeypatch, capsys):
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(json.dumps({"known_failures": [], "known_timeouts": []}), encoding="utf-8")
    monkeypatch.setattr(windows_pytest, "is_windows", lambda: True)
    monkeypatch.setattr(windows_pytest, "install_console_handler", lambda: None)
    monkeypatch.setattr(
        windows_pytest, "write_record", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read-only"))
    )

    assert (
        windows_pytest.main(
            ["--repo", str(tmp_path), "--allowlist", str(allowlist), "--record", str(tmp_path / "record.json")]
        )
        == 2
    )
    assert "unable to write record" in capsys.readouterr().err


def test_discovery_and_allowlist_names_support_nested_tests_without_traversal(tmp_path):
    tests = tmp_path / "tests"
    (tests / "nested").mkdir(parents=True)
    (tests / "test_root.py").touch()
    (tests / "nested" / "test_child.py").touch()

    assert windows_pytest.discover_files(tmp_path) == ["nested/test_child.py", "test_root.py"]
    assert windows_pytest.portable_test_name("nested/test_child.py") == "nested/test_child.py"
    for invalid in ("../test_escape.py", "/test_absolute.py", "C:/test_drive.py"):
        with pytest.raises(ValueError):
            windows_pytest.portable_test_name(invalid)


def test_allowlist_ratchet_rejects_growth_and_allows_deletion():
    baseline = {"test_known.py", "test_removed.py"}

    with pytest.raises(ValueError, match="expanded"):
        windows_pytest.check_allowlist_ratchet({"test_known.py", "test_new.py"}, baseline)
    windows_pytest.check_allowlist_ratchet({"test_known.py"}, baseline)


def test_shipped_allowlist_matches_the_initial_ratchet_cap():
    assert (
        len(windows_pytest.load_allowlist(windows_pytest.DEFAULT_ALLOWLIST)) == windows_pytest.INITIAL_ALLOWLIST_ENTRIES
    )


def test_base_allowlist_read_failure_cannot_reset_an_existing_baseline(tmp_path, monkeypatch):
    allowlist = tmp_path / "tests" / "windows_pytest_allowlist.json"
    allowlist.parent.mkdir()
    allowlist.write_text("{}", encoding="utf-8")
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=128, stdout=""),
            SimpleNamespace(returncode=0, stdout="tests/windows_pytest_allowlist.json\n"),
        ]
    )
    monkeypatch.setattr(windows_pytest.subprocess, "run", lambda *args, **kwargs: next(responses))

    with pytest.raises(RuntimeError, match="unable to read base allowlist"):
        windows_pytest.load_allowlist_from_ref(tmp_path, "base", allowlist)


def test_stale_allowlist_entry_is_rejected():
    with pytest.raises(ValueError, match="not discovered"):
        windows_pytest.validate_allowlist({"test_missing.py"}, ["test_present.py"])


def test_parent_console_handler_must_install_before_children(monkeypatch):
    calls = []
    kernel32 = SimpleNamespace(SetConsoleCtrlHandler=lambda handler, enabled: calls.append((handler, enabled)) or 1)
    monkeypatch.setattr(windows_pytest.ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False)

    windows_pytest.install_console_handler()

    assert calls == [(None, True)]


def test_console_handler_failure_stops_the_driver(monkeypatch):
    kernel32 = SimpleNamespace(SetConsoleCtrlHandler=lambda handler, enabled: 0)
    monkeypatch.setattr(windows_pytest.ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False)

    with pytest.raises(RuntimeError, match="SetConsoleCtrlHandler"):
        windows_pytest.install_console_handler()


def test_file_run_uses_isolated_windows_flags_environment_and_external_basetemp(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    output = tmp_path / "output"
    temp_root = tmp_path / "outside"
    temp_root.mkdir()
    captured = {}

    class Process:
        pid = 123

        def wait(self, timeout):
            captured["wait_timeout"] = timeout
            return 0

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(windows_pytest.subprocess, "Popen", fake_popen)
    result = windows_pytest.run_file(
        "test_example.py",
        repo=repo,
        python=Path("python.exe"),
        output_dir=output,
        temp_root=temp_root,
        timeout_seconds=900,
    )

    assert result.status == "passed"
    assert captured["kwargs"]["creationflags"] == (
        windows_pytest.CREATE_NEW_PROCESS_GROUP | windows_pytest.CREATE_NO_WINDOW
    )
    assert captured["kwargs"]["env"]["BRIGADE_EXTRAS"] == "0"
    assert captured["kwargs"]["env"]["BRIGADE_NO_UPDATE_CHECK"] == "1"
    basetemp = next(argument for argument in captured["argv"] if argument.startswith("--basetemp="))
    assert Path(basetemp.removeprefix("--basetemp=")).is_dir()
    assert not Path(basetemp.removeprefix("--basetemp=")).is_relative_to(repo)
    assert captured["wait_timeout"] == windows_pytest.PROCESS_POLL_SECONDS


def test_file_run_rejects_nonpositive_timeout(tmp_path):
    with pytest.raises(ValueError, match="timeout"):
        windows_pytest.run_file(
            "test_example.py",
            repo=tmp_path,
            python=Path("python.exe"),
            output_dir=tmp_path / "output",
            temp_root=tmp_path,
            timeout_seconds=0,
        )


@pytest.mark.parametrize("returncode", [-1073741510, 3221225786])
def test_console_interrupt_return_codes_are_failures(tmp_path, monkeypatch, returncode):
    class Process:
        pid = 123

        def wait(self, timeout):
            return returncode

    monkeypatch.setattr(windows_pytest.subprocess, "Popen", lambda *args, **kwargs: Process())
    temp_root = tmp_path / "outside"
    temp_root.mkdir()

    result = windows_pytest.run_file(
        "test_interrupt.py",
        repo=tmp_path,
        python=Path("python.exe"),
        output_dir=tmp_path / "output",
        temp_root=temp_root,
        timeout_seconds=900,
    )

    assert result.status == "console-interrupt"


def test_timeout_kills_process_tree_and_waits_for_cleanup(tmp_path, monkeypatch):
    waits = []

    class Process:
        pid = 123

        def wait(self, timeout):
            waits.append(timeout)
            if len(waits) == 1:
                raise windows_pytest.subprocess.TimeoutExpired("pytest", timeout)
            return 1

    taskkill = {}

    def fake_run(argv, **kwargs):
        taskkill["argv"] = argv
        taskkill["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    clock_values = iter([0.0, 0.0, 0.0, 900.0, 900.0])
    monkeypatch.setattr(windows_pytest.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(windows_pytest.subprocess, "run", fake_run)
    temp_root = tmp_path / "outside"
    temp_root.mkdir()

    result = windows_pytest.run_file(
        "test_timeout.py",
        repo=tmp_path,
        python=Path("python.exe"),
        output_dir=tmp_path / "output",
        temp_root=temp_root,
        timeout_seconds=900,
        clock=lambda: next(clock_values),
    )

    assert result.status == "timeout"
    assert taskkill["argv"] == ["taskkill", "/PID", "123", "/T", "/F"]
    assert taskkill["kwargs"]["timeout"] == windows_pytest.CLEANUP_TIMEOUT_SECONDS
    assert waits == [windows_pytest.PROCESS_POLL_SECONDS, windows_pytest.CLEANUP_TIMEOUT_SECONDS]


def test_temp_root_resolves_before_rejecting_an_enclosing_checkout(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    inside = checkout / "temp"
    inside.mkdir()
    link = tmp_path / "temp-link"
    link.symlink_to(inside, target_is_directory=True)
    checked = []
    monkeypatch.setattr(windows_pytest.tempfile, "mkdtemp", lambda prefix: str(link))
    monkeypatch.setattr(windows_pytest, "enclosing_checkout", lambda path: checked.append(path) or checkout)

    with pytest.raises(RuntimeError, match="outside"):
        windows_pytest.make_temp_root(tmp_path / "repo")

    assert checked == [inside.resolve()]
    assert inside.exists()


def test_make_temp_root_cleans_its_created_directory_after_setup_error(tmp_path, monkeypatch):
    created = tmp_path / "created"
    created.mkdir()
    monkeypatch.setattr(windows_pytest.tempfile, "mkdtemp", lambda prefix: str(created))
    monkeypatch.setattr(windows_pytest, "enclosing_checkout", lambda path: tmp_path)

    with pytest.raises(RuntimeError, match="outside"):
        windows_pytest.make_temp_root(tmp_path / "repo")

    assert not created.exists()


def test_main_installs_console_immunity_before_checking_temp_checkout(tmp_path, monkeypatch):
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(json.dumps({"known_failures": [], "known_timeouts": []}), encoding="utf-8")
    events = []
    temp_root = tmp_path / "outside"
    temp_root.mkdir()
    monkeypatch.setattr(windows_pytest, "is_windows", lambda: True)
    monkeypatch.setattr(windows_pytest, "install_console_handler", lambda: events.append("handler"))
    monkeypatch.setattr(windows_pytest.tempfile, "mkdtemp", lambda prefix: str(temp_root))
    monkeypatch.setattr(windows_pytest, "enclosing_checkout", lambda path: events.append("git") or None)
    monkeypatch.setattr(windows_pytest, "discover_files", lambda repo: [])

    code = windows_pytest.main(
        ["--repo", str(tmp_path), "--allowlist", str(allowlist), "--record", str(tmp_path / "record.json")]
    )

    assert code == 2
    assert events == ["handler", "git"]


def test_main_rejects_empty_discovery(tmp_path, monkeypatch):
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(json.dumps({"known_failures": [], "known_timeouts": []}), encoding="utf-8")
    record = tmp_path / "record.json"
    monkeypatch.setattr(windows_pytest, "is_windows", lambda: True)
    monkeypatch.setattr(windows_pytest, "install_console_handler", lambda: None)
    monkeypatch.setattr(windows_pytest, "discover_files", lambda repo: [])
    monkeypatch.setattr(windows_pytest, "make_temp_root", lambda repo: tmp_path / "outside")
    (tmp_path / "outside").mkdir()

    assert windows_pytest.main(["--repo", str(tmp_path), "--allowlist", str(allowlist), "--record", str(record)]) == 2
    assert json.loads(record.read_text(encoding="utf-8"))["driver_error"] == "no test files discovered"


def test_run_files_keeps_completed_results_when_a_later_file_setup_fails(tmp_path, monkeypatch):
    completed = _result("test_completed.py", "passed")
    calls = iter([completed, OSError("setup failed")])

    def fake_run_file(*args, **kwargs):
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(windows_pytest, "run_file", fake_run_file)
    results = windows_pytest.run_files(
        files=["test_completed.py", "test_setup.py"],
        serial=set(),
        repo=tmp_path,
        python=Path("python.exe"),
        output_dir=tmp_path / "output",
        temp_root=tmp_path / "outside",
        workers=1,
        timeout_seconds=900,
        deadline=time.monotonic() + 1,
    )

    assert [result.name for result in results] == ["test_completed.py", "test_setup.py"]
    assert results[1].status == "launch-failure"


def test_run_files_marks_unscheduled_files_at_aggregate_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(windows_pytest, "run_file", lambda name, **kwargs: _result(name, "passed"))

    results = windows_pytest.run_files(
        files=["test_one.py", "test_two.py"],
        serial=set(),
        repo=tmp_path,
        python=Path("python.exe"),
        output_dir=tmp_path / "output",
        temp_root=tmp_path / "outside",
        workers=1,
        timeout_seconds=900,
        deadline=time.monotonic() - 1,
    )

    assert [result.status for result in results] == ["deadline-exceeded", "deadline-exceeded"]


def test_run_files_stops_scheduling_and_records_partial_results_at_deadline(tmp_path, monkeypatch):
    calls = []
    snapshots = []
    second_started = threading.Event()
    allow_second_start = threading.Event()
    release = threading.Event()
    clock = [0.0]
    real_wait = windows_pytest.wait
    real_executor = windows_pytest.ThreadPoolExecutor

    class DelayedSecondStartExecutor:
        def __init__(self, **kwargs):
            self.executor = real_executor(**kwargs)
            self.submissions = 0

        def submit(self, function, *args, **kwargs):
            self.submissions += 1
            if self.submissions == 2:

                def delayed_second_start():
                    assert allow_second_start.wait(1)
                    return function(*args, **kwargs)

                return self.executor.submit(delayed_second_start)
            return self.executor.submit(function, *args, **kwargs)

        def shutdown(self, **kwargs):
            self.executor.shutdown(**kwargs)

    def fake_run_file(name, **kwargs):
        calls.append(name)
        if name == "test_two.py":
            second_started.set()
            assert release.wait(1)
        return _result(name, "passed")

    def fake_wait(active, **kwargs):
        active_name = next(iter(active.values()))
        if active_name == "test_one.py":
            return real_wait(active, **kwargs)
        assert active_name == "test_two.py"
        allow_second_start.set()
        assert second_started.wait(1)
        clock[0] = 1.0
        return set(), set(active)

    def release_after_cleanup(self):
        release.set()

    monkeypatch.setattr(windows_pytest, "run_file", fake_run_file)
    monkeypatch.setattr(windows_pytest, "wait", fake_wait)
    monkeypatch.setattr(windows_pytest, "ThreadPoolExecutor", DelayedSecondStartExecutor)
    monkeypatch.setattr(windows_pytest.ProcessTracker, "kill_all", release_after_cleanup)
    results = windows_pytest.run_files(
        files=["test_one.py", "test_two.py", "test_three.py"],
        serial=set(),
        repo=tmp_path,
        python=Path("python.exe"),
        output_dir=tmp_path / "output",
        temp_root=tmp_path / "outside",
        workers=1,
        timeout_seconds=900,
        deadline=1.0,
        on_result=lambda snapshot: snapshots.append(snapshot),
        clock=lambda: clock[0],
    )

    assert calls == ["test_one.py", "test_two.py"]
    assert [result.status for result in results] == ["passed", "deadline-exceeded", "deadline-exceeded"]
    assert [result.name for result in snapshots[0]] == ["test_one.py"]


def test_run_files_keeps_active_file_as_deadline_failure_after_cleanup(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    clock = [0.0]

    def fake_run_file(name, **kwargs):
        started.set()
        assert release.wait(1)
        return _result(name, "failed")

    original_kill_all = windows_pytest.ProcessTracker.kill_all

    def release_after_cleanup(self):
        original_kill_all(self)
        release.set()

    def fake_wait(active, **kwargs):
        assert started.wait(1)
        clock[0] = 1.0
        return set(), set(active)

    monkeypatch.setattr(windows_pytest, "run_file", fake_run_file)
    monkeypatch.setattr(windows_pytest, "wait", fake_wait)
    monkeypatch.setattr(windows_pytest.ProcessTracker, "kill_all", release_after_cleanup)
    results = windows_pytest.run_files(
        files=["test_allowlisted.py"],
        serial=set(),
        repo=tmp_path,
        python=Path("python.exe"),
        output_dir=tmp_path / "output",
        temp_root=tmp_path / "outside",
        workers=1,
        timeout_seconds=900,
        deadline=1.0,
        clock=lambda: clock[0],
    )

    assert started.is_set()
    assert [(result.name, result.status) for result in results] == [("test_allowlisted.py", "deadline-exceeded")]
    assert windows_pytest.regressions(results, {"test_allowlisted.py"}) == results


def test_process_tracker_terminates_a_child_registered_after_closing(monkeypatch):
    killed = []
    process = SimpleNamespace(pid=123)
    tracker = windows_pytest.ProcessTracker()
    monkeypatch.setattr(windows_pytest, "_kill_process_tree", lambda candidate: killed.append(candidate.pid))

    tracker.kill_all()
    tracker.add(process)

    assert killed == [123]


def test_cancelled_worker_future_records_a_launch_failure(tmp_path):
    future = Future()
    assert future.cancel()

    result = windows_pytest.result_from_future(future, "test_cancelled.py", tmp_path / "output")

    assert result.name == "test_cancelled.py"
    assert result.status == "launch-failure"


def test_run_files_aborts_active_workers_when_progress_recording_fails(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    cleanup_called = threading.Event()
    fast_ready = threading.Event()
    slow_finished = threading.Event()

    def fake_run_file(name, **kwargs):
        if name == "test_fast.py":
            assert started.wait(1)
            fast_ready.set()
            return _result(name, "passed")
        started.set()
        assert release.wait(1)
        slow_finished.set()
        return _result(name, "passed")

    def fake_wait(active, **kwargs):
        assert started.wait(1)
        assert fast_ready.wait(1)
        fast = next(future for future, name in active.items() if name == "test_fast.py")
        return {fast}, set(active).difference({fast})

    original_kill_all = windows_pytest.ProcessTracker.kill_all

    def release_after_cleanup(self):
        cleanup_called.set()
        original_kill_all(self)
        release.set()

    monkeypatch.setattr(windows_pytest, "run_file", fake_run_file)
    monkeypatch.setattr(windows_pytest, "wait", fake_wait)
    monkeypatch.setattr(windows_pytest.ProcessTracker, "kill_all", release_after_cleanup)
    try:
        with pytest.raises(OSError, match="record unavailable"):
            windows_pytest.run_files(
                files=["test_fast.py", "test_slow.py"],
                serial=set(),
                repo=tmp_path,
                python=Path("python.exe"),
                output_dir=tmp_path / "output",
                temp_root=tmp_path / "outside",
                workers=2,
                timeout_seconds=900,
                deadline=time.monotonic() + 1,
                on_result=lambda snapshot: (_ for _ in ()).throw(OSError("record unavailable")),
            )
        assert started.is_set()
        assert cleanup_called.is_set()
        assert slow_finished.wait(1)
    finally:
        release.set()


def test_main_returns_failure_when_driver_errors_and_records_only_measured_results(tmp_path, monkeypatch):
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(json.dumps({"known_failures": [], "known_timeouts": []}), encoding="utf-8")
    record = tmp_path / "record.json"
    monkeypatch.setattr(windows_pytest, "is_windows", lambda: True)
    monkeypatch.setattr(windows_pytest, "install_console_handler", lambda: None)
    monkeypatch.setattr(windows_pytest, "discover_files", lambda repo: ["test_one.py"])
    monkeypatch.setattr(windows_pytest, "make_temp_root", lambda repo: tmp_path / "outside")
    (tmp_path / "outside").mkdir()
    monkeypatch.setattr(
        windows_pytest, "run_files", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("driver boom"))
    )

    code = windows_pytest.main(["--repo", str(tmp_path), "--allowlist", str(allowlist), "--record", str(record)])

    assert code == 2
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert payload["results"] == []
    assert payload["driver_error"] == "driver boom"
