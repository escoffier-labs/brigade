"""Run external tool CLIs and capture their results. No tool is imported in-process."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

_STREAM_ENCODING = "utf-8"


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str
    stdout_decode_error: str | None = None
    stderr_decode_error: str | None = None

    @property
    def decode_failed(self) -> bool:
        return self.stdout_decode_error is not None or self.stderr_decode_error is not None

    @property
    def decode_failure_detail(self) -> str:
        return self.stderr_decode_error or self.stdout_decode_error or "child output is not valid UTF-8"

    def json(self) -> Optional[object]:
        try:
            return json.loads(self.stdout)
        except (json.JSONDecodeError, ValueError):
            return None


def _decode_stream(value: str | bytes | None, *, stream: str) -> tuple[str, str | None]:
    if value is None:
        return "", None
    if isinstance(value, str):
        return value, None
    try:
        return value.decode(_STREAM_ENCODING), None
    except UnicodeDecodeError as exc:
        replaced = value.decode(_STREAM_ENCODING, errors="replace")
        return replaced, f"child {stream} is not valid UTF-8 ({_STREAM_ENCODING}): {exc}"


def _decoded_output(stdout: str | bytes | None, stderr: str | bytes | None) -> tuple[str, str, str | None, str | None]:
    out_text, out_error = _decode_stream(stdout, stream="stdout")
    err_text, err_error = _decode_stream(stderr, stream="stderr")
    return out_text, err_text, out_error, err_error


def _result_from_output(
    *,
    code: int,
    stdout: str | bytes | None,
    stderr: str | bytes | None,
) -> Result:
    out_text, err_text, stdout_error, stderr_error = _decoded_output(stdout, stderr)
    for stream_error in (stdout_error, stderr_error):
        if stream_error is not None:
            if err_text and not err_text.endswith("\n"):
                err_text += "\n"
            err_text += stream_error
    return Result(
        code=code,
        stdout=out_text,
        stderr=err_text,
        stdout_decode_error=stdout_error,
        stderr_decode_error=stderr_error,
    )


def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def run(
    args: List[str],
    timeout: float = 30.0,
    env: Optional[dict] = None,
    cwd: Optional[Path] = None,
    stdin: bytes | None = None,
) -> Result:
    try:
        if stdin is None:
            cp = subprocess.run(
                args,
                capture_output=True,
                text=False,
                timeout=timeout,
                env=env,
                cwd=cwd,
                check=False,
                shell=False,
                stdin=subprocess.DEVNULL,
            )
        else:
            cp = subprocess.run(
                args,
                capture_output=True,
                text=False,
                timeout=timeout,
                env=env,
                cwd=cwd,
                check=False,
                shell=False,
                input=stdin,
            )
        return _result_from_output(code=cp.returncode, stdout=cp.stdout, stderr=cp.stderr)
    except FileNotFoundError:
        return Result(code=127, stdout="", stderr=f"command not found: {args[0]}")
    except subprocess.TimeoutExpired as exc:
        result = _result_from_output(code=124, stdout=exc.stdout, stderr=exc.stderr)
        timeout_detail = f"timeout after {timeout}s"
        stderr = result.stderr
        if stderr and not stderr.endswith("\n"):
            stderr += "\n"
        return Result(
            code=124,
            stdout=result.stdout,
            stderr=stderr + timeout_detail,
            stdout_decode_error=result.stdout_decode_error,
            stderr_decode_error=result.stderr_decode_error,
        )
