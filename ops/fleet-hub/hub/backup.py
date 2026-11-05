#!/usr/bin/env python3
"""Backup job for the dogfood hub: one `brigade fleet backup` run per file.

`brigade fleet backup` checks every `--db` before it copies any, so one call
for both files would let a missing memory file stop the hub file's copy. This
wrapper makes two calls instead, each into its own directory, and makes both
whatever the other one did:

- `fleet-hub.db` into DEST, the layout the restore runbook reads;
- `fleet-hub-memory.db` into DEST/memory. A missing memory file (a hub run
  without `--route-mode memory=native`, or a lost file before the hub's next
  start) is logged and skipped. Any other memory failure fails the job.

Every other argument (owner, retention, free-space floor, `--json`) goes to
both calls. The exit status is the hub call's when it failed, else the memory
call's. Stdlib only.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HUB_FILE = "fleet-hub.db"
MEMORY_FILE = "fleet-hub-memory.db"
MEMORY_DIR = "memory"


def calls(state: Path, dest: Path, rest: list[str]) -> list[tuple[Path, list[str]]]:
    """(source, argv) for each call, the hub file first."""
    backup = [sys.executable, "-m", "brigade", "fleet", "backup"]
    return [
        (state / HUB_FILE, [*backup, "--db", str(state / HUB_FILE), "--dest", str(dest), *rest]),
        (state / MEMORY_FILE, [*backup, "--db", str(state / MEMORY_FILE), "--dest", str(dest / MEMORY_DIR), *rest]),
    ]


def status(argv: list[str]) -> int:
    # A call killed by signal N reports -N; exit as a shell would, 128 + N.
    returncode = subprocess.run(argv, check=False).returncode
    return 128 - returncode if returncode < 0 else returncode


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Back up the hub file and the memory file, one call each.", allow_abbrev=False
    )
    parser.add_argument("--state-dir", default="/var/lib/brigade", type=Path)
    parser.add_argument("--dest", default="/backups", type=Path)
    args, rest = parser.parse_known_args(argv)
    if any(item == "--db" or item.startswith("--db=") for item in rest):
        parser.error("the wrapper names the files; pass no --db")
    (_, hub_call), (memory_source, memory_call) = calls(args.state_dir, args.dest, rest)
    hub = status(hub_call)
    if memory_source.exists():
        memory = status(memory_call)
    else:
        print(f"memory backup skipped: {memory_source} does not exist", file=sys.stderr, flush=True)
        memory = 0
    return hub or memory


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
