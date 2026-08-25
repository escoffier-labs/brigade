import errno
import io
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


class _RecordingBytesIO(io.BytesIO):
    def __init__(self, initial=b""):
        super().__init__(initial)
        self.written = bytearray()

    def write(self, data):
        self.written.extend(data)
        return super().write(data)


class _StubProcess:
    """Popen stand-in with readable pipes for the streaming collector."""

    def __init__(self, stdout=b"", stderr=b"", returncode=0, pid=4242, wait_error=None):
        self.pid = pid
        self.returncode = returncode
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.stdin = _RecordingBytesIO()
        self._wait_error = wait_error
        self._poll_error = None

    def poll(self):
        if self._poll_error is not None:
            raise self._poll_error
        return self.returncode

    def wait(self, timeout=None):
        if self._wait_error is not None:
            raise self._wait_error
        return self.returncode


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

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return _StubProcess(stdout=b"ok\n")

    monkeypatch.setattr(proc.os, "name", "nt")
    monkeypatch.setattr(proc.subprocess, "Popen", fake_popen)

    result = proc.run(["worker.exe"], process_registry=proc.ProcessRegistry())

    assert result.code == 0
    assert result.stdout == "ok\n"
    assert captured["creationflags"] == getattr(proc.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    assert "start_new_session" not in captured


def test_registered_process_terminates_before_unregister_on_base_exception(monkeypatch):
    events = []

    class StubRegistry:
        def register(self, process):
            events.append(("register", process.pid))

        def terminate(self, process):
            events.append(("terminate", process.pid))

        def unregister(self, process):
            events.append(("unregister", process.pid))

    stub = _StubProcess()
    stub._poll_error = KeyboardInterrupt()
    monkeypatch.setattr(proc.subprocess, "Popen", lambda *args, **kwargs: stub)

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


def test_registered_timeout_never_uses_unbounded_output_drain():
    result = proc.run(
        [
            sys.executable,
            "-c",
            "import sys,time; sys.stdout.write('partial output'); sys.stdout.flush(); time.sleep(5)",
        ],
        timeout=0.2,
        process_registry=proc.ProcessRegistry(terminate_grace=0.05, kill_grace=0.05),
    )

    assert result.code == 124
    assert result.stdout == "partial output"
    assert "timeout after" in result.stderr
    assert result.total_bytes <= proc.MAX_CAPTURE_BYTES


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


def test_windows_run_without_registry_still_starts_process_group(monkeypatch):
    captured = {}

    def fake_popen(args, **kwargs):
        captured.update(kwargs)
        return _StubProcess(stdout=b"ok\n")

    monkeypatch.setattr(proc.os, "name", "nt")
    monkeypatch.setattr(proc.subprocess, "Popen", fake_popen)

    result = proc.run(["worker.exe"])

    assert result.code == 0
    assert result.stdout == "ok\n"
    assert captured["creationflags"] == getattr(proc.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    assert captured["stdin"] is subprocess.DEVNULL


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

    monkeypatch.setattr(proc.subprocess, "Popen", raise_launch_error)

    result = proc.run(["/private/bin/worker"])

    assert result.code == expected_code
    assert result.stderr == expected_stderr
    assert "/private/bin/worker" not in result.stderr


def test_run_preserves_partial_output_on_timeout():
    result = proc.run(
        [
            sys.executable,
            "-c",
            (
                "import sys,time; "
                "sys.stdout.write('partial stdout\\n'); sys.stdout.flush(); "
                "sys.stderr.write('partial stderr\\n'); sys.stderr.flush(); "
                "time.sleep(5)"
            ),
        ],
        timeout=0.3,
    )

    assert result.code == 124
    assert result.stdout == "partial stdout\n"
    assert result.stderr.startswith("partial stderr\n")
    assert "timeout after 0.3s" in result.stderr


def test_run_feeds_stdin_when_provided(monkeypatch):
    captured = {}
    stub = _StubProcess(stdout=b"ok\n")

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return stub

    monkeypatch.setattr(proc.subprocess, "Popen", fake_popen)
    result = proc.run(["codex", "exec", "-"], stdin=b"plan prompt")
    assert result.code == 0
    assert captured["stdin"] is subprocess.PIPE
    assert bytes(stub.stdin.written) == b"plan prompt"


def test_run_uses_devnull_stdin_when_stdin_omitted(monkeypatch):
    captured = {}

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return _StubProcess()

    monkeypatch.setattr(proc.subprocess, "Popen", fake_popen)
    proc.run(["true"])
    assert captured["stdin"] is subprocess.DEVNULL


def test_run_decodes_valid_utf8_with_byte_0x9d_despite_cp1252_locale(monkeypatch):
    payload = "review complete: \u275d\n".encode("utf-8")
    assert b"\x9d" in payload
    monkeypatch.setattr(locale, "getpreferredencoding", lambda *args, **kwargs: "cp1252")

    result = proc.run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(" + repr(payload) + ")"],
    )

    assert result.code == 0
    assert result.stdout == "review complete: \u275d\n"
    assert result.stderr == ""


def test_run_timeout_normalizes_none_output():
    result = proc.run(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        timeout=0.2,
    )

    assert result.code == 124
    assert result.stdout == ""
    assert result.stderr == "timeout after 0.2s"


def test_run_returns_typed_failure_for_invalid_utf8():
    result = proc.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'\\x9d alone is invalid utf-8'); sys.stderr.write('stderr ok\\n')",
        ]
    )

    assert result.code == 0
    assert result.decode_failed is True
    assert result.stdout_decode_error is not None
    assert result.stderr_decode_error is None
    assert "\ufffd" in result.stdout
    assert "alone is invalid utf-8" in result.stdout
    assert result.stderr.startswith("stderr ok\nchild stdout is not valid UTF-8 (utf-8):")


def test_run_preserves_valid_prefix_on_invalid_utf8():
    result = proc.run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'prefix\\n\\x9d')"],
    )

    assert result.code == 0
    assert result.decode_failed is True
    assert result.stdout.startswith("prefix\n")
    assert "\ufffd" in result.stdout
    assert result.stdout_decode_error is not None
    assert "child stdout is not valid UTF-8 (utf-8):" in result.stderr


def test_seat_process_registry_exposes_full_call_surface():
    """All methods used by proc.py failure paths must exist on _SeatProcessRegistry."""
    parent = proc.ProcessRegistry()
    seat_registry = parent.for_seat("test-seat")
    assert callable(getattr(seat_registry, "register", None))
    assert callable(getattr(seat_registry, "unregister", None))
    assert callable(getattr(seat_registry, "cancel", None))
    assert callable(getattr(seat_registry, "terminate", None))


def test_seat_process_registry_terminate_delegates_to_parent(monkeypatch):
    terminated = []

    class FakeProcess:
        pid = 1234

    monkeypatch.setattr(
        proc,
        "_terminate_processes",
        lambda processes, **kwargs: terminated.extend(p.pid for p in processes),
    )

    parent = proc.ProcessRegistry(terminate_grace=0, kill_grace=0)
    seat_registry = parent.for_seat("worker-seat")
    process = FakeProcess()
    seat_registry.terminate(process)

    assert terminated == [1234]


def test_seat_registry_timeout_yields_code_124_not_attribute_error():
    """A seat-scoped registry passed to proc.run must not raise AttributeError on timeout."""

    parent = proc.ProcessRegistry(terminate_grace=0.05, kill_grace=0.05)
    seat_registry = parent.for_seat("grok")

    result = proc.run(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        timeout=0.2,
        process_registry=seat_registry,
    )

    assert result.code == 124
    assert "timeout after" in result.stderr


def test_run_caps_combined_output_and_sets_overflow_flag():
    overflow = proc.MAX_CAPTURE_BYTES + 4096
    stdout_n = overflow // 2
    stderr_n = overflow - stdout_n
    result = proc.run(
        [
            sys.executable,
            "-c",
            (
                "import sys;"
                f"sys.stdout.buffer.write(b'O' * {stdout_n});"
                f"sys.stderr.buffer.write(b'E' * {stderr_n});"
                "sys.stdout.buffer.flush(); sys.stderr.buffer.flush()"
            ),
        ],
        timeout=5.0,
    )

    assert result.output_limit_exceeded is True
    assert result.total_bytes == proc.MAX_CAPTURE_BYTES
    assert result.stdout_bytes + result.stderr_bytes == proc.MAX_CAPTURE_BYTES
    assert len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8")) >= proc.MAX_CAPTURE_BYTES
    assert "combined output exceeded" in result.stderr


def test_large_stdin_does_not_block_timeout_or_overflow_monitor():
    """Stdin must be written concurrently so a child filling stdout cannot deadlock past timeout."""

    started = time.monotonic()
    result = proc.run(
        [
            sys.executable,
            "-c",
            (
                "import sys;"
                "sys.stdout.buffer.write(b'O' * (2 * 1024 * 1024));"
                "sys.stdout.buffer.flush();"
                "sys.stdin.buffer.read()"
            ),
        ],
        timeout=0.1,
        stdin=b"I" * (2 * 1024 * 1024),
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.8
    assert result.output_limit_exceeded or result.code == 124


def test_overflow_truncates_returned_text_at_utf8_boundary():
    """A cap cut inside a multibyte char must not return text over the cap."""

    prefix = proc.MAX_CAPTURE_BYTES - 1
    result = proc.run(
        [
            sys.executable,
            "-c",
            (f"import sys;sys.stdout.buffer.write(b'A' * {prefix} + b'\\xe2\\x82\\xac')"),
        ],
        timeout=5.0,
    )

    assert result.output_limit_exceeded is True
    assert len(result.stdout.encode("utf-8")) <= proc.MAX_CAPTURE_BYTES


def test_bound_text_pair_stays_at_or_below_cap():
    overshot = ("A" * (proc.MAX_CAPTURE_BYTES - 1)) + "\ufffd"
    stdout, stderr = proc.bound_text_pair(overshot, "E", proc.MAX_CAPTURE_BYTES)
    assert len(stdout.encode("utf-8")) + len(stderr.encode("utf-8")) <= proc.MAX_CAPTURE_BYTES


def test_run_delimited_splits_nul_fields():
    result = proc.run_delimited([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'a\\x00bb\\x00ccc\\x00')"])

    assert result.code == 0
    assert result.items == ["a", "bb", "ccc"]
    assert result.truncated is False
    assert result.timed_out is False


def test_run_delimited_keeps_final_field_without_trailing_delimiter():
    result = proc.run_delimited([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x\\x00y')"])

    assert result.code == 0
    assert result.items == ["x", "y"]


def test_run_delimited_decodes_multibyte_char_across_read_chunks():
    # A UTF-8 sequence straddling the internal read-chunk boundary must decode
    # as one character, not as replacement garbage from per-chunk decoding.
    payload = b"a" * 4095 + "é".encode() + b"\x00z"
    result = proc.run_delimited([sys.executable, "-c", f"import sys; sys.stdout.buffer.write({payload!r})"])

    assert result.code == 0
    assert result.items == ["a" * 4095 + "é", "z"]


def test_run_delimited_stops_child_over_budget_and_flags_truncation():
    result = proc.run_delimited(
        [sys.executable, "-c", "import sys,time; sys.stdout.buffer.write(b'x'*100000); time.sleep(5)"],
        max_bytes=1024,
    )

    assert result.truncated is True
    assert result.stdout_bytes <= 1024
    assert result.code != 0
    assert "streaming output exceeded 1024 byte delimiter-split limit" in result.stderr


def test_run_delimited_truncation_boundary_is_exact_and_excludes_partial_field():
    payload = b"a" * 10 + b"\x00" + b"b" * 10 + b"\x00"

    complete = proc.run_delimited(
        [sys.executable, "-c", f"import sys; sys.stdout.buffer.write({payload!r})"],
        max_bytes=len(payload),
    )
    assert complete.truncated is False
    assert complete.items == ["a" * 10, "b" * 10]
    assert complete.stdout_bytes == len(payload)

    one_short = proc.run_delimited(
        [sys.executable, "-c", f"import sys; sys.stdout.buffer.write({payload!r})"],
        max_bytes=len(payload) - 1,
    )
    # The final field lacks its trailing NUL inside the budget, so only
    # complete fields are retained and the raw byte count stops at the cap.
    # The child already exited successfully here, so its exit code stands
    # (same as proc.run overflow); callers must check truncated first.
    assert one_short.truncated is True
    assert one_short.items == ["a" * 10]
    assert one_short.stdout_bytes == len(payload) - 1


def test_run_delimited_reports_timeout_with_code_124():
    result = proc.run_delimited([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3)

    assert result.timed_out is True
    assert result.code == 124
    assert "timeout after 0.3s" in result.stderr


def test_run_delimited_rejects_non_single_byte_delimiter():
    with pytest.raises(ValueError, match="delimiter must be exactly one byte"):
        proc.run_delimited(["true"], delimiter=b"")


def test_run_delimited_reports_missing_command():
    result = proc.run_delimited(["definitely-not-a-real-binary-xyz"])

    assert result.code == 127
    assert result.items == []
    assert "command not found" in result.stderr


def test_generic_capture_cap_still_applies_to_plain_run():
    # The delimited streaming budget is separate: unrelated commands keep the
    # generic MAX_CAPTURE_BYTES cap (issue #1165 acceptance).
    result = proc.run([sys.executable, "-c", "import sys; sys.stdout.write('x' * (2 * 1024 * 1024))"])

    assert result.output_limit_exceeded is True


def test_run_delimited_flags_stderr_over_capture_limit():
    result = proc.run_delimited(
        [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('e' * (2 * 1024 * 1024)); sys.stderr.flush()",
        ],
    )

    assert result.code == 0
    assert result.stderr_truncated is True
    assert result.stderr_bytes <= proc.MAX_CAPTURE_BYTES
    assert f"stderr capture exceeded {proc.MAX_CAPTURE_BYTES} byte limit" in result.stderr


def test_run_delimited_stderr_under_limit_is_not_truncated():
    result = proc.run_delimited(
        [sys.executable, "-c", "import sys; sys.stderr.write('warn\\n')"],
    )

    assert result.code == 0
    assert result.stderr_truncated is False
    assert result.stderr == "warn\n"
