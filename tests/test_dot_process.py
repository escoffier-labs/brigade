"""Process bounds and environment regressions ported from the bridge."""

import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch
from typing import Any

import pytest

from brigade.dot.contracts import DotError, encoded_size
from brigade.dot.process import (
    CommandResult,
    CommandRunner,
    MAX_RESULT_BYTES,
    bound_result,
    child_environment,
    child_path,
)
from brigade.dot.runtime_config import BinPaths


def run_python(code: str, **kwargs: Any) -> CommandResult:
    return CommandRunner().run([sys.executable, "-c", code], **kwargs)


def test_stdin_exit_code_timeout_overflow_and_env() -> None:
    result = run_python(
        "import sys; sys.stdout.write(sys.stdin.read().upper())", stdin=b"hello", timeout=30, max_output=1000
    )
    assert (result.returncode, result.stdout, result.timed_out) == (0, b"HELLO", False)
    result = run_python("import sys; print('{}'); sys.exit(3)", timeout=30, max_output=1000)
    assert (result.returncode, result.stdout.splitlines()) == (3, [b"{}"])
    started = time.monotonic()
    result = run_python("import time; time.sleep(30)", timeout=0.5, max_output=1000)
    assert result.timed_out
    assert time.monotonic() - started < 10
    result = run_python("import sys; sys.stdout.write('x' * 200000)", timeout=30, max_output=1000)
    assert result.overflow
    assert len(result.stdout) == 1000
    with patch.dict(os.environ, {"DOT_TEST_SECRET": "leak"}):
        result = run_python("import json, os; print(json.dumps(dict(os.environ)))", timeout=30, max_output=100000)
    environment = json.loads(result.stdout)
    system = Path(os.environ.get("SystemRoot", "C:/Windows"))
    default_path = (
        os.pathsep.join((str(system / "System32"), str(system))) if sys.platform == "win32" else "/usr/bin:/bin"
    )
    assert environment["PATH"] == default_path
    assert "DOT_TEST_SECRET" not in environment
    result = run_python("import os; print(os.getcwd())", timeout=30, max_output=1000)
    assert result.stdout.strip().decode() == Path(sys.executable).anchor


def test_refuses_bad_argv_without_spawning() -> None:
    for argv in ([], ["a\x00b"], "t3-fleet capacity"):
        with pytest.raises(DotError):
            CommandRunner().run(argv, timeout=5, max_output=10)
    with pytest.raises(DotError, match="^command_unavailable$"):
        CommandRunner().run(["/nonexistent/binary"], timeout=5, max_output=10)


def test_large_output_is_truncated_to_the_result_limit() -> None:
    result = bound_result(
        {
            "result": {
                "schema": "t3-fleet.result.v1",
                "state": "running",
                "output": "y" * 300000,
                "items": list(range(5000)),
            }
        }
    )
    assert encoded_size(result) <= MAX_RESULT_BYTES
    assert result["truncated"]
    assert result["result"]["state"] == "running"


def test_capture_overflow_and_runner_limits() -> None:
    result = run_python("import sys; sys.stdout.write('x' * 200000)", timeout=30, max_output=4096)
    assert result.overflow
    assert len(result.stdout) == 4096


def test_credentials_redacted_but_paths_and_hex_kept() -> None:
    token = "ghp_" + "Z" * 36
    sha = "a" * 40
    path, url = "/opt/example/repos/project", "https://example.com/project/pull/1"
    bearer = "".join(("abcdefghijklmnop", "qrstuvwxyz0123"))
    text = f"Authorization: Bearer {bearer} {token} api_key=supersecret99 {path} {sha} {url}"
    note = bound_result({"note": text})["note"]
    for secret in ("abcdefghijklmnopqrstuvwxyz0123", token, "supersecret99"):
        assert secret not in note
    for kept in (path, sha, url):
        assert kept in note


def test_child_path_and_explicit_environment(dot_private_dir: Path) -> None:
    bins = BinPaths(
        str(dot_private_dir / "t3-fleet"),
        str(Path("/opt/example/bin/brigade").absolute()),
        str(Path("/usr/bin/git").absolute()),
        str(Path("/usr/bin/gh").absolute()),
        None,
    )
    system = Path(os.environ.get("SystemRoot", "C:/Windows"))
    defaults = [str(system / "System32"), str(system)] if sys.platform == "win32" else ["/usr/bin", "/bin"]
    expected = os.pathsep.join(
        dict.fromkeys([str(dot_private_dir), str(Path(bins.brigade).parent), str(Path(bins.git).parent), *defaults])
    )
    assert child_path(bins) == expected
    assert child_environment(expected, {"DOT_EXAMPLE": "value"})["DOT_EXAMPLE"] == "value"
    assert child_environment(expected)["PATH"] == expected

    if sys.platform != "win32":
        assert (
            child_path(
                BinPaths("/bin/t3-fleet", "/bin/brigade", "/usr/bin/git", "/usr/bin/gh", "/opt/example/pool/octopool")
            )
            == "/bin:/usr/bin:/opt/example/pool"
        )


def test_windows_runner_refuses_launch_without_owned_job(monkeypatch):
    from brigade import proc
    from brigade.dot import process

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(proc, "_create_windows_child_job", lambda: None)
    with patch.object(process.subprocess, "Popen", side_effect=AssertionError("uncontained launch")):
        with pytest.raises(DotError, match="^command_unavailable$"):
            run_python("print('must not run')", timeout=1, max_output=100)


@pytest.mark.parametrize(
    ("raised", "expected"),
    [(TypeError("environment key must be str"), DotError), (RuntimeError("unexpected launch failure"), RuntimeError)],
)
def test_windows_runner_closes_job_for_every_launch_exception(monkeypatch, raised, expected):
    from types import SimpleNamespace

    from brigade import proc
    from brigade.dot import process

    operations = []
    job = SimpleNamespace(close=lambda: operations.append("close"))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(proc, "_create_windows_child_job", lambda: job)
    with patch.object(process.subprocess, "Popen", side_effect=raised):
        with pytest.raises(expected) as excinfo:
            run_python("", timeout=1, max_output=100)
    if expected is DotError:
        assert str(excinfo.value) == "command_unavailable"
    assert operations == ["close"]


def test_full_duplex_capture_preserves_bytes():
    payload = bytes(range(256)) * 256
    result = CommandRunner().run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        stdin=payload,
        timeout=10,
        max_output=len(payload),
    )
    assert result.returncode == 0
    assert result.stdout == payload
    assert not result.overflow
    assert not result.timed_out


def test_windows_cleanup_closes_pipes_when_wait_times_out(monkeypatch):
    import io
    import subprocess
    from types import SimpleNamespace

    from brigade import proc

    operations = []
    job = SimpleNamespace(terminate=lambda: operations.append("terminate"), close=lambda: operations.append("close"))

    def wait(*, timeout):
        raise subprocess.TimeoutExpired("fixture", timeout)

    child = SimpleNamespace(
        stdin=io.BytesIO(),
        stdout=io.BytesIO(b"output"),
        poll=lambda: 0,
        wait=wait,
        returncode=0,
    )
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(proc, "_create_windows_child_job", lambda: job)
    monkeypatch.setattr(proc, "_bind_suspended_windows_child", lambda *args: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: child)
    with pytest.raises(DotError, match="^command_unavailable$"):
        run_python("", timeout=1, max_output=100)
    assert child.stdin.closed and child.stdout.closed
    assert operations == ["terminate", "close"]
