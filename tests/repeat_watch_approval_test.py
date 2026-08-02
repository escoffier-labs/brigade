"""Run the watch approval stability test repeatedly for Brigade verify."""

from __future__ import annotations

import subprocess
import sys

TEST_NODE = "tests/test_runs_cmd.py::test_watch_waits_for_consumed_approval_while_provider_lock_is_live"
REPEAT_COUNT = 20


def main() -> int:
    for iteration in range(1, REPEAT_COUNT + 1):
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", TEST_NODE],
            check=False,
        )
        if completed.returncode != 0:
            print(f"repeat_runs: failed on iteration {iteration}/{REPEAT_COUNT}", file=sys.stderr)
            return completed.returncode
    print(f"repeat_runs: {REPEAT_COUNT} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
