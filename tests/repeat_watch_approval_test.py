"""Run the watch approval stability test repeatedly for Brigade verify."""

from __future__ import annotations

import subprocess
import sys

TEST_NODE = "tests/test_runs_cmd.py::test_watch_waits_for_consumed_approval_while_provider_lock_is_live"
REPEAT_COUNT = 20
ITERATION_TIMEOUT_SECONDS = 60


def main() -> int:
    for iteration in range(1, REPEAT_COUNT + 1):
        command = [sys.executable, "-m", "pytest", "-q", TEST_NODE]
        try:
            completed = subprocess.run(
                command,
                check=False,
                timeout=ITERATION_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            # ``subprocess.run`` kills and waits for its child before it
            # re-raises ``TimeoutExpired``, so returning here cannot orphan
            # the timed-out pytest process.
            print(
                f"repeat_runs: timed out on iteration {iteration}/{REPEAT_COUNT} after {ITERATION_TIMEOUT_SECONDS}s",
                file=sys.stderr,
            )
            return 124
        if completed.returncode != 0:
            print(f"repeat_runs: failed on iteration {iteration}/{REPEAT_COUNT}", file=sys.stderr)
            return completed.returncode
    print(f"repeat_runs: {REPEAT_COUNT} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
