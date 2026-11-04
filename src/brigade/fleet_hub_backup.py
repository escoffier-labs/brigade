"""Online copies of the Fleet Hub's SQLite files, their retention, and the restore tool.

``fleet backup`` copies each database file through the SQLite online backup API
into a dated directory, runs ``integrity_check`` on every copy and keeps the
newest 48 hourly copies plus the newest copy of each of the last 14 days. A copy
that would leave the backup volume under its free-space floor is not written:
the oldest copy that only the hourly window keeps goes first, and the report
says so.

``fleet restore`` turns a copy into a hub file that is safe to serve after a
lossy restore. It advances the message sequence and the roster and policy
revisions, ends every claim and writes the claim recovery floor, each 2**32
above the higher of the copy's value and the newest value in the recovery
ledger. The ledger is an append-only JSON-lines file kept outside every
database and copy, so two restores from the same copy never hand out the same
range.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import fleet_hub, fleet_hub_claims

ADVANCE = 2**32
DEFAULT_KEEP_HOURLY = 48
DEFAULT_KEEP_DAILY = 14
DEFAULT_MIN_FREE_BYTES = 20 * 2**30
# Retention sized at setup keeps the directory under this share of free space.
SETUP_FREE_SHARE = 0.10
MANIFEST = "manifest.json"
LEDGER_NAME = "recovery-ledger.jsonl"
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_STAMP = re.compile(r"^\d{8}T\d{6}Z$")
LEDGER_FIELDS = ("message_sequence", "claim_floor", "roster_revision", "policy_revision")
RESTORE_ACTOR = "fleet-restore"


class BackupError(Exception):
    """A backup or restore refused to proceed. The message says why."""


DiskUsage = Callable[[Path], Any]


def _utc(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file(path: Path) -> None:
    with open(path, "rb") as handle:
        os.fsync(handle.fileno())


Owner = tuple[int, int]


def _chown(path: Path, owner: Owner | None) -> None:
    if owner is not None and os.name != "nt":
        os.chown(path, owner[0], owner[1], follow_symlinks=False)


def ensure_private_dir(path: Path, owner: Owner | None = None) -> None:
    """Create ``path`` mode 0700, or refuse an existing one that others can read.

    ``owner`` names the account the directory must belong to when the job runs
    as root in a container; otherwise it must belong to this account.
    """
    if path.is_symlink():
        raise BackupError(f"backup directory {path} is a symlink; refusing")
    if not path.exists():
        path.mkdir(parents=True, mode=0o700)
        if os.name != "nt":
            os.chmod(path, 0o700)
        _chown(path, owner)
        return
    if not path.is_dir():
        raise BackupError(f"backup directory {path} is not a directory")
    if os.name == "nt":
        return
    info = path.stat()
    expected = owner[0] if owner is not None else os.geteuid()
    if info.st_uid != expected:
        raise BackupError(f"backup directory {path} belongs to uid {info.st_uid}, not uid {expected}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise BackupError(f"backup directory {path} has mode {stat.S_IMODE(info.st_mode):04o}; it must be 0700")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_bytes(source: Path) -> int:
    total = source.stat().st_size
    wal = source.with_name(source.name + "-wal")
    if wal.exists():
        total += wal.stat().st_size
    return total


def _tree_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file() and not item.is_symlink())


def _integrity(path: Path) -> str:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    return "ok" if rows == [("ok",)] else "; ".join(str(row[0]) for row in rows[:5])


def _online_copy(source: Path, destination: Path) -> int:
    """Copy ``source`` through the online backup API into a rollback-journal file."""
    src = sqlite3.connect(f"file:{source}?mode=rw", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
            version = int(dst.execute("PRAGMA user_version").fetchone()[0])
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    return version


@dataclass(frozen=True)
class Copy:
    stamp: str
    path: Path
    taken_at: datetime


def list_copies(dest: Path) -> list[Copy]:
    """Completed copies, oldest first. Staging directories and strays are ignored."""
    copies: list[Copy] = []
    if not dest.is_dir():
        return copies
    for entry in dest.iterdir():
        if entry.is_dir() and not entry.is_symlink() and _STAMP.match(entry.name) and (entry / MANIFEST).is_file():
            taken = datetime.strptime(entry.name, STAMP_FORMAT).replace(tzinfo=timezone.utc)
            copies.append(Copy(entry.name, entry, taken))
    return sorted(copies, key=lambda item: item.stamp)


def retained(copies: list[Copy], *, keep_hourly: int, keep_daily: int) -> tuple[set[str], set[str]]:
    """Return (hourly keeps, daily keeps) as stamps. A stamp can be in both."""
    newest_first = sorted(copies, key=lambda item: item.stamp, reverse=True)
    hourly = {item.stamp for item in newest_first[:keep_hourly]}
    daily: set[str] = set()
    days: list[str] = []
    for item in newest_first:
        day = item.stamp[:8]
        if day in days:
            continue
        if len(days) >= keep_daily:
            break
        days.append(day)
        daily.add(item.stamp)
    return hourly, daily


def plan_retention(copy_bytes: int, free_bytes: int, *, keep_daily: int = DEFAULT_KEEP_DAILY) -> dict[str, int]:
    """Hourly copies that keep the directory under 10% of free space, at most 48."""
    budget = int(free_bytes * SETUP_FREE_SHARE)
    fits = budget // max(copy_bytes, 1)
    hourly = max(1, min(DEFAULT_KEEP_HOURLY, fits - keep_daily))
    return {"copy_bytes": copy_bytes, "free_bytes": free_bytes, "budget_bytes": budget, "keep_hourly": hourly}


def take_backup(
    sources: Iterable[Path],
    dest: Path,
    *,
    keep_hourly: int = DEFAULT_KEEP_HOURLY,
    keep_daily: int = DEFAULT_KEEP_DAILY,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    now: datetime | None = None,
    disk_usage: DiskUsage = shutil.disk_usage,
    owner: Owner | None = None,
) -> dict[str, Any]:
    """Write one dated copy of every source file, then apply retention."""
    if keep_hourly < 1 or keep_daily < 0 or min_free_bytes < 0:
        raise BackupError("retention needs keep_hourly >= 1, keep_daily >= 0 and a non-negative free floor")
    files = [Path(item) for item in sources]
    if not files:
        raise BackupError("no database file to back up")
    names = [item.name for item in files]
    if len(set(names)) != len(names):
        raise BackupError("two source files share a name; each copy directory holds one file per name")
    for item in files:
        if not item.is_file():
            raise BackupError(f"database file {item} does not exist")
    ensure_private_dir(dest, owner)
    taken = _utc(now)
    stamp = taken.strftime(STAMP_FORMAT)
    final = dest / stamp
    if final.exists():
        raise BackupError(f"a copy named {stamp} already exists")
    need = sum(_source_bytes(item) for item in files)
    pruned_for_space: list[str] = []
    copies = list_copies(dest)
    hourly, daily = retained(copies, keep_hourly=keep_hourly, keep_daily=keep_daily)
    deficit = min_free_bytes + need - int(disk_usage(dest).free)
    if deficit > 0:
        # Only copies the hourly window alone keeps may go, oldest first, and
        # never the newest copy. Decide before deleting: a copy that cannot
        # be made to fit removes nothing.
        candidates = [old for old in copies[:-1] if old.stamp not in daily and old.stamp in hourly]
        chosen: list[Copy] = []
        freed = 0
        for candidate in candidates:
            if freed >= deficit:
                break
            chosen.append(candidate)
            freed += _tree_bytes(candidate.path)
        if freed < deficit:
            raise BackupError(
                f"refusing copy {stamp}: it needs about {need} bytes and would leave {dest} under "
                f"{min_free_bytes} bytes free even after removing every older hourly copy"
            )
        for old in chosen:
            shutil.rmtree(old.path)
            pruned_for_space.append(old.stamp)
        if int(disk_usage(dest).free) - need < min_free_bytes:
            raise BackupError(
                f"refusing copy {stamp}: removed {', '.join(pruned_for_space)} and {dest} is still under "
                f"{min_free_bytes} bytes free"
            )
    staging = dest / f".partial-{stamp}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(mode=0o700)
    _chown(staging, owner)
    entries = []
    try:
        for item in files:
            target = staging / item.name
            version = _online_copy(item, target)
            check = _integrity(target)
            if check != "ok":
                raise BackupError(f"integrity_check failed on the copy of {item.name}: {check}")
            if os.name != "nt":
                os.chmod(target, 0o600)
            _chown(target, owner)
            _fsync_file(target)
            entries.append(
                {
                    "name": item.name,
                    "source": str(item),
                    "bytes": target.stat().st_size,
                    "sha256": _sha256(target),
                    "user_version": version,
                    "integrity_check": check,
                }
            )
        manifest = {"schema": 1, "taken_at": taken.isoformat().replace("+00:00", "Z"), "files": entries}
        (staging / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if os.name != "nt":
            os.chmod(staging / MANIFEST, 0o600)
        _chown(staging / MANIFEST, owner)
        _fsync_file(staging / MANIFEST)
        _fsync_dir(staging)
        os.replace(staging, final)
        _fsync_dir(dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    copies = list_copies(dest)
    hourly, daily = retained(copies, keep_hourly=keep_hourly, keep_daily=keep_daily)
    removed = []
    for kept in copies:
        if kept.stamp not in hourly and kept.stamp not in daily:
            shutil.rmtree(kept.path)
            removed.append(kept.stamp)
    return {
        "copy": str(final),
        "stamp": stamp,
        "files": entries,
        "pruned_for_space": pruned_for_space,
        "removed_by_retention": removed,
        "kept": len(copies) - len(removed),
        "free_bytes": int(disk_usage(dest).free),
    }


# ------------------------------------------------------------------ restore


def _ledger_reachable(path: Path) -> bool:
    if path.exists():
        return path.is_file() and os.access(path, os.R_OK | os.W_OK)
    return path.parent.is_dir() and os.access(path.parent, os.W_OK)


def read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise BackupError(f"recovery ledger {path} line {number} is not JSON; refusing") from exc
        if not isinstance(record, dict) or any(
            type(record.get(field)) is not int or record[field] < 0 for field in LEDGER_FIELDS
        ):
            raise BackupError(f"recovery ledger {path} line {number} is missing a field; refusing")
        records.append(record)
    return records


def ledger_base(records: Iterable[dict[str, Any]]) -> dict[str, int]:
    """The highest value of each advance across every record in every ledger copy."""
    base = dict.fromkeys(LEDGER_FIELDS, 0)
    for record in records:
        for field in LEDGER_FIELDS:
            base[field] = max(base[field], int(record[field]))
    return base


def _append_ledger(path: Path, record: dict[str, Any]) -> None:
    created = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    if created:
        _fsync_dir(path.parent)


def _message_sequence(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'fleet_messages'").fetchone()
    highest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM fleet_messages").fetchone()[0]
    return max(int(row[0]) if row else 0, int(highest))


def _set_message_sequence(conn: sqlite3.Connection, value: int) -> None:
    if conn.execute("SELECT 1 FROM sqlite_sequence WHERE name = 'fleet_messages'").fetchone():
        conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'fleet_messages'", (value,))
    else:
        conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('fleet_messages', ?)", (value,))


def _advance_policy(conn: sqlite3.Connection, base: int, *, reason: str, now: str) -> int:
    from . import fleet_hub_policy

    current = fleet_hub_policy.current_policy(conn)
    revision = max(int(current["revision"]), base) + ADVANCE
    conn.execute(
        f"INSERT INTO fleet_policy_revisions ({fleet_hub_policy._REVISION_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            revision,
            current["schema"],
            current["digest"],
            fleet_hub_policy.fleet_policy.canonical_json(current["document"]),
            RESTORE_ACTOR,
            reason,
            now,
            int(current["revision"]),
        ),
    )
    conn.execute(
        "UPDATE fleet_policy_meta SET current_revision = ?, updated_at = ?, updated_by = ? WHERE singleton = 1",
        (revision, now, RESTORE_ACTOR),
    )
    return revision


def _advance_roster(conn: sqlite3.Connection, base: int, *, policy_revision: int, now: str) -> int:
    from . import fleet_hub_model_roster, fleet_policy_migration

    if fleet_policy_migration.is_activated(conn):
        # The activated roster is projected from the policy, so its revision
        # is the policy revision just advanced. The raw table is write-guarded.
        return policy_revision
    revision = max(fleet_hub_model_roster.raw_revision(conn), base) + ADVANCE
    conn.execute(
        "UPDATE model_roster_meta SET revision = ?, updated_at = ?, updated_by = ? WHERE singleton = 1",
        (revision, now, RESTORE_ACTOR),
    )
    return revision


def restore(
    copy: Path,
    target: Path,
    *,
    ledgers: Iterable[Path],
    assert_no_prior_restore: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Restore ``copy`` to ``target`` and advance every rewindable counter past any earlier restore."""
    copy, target = Path(copy), Path(target)
    ledger_paths = [Path(item) for item in ledgers]
    if not ledger_paths:
        raise BackupError("name at least one recovery ledger with --ledger (beside the copies and on the hub host)")
    if not copy.is_file():
        raise BackupError(f"copy {copy} is not a file")
    for path in (target, target.with_name(target.name + "-wal"), target.with_name(target.name + "-shm")):
        if path.exists():
            raise BackupError(f"{path} already exists; restore only into an absent hub file")
    if not target.parent.is_dir():
        raise BackupError(f"target directory {target.parent} does not exist")
    check = _integrity(copy)
    if check != "ok":
        raise BackupError(f"integrity_check failed on copy {copy}: {check}")
    reachable = [path for path in ledger_paths if _ledger_reachable(path)]
    unreachable = [str(path) for path in ledger_paths if path not in reachable]
    if not reachable and not assert_no_prior_restore:
        raise BackupError(
            "no recovery ledger is reachable; refusing. If no earlier restore from this copy happened, "
            "say so with --assert-no-prior-restore and the statement is recorded in the new ledger record"
        )
    records: list[dict[str, Any]] = []
    for path in reachable:
        records.extend(read_ledger(path))
    base = ledger_base(records)
    copy_digest = _sha256(copy)
    taken = _utc(now)
    stamp = taken.isoformat().replace("+00:00", "Z")
    staging = target.with_name(target.name + ".restoring")
    for path in (staging, staging.with_name(staging.name + "-wal"), staging.with_name(staging.name + "-shm")):
        if path.exists():
            path.unlink()
    try:
        _online_copy(copy, staging)
        conn = fleet_hub.init_db(staging)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                copied_sequence = _message_sequence(conn)
                sequence = max(copied_sequence, base["message_sequence"]) + ADVANCE
                _set_message_sequence(conn, sequence)
                ended = fleet_hub_claims.end_all_for_restore(conn, taken.timestamp())
                floor = fleet_hub_claims.write_recovery_floor(
                    conn, base["claim_floor"], advance=ADVANCE, restored_at=stamp, copy_sha256=copy_digest
                )
                reason = f"restore from copy sha256:{copy_digest[:16]}"
                policy = _advance_policy(conn, base["policy_revision"], reason=reason, now=stamp)
                roster = _advance_roster(conn, base["roster_revision"], policy_revision=policy, now=stamp)
                record = {
                    "time": stamp,
                    "copy": {"path": str(copy), "sha256": copy_digest},
                    "message_sequence": sequence,
                    "claim_floor": floor,
                    "roster_revision": roster,
                    "policy_revision": policy,
                    "no_prior_restore_asserted": bool(assert_no_prior_restore),
                }
                # The ledger record is durable before the restored file is: a
                # record whose restore never committed only raises the next base.
                written = []
                for path in reachable or ledger_paths:
                    _append_ledger(path, record)
                    written.append(str(path))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            conn.close()
        if _integrity(staging) != "ok":
            raise BackupError("the restored file failed integrity_check after the advances")
        _fsync_file(staging)
        os.replace(staging, target)
        _fsync_dir(target.parent)
    except BaseException:
        for path in (staging, staging.with_name(staging.name + "-wal"), staging.with_name(staging.name + "-shm")):
            if path.exists():
                path.unlink()
        raise
    return {
        "target": str(target),
        "copy": str(copy),
        "copy_sha256": copy_digest,
        "copied_message_sequence": copied_sequence,
        "message_sequence": sequence,
        "claims_ended": ended,
        "claim_floor": floor,
        "roster_revision": roster,
        "policy_revision": policy,
        "ledgers_written": written,
        "ledgers_unreachable": unreachable,
        "next_steps": [
            "each mesh signer publishes its newest membership, manifest and revocation statements again",
            "re-apply every credential revocation, roster change and policy edit made since the copy",
        ],
    }
