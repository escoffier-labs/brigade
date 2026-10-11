#!/usr/bin/env python3
"""Acquire a bounded per-clone verification slot, then exec scripts/verify."""

from __future__ import annotations

import fcntl
import math
import os
import subprocess
import sys
import time

from pathlib import Path

from verify_parallel import positive_setting, shard_count

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


def slot_path(prefix: Path, index: int) -> str:
    # Slot 0 is the pre-slot lock path, so a gate from an older checkout still
    # contends with new gates, and slot i >= 1 matches the old per-slot
    # BRIGADE_VERIFY_LOCK_PATH=<lock>.<i> operator override.
    return str(prefix) if index == 0 else f"{prefix}.{index}"


def acquire_slot(prefix: Path, slots: int) -> int | None:
    for index in range(slots):
        fd = os.open(slot_path(prefix, index), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
        else:
            return fd
    return None


def main() -> None:
    try:
        slots = positive_setting("BRIGADE_VERIFY_SLOTS", max(1, (os.cpu_count() or 1) // shard_count()))
        wait = float(os.environ.get("BRIGADE_VERIFY_WAIT", "0"))
        if not math.isfinite(wait) or wait < 0:
            raise ValueError("BRIGADE_VERIFY_WAIT must be finite and nonnegative")
    except ValueError as exc:
        print(f"verify: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    prefix = _lock_path()
    if prefix.is_dir():
        prefix = prefix / "brigade-full-verify.lock"
    prefix.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    while True:
        fd = acquire_slot(prefix, slots)
        if fd is not None:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(CONTENTION_MESSAGE, file=sys.stderr)
            raise SystemExit(75)
        time.sleep(min(0.1, remaining))
    os.set_inheritable(fd, True)
    env = os.environ.copy()
    env["BRIGADE_VERIFY_LOCK_HELD"] = "1"
    verify = REPO_ROOT / "scripts" / "verify"
    os.execvpe(os.fsdecode(verify), [os.fsdecode(verify), *sys.argv[1:]], env)


if __name__ == "__main__":
    main()
