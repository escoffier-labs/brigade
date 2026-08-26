#!/usr/bin/env python3
"""Hold a nonblocking exclusive lock, then exec scripts/verify."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTENTION_MESSAGE = (
    "full verification already running for this checkout; "
    "run ./scripts/verify-focused <pytest-selector>... or wait for the active Brigade receipt"
)


def _lock_path() -> Path:
    override = os.environ.get("BRIGADE_VERIFY_LOCK_PATH")
    if override:
        return Path(override)
    git_common_dir = Path(
        subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=REPO_ROOT,
            text=True,
        ).strip()
    )
    if not git_common_dir.is_absolute():
        git_common_dir = REPO_ROOT / git_common_dir
    return git_common_dir / "brigade-full-verify.lock"


def main() -> None:
    fd = os.open(_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(CONTENTION_MESSAGE, file=sys.stderr)
        raise SystemExit(75) from None
    os.set_inheritable(fd, True)
    env = os.environ.copy()
    env["BRIGADE_VERIFY_LOCK_HELD"] = "1"
    verify = REPO_ROOT / "scripts" / "verify"
    os.execvpe(os.fsdecode(verify), [os.fsdecode(verify), *sys.argv[1:]], env)


if __name__ == "__main__":
    main()
