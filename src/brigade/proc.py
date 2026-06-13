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


def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def run(
    args: List[str],
    timeout: float = 30.0,
    env: Optional[dict] = None,
    cwd: Optional[Path] = None,
) -> Result:
    try:
        cp = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=cwd,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        return Result(code=cp.returncode, stdout=cp.stdout, stderr=cp.stderr)
    except FileNotFoundError:
        return Result(code=127, stdout="", stderr=f"command not found: {args[0]}")
    except subprocess.TimeoutExpired:
        return Result(code=124, stdout="", stderr=f"timeout after {timeout}s")
