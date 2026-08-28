"""Explicit local reaper for runs whose recorded owner process is gone."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import proc, run_checkpoint, run_lifecycle, runguard, runs_cmd

SCHEMA = "brigade.runs-reap.v1"
DEFAULT_OLDER_THAN = "2h"
MAX_UNCOMMITTED_CHANGE_COUNT = 10_000
# Bounded scan for the work-brief surface, matched to doctor's
# ``_RECOVERY_CHECKPOINTS_SCAN_LIMIT`` so a brief on a repo with no orphan
# never parses unbounded run history.
ORPHAN_SCAN_LIMIT = 50
_OLDER_THAN_RE = re.compile(r"^(\d+)([smhd])$", re.IGNORECASE)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_REAPABLE_STATUSES = runs_cmd._NONTERMINAL_STATUSES


class ReapError(ValueError):
    """Bounded CLI / contract error for the local reaper."""


def parse_older_than(value: str) -> timedelta:
    raw = value.strip()
    match = _OLDER_THAN_RE.fullmatch(raw)
    if match is None:
        raise ValueError("older-than must look like 2h, 30m, 90s, or 1d")
    amount = int(match.group(1))
    seconds = amount * _UNITS[match.group(2).lower()]
    if seconds <= 0:
        raise ValueError("older-than must be a positive duration")
    return timedelta(seconds=seconds)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _run_fingerprint(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _porcelain_path(line: str) -> str:
    if len(line) < 4:
        return ""
    path = line[3:]
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    if len(path) >= 2 and path[0] == path[-1] == '"':
        path = path[1:-1]
    return path


def _is_brigade_runtime_path(path: str) -> bool:
    return path == ".brigade" or path.startswith(".brigade/")


def _count_uncommitted_changes(workspace: Path) -> int:
    result = proc.run(["git", "status", "--porcelain=v1"], cwd=workspace)
    if result.code != 0:
        return 0
    count = 0
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        if _is_brigade_runtime_path(_porcelain_path(line)):
            continue
        count += 1
    return min(count, MAX_UNCOMMITTED_CHANGE_COUNT)


def _utc_now(now: datetime | None) -> datetime:
    clock = now if now is not None else datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return clock.astimezone(timezone.utc)


def _read_run_bytes(run_dir: Path) -> tuple[bytes | None, dict[str, Any] | None]:
    path = run_dir / "run.json"
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    return raw, payload


def _skip(run_id: str, reason: str) -> dict[str, str]:
    return {"run_id": run_id, "reason": reason}


def _classify_child(child: Path, resolved_root: Path) -> str | None:
    try:
        if child.is_symlink():
            return "symlink"
        if not child.is_dir():
            return None
        if not runs_cmd._run_dir_is_contained(child, resolved_root):
            return "malformed"
    except OSError:
        return "malformed"
    return None


def _revalidate_metadata(
    meta: dict[str, Any],
    *,
    older_than: timedelta,
    now: datetime,
) -> str | None:
    """CAS-time run.json checks. Does not inspect lock ownership or liveness."""
    status = meta.get("status")
    if status == "orphaned":
        return "already-orphaned"
    if not isinstance(status, str) or status not in _REAPABLE_STATUSES or meta.get("finished_at"):
        return "terminal"
    started = _parse_timestamp(meta.get("started_at"))
    if started is None:
        return "malformed"
    if now - started < older_than:
        return "too-young"
    return None


def _preflight(
    run_dir: Path,
    meta: dict[str, Any],
    *,
    older_than: timedelta,
    now: datetime,
) -> str | None:
    reason = _revalidate_metadata(meta, older_than=older_than, now=now)
    if reason is not None:
        return reason
    workspace = runguard.resolve_run_lock_workspace(meta, run_dir)
    if workspace is None:
        return "ambiguous"
    state = runguard.run_lock_state(workspace, run_dir)
    if state == "live":
        return "live"
    if state != "stale":
        return "ambiguous"
    return None


def _terminalize(run_dir: Path, meta: dict[str, Any], *, workspace: Path, now: datetime) -> dict[str, Any]:
    """Write the orphaned snapshot through the sanctioned run.json writer.

    ``aboyeur._write_json`` owns the whole transaction: it activates the
    lifecycle journal only when this run durably requested it, publishes the
    recovery checkpoint paired with the ``run.orphaned`` event, appends the
    lifecycle transition, records shadow parity, and atomically replaces
    ``run.json`` (projecting it for authority-requested and authoritative
    runs). The reaper holds the matching run lock for ``run_dir``, which is
    what every one of those steps verifies ownership against.

    A legacy snapshot-only run is never migrated: no journal is manufactured
    here, so ``brigade doctor``'s recovery-checkpoint check keeps omitting it
    as ``no-journal`` instead of failing on a journal with no checkpoint.
    """
    from . import aboyeur, receipt_schema

    last_status = meta.get("status")
    if not isinstance(last_status, str) or not last_status:
        last_status = "unknown"
    dirty = _count_uncommitted_changes(workspace)
    orphaned_at = now.isoformat()
    updated = dict(meta)
    updated.update(
        {
            "status": "orphaned",
            "orphaned_at": orphaned_at,
            "last_observed_status": last_status,
            "uncommitted_change_count": dirty,
            "finished_at": orphaned_at,
        }
    )
    aboyeur._write_json(run_dir / "run.json", receipt_schema.stamp_run_receipt(updated))
    return {
        "run_id": run_dir.name,
        "last_observed_status": last_status,
        "uncommitted_change_count": dirty,
        "orphaned_at": orphaned_at,
    }


def _reap_one(
    run_dir: Path,
    meta: dict[str, Any],
    *,
    fingerprint: str,
    older_than: timedelta,
    now: datetime,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    reason = _preflight(run_dir, meta, older_than=older_than, now=now)
    if reason is not None:
        return None, _skip(run_dir.name, reason)
    workspace = runguard.resolve_run_lock_workspace(meta, run_dir)
    assert workspace is not None
    try:
        with runguard.run_lock(workspace, run_dir=run_dir, wait_seconds=0, stale_action="claim"):
            raw, current = _read_run_bytes(run_dir)
            if raw is None or current is None:
                return None, _skip(run_dir.name, "concurrently-changed")
            if _run_fingerprint(raw) != fingerprint:
                return None, _skip(run_dir.name, "concurrently-changed")
            # Under the reaper's newly claimed live lock, re-check run.json
            # only. Original-owner liveness was already proven before acquire.
            reason = _revalidate_metadata(current, older_than=older_than, now=now)
            if reason is not None:
                return None, _skip(run_dir.name, reason)
            try:
                return _terminalize(run_dir, current, workspace=workspace, now=now), None
            except (run_lifecycle.LifecycleJournalError, run_checkpoint.CheckpointError):
                # The sanctioned writer refused this run's transaction (an
                # unready authority gate, a broken chain). Skip it rather than
                # abandoning the rest of the scan; the diagnostic is bounded
                # but may name a path, so it stays out of the contract.
                return None, _skip(run_dir.name, "write-refused")
    except runguard.RunLockError:
        return None, _skip(run_dir.name, "concurrently-changed")


def list_orphaned_runs(
    target: Path,
    *,
    limit: int = 8,
    scan_limit: int = ORPHAN_SCAN_LIMIT,
) -> list[dict[str, Any]]:
    """Newest orphaned runs, over a bounded window of run directories.

    ``limit`` bounds the rows returned; ``scan_limit`` bounds how many run
    directories are opened at all, so a repo with hundreds of runs and no
    orphan costs the work brief a fixed number of ``run.json`` parses.
    """
    root = target.expanduser().resolve() / ".brigade" / "runs"
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    resolved_root = runs_cmd._resolved_runs_root(root)
    scanned = 0
    for child in sorted(root.iterdir(), key=lambda path: path.name, reverse=True):
        if scanned >= scan_limit:
            break
        if _classify_child(child, resolved_root) is not None:
            continue
        scanned += 1
        _raw, meta = _read_run_bytes(child)
        if meta is None or meta.get("status") != "orphaned":
            continue
        dirty = meta.get("uncommitted_change_count")
        count = dirty if isinstance(dirty, int) and not isinstance(dirty, bool) and dirty >= 0 else 0
        rows.append(
            {
                "run_id": child.name,
                "last_observed_status": meta.get("last_observed_status"),
                "uncommitted_change_count": min(count, MAX_UNCOMMITTED_CHANGE_COUNT),
                "suggested_command": f"brigade runs show {child.name}",
            }
        )
        if len(rows) >= limit:
            break
    return rows


def reap(
    *,
    cwd: Path,
    runs_dir: Path | None,
    older_than: str = DEFAULT_OLDER_THAN,
    json_output: bool = False,
    now: datetime | None = None,
) -> int:
    try:
        threshold = parse_older_than(older_than)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    cwd = cwd.expanduser().resolve()
    if not cwd.is_dir():
        print(f"error: --cwd is not a directory: {cwd}", file=sys.stderr)
        return 2
    root = runs_dir.expanduser() if runs_dir is not None else cwd / ".brigade" / "runs"
    if not root.is_dir():
        print(f"error: runs directory not found: {root}", file=sys.stderr)
        return 2

    clock = _utc_now(now)
    resolved_root = runs_cmd._resolved_runs_root(root)
    reaped: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for child in sorted(root.iterdir(), key=lambda path: path.name):
        classified = _classify_child(child, resolved_root)
        if classified == "symlink":
            skipped.append(_skip(child.name, "symlink"))
            continue
        if classified == "malformed":
            skipped.append(_skip(child.name, "malformed"))
            continue
        if classified is not None or not child.is_dir():
            continue
        raw, meta = _read_run_bytes(child)
        if raw is None or meta is None:
            skipped.append(_skip(child.name, "malformed"))
            continue
        won, lost = _reap_one(
            child,
            meta,
            fingerprint=_run_fingerprint(raw),
            older_than=threshold,
            now=clock,
        )
        if won is not None:
            reaped.append(won)
        elif lost is not None:
            skipped.append(lost)

    if json_output:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "older_than": older_than,
                    "reaped": reaped,
                    "skipped": skipped,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(f"reaped: {len(reaped)}")
    for row in reaped:
        print(f"  {row['run_id']} {row['last_observed_status']} -> orphaned dirty={row['uncommitted_change_count']}")
    if skipped:
        print(f"skipped: {len(skipped)}")
        for row in skipped:
            print(f"  {row['run_id']} {row['reason']}")
    return 0
