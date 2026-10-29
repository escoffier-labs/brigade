"""Opt-in renewal of last-published local sessions with a proven live owner."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Callable, Sequence

from . import dirfd, fleet_client, proc
from .claude_hooks import anchored_io
from .fleet_openclaw_observer import ObservationError, _remaining, _require_node_auth
from .fleet_session_heartbeat_state import (
    LOCK_SECONDS,
    OBSOLETE_GRACE_SECONDS,
    SUFFIX,
    read_record,
    snapshot_from_record,
)
from .fleet_session_presence import SessionSnapshot, _hub_url_configured
from .fleet_session_process import ProcessTable
from .wiring import resolve_wired_target

SESSION_LIMIT = 200
PUBLISH_LIMIT = 50
DIRECTORY_LIMIT = 2048
TARGET_LIMIT = 32
WORKTREE_ENTRY_LIMIT = 512
WORKTREE_TIMEOUT_SECONDS = 2
OBSERVATION_BUDGET_SECONDS = 15
Publisher = Callable[[SessionSnapshot], fleet_client.SessionWriteResult]


def _ordered_records(anchor: anchored_io.SessionDirectory, names: list[str], deadline: float) -> list[str]:
    """Prefer recent hook activity within the existing entry and time bounds."""
    candidates = []
    for name in names:
        _remaining(deadline, cap=1)
        if not name.endswith(SUFFIX):
            continue
        try:
            with anchor.open_file(name, os.O_RDONLY) as fd:
                candidates.append((os.fstat(fd).st_mtime_ns, name))
        except OSError:
            continue
    return [name for _, name in sorted(candidates, reverse=True)]


def _prune_record(
    anchor: anchored_io.SessionDirectory, name: str, snapshot: SessionSnapshot, *, enabled: bool, deadline: float
) -> None:
    """Caller holds the publication lock and has ruled out a proven live owner."""
    _remaining(deadline, cap=1)
    with anchor.open_file(name, os.O_RDONLY) as fd:
        info = anchor.validate_file(name, fd)
        if not enabled or time.time() - info.st_mtime > snapshot.ttl_seconds + OBSOLETE_GRACE_SECONDS:
            anchor.validate()
            # Keep the lock inode: unlinking it could split concurrent lockers.
            dirfd.unlink_child(anchor.fd, name)


def _presence_mtime(target: Path, deadline: float) -> tuple[int | None, bool]:
    """Inspect held, no-follow sidecars without reading their contents."""
    newest = None
    truncated = False
    try:
        with anchored_io.session_directory(target) as anchor:
            names = dirfd.child_names(anchor.fd, DIRECTORY_LIMIT + 1)
            truncated = len(names) > DIRECTORY_LIMIT
            for name in names[:DIRECTORY_LIMIT]:
                _remaining(deadline, cap=1)
                if name.endswith(SUFFIX):
                    try:
                        with anchor.open_file(name, os.O_RDONLY) as fd:
                            mtime = os.fstat(fd).st_mtime_ns
                            newest = mtime if newest is None else max(newest, mtime)
                    except OSError:
                        continue
    except OSError:
        return None, truncated
    return newest, truncated


def _discover_worktrees(repositories: Sequence[Path], deadline: float) -> tuple[list[Path], int, bool]:
    """Bound Git output, repository queries, entries, and filesystem inspection."""
    candidates: dict[Path, int] = {}
    seen_repositories: set[Path] = set()
    entries = errors = 0
    truncated = False
    for repository in repositories:
        _remaining(deadline, cap=1)
        if entries >= WORKTREE_ENTRY_LIMIT or len(seen_repositories) >= TARGET_LIMIT:
            truncated = True
            break
        try:
            repository = repository.expanduser().resolve()
            if repository in seen_repositories:
                continue
            seen_repositories.add(repository)
            result = proc.run(
                ["git", "-C", str(repository), "worktree", "list", "--porcelain", "-z"],
                timeout=_remaining(deadline, cap=WORKTREE_TIMEOUT_SECONDS),
            )
        except (OSError, RuntimeError):
            errors += 1
            continue
        if (
            result.code != 0
            or result.decode_failed
            or result.output_limit_exceeded
            or result.stream_limit_exceeded
            or result.incomplete_process_group
        ):
            errors += 1
            continue
        remaining = WORKTREE_ENTRY_LIMIT - entries
        records = result.stdout.split("\0\0", remaining)
        if len(records) > remaining and records[-1]:
            truncated = True
        for record in records[:remaining]:
            _remaining(deadline, cap=1)
            if not record:
                continue
            entries += 1
            fields = record.split("\0")
            if not fields[0].startswith("worktree ") or any(
                field == "bare" or field == "prunable" or field.startswith("prunable ") for field in fields[1:]
            ):
                continue
            try:
                target = Path(fields[0][9:])
                if not target.is_absolute() or target.is_symlink() or not target.is_dir():
                    continue
                target = target.resolve()
                if target in candidates or resolve_wired_target(str(target), harness=None) != target:
                    continue
                mtime, capped = _presence_mtime(target, deadline)
                truncated |= capped
                if mtime is not None:
                    candidates[target] = mtime
            except ObservationError:
                raise
            except (OSError, RuntimeError, ValueError):
                continue
    return sorted(candidates, key=lambda target: (-candidates[target], str(target))), errors, truncated


def observe(
    targets: Sequence[Path],
    *,
    publish: bool = False,
    worktrees_of: Sequence[Path] = (),
    processes: ProcessTable | None = None,
    publisher: Publisher | None = None,
) -> dict[str, object]:
    deadline = time.monotonic() + OBSERVATION_BUDGET_SECONDS
    report: dict[str, object] = {
        "ok": True,
        "mode": "publish" if publish else "inspect",
        "observed": 0,
        "live": 0,
        "published": 0,
        "skipped": 0,
        "discovered_targets": 0,
        "discovery_errors": 0,
        "truncated": False,
        "session_limit": SESSION_LIMIT,
        "publish_limit": PUBLISH_LIMIT,
        "budget_seconds": OBSERVATION_BUDGET_SECONDS,
    }
    if publish:
        _require_node_auth()
    if not _hub_url_configured():
        return {**report, "ok": False, "error": "hub_unconfigured"}
    table = processes or ProcessTable()
    send = publisher or fleet_client.publish_session
    observed = live = published = attempted = 0
    seen: set[Path] = set()
    try:
        discovered, discovery_errors, discovery_truncated = _discover_worktrees(worktrees_of, deadline)
        report.update(
            discovered_targets=len(discovered), discovery_errors=discovery_errors, truncated=discovery_truncated
        )
        targets = [*targets, *discovered]
        for target_index, raw_target in enumerate(targets):
            _remaining(deadline, cap=1)
            target = raw_target.expanduser().resolve()
            if target in seen:
                continue
            if len(seen) >= TARGET_LIMIT:
                report["truncated"] = True
                break
            seen.add(target)
            try:
                with anchored_io.session_directory(target) as anchor:
                    names = dirfd.child_names(anchor.fd, DIRECTORY_LIMIT + 1)
                    if len(names) > DIRECTORY_LIMIT:
                        report["truncated"] = True
                    for name in _ordered_records(anchor, names[:DIRECTORY_LIMIT], deadline):
                        _remaining(deadline, cap=1)
                        if observed >= SESSION_LIMIT or (publish and attempted >= PUBLISH_LIMIT):
                            report["truncated"] = True
                            break
                        try:
                            with anchor.held_lock(
                                name + ".lock", deadline_seconds=_remaining(deadline, cap=LOCK_SECONDS)
                            ):
                                # Every probe reads at most MAX_RECORD_BYTES + 1,
                                # bounded separately by DIRECTORY_LIMIT and deadline.
                                record = read_record(anchor, name)
                                if record is None:
                                    continue
                                enabled = record["enabled"]
                                if enabled:
                                    observed += 1
                                snapshot = snapshot_from_record(record, target, name)
                                if snapshot is None:
                                    continue
                                owner_alive = table.alive(record.get("process"), snapshot.harness)
                                if not owner_alive:
                                    _prune_record(anchor, name, snapshot, enabled=enabled, deadline=deadline)
                                    continue
                                if not enabled or resolve_wired_target(str(target), harness=snapshot.harness) != target:
                                    continue
                                live += 1
                                if publish:
                                    _remaining(deadline, cap=1, reserve=fleet_client.REPORT_TIMEOUT_SECONDS)
                                    anchor.validate()
                                    attempted += 1
                                    if not send(snapshot).ok:
                                        raise ObservationError("publication_failed")
                                    published += 1
                        except ObservationError:
                            raise
                        except (OSError, ValueError, KeyError, TypeError):
                            continue
            except FileNotFoundError:
                continue
            if observed >= SESSION_LIMIT or (publish and attempted >= PUBLISH_LIMIT):
                if target_index + 1 < len(targets):
                    report["truncated"] = True
                break
    except ObservationError as exc:
        report.update(ok=False, error=str(exc))
        if str(exc) == "observation_timeout":
            report["truncated"] = True
    except Exception:
        report.update(ok=False, error="observation_failed")
    report.update(observed=observed, live=live, published=published, skipped=observed - published)
    report["ok"] = not (report.get("error") or report["truncated"] or report["discovery_errors"])
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, action="append", default=[], help="Wired checkout (repeatable)")
    parser.add_argument(
        "--worktrees-of", type=Path, action="append", default=[], help="Discover wired worktrees (repeatable)"
    )
    parser.add_argument("--publish", action="store_true", help="Renew through this enrolled node")
    args = parser.parse_args(argv)
    if not args.target and not args.worktrees_of:
        parser.error("provide --target or --worktrees-of")
    try:
        report = observe(args.target, worktrees_of=args.worktrees_of, publish=args.publish)
    except ObservationError as exc:
        report = {"ok": False, "error": str(exc), "observed": 0, "published": 0, "skipped": 0}
    except Exception:
        report = {"ok": False, "error": "observation_failed", "observed": 0, "published": 0, "skipped": 0}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 2 if report.get("truncated") else 1


if __name__ == "__main__":
    raise SystemExit(main())
