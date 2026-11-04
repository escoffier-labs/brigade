"""`brigade fleet backup` and `brigade fleet restore`: hourly online copies and the lossy-restore tool."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .. import fleet_hub, fleet_hub_backup as backup

DEFAULT_DB_REL_PATH = Path(".brigade") / "fleet-hub.db"


def register(sub: argparse._SubParsersAction) -> None:
    p_backup = sub.add_parser(
        "backup",
        help="Copy the hub's SQLite files through the online backup API into a dated, integrity-checked directory.",
    )
    p_backup.add_argument(
        "--db",
        type=Path,
        action="append",
        default=None,
        help="Database file to copy; repeat for the memory file (default ~/.brigade/fleet-hub.db).",
    )
    p_backup.add_argument("--dest", type=Path, required=True, help="Backup directory (created mode 0700).")
    p_backup.add_argument("--keep-hourly", type=int, default=backup.DEFAULT_KEEP_HOURLY)
    p_backup.add_argument("--keep-daily", type=int, default=backup.DEFAULT_KEEP_DAILY)
    p_backup.add_argument(
        "--min-free-gib",
        type=float,
        default=backup.DEFAULT_MIN_FREE_BYTES / 2**30,
        help="Refuse a copy that would leave the backup volume under this much free space (default 20).",
    )
    p_backup.add_argument(
        "--plan",
        action="store_true",
        help="Print the file sizes and the hourly retention that fits 10%% of free space, and write nothing.",
    )
    p_backup.add_argument(
        "--owner",
        type=_owner,
        default=None,
        metavar="UID:GID",
        help="Account the copies belong to, when the job runs as root in a container (POSIX only).",
    )
    p_backup.add_argument("--json", action="store_true", help="Emit JSON.")
    p_backup.set_defaults(func=_dispatch_backup)

    p_restore = sub.add_parser(
        "restore",
        help="Restore a backup copy to an absent hub file and advance every counter a lossy restore could rewind.",
    )
    p_restore.add_argument("--copy", type=Path, required=True, help="The hub database file inside a backup copy.")
    p_restore.add_argument("--db", type=Path, required=True, help="Hub database path to create (must not exist).")
    p_restore.add_argument(
        "--ledger",
        type=Path,
        action="append",
        required=True,
        help="Recovery ledger file; repeat it for the copy beside the backups and the one on the hub host.",
    )
    p_restore.add_argument(
        "--assert-no-prior-restore",
        action="store_true",
        help="State that no earlier restore from this copy happened, when no ledger can be reached.",
    )
    p_restore.add_argument("--json", action="store_true", help="Emit JSON.")
    p_restore.set_defaults(func=_dispatch_restore)


def _owner(value: str) -> tuple[int, int]:
    uid, sep, gid = value.partition(":")
    if not sep or not uid.isdigit() or not gid.isdigit():
        raise argparse.ArgumentTypeError("expected UID:GID, two non-negative integers")
    return int(uid), int(gid)


def _emit(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    for key in sorted(result):
        print(f"{key}: {result[key]}")


def _dispatch_backup(args: argparse.Namespace) -> int:
    sources = args.db or [Path.home() / DEFAULT_DB_REL_PATH]
    try:
        if args.plan:
            missing = [str(item) for item in sources if not item.is_file()]
            if missing:
                raise backup.BackupError(f"database file {', '.join(missing)} does not exist")
            copy_bytes = sum(item.stat().st_size for item in sources)
            probe = args.dest if args.dest.exists() else args.dest.parent
            plan: dict[str, Any] = dict(
                backup.plan_retention(copy_bytes, int(shutil.disk_usage(probe).free), keep_daily=args.keep_daily)
            )
            plan["files"] = {str(item): item.stat().st_size for item in sources}
            result = plan
        else:
            result = backup.take_backup(
                sources,
                args.dest,
                keep_hourly=args.keep_hourly,
                keep_daily=args.keep_daily,
                min_free_bytes=int(args.min_free_gib * 2**30),
                owner=args.owner,
            )
    except (backup.BackupError, sqlite3.Error, OSError) as exc:
        print(f"error: fleet backup: {exc}", file=sys.stderr)
        return 1
    _emit(result, args.json)
    return 0


def _dispatch_restore(args: argparse.Namespace) -> int:
    try:
        result = backup.restore(
            args.copy, args.db, ledgers=args.ledger, assert_no_prior_restore=args.assert_no_prior_restore
        )
    except (backup.BackupError, fleet_hub.FleetHubError, sqlite3.Error, OSError) as exc:
        print(f"error: fleet restore: {exc}", file=sys.stderr)
        return 1
    _emit(result, args.json)
    return 0
