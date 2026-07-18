"""Run external tool CLIs and capture their results. No tool is imported in-process."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str

    def json(self) -> Optional[object]:
        try:
            return json.loads(self.stdout)
        except (json.JSONDecodeError, ValueError):
            return None


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


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
        return Result(code=cp.returncode, stdout=_timeout_text(cp.stdout), stderr=_timeout_text(cp.stderr))
    except FileNotFoundError:
        return Result(code=127, stdout="", stderr=f"command not found: {args[0]}")
    except subprocess.TimeoutExpired as exc:
        stdout = _timeout_text(exc.stdout)
        stderr = _timeout_text(exc.stderr)
        timeout_detail = f"timeout after {timeout}s"
        if stderr and not stderr.endswith("\n"):
            stderr += "\n"
        return Result(code=124, stdout=stdout, stderr=stderr + timeout_detail)
