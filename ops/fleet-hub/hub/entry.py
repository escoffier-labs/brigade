#!/usr/bin/env python3
"""Entrypoint for the dogfood hub container: `brigade-hub` in front of the Python hub.

When the image's Brigade has the Rust engine (S10) and the image recorded the
binary's checksum, this execs `brigade fleet serve --engine rust`, which checks
the binary, supervises it and runs the watchdog. The recipe's default
BRIGADE_REF predates that engine, so otherwise it falls back to the S31
wrapper below. Bump the pin past S10 at the next release and the fallback
goes unused.

`brigade-hub` runs in managed mode. It starts `python3 -m brigade fleet serve`
as its child on an ephemeral loopback port, gives it a per-start key, and
exits non-zero if that child dies. This wrapper is the interim stand-in for the
planned `fleet serve --engine rust` (S10): it starts `brigade-hub` as its only
child, forwards SIGTERM and SIGINT to it, and writes the two lines the S31
checks read from the container log, which the Python hub wrote before the Rust
front took the port:

- the listening line, once `/health` answers on the front's own port;
- the clean-exit line, when `brigade-hub` exits 0 after a stop signal.
  `brigade-hub` exits 0 only when both hubs shut down cleanly: its drain of at
  most 25 s ended, and the Python upstream, sent SIGTERM after it, ran its own
  shutdown and exited 0 within 10 s. An upstream it had to kill, an upstream
  that exited non-zero, or a stop that came during startup makes it exit 2,
  and its log says which.

Any other exit passes through unchanged, so the restart policy sees a crash as
a crash. Stdlib only.
"""

from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import urllib.request

HUB = os.environ.get("BRIGADE_HUB_BIN", "brigade-hub")
# Where the dogfood stage installs brigade-hub and the checksum it recorded.
ENGINE_BINARY = "/usr/local/bin/brigade-hub"
ENGINE_SUM = "/usr/local/share/brigade/brigade-hub.sha256"
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


def engine_command(argv: list[str]) -> list[str] | None:
    """`fleet serve --engine rust` with these flags, when this image supports it."""
    if not os.path.isfile(ENGINE_SUM):
        return None
    try:
        if importlib.util.find_spec("brigade.fleet_hub_engine") is None:
            return None
    except ModuleNotFoundError:
        return None
    return [
        sys.executable,
        "-m",
        "brigade",
        "fleet",
        "serve",
        "--engine",
        "rust",
        "--hub-binary",
        ENGINE_BINARY,
        "--hub-sha256-file",
        ENGINE_SUM,
        *argv,
    ]


def main(argv: list[str]) -> int:
    command = engine_command(argv)
    if command is not None:
        # Same PID, so the init process's signals reach fleet serve directly.
        os.execv(command[0], command)
    host = option(argv, "--host", "127.0.0.1")
    port = option(argv, "--port", "3774")
    db = option(argv, "--db", "~/.brigade/fleet-hub.db")
    child: subprocess.Popen[bytes] | None = None
    pending: int | None = None
    stopping = False

    def send(signum: int) -> None:
        if child is None:
            return
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    def forward(signum: int, _frame: object) -> None:
        # Installed before the child exists, so a stop during the spawn is
        # kept and forwarded below instead of killing this wrapper.
        nonlocal stopping, pending
        stopping = True
        if child is None:
            pending = signum
        else:
            send(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    # The child starts with both blocked (the mask survives fork and exec)
    # and `brigade-hub` unblocks them once its handlers exist, so neither a
    # stop forwarded right after the spawn nor one sent to the process group
    # meets its pre-main default action.
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
    try:
        child = subprocess.Popen([HUB, *argv])
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
    if pending is not None:
        send(pending)
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
