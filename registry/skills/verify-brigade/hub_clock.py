"""Run a Brigade command on a clock the storage parity drive controls.

`python hub_clock.py --clock-file FILE -- fleet serve ...` starts the in-repo
Python hub with every wall-clock read answered from FILE, which holds one
decimal epoch-seconds value. The drive rewrites FILE between requests, so the
hub's timestamps, expiries and retention cutoffs are exact and repeatable, and
the drive can step past the 7-day sealed-content lifetime without waiting.

This is the Python hub's adapter for `storage_parity.py`. A hub written in
another language reads the same file through its own clock seam; the drive
passes the path as `{clock_file}` in the launch template.

How it works: `_datetime` (the C module) is blocked before anything imports
`datetime`, so `datetime.now()` is the pure-Python version, which reads
`time.time()`. `time.time` and its relatives are then replaced. Monotonic
clocks are left alone, so timeouts, long-poll deadlines and thread waits keep
real time.
"""

from __future__ import annotations

import os
import sys
import time


def _install(clock_file: str) -> None:
    if "datetime" in sys.modules and "_datetime" in sys.modules:
        # Something imported the C datetime first; drop it so the pure version loads.
        del sys.modules["datetime"]
    sys.modules["_datetime"] = None  # type: ignore[assignment]
    real_time = time.time
    last = [real_time()]

    def now() -> float:
        try:
            with open(clock_file, encoding="ascii") as handle:
                last[0] = float(handle.read().strip())
        except (OSError, ValueError):
            pass
        return last[0]

    def now_ns() -> int:
        return int(round(now() * 1_000_000)) * 1000

    real_gmtime, real_localtime, real_strftime = time.gmtime, time.localtime, time.strftime

    def gmtime(secs: float | None = None) -> time.struct_time:
        return real_gmtime(now() if secs is None else secs)

    def localtime(secs: float | None = None) -> time.struct_time:
        return real_localtime(now() if secs is None else secs)

    def strftime(fmt: str, t: tuple | time.struct_time | None = None) -> str:
        return real_strftime(fmt, localtime() if t is None else t)

    time.time = now
    time.time_ns = now_ns
    time.gmtime = gmtime
    time.localtime = localtime
    time.strftime = strftime  # type: ignore[assignment]
    import datetime

    if getattr(datetime.datetime.now, "__module__", None) not in (
        "_pydatetime",
        "datetime",
    ):  # pragma: no cover - defensive
        raise SystemExit("hub_clock: could not load the pure-Python datetime module")


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[0] != "--clock-file" or argv[2] != "--":
        print("usage: hub_clock.py --clock-file FILE -- <brigade arguments>", file=sys.stderr)
        return 2
    clock_file = os.path.abspath(argv[1])
    if not os.path.isfile(clock_file):
        print(f"hub_clock: clock file {clock_file} does not exist", file=sys.stderr)
        return 2
    _install(clock_file)
    from brigade.cli import process_main

    sys.argv = ["brigade", *argv[3:]]
    process_main()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
