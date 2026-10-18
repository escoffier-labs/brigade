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
    assert (result.returncode, result.stdout) == (3, b"{}\n")
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
    assert environment["PATH"] == "/usr/bin:/bin"
    assert "DOT_TEST_SECRET" not in environment
    result = run_python("import os; print(os.getcwd())", timeout=30, max_output=1000)
    assert result.stdout.strip() == b"/"


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
    bins = BinPaths(str(dot_private_dir / "t3-fleet"), "/opt/example/bin/brigade", "/usr/bin/git", "/usr/bin/gh", None)
    expected = f"{dot_private_dir}:/opt/example/bin:/usr/bin:/bin"
    assert child_path(bins) == expected
    assert child_environment(expected, {"DOT_EXAMPLE": "value"})["DOT_EXAMPLE"] == "value"
    assert child_environment(expected)["PATH"] == expected
    assert (
        child_path(
            BinPaths("/bin/t3-fleet", "/bin/brigade", "/usr/bin/git", "/usr/bin/gh", "/opt/example/pool/octopool")
        )
        == "/bin:/usr/bin:/opt/example/pool"
    )
