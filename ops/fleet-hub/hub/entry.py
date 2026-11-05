#!/usr/bin/env python3
"""Entrypoint for the dogfood hub container: `brigade-hub` in front of the Python hub.

`brigade-hub` runs in managed mode. It starts `python3 -m brigade fleet serve`
as its child on an ephemeral loopback port, gives it a per-start key, and
exits non-zero if that child dies. This wrapper is the interim stand-in for the
planned `fleet serve --engine rust` (S10): it starts `brigade-hub` as its only
child, forwards SIGTERM and SIGINT to it, and writes the two lines the S31
checks read from the container log, which the Python hub wrote before the Rust
front took the port:

- the listening line, once `/health` answers on the front's own port;
- the clean-exit line, when `brigade-hub` exits 0 after a stop signal, that is
  after its drain of at most 25 s and the upstream's stop.

Any other exit passes through unchanged, so the restart policy sees a crash as
a crash. Stdlib only.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import urllib.request

HUB = os.environ.get("BRIGADE_HUB_BIN", "brigade-hub")
STARTED = "brigade fleet hub listening on"
STOPPED = "brigade fleet hub stopped cleanly"
POLL_SECONDS = 0.25


def option(argv: list[str], name: str, default: str) -> str:
    """The last value of `--name VALUE` or `--name=VALUE` in argv."""
    value = default
    for index, item in enumerate(argv):
        if item == name and index + 1 < len(argv):
            value = argv[index + 1]
        elif item.startswith(name + "="):
            value = item.split("=", 1)[1]
    return value


def route_modes(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, item in enumerate(argv[:-1]) if item == "--route-mode"] + [
        item.split("=", 1)[1] for item in argv if item.startswith("--route-mode=")
    ]


def healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def exit_code(returncode: int) -> int:
    # A child killed by signal N reports -N; a shell reports 128 + N.
    return 128 - returncode if returncode < 0 else returncode


def main(argv: list[str]) -> int:
    host = option(argv, "--host", "127.0.0.1")
    port = option(argv, "--port", "3774")
    db = option(argv, "--db", "~/.brigade/fleet-hub.db")
    child = subprocess.Popen([HUB, *argv])
    stopping = False

    def forward(signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    url = f"http://{'[' + host + ']' if ':' in host else host}:{port}/health"
    while child.poll() is None and not stopping:
        if healthy(url):
            modes = ", ".join(route_modes(argv)) or "defaults"
            print(
                f"{STARTED} {host}:{port} (brigade-hub front, managed Python upstream; db {db}; route modes {modes})",
                flush=True,
            )
            break
        try:
            child.wait(timeout=POLL_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    returncode = child.wait()
    if returncode == 0 and stopping:
        print(STOPPED, flush=True)
    return exit_code(returncode)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
