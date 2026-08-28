"""Adversarial subprocess tests for Fleet Steward execution."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from brigade.grokbot_fleet.contracts import FleetError
from brigade.grokbot_fleet.exec import (
    EXEC_MAX_OUTPUT_BYTES,
    EXEC_MAX_TIMEOUT_MS,
    EXEC_MIN_OUTPUT_BYTES,
    EXEC_MIN_TIMEOUT_MS,
    ExecRequest,
    PrivateExecResult,
    run_exec,
)
from brigade.grokbot_fleet.runtime_config import FLEET_PROBE_ENVIRONMENT_KEYS


def _request(
    file: str,
    *args: str,
    timeout_ms: int = 2_000,
    max_buffer_bytes: int = 4_096,
    cwd: str = "/",
) -> ExecRequest:
    return ExecRequest(
        file=file,
        args=args,
        cwd=cwd,
        timeout_ms=timeout_ms,
        max_buffer_bytes=max_buffer_bytes,
        env={
            **{key: os.environ.get(key, "ok") for key in FLEET_PROBE_ENVIRONMENT_KEYS},
            "GROKBOT_FLEET_TOKEN": "F" * 32,
        },
    )


def _python_request(code: str, *, timeout_ms: int = 2_000, max_buffer_bytes: int = 4_096) -> ExecRequest:
    return _request(sys.executable, "-c", code, timeout_ms=timeout_ms, max_buffer_bytes=max_buffer_bytes)


def _reap_pid(pid: int) -> None:
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError:
        pass


def test_run_exec_rejects_out_of_range_bounds():
    request = _request("/bin/true")
    with pytest.raises(FleetError) as caught:
        run_exec(ExecRequest(**{**request.__dict__, "timeout_ms": EXEC_MIN_TIMEOUT_MS - 1}))
    assert caught.value.code == "invalid_request"
    with pytest.raises(FleetError):
        run_exec(ExecRequest(**{**request.__dict__, "timeout_ms": EXEC_MAX_TIMEOUT_MS + 1}))
    with pytest.raises(FleetError):
        run_exec(ExecRequest(**{**request.__dict__, "max_buffer_bytes": EXEC_MIN_OUTPUT_BYTES - 1}))
    with pytest.raises(FleetError):
        run_exec(ExecRequest(**{**request.__dict__, "max_buffer_bytes": EXEC_MAX_OUTPUT_BYTES + 1}))


def test_run_exec_times_out_and_reaps_descendants(tmp_path: Path):
    pid_path = tmp_path / "descendant.pid"
    descendant = (
        "import os,time; from pathlib import Path; "
        f"Path({str(pid_path)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', {descendant!r}]); time.sleep(30)"
    started = time.monotonic()
    try:
        with pytest.raises(FleetError) as caught:
            run_exec(_python_request(parent, timeout_ms=400))
        assert caught.value.code == "timeout"
        assert "sleep" not in str(caught.value)
        assert time.monotonic() - started < 5
    finally:
        if pid_path.is_file():
            try:
                _reap_pid(int(pid_path.read_text(encoding="utf-8").strip()))
            except ValueError:
                pass


def test_run_exec_overflows_without_leaking_output():
    with pytest.raises(FleetError) as caught:
        run_exec(
            _python_request(
                "import sys; sys.stdout.write('SECRET_STDOUT ' * 5000); sys.stdout.flush()",
                max_buffer_bytes=1_024,
            )
        )
    assert caught.value.code == "protocol_error"
    assert "SECRET_STDOUT" not in str(caught.value)
    assert "SECRET_STDOUT" not in repr(caught.value)


def test_run_exec_allowlists_environment():
    captured: dict[str, str] = {}

    def runner(request: ExecRequest) -> PrivateExecResult:
        captured.update(dict(request.env))
        return PrivateExecResult(stdout="")

    run_exec(_request("/bin/true"), runner=runner)
    assert "GROKBOT_FLEET_TOKEN" not in captured
    assert set(captured) <= set(FLEET_PROBE_ENVIRONMENT_KEYS)
    assert "PATH" in captured
