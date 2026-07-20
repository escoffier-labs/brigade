import errno
import locale
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from brigade import proc


def test_process_registry_escalates_every_owned_process_group(monkeypatch):
    class ExitsOnTerminate:
        pid = 101

        def __init__(self):
            self.polls = 0

        def poll(self):
            self.polls += 1
            return None if self.polls == 1 else 0

    class IgnoresTerminate:
        pid = 202

        def poll(self):
            return None

    exited = ExitsOnTerminate()
    running = IgnoresTerminate()
    signals = []
    monkeypatch.setattr(
        proc,
        "_signal_process_group",
        lambda process, sig: signals.append((process.pid, sig)),
    )

    proc._terminate_processes(
        (exited, running),
        terminate_grace=0,
        kill_grace=0,
    )

    assert signals == [
        (101, signal.SIGTERM),
        (202, signal.SIGTERM),
        (101, signal.SIGKILL),
        (202, signal.SIGKILL),
    ]


def test_registered_windows_process_starts_in_new_process_group(monkeypatch):
    captured = {}

    class StubProcess:
        pid = 4242
        returncode = 0

        def communicate(self, *, input, timeout):
            return b"ok\n", b""

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return StubProcess()

    monkeypatch.setattr(proc.os, "name", "nt")
    monkeypatch.setattr(proc.subprocess, "Popen", fake_popen)

    result = proc.run(["worker.exe"], process_registry=proc.ProcessRegistry())

    assert result.code == 0
    assert captured["creationflags"] == getattr(proc.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    assert "start_new_session" not in captured


def test_registered_process_terminates_before_unregister_on_base_exception(monkeypatch):
    events = []

    class StubProcess:
        pid = 4242

        def communicate(self, *, input, timeout):
            raise KeyboardInterrupt

    class StubRegistry:
        def register(self, process):
            events.append(("register", process.pid))

        def terminate(self, process):
            events.append(("terminate", process.pid))

        def unregister(self, process):
            events.append(("unregister", process.pid))

    monkeypatch.setattr(proc.subprocess, "Popen", lambda *args, **kwargs: StubProcess())

    with pytest.raises(KeyboardInterrupt):
        proc.run(["worker"], process_registry=StubRegistry())

    assert events == [("register", 4242), ("terminate", 4242), ("unregister", 4242)]


def test_windows_registry_cancellation_targets_owned_descendant_tree(monkeypatch):
    class StubProcess:
        pid = 4242

        def __init__(self):
            self.running = True
            self.killed = False

        def poll(self):
            return None if self.running else 1

        def kill(self):
            self.killed = True

    process = StubProcess()
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        process.running = False
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(proc.os, "name", "nt")
    monkeypatch.setattr(proc.subprocess, "run", fake_run)

    proc._terminate_processes((process,), terminate_grace=0, kill_grace=0)

    assert calls[0][0] == ["taskkill", "/PID", "4242", "/T", "/F"]
    assert process.killed is False


def test_windows_registry_targets_tree_after_group_leader_exits(monkeypatch):
    class ExitedProcess:
        pid = 4242

        def poll(self):
            return 0

        def kill(self):
            pytest.fail("an exited group leader must not be killed directly")

    calls = []
    monkeypatch.setattr(proc.os, "name", "nt")
    monkeypatch.setattr(
        proc.subprocess,
        "run",
        lambda args, **kwargs: calls.append((args, kwargs)) or subprocess.CompletedProcess(args, 0),
    )

    proc._terminate_processes((ExitedProcess(),), terminate_grace=0, kill_grace=0)

    assert calls[0][0] == ["taskkill", "/PID", "4242", "/T", "/F"]


def test_registered_timeout_never_uses_unbounded_output_drain(monkeypatch):
    communicate_timeouts = []

    class StubProcess:
        pid = 4242
        returncode = 0

        def communicate(self, *, input=None, timeout=None):
            communicate_timeouts.append(timeout)
            raise subprocess.TimeoutExpired(
                ["worker"],
                timeout,
                output=b"partial output",
                stderr=b"partial error",
            )

    class StubRegistry:
        def register(self, process):
            pass

        def terminate(self, process):
            pass

        def unregister(self, process):
            pass

    monkeypatch.setattr(proc.subprocess, "Popen", lambda *args, **kwargs: StubProcess())

    result = proc.run(["worker"], timeout=0.01, process_registry=StubRegistry())

    assert result.code == 124
    assert result.stdout == "partial output"
    assert result.stderr.startswith("partial error")
    assert len(communicate_timeouts) == 2
    assert all(timeout is not None for timeout in communicate_timeouts)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_registered_timeout_kills_descendant_after_group_leader_exits(tmp_path):
    descendant_pid_path = tmp_path / "descendant.pid"
    descendant_code = (
        "import os,time; from pathlib import Path; "
        f"Path({str(descendant_pid_path)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    parent_code = f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {descendant_code!r}])"
    result_holder = {}

    def invoke():
        result_holder["result"] = proc.run(
            [sys.executable, "-c", parent_code],
            timeout=0.2,
            process_registry=proc.ProcessRegistry(terminate_grace=0.05, kill_grace=0.05),
        )

    runner = threading.Thread(target=invoke, daemon=True)
    runner.start()
    deadline = time.monotonic() + 2
    while not descendant_pid_path.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert descendant_pid_path.is_file()
    descendant_pid = int(descendant_pid_path.read_text())
    runner.join(timeout=1)
    returned_without_external_cleanup = not runner.is_alive()
    if runner.is_alive():
        try:
            os.kill(descendant_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        runner.join(timeout=1)

    assert returned_without_external_cleanup
    assert result_holder["result"].code == 124

    def descendant_is_running() -> bool:
        try:
            state = Path(f"/proc/{descendant_pid}/stat").read_text().split()[2]
        except (FileNotFoundError, IndexError, OSError):
            return False
        return state != "Z"

    deadline = time.monotonic() + 1
    while descendant_is_running() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not descendant_is_running(), "descendant retained the timed-out process group's output pipes"


def test_windows_registry_taskkill_timeout_is_bounded_and_falls_back(monkeypatch):
    class StubProcess:
        pid = 4242

        def __init__(self):
            self.killed = False

        def poll(self):
            return None

        def kill(self):
            self.killed = True

    process = StubProcess()
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(proc.os, "name", "nt")
    monkeypatch.setattr(proc.subprocess, "run", fake_run)

    proc._terminate_processes((process,), terminate_grace=0, kill_grace=0)

    assert calls == [
        (
            ["taskkill", "/PID", "4242", "/T", "/F"],
            {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "check": False,
                "timeout": 0.1,
            },
        )
    ]
    assert process.killed is True


def test_windows_run_without_registry_keeps_legacy_subprocess_path(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout=b"ok\n", stderr=b"")

    monkeypatch.setattr(proc.os, "name", "nt")
    monkeypatch.setattr(proc.subprocess, "run", fake_run)
    monkeypatch.setattr(
        proc.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("legacy proc.run must not use Popen"),
    )

    result = proc.run(["worker.exe"])

    assert result.code == 0
    assert len(calls) == 1


def test_run_captures_exit_and_output():
    r = proc.run([sys.executable, "-c", "import sys; print('hi'); sys.exit(3)"])
    assert r.code == 3
    assert r.stdout.strip() == "hi"


def test_run_passes_explicit_stdin_bytes_without_a_shell():
    payload = b'{"body":"hello"}\n'
    result = proc.run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        stdin=payload,
    )

    assert result.code == 0
    assert result.stdout == payload.decode()


def test_run_json_parses_stdout():
    r = proc.run([sys.executable, "-c", "print('{\"a\": 1}')"])
    assert r.json() == {"a": 1}


def test_run_json_returns_none_on_nonjson():
    r = proc.run(["python3", "-c", "print('not json')"])
    assert r.json() is None


def test_which_detects_present_and_absent():
    assert proc.which("python3") is not None
    assert proc.which("definitely-not-a-real-binary-xyz") is None


def test_resolve_executable_reports_missing_without_paths():
    identity = proc.resolve_executable("definitely-not-a-real-binary-xyz")

    assert identity.path is None
    assert identity.kind == "missing"
    assert identity.runnable is False
    assert "not on PATH" in identity.detail


@pytest.mark.parametrize(
    ("launch_error", "expected_code", "expected_stderr"),
    [
        (FileNotFoundError(errno.ENOENT, "not found", "/private/bin/worker"), 127, "command not found: worker"),
        (
            PermissionError(errno.EACCES, "permission denied", "/private/bin/worker"),
            126,
            "command permission denied: worker",
        ),
        (
            OSError(errno.ENOEXEC, "exec format error", "/private/bin/worker"),
            126,
            "command has invalid executable format: worker",
        ),
        (OSError(errno.EIO, "launch failed", "/private/bin/worker"), 126, "command launch failed: worker"),
    ],
    ids=("missing", "permission-denied", "exec-format", "generic-launch-error"),
)
def test_run_classifies_launch_errors_without_absolute_paths(monkeypatch, launch_error, expected_code, expected_stderr):
    def raise_launch_error(*args, **kwargs):
        raise launch_error

    monkeypatch.setattr(proc.subprocess, "run", raise_launch_error)

    result = proc.run(["/private/bin/worker"])

    assert result.code == expected_code
    assert result.stderr == expected_stderr
    assert "/private/bin/worker" not in result.stderr


def test_run_preserves_partial_output_on_timeout(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["worker"],
            timeout=3.0,
            output=b"partial stdout\n",
            stderr=b"partial stderr\n",
        )

    monkeypatch.setattr(proc.subprocess, "run", timeout)

    result = proc.run(["worker"], timeout=3.0)

    assert result.code == 124
    assert result.stdout == "partial stdout\n"
    assert result.stderr == "partial stderr\ntimeout after 3.0s"


def test_run_feeds_stdin_when_provided(monkeypatch):
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args=kwargs.get("args") or args, returncode=0, stdout=b"ok\n", stderr=b"")

    monkeypatch.setattr(proc.subprocess, "run", fake_run)
    result = proc.run(["codex", "exec", "-"], stdin=b"plan prompt")
    assert result.code == 0
    assert captured["input"] == b"plan prompt"
    assert "stdin" not in captured


def test_run_uses_devnull_stdin_when_stdin_omitted(monkeypatch):
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args=kwargs.get("args") or args, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(proc.subprocess, "run", fake_run)
    proc.run(["true"])
    assert captured["stdin"] is subprocess.DEVNULL
    assert "input" not in captured


def test_run_decodes_valid_utf8_with_byte_0x9d_despite_cp1252_locale(monkeypatch):
    payload = "review complete: \u275d\n".encode("utf-8")
    assert b"\x9d" in payload

    def fake_run(*args, **kwargs):
        assert kwargs.get("text") is not True
        return subprocess.CompletedProcess(args[0], 0, payload, b"")

    monkeypatch.setattr(proc.subprocess, "run", fake_run)
    monkeypatch.setattr(locale, "getpreferredencoding", lambda *args, **kwargs: "cp1252")

    result = proc.run(["worker"])

    assert result.code == 0
    assert result.stdout == "review complete: \u275d\n"
    assert result.stderr == ""


def test_run_timeout_normalizes_none_output(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["worker"], timeout=3.0, output=None, stderr=None)

    monkeypatch.setattr(proc.subprocess, "run", timeout)

    result = proc.run(["worker"], timeout=3.0)

    assert result.code == 124
    assert result.stdout == ""
    assert result.stderr == "timeout after 3.0s"


def test_run_returns_typed_failure_for_invalid_utf8(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, b"\x9d alone is invalid utf-8", b"stderr ok\n")

    monkeypatch.setattr(proc.subprocess, "run", fake_run)

    result = proc.run(["worker"])

    assert result.code == 0
    assert result.decode_failed is True
    assert result.stdout_decode_error is not None
    assert result.stderr_decode_error is None
    assert "\ufffd" in result.stdout
    assert "alone is invalid utf-8" in result.stdout
    assert result.stderr.startswith("stderr ok\nchild stdout is not valid UTF-8 (utf-8):")


def test_run_preserves_valid_prefix_on_invalid_utf8(monkeypatch):
    payload = b"prefix\n\x9d"

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, payload, b"")

    monkeypatch.setattr(proc.subprocess, "run", fake_run)

    result = proc.run(["worker"])

    assert result.code == 0
    assert result.decode_failed is True
    assert result.stdout.startswith("prefix\n")
    assert "\ufffd" in result.stdout
    assert result.stdout_decode_error is not None
    assert "child stdout is not valid UTF-8 (utf-8):" in result.stderr
