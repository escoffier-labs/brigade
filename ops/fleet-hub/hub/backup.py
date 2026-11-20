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
call's. Each memory call also records its result in DEST/memory/last-run.json:
`time`, `ok`, `stamp`, the durable `file_since` the current memory file was
first seen, and its `ident`. The S31 backups check uses those to tell a run
that has covered the file from one that is still due. The status writer never
creates DEST/memory and never changes the copy result: that directory belongs
to `--owner` and is created by `brigade fleet backup` itself.
Stdlib only.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HUB_FILE = "fleet-hub.db"
MEMORY_FILE = "fleet-hub-memory.db"
MEMORY_DIR = "memory"
STATUS_NAME = "last-run.json"
Owner = tuple[int, int]


def calls(state: Path, dest: Path, rest: list[str]) -> list[tuple[Path, list[str]]]:
    """(source, argv) for each call, the hub file first."""
    backup = [sys.executable, "-m", "brigade", "fleet", "backup"]
    return [
        (state / HUB_FILE, [*backup, "--db", str(state / HUB_FILE), "--dest", str(dest), *rest]),
        (state / MEMORY_FILE, [*backup, "--db", str(state / MEMORY_FILE), "--dest", str(dest / MEMORY_DIR), *rest]),
    ]


def now() -> float:
    """The time a backup run started."""
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


def owner_arg(value: str) -> Owner:
    uid, sep, gid = value.partition(":")
    if not sep or not uid.isdigit() or not gid.isdigit():
        raise argparse.ArgumentTypeError("expected UID:GID, two non-negative integers")
    return int(uid), int(gid)


def previous_status(folder: Path) -> dict[str, Any] | None:
    try:
        report = json.loads((folder / STATUS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) else None


def memory_ident(source: Path) -> int | None:
    try:
        return source.stat().st_ino
    except OSError:
        return None


def write_status(dest: Path, *, report: dict[str, Any], owner: Owner | None) -> Path | None:
    """Record one memory result beside the memory copies, or warn and skip.

    The directory and its ownership are the copy job's contract. This job runs
    as root in the backup container, so creating DEST/memory here would leave
    it root-owned and make every later `fleet backup` memory call fail in
    ensure_private_dir. Warn and skip unless it already passes those checks.
    """
    folder = dest / MEMORY_DIR
    if folder.is_symlink() or not folder.is_dir():
        print(f"memory status skipped: {folder} is missing or a symlink", file=sys.stderr, flush=True)
        return None
    if os.name != "nt":
        info = folder.stat()
        expected = owner[0] if owner is not None else os.geteuid()
        if info.st_uid != expected:
            print(
                f"memory status skipped: {folder} belongs to uid {info.st_uid}, not uid {expected}",
                file=sys.stderr,
                flush=True,
            )
            return None
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o077:
            print(f"memory status skipped: {folder} has mode {mode:04o}; it must be 0700", file=sys.stderr, flush=True)
            return None
    path = folder / STATUS_NAME
    temp = folder / f".{path.name}.{os.getpid()}.tmp"
    payload = (json.dumps(report, sort_keys=True) + "\n").encode("utf-8")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        print(f"memory status not written: {exc}", file=sys.stderr, flush=True)
        return None
    try:
        if owner is not None and os.name != "nt":
            os.fchown(fd, owner[0], owner[1])
        os.write(fd, payload)
        os.fsync(fd)
    except OSError as exc:
        os.close(fd)
        with contextlib.suppress(OSError):
            temp.unlink()
        print(f"memory status not written: {exc}", file=sys.stderr, flush=True)
        return None
    os.close(fd)
    try:
        os.replace(temp, path)
        if os.name != "nt":
            directory = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError as exc:
        with contextlib.suppress(OSError):
            temp.unlink()
        print(f"memory status not written: {exc}", file=sys.stderr, flush=True)
        return None
    return path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Back up the hub file and the memory file, one call each.", allow_abbrev=False
    )
    parser.add_argument("--state-dir", default="/var/lib/brigade", type=Path)
    parser.add_argument("--dest", default="/backups", type=Path)
    parser.add_argument("--owner", default=None, type=owner_arg, metavar="UID:GID")
    args, rest = parser.parse_known_args(argv)
    if any(item == "--db" or item.startswith("--db=") for item in rest):
        parser.error("the wrapper names the files; pass no --db")
    if args.owner is not None:
        rest = [*rest, "--owner", f"{args.owner[0]}:{args.owner[1]}"]
    (_, hub_call), (memory_source, memory_call) = calls(args.state_dir, args.dest, rest)
    started = now()
    hub, _ = call(hub_call)
    memory, stamp = 0, None
    if memory_source.exists():
        memory, stamp = call(memory_call)
        folder = args.dest / MEMORY_DIR
        previous = previous_status(folder)
        ident = memory_ident(memory_source)
        file_since = None
        if previous is not None and ident is not None and previous.get("ident") == ident:
            value = previous.get("file_since")
            if isinstance(value, (int, float)):
                file_since = float(value)
        if file_since is None:
            file_since = started
        report = {"time": started, "ok": memory == 0, "stamp": stamp, "file_since": file_since, "ident": ident}
        try:
            write_status(args.dest, report=report, owner=args.owner)
        except Exception as exc:
            print(f"memory status not written: {exc}", file=sys.stderr, flush=True)
    else:
        print(f"memory backup skipped: {memory_source} does not exist", file=sys.stderr, flush=True)
    return hub or memory


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
