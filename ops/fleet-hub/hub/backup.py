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
call's. The memory call's result is also recorded in DEST/memory/last-run.json
as `time`, `ok` and `stamp`, so the S31 backups check can tell a run that has
already followed the memory file appearing from one that is still due.
Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HUB_FILE = "fleet-hub.db"
MEMORY_FILE = "fleet-hub-memory.db"
MEMORY_DIR = "memory"
STATUS_NAME = "last-run.json"


def calls(state: Path, dest: Path, rest: list[str]) -> list[tuple[Path, list[str]]]:
    """(source, argv) for each call, the hub file first."""
    backup = [sys.executable, "-m", "brigade", "fleet", "backup"]
    return [
        (state / HUB_FILE, [*backup, "--db", str(state / HUB_FILE), "--dest", str(dest), *rest]),
        (state / MEMORY_FILE, [*backup, "--db", str(state / MEMORY_FILE), "--dest", str(dest / MEMORY_DIR), *rest]),
    ]


def now() -> float:
    """The time a backup run started, comparable to the memory file's mtime."""
    return time.time()


def call(argv: list[str]) -> tuple[int, str | None]:
    """Run one backup call, replaying its output and returning (exit code, stamp).

    A successful `--json` call prints the report from `fleet backup`, whose
    `stamp` names the dated copy it made. Child output is replayed so a refusal
    still reaches the S31 check that reads this job's stderr.
    """
    done = subprocess.run(argv, capture_output=True, text=True, check=False)
    if done.stdout:
        print(done.stdout, end="", flush=True)
    if done.stderr:
        print(done.stderr, end="", file=sys.stderr, flush=True)
    returncode = done.returncode
    code = 128 - returncode if returncode < 0 else returncode
    stamp = None
    if returncode == 0:
        try:
            stamp = json.loads(done.stdout.strip().splitlines()[-1]).get("stamp")
        except (ValueError, IndexError, AttributeError):
            pass
    return code, stamp


def status(argv: list[str]) -> int:
    # A call killed by signal N reports -N; exit as a shell would, 128 + N.
    return call(argv)[0]


def write_status(dest: Path, *, at: float, ok: bool, stamp: str | None) -> Path:
    """Record the memory call's result beside the memory copies, atomically."""
    folder = dest / MEMORY_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / STATUS_NAME
    temp = folder / f".{path.name}.{os.getpid()}.tmp"
    temp.write_text(json.dumps({"time": at, "ok": ok, "stamp": stamp}, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)
    return path


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
    started = now()
    hub, _ = call(hub_call)
    if memory_source.exists():
        memory, stamp = call(memory_call)
    else:
        print(f"memory backup skipped: {memory_source} does not exist", file=sys.stderr, flush=True)
        memory, stamp = 0, None
    write_status(args.dest, at=started, ok=memory == 0, stamp=stamp)
    return hub or memory


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
