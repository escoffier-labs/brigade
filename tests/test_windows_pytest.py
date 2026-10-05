import json
import subprocess
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import windows_job, windows_pytest


class FakeProcess:
    token = SimpleNamespace(leaked_descendants=0)
    cause = None

    def terminate(self, cause):
        if self.cause is None:
            self.cause = cause

    def finish(self, **kwargs):
        # Natural finalization can terminate leftover descendants with this cause.
        self.terminate(windows_job.DRIVER_ABORT)

    def close(self):
        pass


def test_natural_failure_racing_shutdown_keeps_observed_failure(tmp_path, monkeypatch):
    """Cancellation cannot erase a real pytest failure already returned by wait."""
    tracker = windows_pytest.ProcessTracker()

    class Process(FakeProcess):
        pid = 123

        def wait(self, timeout):
            tracker.close()
            return 1

    monkeypatch.setattr(windows_pytest, "launch_process", lambda *args, **kwargs: Process())
    result = windows_pytest.run_file(
        "test_failure.py",
        repo=tmp_path,
        python=Path("python.exe"),
        output_dir=tmp_path / "output",
        temp_root=tmp_path,
        timeout_seconds=900,
        tracker=tracker,
    )
    assert (result.status, result.returncode) == ("failed", 1)


def test_budget_consumed_by_file_setup_prevents_launch(tmp_path, monkeypatch):
    ticks = iter([0.0, 1.0])
    monkeypatch.setattr(windows_pytest, "launch_process", lambda **kwargs: pytest.fail("expired file launched"))
    result = windows_pytest.run_file(
        "test_expired.py",
        repo=tmp_path,
        python=Path("python.exe"),
        output_dir=tmp_path / "out",
        temp_root=tmp_path,
        timeout_seconds=900,
        deadline=0.5,
        clock=lambda: next(ticks),
    )
    assert result.status == "unstarted"


@pytest.mark.parametrize("boundary", ["launch-budget", "queued-worker"])
def test_deadline_only_unstarted_row_uses_aggregate_diagnostic(tmp_path, monkeypatch, boundary):
    tracker = windows_pytest.ProcessTracker()
    monkeypatch.setattr(windows_pytest, "ProcessTracker", lambda: tracker)
    monkeypatch.setattr(windows_pytest, "_child_environment", lambda: {"A": "1"})
    kwargs = {
        "repo": tmp_path,
        "python": Path("python.exe").absolute(),
        "output_dir": tmp_path / "out",
        "temp_root": tmp_path,
        "timeout_seconds": 900,
        "deadline": 5.0,
    }
    name = "test_late.py"
    if boundary == "launch-budget":
        # Cross between run_file's check and the real launcher's budget check.
        ticks = iter([0.0, 0.0, 10.0])
        rows = [windows_pytest.run_file(name, **kwargs, tracker=tracker, clock=lambda: next(ticks))]
    else:
        coordinator = threading.get_ident()
        rows = windows_pytest.run_files(
            files=[name],
            serial=set(),
            workers=1,
            **kwargs,
            clock=lambda: 0.0 if threading.get_ident() == coordinator else 10.0,
        )
    assert not tracker.is_closing()
    assert tracker.closing_cause == windows_job.DRIVER_ABORT
    assert len(rows) == 1
    assert rows[0].status == "unstarted"
    assert rows[0].returncode is None
    assert rows[0].diagnostic == "driver aggregate deadline exceeded"
    assert windows_pytest.regressions(rows, set()) == []
    assert windows_pytest.infrastructure_results(rows) == rows
    record = tmp_path / "record.json"
    windows_pytest.write_record(record, results=rows, expected_files=[name], status="incomplete")
    payload = json.loads(record.read_text())
    assert payload["expected_count"] == payload["accounted_count"] == 1
    assert payload["completed_count"] == 0
    assert payload["status"] == "incomplete"


@pytest.mark.parametrize("returncode", [7, *windows_job.REASONS])
def test_cleanup_failure_retains_observed_product_failure_and_infrastructure_error(tmp_path, monkeypatch, returncode):
    natural = []

    class Process(FakeProcess):
        def wait(self, timeout):
            return returncode

        def finish(self, **kwargs):
            natural.append(kwargs["natural"])
            super().finish(**kwargs)
            raise windows_job.CleanupError("QueryInformationJobObject", 5)

    monkeypatch.setattr(windows_pytest, "launch_process", lambda **kwargs: Process())
    result = windows_pytest.run_file(
        "test_failed.py",
        repo=tmp_path,
        python=Path("python.exe"),
        output_dir=tmp_path / "out",
        temp_root=tmp_path,
        timeout_seconds=900,
    )
    assert (result.status, result.returncode) == ("failed", returncode)
    assert natural == [True]
    assert result.cleanup_error is not None
    assert windows_pytest.regressions([result], set()) == [result]
    assert windows_pytest.infrastructure_results([result]) == [result]


@pytest.mark.parametrize("returncode", list(windows_job.REASONS))
@pytest.mark.parametrize("cause_state", ["none", "different", "matching"])
def test_reserved_root_result_uses_cause_observed_before_finish(tmp_path, monkeypatch, returncode, cause_state):
    natural = []

    class Process(FakeProcess):
        def wait(self, timeout):
            return returncode

        def finish(self, **kwargs):
            natural.append(kwargs["natural"])
            super().finish(**kwargs)

    process = Process()
    if cause_state == "matching":
        process.cause = returncode
    elif cause_state == "different":
        process.cause = next(code for code in windows_job.REASONS if code != returncode)
    monkeypatch.setattr(windows_pytest, "launch_process", lambda **kwargs: process)
    result = windows_pytest.run_file(
        "test_reserved.py",
        repo=tmp_path,
        python=Path("python.exe"),
        output_dir=tmp_path / "out",
        temp_root=tmp_path,
        timeout_seconds=900,
    )
    caused = cause_state == "matching"
    assert (result.status, result.returncode) == (
        windows_job.REASONS[returncode] if caused else "failed",
        returncode,
    )
    assert natural == [not caused]
    assert result.diagnostic == (f"job termination: {result.status}" if caused else None)
    if cause_state == "none":
        assert process.cause == windows_job.DRIVER_ABORT


@pytest.mark.parametrize("root_outcome", ["return", "timeout", "error", "terminate-error"])
@pytest.mark.parametrize("aggregate_cleanup_deadline", [None, 10.0])
def test_run_file_shares_cleanup_deadline_from_before_termination(
    tmp_path, monkeypatch, root_outcome, aggregate_cleanup_deadline
):
    execution_clock = [0.0]
    cleanup_clock = [0.0]
    waits, finishes, closed = [], [], []
    tracker = windows_pytest.ProcessTracker()
    if aggregate_cleanup_deadline is not None:
        tracker.kill_all(deadline=aggregate_cleanup_deadline)
    monkeypatch.setattr(windows_pytest.time, "monotonic", lambda: cleanup_clock[0])

    class Process(FakeProcess):
        def terminate(self, cause):
            super().terminate(cause)
            cleanup_clock[0] = 2.0
            if root_outcome == "terminate-error":
                raise windows_job.CleanupError("TerminateJobObject", 5)

        def wait(self, timeout):
            if self.cause is None:
                execution_clock[0] = 1.0
                raise subprocess.TimeoutExpired("pytest", timeout)
            waits.append(timeout)
            cleanup_clock[0] = 14.0
            if root_outcome == "timeout":
                raise subprocess.TimeoutExpired("pytest", timeout)
            if root_outcome == "error":
                raise windows_job.CleanupError("WaitForSingleObject", 5)
            return self.cause

        def finish(self, **kwargs):
            finishes.append(kwargs)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(windows_pytest, "launch_process", lambda **kwargs: Process())
    result = windows_pytest.run_file(
        "test_cleanup_budget.py",
        repo=tmp_path,
        python=Path("python.exe"),
        output_dir=tmp_path / "out",
        temp_root=tmp_path,
        timeout_seconds=1,
        tracker=tracker,
        clock=lambda: execution_clock[0],
    )
    expected_deadline = 15.0 if aggregate_cleanup_deadline is None else 10.0
    assert finishes == [{"deadline": expected_deadline, "natural": False}]
    assert waits == ([] if root_outcome == "terminate-error" else [expected_deadline - 2.0])
    assert closed == [True]
    assert result.status == (
        ("timeout" if aggregate_cleanup_deadline is None else "deadline-exceeded")
        if root_outcome == "return"
        else "cleanup-unconfirmed"
    )


def _result(name: str, status: str) -> windows_pytest.FileResult:
    return windows_pytest.FileResult(name=name, status=status, returncode=1, seconds=1.0, log=f"logs/{name}.log")


def test_removed_allowlist_entry_makes_failure_a_regression():
    allowlist = {"test_known_failure.py"}
    results = [_result("test_known_failure.py", "failed")]

    assert windows_pytest.regressions(results, allowlist) == []
    assert windows_pytest.regressions(results, set()) == results


def test_allowlist_does_not_forgive_infrastructure_errors():
    result = _result("test_known_failure.py", "launch-failure")

    assert windows_pytest.regressions([result], {result.name}) == []
    assert windows_pytest.infrastructure_results([result]) == [result]


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
    monkeypatch.setattr(windows_pytest, "console_handler", lambda: calls.append("installed"))
    windows_pytest.install_console_handler()
    assert calls == ["installed"]


def test_console_handler_failure_stops_the_driver(monkeypatch):
    monkeypatch.setattr(
        windows_pytest,
        "console_handler",
        lambda: (_ for _ in ()).throw(windows_job.LaunchError("SetConsoleCtrlHandler", 5)),
    )
    with pytest.raises(windows_job.LaunchError, match="SetConsoleCtrlHandler"):
        windows_pytest.install_console_handler()


def test_file_run_uses_isolated_windows_flags_environment_and_external_basetemp(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    output = tmp_path / "output"
    temp_root = tmp_path / "outside"
    temp_root.mkdir()
    captured = {}

    class Process(FakeProcess):
        pid = 123

        def wait(self, timeout):
            captured["wait_timeout"] = timeout
            return 0

    def fake_popen(**kwargs):
        captured["argv"] = kwargs["args"]
        captured["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(windows_pytest, "launch_process", fake_popen)
    result = windows_pytest.run_file(
        "test_example.py",
        repo=repo,
        python=Path("python.exe"),
        output_dir=output,
        temp_root=temp_root,
        timeout_seconds=900,
    )

    assert result.status == "passed"
    assert captured["kwargs"]["python"].is_absolute()
    assert captured["kwargs"]["cwd"] == repo.resolve()
    assert isinstance(captured["kwargs"]["tracker"], windows_job.ProcessTracker)
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
    class Process(FakeProcess):
        pid = 123

        def wait(self, timeout):
            return returncode

    monkeypatch.setattr(windows_pytest, "launch_process", lambda *args, **kwargs: Process())
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


def test_timeout_terminates_job_and_waits_for_cleanup(tmp_path, monkeypatch):
    calls = []
    clock = [0.0]

    class Process(FakeProcess):
        def wait(self, timeout):
            calls.append(("wait", timeout))
            if len(calls) == 1:
                clock[0] = 900
                raise subprocess.TimeoutExpired("pytest", timeout)
            return windows_job.FILE_TIMEOUT

        def terminate(self, cause):
            super().terminate(cause)
            calls.append(("terminate", cause))

        def finish(self, **kwargs):
            calls.append(("finish", kwargs["natural"]))

    monkeypatch.setattr(windows_pytest, "launch_process", lambda **kwargs: Process())
    result = windows_pytest.run_file(
        "test_timeout.py",
        repo=tmp_path,
        python=Path("python.exe"),
        output_dir=tmp_path / "output",
        temp_root=tmp_path,
        timeout_seconds=900,
        clock=lambda: clock[0],
    )
    assert result.status == "timeout"
    assert calls[0] == ("wait", 1)
    assert calls[1] == ("terminate", windows_job.FILE_TIMEOUT)
    assert 0 <= calls[2][1] <= windows_pytest.CLEANUP_TIMEOUT_SECONDS
    assert calls[3] == ("finish", False)


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

    assert [result.status for result in results] == ["unstarted", "unstarted"]


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

    def release_after_cleanup(self, **kwargs):
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
    assert [result.status for result in results] == ["passed", "unstarted", "passed"]
    assert [result.name for result in snapshots[0]] == ["test_one.py"]


def test_run_files_preserves_natural_failure_at_deadline_after_cleanup(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    clock = [0.0]

    def fake_run_file(name, **kwargs):
        started.set()
        assert release.wait(1)
        return _result(name, "failed")

    original_kill_all = windows_pytest.ProcessTracker.kill_all

    def release_after_cleanup(self, **kwargs):
        original_kill_all(self, **kwargs)
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
    assert [(result.name, result.status) for result in results] == [("test_allowlisted.py", "failed")]
    assert windows_pytest.regressions(results, {"test_allowlisted.py"}) == []
    assert windows_pytest.regressions(results, set()) == results


def test_process_tracker_prevents_a_launch_after_closing(tmp_path):
    tracker = windows_pytest.ProcessTracker()
    tracker.kill_all()
    with (tmp_path / "log").open("wb") as log, pytest.raises(windows_job.LaunchClosed):
        windows_job.launch_process(
            python=Path(__import__("sys").executable), args=[], cwd=tmp_path, env={}, log=log, tracker=tracker
        )


def test_cancelled_worker_future_records_unstarted_coverage(tmp_path):
    future = Future()
    assert future.cancel()

    result = windows_pytest.result_from_future(future, "test_cancelled.py", tmp_path / "output")

    assert result.name == "test_cancelled.py"
    assert result.status == "unstarted"


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

    def release_after_cleanup(self, **kwargs):
        cleanup_called.set()
        original_kill_all(self, **kwargs)
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


def _main_to_completion(tmp_path, monkeypatch, *, allowlisted, results):
    """Drive main() past every error path so the normal exit-status derivation runs."""
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(json.dumps({"known_failures": allowlisted, "known_timeouts": []}), encoding="utf-8")
    record = tmp_path / "record.json"
    temp_root = tmp_path / "outside"
    temp_root.mkdir(exist_ok=True)
    monkeypatch.setattr(windows_pytest, "is_windows", lambda: True)
    monkeypatch.setattr(windows_pytest, "install_console_handler", lambda: None)
    monkeypatch.setattr(windows_pytest, "discover_files", lambda repo: [result.name for result in results])
    monkeypatch.setattr(windows_pytest, "make_temp_root", lambda repo: temp_root)
    monkeypatch.setattr(windows_pytest, "run_files", lambda **kwargs: results)
    code = windows_pytest.main(["--repo", str(tmp_path), "--allowlist", str(allowlist), "--record", str(record)])
    return code, json.loads(record.read_text(encoding="utf-8"))


def test_main_exits_nonzero_when_a_measured_failure_is_not_allowlisted(tmp_path, monkeypatch, capsys):
    code, payload = _main_to_completion(
        tmp_path,
        monkeypatch,
        allowlisted=[],
        results=[_result("test_regressed.py", "failed")],
    )

    assert code == 1
    assert "driver_error" not in payload
    assert "windows pytest: files=1 regressions=1 removal_candidates=0" in capsys.readouterr().out


def test_main_exits_zero_when_every_measured_failure_is_allowlisted(tmp_path, monkeypatch, capsys):
    code, payload = _main_to_completion(
        tmp_path,
        monkeypatch,
        allowlisted=["test_known_failure.py"],
        results=[_result("test_known_failure.py", "failed"), _result("test_healthy.py", "passed")],
    )

    assert code == 0
    assert "driver_error" not in payload
    assert {row["name"] for row in payload["results"]} == {"test_known_failure.py", "test_healthy.py"}
    assert "windows pytest: files=2 regressions=0 removal_candidates=0" in capsys.readouterr().out


def test_main_reports_removal_candidates_but_still_exits_zero(tmp_path, monkeypatch, capsys):
    """An allowlisted file that starts passing is good news: report it, never fail the lane.

    check_allowlist_ratchet already permits shrinking the allowlist, so a newly
    passing file must not be able to turn the ratchet red before a human prunes it.
    """
    code, _ = _main_to_completion(
        tmp_path,
        monkeypatch,
        allowlisted=["test_now_passing.py"],
        results=[_result("test_now_passing.py", "passed")],
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "windows pytest: files=1 regressions=0 removal_candidates=1" in out
    assert "allowlist removal candidates: test_now_passing.py" in out


def test_main_removal_candidate_never_masks_a_concurrent_regression(tmp_path, monkeypatch, capsys):
    code, _ = _main_to_completion(
        tmp_path,
        monkeypatch,
        allowlisted=["test_now_passing.py"],
        results=[_result("test_now_passing.py", "passed"), _result("test_regressed.py", "failed")],
    )

    assert code == 1
    assert "windows pytest: files=2 regressions=1 removal_candidates=1" in capsys.readouterr().out


def test_cancellation_poll_terminates_with_aggregate_cause_and_waits(monkeypatch):
    calls = []

    class Process(FakeProcess):
        def terminate(self, cause):
            calls.append(("terminate", cause))

        def wait(self, timeout):
            calls.append(("wait", timeout))
            return windows_job.AGGREGATE_DEADLINE

    tracker = windows_pytest.ProcessTracker()
    tracker.kill_all()
    code = windows_pytest._wait_for_process(
        Process(), timeout_seconds=900, tracker=tracker, cleanup=windows_pytest._CleanupDeadline()
    )
    assert code == windows_job.AGGREGATE_DEADLINE
    assert calls[0] == ("terminate", windows_job.AGGREGATE_DEADLINE)
    assert 0 <= calls[1][1] <= windows_pytest.CLEANUP_TIMEOUT_SECONDS


@pytest.mark.parametrize("cause", [windows_job.AGGREGATE_DEADLINE, windows_job.DRIVER_ABORT])
def test_run_file_cannot_launch_after_cleanup_started(tmp_path, cause):
    tracker = windows_pytest.ProcessTracker()
    tracker.kill_all(cause=cause)
    result = windows_pytest.run_file(
        "test_late.py",
        repo=tmp_path,
        python=Path("python.exe"),
        output_dir=tmp_path / "output",
        temp_root=tmp_path,
        timeout_seconds=900,
        tracker=tracker,
    )
    assert result.status == "unstarted"
    assert result.diagnostic == (
        "driver aggregate deadline exceeded" if cause == windows_job.AGGREGATE_DEADLINE else "driver abort"
    )
    assert (tmp_path / "output" / result.log).read_bytes() == b""


@pytest.mark.parametrize("status", ["deadline-exceeded", "unstarted", "console-interrupt", "launch-failure"])
def test_main_incomplete_is_nonzero_and_separate_from_regressions(tmp_path, monkeypatch, capsys, status):
    code, payload = _main_to_completion(
        tmp_path, monkeypatch, allowlisted=["test_known.py"], results=[_result("test_known.py", status)]
    )
    assert code != 0
    assert payload["status"] == "incomplete"
    assert payload["expected_count"] == 1
    assert payload["completed_count"] == 0
    assert payload["accounted_count"] == 1
    assert "regressions=0" in capsys.readouterr().out


def test_record_accounts_observations_and_missing_files(tmp_path):
    record = tmp_path / "record.json"
    windows_pytest.write_record(
        record,
        results=[_result("test_failed.py", "failed"), _result("test_late.py", "unstarted")],
        expected_files=["test_failed.py", "test_late.py", "test_missing.py"],
        status="incomplete",
    )
    payload = json.loads(record.read_text())
    assert payload["schema"] == "brigade.windows_pytest.v2"
    assert payload["expected_count"] == 3
    assert payload["completed_count"] == 1
    assert payload["accounted_count"] == 2
    assert payload["expected_files"] == ["test_failed.py", "test_late.py", "test_missing.py"]


@pytest.mark.parametrize("recover", [True, False])
def test_atomic_record_replace_retries_and_preserves_previous_json(tmp_path, monkeypatch, recover):
    record = tmp_path / "record.json"
    record.write_text('{"previous": true}\n')
    real_replace = windows_pytest.os.replace
    attempts = []

    def replace(source, target):
        attempts.append(source)
        assert Path(source).parent == record.parent
        assert json.loads(Path(source).read_text())["status"] == "running"
        if not recover or len(attempts) == 1:
            raise PermissionError("busy")
        real_replace(source, target)

    monkeypatch.setattr(windows_pytest.os, "replace", replace)
    monkeypatch.setattr(windows_pytest.time, "sleep", lambda seconds: None)
    if recover:
        windows_pytest.write_record(record, results=[])
        assert json.loads(record.read_text())["status"] == "running"
    else:
        with pytest.raises(PermissionError, match="busy"):
            windows_pytest.write_record(record, results=[])
        assert json.loads(record.read_text()) == {"previous": True}
    assert 2 <= len(attempts) <= 5
    assert list(tmp_path.iterdir()) == [record]


@pytest.mark.parametrize("kind", ["deadline", "launch"])
def test_driver_diagnostics_never_truncate_worker_log(tmp_path, kind):
    log = tmp_path / "logs" / "test_sentinel.py.log"
    log.parent.mkdir()
    log.write_bytes(b"pytest sentinel\n")
    if kind == "deadline":
        result = windows_pytest._deadline_failure("test_sentinel.py", tmp_path)
    else:
        result = windows_pytest._launch_failure("test_sentinel.py", tmp_path, OSError("broken"))
    assert result.diagnostic
    assert log.read_bytes() == b"pytest sentinel\n"


def test_worker_oserror_after_output_preserves_sentinel(tmp_path, monkeypatch):
    class Process(FakeProcess):
        pid = 123

        def wait(self, timeout):
            raise OSError("wait failed")

    def popen(*args, **kwargs):
        kwargs["log"].write(b"pytest sentinel\n")
        kwargs["log"].flush()
        return Process()

    monkeypatch.setattr(windows_pytest, "launch_process", popen)
    result = windows_pytest.run_file(
        "test_sentinel.py",
        repo=tmp_path,
        python=Path("python.exe"),
        output_dir=tmp_path / "output",
        temp_root=tmp_path,
        timeout_seconds=900,
    )
    assert result.status == "launch-failure"
    assert (tmp_path / "output" / result.log).read_bytes() == b"pytest sentinel\n"


def test_serial_phase_is_first_and_never_overlaps_parallel_workers(tmp_path, monkeypatch):
    events = []
    lock = threading.Lock()
    active = set()
    serial = {"nested/test_serial.py", "test_serial_two.py"}

    def run(name, **kwargs):
        with lock:
            if name in serial:
                assert not active
            else:
                assert not active.intersection(serial)
            active.add(name)
            events.append(name)
        time.sleep(0.01)
        with lock:
            active.remove(name)
        return _result(name, "passed")

    monkeypatch.setattr(windows_pytest, "run_file", run)
    results = windows_pytest.run_files(
        files=["test_parallel.py", "nested/test_serial.py", "test_serial_two.py", "test_parallel_two.py"],
        serial=serial,
        repo=tmp_path,
        python=Path("python.exe"),
        output_dir=tmp_path / "output",
        temp_root=tmp_path,
        workers=2,
        timeout_seconds=900,
        deadline=time.monotonic() + 5,
    )
    assert events[:2] == ["nested/test_serial.py", "test_serial_two.py"]
    assert len(results) == 4


@pytest.mark.parametrize("selectors", [["test_unknown.py"], ["test_one.py", "test_one.py"], ["../test_one.py"]])
def test_invalid_serial_selectors_fail_before_launch(tmp_path, monkeypatch, selectors):
    monkeypatch.setattr(windows_pytest, "is_windows", lambda: True)
    monkeypatch.setattr(windows_pytest, "install_console_handler", lambda: None)
    monkeypatch.setattr(windows_pytest, "discover_files", lambda repo: ["test_one.py"])
    monkeypatch.setattr(windows_pytest, "run_files", lambda **kwargs: pytest.fail("must not launch"))
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text('{"known_failures": [], "known_timeouts": []}')
    argv = ["--repo", str(tmp_path), "--allowlist", str(allowlist), "--record", str(tmp_path / "record.json")]
    for selector in selectors:
        argv += ["--serial", selector]
    assert windows_pytest.main(argv) == 2


def _budget_main(tmp_path, monkeypatch, *, extra=(), setup_seconds=0):
    now = [1000.0]
    monkeypatch.setattr(windows_pytest.time, "time", lambda: now[0])
    monkeypatch.setattr(windows_pytest.time, "monotonic", lambda: now[0] - 900)
    monkeypatch.setattr(windows_pytest, "is_windows", lambda: True)
    monkeypatch.setattr(windows_pytest, "install_console_handler", lambda: None)
    monkeypatch.setattr(windows_pytest, "discover_files", lambda repo: ["test_one.py"])
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text('{"known_failures": [], "known_timeouts": []}')
    root = tmp_path / "outside"
    captured = {}

    def setup(repo):
        now[0] += setup_seconds
        root.mkdir()
        return root

    def run(**kwargs):
        captured.update(kwargs)
        initial = json.loads((tmp_path / "record.json").read_text())
        assert initial["status"] == "running"
        assert initial["expected_count"] == 1
        assert initial["completed_count"] == initial["accounted_count"] == 0
        return [_result("test_one.py", "passed")]

    monkeypatch.setattr(windows_pytest, "make_temp_root", setup)
    monkeypatch.setattr(windows_pytest, "run_files", run)
    argv = ["--repo", str(tmp_path), "--allowlist", str(allowlist), "--record", str(tmp_path / "record.json")]
    code = windows_pytest.main(argv + list(extra))
    return code, json.loads((tmp_path / "record.json").read_text()), captured


@pytest.mark.parametrize("setup_seconds", [0, 50, 150])
def test_budget_deadline_accounts_elapsed_job_and_driver_setup(tmp_path, monkeypatch, setup_seconds):
    code, payload, captured = _budget_main(
        tmp_path,
        monkeypatch,
        setup_seconds=setup_seconds,
        extra=["--job-started-at", "900", "--job-limit", "500", "--startup-reserve", "20", "--finalize-reserve", "100"],
    )
    assert code == 0
    assert captured["deadline"] == 380.0
    assert captured["deadline"] - windows_pytest.time.monotonic() == 280 - setup_seconds
    assert payload["status"] == "complete"


@pytest.mark.parametrize("started", ["0", "900"])
def test_exhausted_budget_records_unstarted_without_setup_or_launch(tmp_path, monkeypatch, started):
    code, payload, captured = _budget_main(
        tmp_path,
        monkeypatch,
        extra=["--job-started-at", started, "--job-limit", "100", "--startup-reserve", "0", "--finalize-reserve", "0"],
    )
    assert code != 0
    assert not captured
    assert not (tmp_path / "outside").exists()
    assert payload["status"] == "incomplete"
    assert payload["results"][0]["status"] == "unstarted"


@pytest.mark.parametrize(
    "extra",
    [
        ["--job-started-at", "nan"],
        ["--job-started-at", "inf"],
        ["--job-started-at", "1001"],
        ["--job-started-at", "-1"],
        ["--startup-reserve", "-1"],
        ["--finalize-reserve", "-1"],
        ["--job-limit", "nan"],
    ],
)
def test_invalid_budget_is_error_with_unknown_coverage(tmp_path, monkeypatch, extra):
    code, payload, captured = _budget_main(tmp_path, monkeypatch, extra=extra)
    assert code == 2
    assert not captured
    assert payload["status"] == "error"
    assert payload["expected_count"] is None
    assert payload["completed_count"] == 0


def test_main_marks_keyboard_interrupt_and_retains_unconfirmed_temp(tmp_path, monkeypatch):
    def interrupt(**kwargs):
        kwargs["on_result"]([_result("test_one.py", "passed")])
        raise KeyboardInterrupt()

    monkeypatch.setattr(windows_pytest, "is_windows", lambda: True)
    monkeypatch.setattr(windows_pytest, "install_console_handler", lambda: None)
    monkeypatch.setattr(windows_pytest, "discover_files", lambda repo: ["test_one.py", "test_two.py"])
    root = tmp_path / "outside"
    root.mkdir()
    monkeypatch.setattr(windows_pytest, "make_temp_root", lambda repo: root)
    monkeypatch.setattr(windows_pytest, "run_files", interrupt)
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text('{"known_failures": [], "known_timeouts": []}')
    record = tmp_path / "record.json"
    assert windows_pytest.main(["--repo", str(tmp_path), "--allowlist", str(allowlist), "--record", str(record)]) == 2
    payload = json.loads(record.read_text())
    assert payload["status"] == "interrupted"
    assert payload["completed_count"] == 1
    assert payload["expected_count"] == 2
    assert root.exists()


def test_cleanup_timeout_retains_temp_and_records_partial_evidence(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    cleanup_started = threading.Event()
    real_monotonic = time.monotonic
    real_write = windows_pytest.write_record
    writes = []
    cleanup_failure = windows_job.CleanupError("cleanup unconfirmed")
    root = tmp_path / "outside"
    root.mkdir()
    monkeypatch.setattr(windows_pytest, "is_windows", lambda: True)
    monkeypatch.setattr(windows_pytest, "install_console_handler", lambda: None)
    monkeypatch.setattr(windows_pytest, "discover_files", lambda repo: ["test_one.py"])
    monkeypatch.setattr(windows_pytest, "make_temp_root", lambda repo: root)
    monkeypatch.setattr(windows_pytest, "CLEANUP_TIMEOUT_SECONDS", 0.05)

    def worker(name, **kwargs):
        started.set()
        release.wait(2)
        return _result(name, "passed")

    def cleanup(self, **kwargs):
        cleanup_started.set()
        raise cleanup_failure

    def write(path, **kwargs):
        writes.append((kwargs.get("status", "running"), list(kwargs["results"])))
        real_write(path, **kwargs)

    def expire(active, **kwargs):
        assert started.wait(1)
        return set(), set(active)

    monkeypatch.setattr(windows_pytest, "run_file", worker)
    monkeypatch.setattr(windows_pytest.ProcessTracker, "kill_all", cleanup)
    monkeypatch.setattr(windows_pytest, "wait", expire)
    monkeypatch.setattr(windows_pytest, "write_record", write)
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text('{"known_failures": [], "known_timeouts": []}')
    record = tmp_path / "record.json"
    begin = real_monotonic()
    try:
        assert (
            windows_pytest.main(["--repo", str(tmp_path), "--allowlist", str(allowlist), "--record", str(record)]) == 2
        )
        assert real_monotonic() - begin < 1
        assert cleanup_started.is_set()
        assert root.exists()
        payload = json.loads(record.read_text())
        assert payload["completed_count"] == 0
        assert payload["accounted_count"] == payload["expected_count"] == 1
        assert payload["results"][0]["status"] == "deadline-exceeded"
        assert payload["results"][0]["returncode"] is None
        assert payload["results"][0]["cleanup_error"] == (
            f"worker unfinished after cleanup deadline; driver cleanup failed: {cleanup_failure}"
        )
        assert [status for status, _ in writes if status != "running"] == ["error"]
        assert all(not rows for status, rows in writes if status == "running")
        assert payload["retained_temp_root"] == str(root)
        assert payload["status"] == "error"
        assert "cleanup unconfirmed" in payload["driver_error"]
        before = record.read_bytes()
    finally:
        release.set()
    time.sleep(0.05)
    assert record.read_bytes() == before


def test_serial_budget_exhaustion_leaves_parallel_files_unstarted(tmp_path, monkeypatch):
    clock = [0.0]
    calls = []

    def worker(name, **kwargs):
        calls.append(name)
        clock[0] = 10
        return _result(name, "failed")

    monkeypatch.setattr(windows_pytest, "run_file", worker)
    results = windows_pytest.run_files(
        files=["test_serial.py", "test_parallel.py"],
        serial={"test_serial.py"},
        repo=tmp_path,
        python=Path("python.exe"),
        output_dir=tmp_path,
        temp_root=tmp_path,
        workers=2,
        timeout_seconds=900,
        deadline=1,
        clock=lambda: clock[0],
    )
    assert calls == ["test_serial.py"]
    assert {row.name: row.status for row in results} == {"test_serial.py": "failed", "test_parallel.py": "unstarted"}


def test_driver_cancellation_preserves_natural_failure(tmp_path, monkeypatch):
    tracker = windows_pytest.ProcessTracker()

    class Process(FakeProcess):
        pid = 123

        def wait(self, timeout):
            tracker.close()
            return 1

    monkeypatch.setattr(windows_pytest, "launch_process", lambda *args, **kwargs: Process())
    result = windows_pytest.run_file(
        "test_known.py",
        repo=tmp_path,
        python=Path("python.exe"),
        output_dir=tmp_path / "output",
        temp_root=tmp_path,
        timeout_seconds=900,
        tracker=tracker,
    )
    assert result.status == "failed"
    assert windows_pytest.infrastructure_results([result]) == []
    assert windows_pytest.regressions([result], {result.name}) == []


def test_file_aggregate_deadline_has_incomplete_classification(tmp_path, monkeypatch):
    clock = [0.0]
    waits = []

    class Process(FakeProcess):
        pid = 123

        def wait(self, timeout):
            waits.append(timeout)
            if len(waits) == 1:
                clock[0] += timeout
                raise subprocess.TimeoutExpired("pytest", timeout)
            return windows_job.AGGREGATE_DEADLINE

    monkeypatch.setattr(windows_pytest, "launch_process", lambda *args, **kwargs: Process())
    result = windows_pytest.run_file(
        "test_known.py",
        repo=tmp_path,
        python=Path("python.exe"),
        output_dir=tmp_path / "output",
        temp_root=tmp_path,
        timeout_seconds=900,
        deadline=0.5,
        clock=lambda: clock[0],
    )
    assert waits[0] == 0.5
    assert 0 <= waits[1] <= windows_pytest.CLEANUP_TIMEOUT_SECONDS
    assert result.status == "deadline-exceeded"


def test_unconfirmed_process_cleanup_retains_target(tmp_path, monkeypatch):
    class Process(FakeProcess):
        pid = 123

        def wait(self, timeout):
            raise OSError("wait failed")

    monkeypatch.setattr(windows_pytest, "is_windows", lambda: True)
    monkeypatch.setattr(windows_pytest, "install_console_handler", lambda: None)
    monkeypatch.setattr(windows_pytest, "discover_files", lambda repo: ["test_one.py"])
    monkeypatch.setattr(windows_pytest, "launch_process", lambda *args, **kwargs: Process())
    monkeypatch.setattr(
        Process, "finish", lambda self, **kwargs: (_ for _ in ()).throw(windows_job.CleanupError("cleanup unconfirmed"))
    )
    root = tmp_path / "outside"
    root.mkdir()
    monkeypatch.setattr(windows_pytest, "make_temp_root", lambda repo: root)
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text('{"known_failures": [], "known_timeouts": []}')
    record = tmp_path / "record.json"
    assert windows_pytest.main(["--repo", str(tmp_path), "--allowlist", str(allowlist), "--record", str(record)]) == 2
    payload = json.loads(record.read_text())
    assert root.exists()
    assert payload["retained_temp_root"] == str(root)
    assert payload["completed_count"] == 0
    assert "cleanup unconfirmed" in payload["driver_error"]


def test_main_never_removes_a_target_with_unconfirmed_child_cleanup(tmp_path, monkeypatch):
    code, payload = _main_to_completion(
        tmp_path, monkeypatch, allowlisted=["test_known.py"], results=[_result("test_known.py", "cleanup-unconfirmed")]
    )
    assert code == 2
    assert (tmp_path / "outside").exists()
    assert payload["retained_temp_root"] == str(tmp_path / "outside")
    assert payload["completed_count"] == 0


def test_summary_counts_missing_coverage_after_driver_error(tmp_path, monkeypatch, capsys):
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text('{"known_failures": [], "known_timeouts": []}')
    monkeypatch.setattr(windows_pytest, "is_windows", lambda: True)
    monkeypatch.setattr(windows_pytest, "install_console_handler", lambda: None)
    monkeypatch.setattr(windows_pytest, "discover_files", lambda repo: ["test_one.py", "test_two.py"])
    root = tmp_path / "outside"
    root.mkdir()
    monkeypatch.setattr(windows_pytest, "make_temp_root", lambda repo: root)

    def fail(**kwargs):
        kwargs["on_result"]([_result("test_one.py", "passed")])
        raise RuntimeError("driver boom")

    monkeypatch.setattr(windows_pytest, "run_files", fail)
    assert (
        windows_pytest.main(
            ["--repo", str(tmp_path), "--allowlist", str(allowlist), "--record", str(tmp_path / "record.json")]
        )
        == 2
    )
    assert "infrastructure_incomplete=1" in capsys.readouterr().out


def test_main_deadline_expiry_preserves_measurement_but_cannot_succeed(tmp_path, monkeypatch):
    code, payload, captured = _budget_main(
        tmp_path,
        monkeypatch,
        setup_seconds=300,
        extra=["--job-started-at", "900", "--job-limit", "500", "--startup-reserve", "20", "--finalize-reserve", "100"],
    )
    assert captured["deadline"] == 380.0
    assert payload["results"][0]["status"] == "passed"
    assert payload["completed_count"] == 1
    assert payload["aggregate_deadline_exceeded"] is True
    assert payload["status"] == "incomplete"
    assert code == 1


@pytest.mark.parametrize("abort", ["record", "cleanup", "interrupt"])
def test_main_abort_drains_natural_failure_and_accounts_pending(tmp_path, monkeypatch, abort):
    started = threading.Event()
    release = threading.Event()
    real_wait = windows_pytest.wait
    real_write = windows_pytest.write_record
    root = tmp_path / "outside"
    root.mkdir()
    files = ["test_trigger.py", "test_natural.py", "test_pending.py"]
    launches, progress, causes = [], [], []
    terminal_writes = []
    failure = OSError("original record failure")
    worker_cleanup_error = "worker cleanup failure"
    cleanup_failure = windows_job.CleanupError("secondary cleanup failure")

    def worker(name, **kwargs):
        launches.append(name)
        if name == "test_trigger.py":
            assert started.wait(2)
            return _result(name, "cleanup-unconfirmed" if abort == "cleanup" else "passed")
        started.set()
        assert release.wait(2)
        return windows_pytest.FileResult(name, "failed", 1, 1.0, f"logs/{name}.log", cleanup_error=worker_cleanup_error)

    def wait_for_trigger(active, **kwargs):
        assert started.wait(2)
        trigger = next(future for future, name in active.items() if name == "test_trigger.py")
        real_wait([trigger], timeout=2)
        assert trigger.done()
        if abort == "interrupt":
            raise KeyboardInterrupt("original interrupt")
        return {trigger}, set(active).difference({trigger})

    def cleanup(self, **kwargs):
        causes.append(kwargs["cause"])
        release.set()
        raise cleanup_failure

    def write(path, **kwargs):
        if kwargs.get("status", "running") != "running":
            terminal_writes.append(kwargs["status"])
        if kwargs.get("status", "running") == "running" and kwargs["results"]:
            progress.append(kwargs["results"])
            if abort == "record":
                raise failure
        real_write(path, **kwargs)

    monkeypatch.setattr(windows_pytest, "is_windows", lambda: True)
    monkeypatch.setattr(windows_pytest, "install_console_handler", lambda: None)
    monkeypatch.setattr(windows_pytest, "discover_files", lambda repo: files)
    monkeypatch.setattr(windows_pytest, "make_temp_root", lambda repo: root)
    monkeypatch.setattr(windows_pytest, "run_file", worker)
    monkeypatch.setattr(windows_pytest, "wait", wait_for_trigger)
    monkeypatch.setattr(windows_pytest.ProcessTracker, "kill_all", cleanup)
    monkeypatch.setattr(windows_pytest, "write_record", write)
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text('{"known_failures": [], "known_timeouts": []}')
    record = tmp_path / "record.json"
    try:
        code = windows_pytest.main(
            ["--repo", str(tmp_path), "--allowlist", str(allowlist), "--record", str(record), "--workers", "2"]
        )
    finally:
        release.set()
    payload = json.loads(record.read_text())
    assert code == 2
    assert payload["accounted_count"] == payload["expected_count"] == 3
    rows = {row["name"]: row for row in payload["results"]}
    assert rows["test_natural.py"]["status"] == "failed"
    assert rows["test_natural.py"]["returncode"] == 1
    assert rows["test_pending.py"]["status"] == "unstarted"
    assert "abort" in rows["test_pending.py"]["diagnostic"]
    assert set(launches) == {"test_trigger.py", "test_natural.py"}
    assert causes == [windows_job.DRIVER_ABORT]
    assert payload["completed_count"] == (1 if abort == "cleanup" else 2)
    assert payload["status"] == ("interrupted" if abort == "interrupt" else "error")
    assert (
        payload["driver_error"]
        == {
            "record": "original record failure",
            "cleanup": str(windows_pytest.CleanupUnconfirmed("cleanup unconfirmed; retaining temporary target")),
            "interrupt": "original interrupt",
        }[abort]
    )
    assert len(progress) == (0 if abort == "interrupt" else 1)
    assert terminal_writes == [payload["status"]]
    assert rows["test_natural.py"]["cleanup_error"] == (
        f"{worker_cleanup_error}; driver cleanup failed: {cleanup_failure}"
    )
    assert payload["retained_temp_root"] == str(root)


@pytest.mark.parametrize("queued", [False, True], ids=["unscheduled", "queued-cancelled"])
def test_deadline_batches_hundreds_of_synthetic_rows(tmp_path, monkeypatch, queued):
    files = [f"test_{index}.py" for index in range(485)]
    submitted, launches, snapshots, shutdowns = [], [], [], []
    now = 100.0
    real_run_files = windows_pytest.run_files
    real_write = windows_pytest.write_record
    root = tmp_path / "outside"
    root.mkdir()

    class QueuedExecutor:
        def __init__(self, **kwargs):
            pass

        def submit(self, function, name):
            # Keep real futures pending; no worker or child process may execute.
            future = Future()
            submitted.append(future)
            return future

        def shutdown(self, **kwargs):
            assert all(future.cancelled() for future in submitted)
            assert not snapshots
            shutdowns.append(True)

    def deadline_wait(active, **kwargs):
        nonlocal now
        now = 101.0
        return set(), set(active)

    def run_with_clock(**kwargs):
        nonlocal now
        assert kwargs["deadline"] == 101.0
        if not queued:
            now = 101.0
        return real_run_files(**kwargs, clock=lambda: now)

    def write(path, **kwargs):
        if kwargs.get("status", "running") == "running" and kwargs["results"]:
            assert shutdowns == [True]
            snapshots.append(list(kwargs["results"]))
        real_write(path, **kwargs)

    monkeypatch.setattr(windows_pytest.time, "monotonic", lambda: now)
    monkeypatch.setattr(windows_pytest, "is_windows", lambda: True)
    monkeypatch.setattr(windows_pytest, "install_console_handler", lambda: None)
    monkeypatch.setattr(windows_pytest, "discover_files", lambda repo: files)
    monkeypatch.setattr(windows_pytest, "make_temp_root", lambda repo: root)
    monkeypatch.setattr(windows_pytest, "ThreadPoolExecutor", QueuedExecutor)
    monkeypatch.setattr(windows_pytest, "wait", deadline_wait)
    monkeypatch.setattr(windows_pytest, "run_file", lambda *args, **kwargs: launches.append(args))
    monkeypatch.setattr(windows_pytest, "run_files", run_with_clock)
    monkeypatch.setattr(windows_pytest, "write_record", write)
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text('{"known_failures": [], "known_timeouts": []}')
    record = tmp_path / "record.json"

    code = windows_pytest.main(
        [
            "--repo",
            str(tmp_path),
            "--allowlist",
            str(allowlist),
            "--record",
            str(record),
            "--workers",
            "6",
            "--job-timeout",
            "1",
        ]
    )

    payload = json.loads(record.read_text())
    assert not launches
    assert len(submitted) == (6 if queued else 0)
    assert shutdowns == [True]
    assert len(snapshots) == 1 and len(snapshots[0]) == 485
    assert code == 1
    assert payload["expected_files"] == files
    assert payload["accounted_count"] == payload["expected_count"] == 485
    assert payload["completed_count"] == 0
    assert payload["status"] == "incomplete"
    assert payload["aggregate_deadline_exceeded"] is True
    assert "driver_error" not in payload
    assert "retained_temp_root" not in payload
    assert not root.exists()
    assert {row["name"] for row in payload["results"]} == set(files)
    assert all(row["status"] == "unstarted" for row in payload["results"])
    assert all(row["diagnostic"] == "driver aggregate deadline exceeded" for row in payload["results"])


def test_run_file_preserves_symlinked_interpreter_identity(tmp_path, monkeypatch):
    interpreter = tmp_path / "venv" / "python"
    interpreter.parent.mkdir()
    base = tmp_path / "base-python"
    base.touch()
    try:
        interpreter.symlink_to(base)
    except OSError:
        pytest.skip("symlinks unavailable")
    captured = []

    class Process(FakeProcess):
        def wait(self, timeout):
            return 0

    def launch(**kwargs):
        captured.append(kwargs["python"])
        return Process()

    monkeypatch.setattr(windows_pytest, "launch_process", launch)
    result = windows_pytest.run_file(
        "test_identity.py",
        repo=tmp_path,
        python=interpreter,
        output_dir=tmp_path / "out",
        temp_root=tmp_path,
        timeout_seconds=10,
    )
    assert result.status == "passed"
    assert captured == [interpreter.absolute()]
    assert captured[0] != base


def test_submit_failure_retains_original_error_and_accounts_unsubmitted_file(tmp_path, monkeypatch):
    failure = RuntimeError("executor refused submission")
    shutdown = []

    class FailedExecutor:
        def __init__(self, **kwargs):
            pass

        def submit(self, function, name):
            raise failure

        def shutdown(self, **kwargs):
            shutdown.append(True)

    monkeypatch.setattr(windows_pytest, "ThreadPoolExecutor", FailedExecutor)
    with pytest.raises(RuntimeError) as caught:
        windows_pytest.run_files(
            files=["test_submit.py", "test_pending.py"],
            serial=set(),
            repo=tmp_path,
            python=Path("python.exe"),
            output_dir=tmp_path,
            temp_root=tmp_path,
            workers=1,
            timeout_seconds=10,
            deadline=time.monotonic() + 1,
        )
    assert caught.value is failure
    assert shutdown == [True]
    rows = caught.value.windows_pytest_results
    assert {row.name for row in rows} == {"test_submit.py", "test_pending.py"}
    assert all(row.status == "unstarted" and row.diagnostic == "driver abort" for row in rows)


def test_keyboard_interrupt_during_deadline_cleanup_remains_interrupted(tmp_path, monkeypatch):
    failure = KeyboardInterrupt("cleanup interrupt")
    monkeypatch.setattr(
        windows_pytest.ProcessTracker, "kill_all", lambda self, **kwargs: (_ for _ in ()).throw(failure)
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        windows_pytest.run_files(
            files=["test_pending.py"],
            serial=set(),
            repo=tmp_path,
            python=Path("python.exe"),
            output_dir=tmp_path,
            temp_root=tmp_path,
            workers=1,
            timeout_seconds=10,
            deadline=time.monotonic() - 1,
        )
    assert caught.value is failure
    assert caught.value.windows_pytest_results[0].status == "unstarted"
