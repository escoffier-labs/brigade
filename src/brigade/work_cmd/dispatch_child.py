"""Durably record the child identity before exec, including dispatcher crashes."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .. import localio
from ..inbox_lock import InboxLockTimeout, held_file_lock
from .dispatch_state import process_identity


def main() -> int:
    run_dir = Path(sys.argv[1])
    try:
        with held_file_lock(run_dir / "dispatch-launch.lock", deadline_seconds=0):
            receipt = run_dir / "dispatch-launch.json"
            if receipt.exists():
                return 0
            identity = {
                "pid": os.getpid(),
                "process_token": process_identity(os.getpid())[1],
                "created_at": localio.utc_now_iso(),
            }
            localio.write_json_exclusive(receipt, identity)
            # A second bootstrap can never exec after this exclusive receipt,
            # even when the first parent died before observing its PID.
            try:
                os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
            except OSError as exc:
                localio.write_json(receipt, {**identity, "launch_error": str(exc)})
                return 127
    except InboxLockTimeout:
        return 0
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
