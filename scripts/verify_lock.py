#!/usr/bin/env python3
"""Acquire a host-local per-user verification slot, then exec scripts/verify."""

from __future__ import annotations

import fcntl
import math
import os
import stat
import sys
import time

from pathlib import Path

from verify_parallel import positive_setting, shard_count

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTENTION_MESSAGE = (
    "full verification already running for this user on this host; "
    "run ./scripts/verify-focused <pytest-selector>... or wait for the active Brigade receipt"
)


def _lock_path() -> Path:
    override = os.environ.get("BRIGADE_VERIFY_LOCK_PATH")
    if override:
        return Path(override)
    # Do not use HOME, TMPDIR, XDG_RUNTIME_DIR, or a Git directory: separate
    # clones and agent environments on this host must share one admission pool.
    directory = Path("/tmp") / f"brigade-verify-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"unsafe verification lock directory: {directory} (requires owner-only access)")
    return directory / "brigade-full-verify.lock"


def slot_path(prefix: Path, index: int) -> str:
    # Slot 0 keeps compatibility with an older gate using the same explicit
    # lock override, and slot i >= 1 matches the old per-slot
    # BRIGADE_VERIFY_LOCK_PATH=<lock>.<i> operator override.
    return str(prefix) if index == 0 else f"{prefix}.{index}"


def acquire_slot(prefix: Path, slots: int) -> int | None:
    directory = os.open(prefix.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index in range(slots):
            fd = os.open(
                Path(slot_path(prefix, index)).name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory,
            )
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or info.st_mode & 0o077
                ):
                    raise ValueError(f"unsafe verification slot file: {slot_path(prefix, index)}")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
            except BaseException:
                os.close(fd)
                raise
            else:
                return fd
    finally:
        os.close(directory)
    return None


def main() -> None:
    try:
        shard_count()  # Reject an invalid worker override before admitting a full gate.
        slots = positive_setting("BRIGADE_VERIFY_SLOTS", 1)
        wait = float(os.environ.get("BRIGADE_VERIFY_WAIT", "0"))
        if not math.isfinite(wait) or wait < 0:
            raise ValueError("BRIGADE_VERIFY_WAIT must be finite and nonnegative")
    except ValueError as exc:
        print(f"verify: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    try:
        prefix = _lock_path()
        if prefix.is_symlink():
            raise ValueError(f"unsafe verification lock path: {prefix}")
        if prefix.is_dir():
            prefix = prefix / "brigade-full-verify.lock"
        prefix.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
    except (OSError, ValueError) as exc:
        print(f"verify: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    os.set_inheritable(fd, True)
    env = os.environ.copy()
    env["BRIGADE_VERIFY_LOCK_HELD"] = str(os.getpid())
    env["BRIGADE_VERIFY_LOCK_FD"] = str(fd)
    verify = REPO_ROOT / "scripts" / "verify"
    try:
        os.execvpe(os.fsdecode(verify), [os.fsdecode(verify), *sys.argv[1:]], env)
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
