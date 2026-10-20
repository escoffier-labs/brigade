"""Read-only, node-local lane evidence and patrol classification.

``intent`` is reserved for the dispatcher's task-to-run join. Collection never
repairs a lock, refreshes a claim, activates a journal, or updates a Git index.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import proc, run_journal, run_reap, runs_cmd, runguard, worker_failure
from .work_cmd.claiming import claim_record

STATES = frozenset(
    {
        "complete",
        "failed",
        "orphaned",
        "zombie",
        "dead",
        "stalled",
        "idle",
        "working",
        "claim-without-run",
        "stranded",
        "lost-worktree",
        "waiting",
        "unknown",
    }
)
FILE_BYTE_CAP = 1024 * 1024
PATH_CAP = 200
MAX_LIMIT = 1000


@dataclass(frozen=True)
class Thresholds:
    older_than: timedelta = timedelta(hours=2)
    idle_after: timedelta = timedelta(minutes=15)
    stall_after: timedelta = timedelta(minutes=30)
    stranded_after: timedelta = timedelta(hours=24)
    zombie_after: timedelta = timedelta(seconds=60)
    claim_grace: timedelta = timedelta(hours=1)
    evidence_max_age: timedelta = timedelta(seconds=60)

    def as_dict(self) -> dict[str, float]:
        return {name: value.total_seconds() for name, value in vars(self).items()}


@dataclass(frozen=True)
class Evidence:
    source: str
    observed_at: datetime
    detail: str
    timestamp: datetime | None = None
    digest: str | None = None


@dataclass
class Lane:
    lane_id: str
    run_dir: Path | None = None
    run: dict[str, Any] | None = None
    intent: dict[str, Any] | None = None
    task: dict[str, Any] | None = None
    claim: dict[str, Any] | None = None
    worktree: Path | None = None
    worktree_missing: bool = False
    dirty: bool = False
    lock_state: str = "absent"
    owner_live: bool | None = None
    terminal_status: str | None = None
    terminal_at: datetime | None = None
    phase: str | None = None
    timeout_seconds: float | None = None
    progress: list[datetime] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Classification:
    state: str
    reason: str
    evidence: tuple[Evidence, ...]
    infrastructure_failure: bool | None = None


def _timestamp(value: object) -> datetime | None:
    return run_reap._parse_timestamp(value)


def _read_json(path: Path) -> tuple[dict[str, Any], str]:
    """Bound allocations and refuse symlinks, devices, and raced FIFOs."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > FILE_BYTE_CAP:
        raise ValueError("not a bounded regular JSON file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("JSON file identity changed")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(FILE_BYTE_CAP + 1)
        if len(raw) > FILE_BYTE_CAP:
            raise ValueError("JSON byte cap exceeded")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("JSON must be an object")
        return payload, hashlib.sha256(raw).hexdigest()
    finally:
        os.close(fd)


def _git(target: Path, *args: str) -> proc.Result:
    return proc.run(
        ["git", "--no-optional-locks", *args], cwd=target, timeout=10, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    )


def _linked_worktrees(target: Path, limit: int) -> tuple[set[Path], str | None]:
    result = _git(target, "worktree", "list", "--porcelain", "-z")
    if result.code != 0 or result.output_limit_exceeded:
        return set(), "linked worktrees unavailable"
    paths = [Path(part[9:]).resolve() for part in result.stdout.split("\0") if part.startswith("worktree ")]
    if len(paths) > limit:
        return set(paths[:limit]), "linked worktree limit exceeded"
    return set(paths), None


def _worktree_progress(lane: Lane, now: datetime) -> None:
    path = lane.worktree
    if path is None:
        return
    lane.evidence.append(Evidence("worktree", now, "recorded worktree observation"))
    if path.is_symlink():
        lane.errors.append("symlinked worktree")
        return
    if not path.exists():
        lane.worktree_missing = True
        lane.evidence.append(Evidence("worktree", now, "recorded path missing"))
        return
    result = _git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if result.code != 0 or result.output_limit_exceeded:
        lane.errors.append("worktree status unavailable")
        return
    parts = iter(result.stdout.split("\0"))
    paths: list[str] = []
    for entry in parts:
        if not entry:
            continue
        name = entry[3:]
        if entry[:1] in {"R", "C"} or entry[1:2] in {"R", "C"}:
            next(parts, None)  # porcelain -z emits the old name separately
        if name == ".brigade" or name.startswith(".brigade/"):
            continue
        if len(paths) >= PATH_CAP:
            lane.errors.append("worktree path cap exceeded")
            break
        paths.append(name)
    lane.dirty = bool(paths)
    newest: datetime | None = None
    for name in paths:
        candidate = path / name
        try:
            # Never follow a changed symlink or a parent outside this worktree.
            if not candidate.resolve().is_relative_to(path.resolve()):
                raise ValueError("changed path outside worktree")
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                continue
            stamp = datetime.fromtimestamp(info.st_mtime, timezone.utc)
            newest = max(newest, stamp) if newest else stamp
        except FileNotFoundError:
            continue  # deletion has no file mtime
        except (OSError, ValueError, RuntimeError):
            lane.errors.append("changed path unavailable")
    if newest is not None:
        lane.progress.append(newest)
    lane.evidence.append(Evidence("worktree:git-status", now, f"{len(paths)} changed paths", newest))


def _journal(lane: Lane, now: datetime) -> None:
    assert lane.run_dir is not None and lane.run is not None
    path = lane.run_dir / "events/lifecycle.jsonl"
    if not path.exists() and not path.is_symlink():
        requested = lane.run.get("lifecycle_journal_requested") is True
        lane.evidence.append(
            Evidence("events/lifecycle.jsonl", now, "missing" if requested else "legacy run, no journal")
        )
        if requested:
            lane.errors.append("requested journal missing")
        return
    info = run_journal.bound_lstat(path)
    if info is None or not stat.S_ISREG(info.st_mode):
        raise ValueError("journal is not a regular file")
    report = run_journal.read_journal_bounded(path)
    if report.chain_errors or report.partial_tail is not None or not report.events:
        raise ValueError("corrupt or empty lifecycle journal")
    if any(event.run_id != lane.run_dir.name for event in report.events):
        raise ValueError("journal run identity mismatch")
    last = report.events[-1]
    stamp = _timestamp(last.recorded_at)
    if stamp is None:
        raise ValueError("invalid journal timestamp")
    lane.progress.append(stamp)
    lane.evidence.append(Evidence("events/lifecycle.jsonl", now, last.event_type, stamp, last.event_digest))
    terminal = next(
        (event for event in report.events if event.event_type in runs_cmd._TERMINAL_LIFECYCLE_EVENT_TYPES), None
    )
    if terminal is not None:
        lane.terminal_status = {"run.completed": "ok", "run.orphaned": "orphaned"}.get(terminal.event_type, "failed")
        lane.terminal_at = _timestamp(terminal.recorded_at)
    # The journal wins over a snapshot left behind by a crash.
    phase = None
    for event in report.events:
        if event.event_type == "run.paused":
            phase = "paused"
        elif event.event_type == "run.resumed":
            phase = "running"
    if phase is not None:
        lane.phase = phase


def _events_progress(lane: Lane, now: datetime) -> None:
    assert lane.run_dir is not None
    root = lane.run_dir / "events"
    if root.is_symlink():
        raise ValueError("invalid events directory")
    if not root.exists():
        return
    if not root.is_dir():
        raise ValueError("invalid events directory")
    with os.scandir(root) as entries:
        for index, entry in enumerate(entries):
            if index >= PATH_CAP:
                raise ValueError("events file cap exceeded")
            if entry.name == "lifecycle.jsonl" or not entry.name.endswith(".jsonl"):
                continue
            info = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("invalid event signal")
            stamp = datetime.fromtimestamp(info.st_mtime, timezone.utc)
            lane.progress.append(stamp)
            lane.evidence.append(Evidence(f"events/{entry.name}", now, "app-server event mtime", stamp))


def _lock(lane: Lane, now: datetime) -> None:
    assert lane.run_dir is not None and lane.run is not None
    workspace = runguard.resolve_run_lock_workspace(lane.run, lane.run_dir)
    if workspace is None:
        raise ValueError("lock workspace unavailable")
    path = runguard.lock_path(workspace)
    if path.is_symlink():
        raise ValueError("symlinked lock")
    if path.exists():
        owner, digest = _read_json(path / "owner.json")
        pid = owner.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ValueError("invalid owner pid")
        if not runguard._owner_matches_run(owner, lane.run_dir):
            lane.lock_state = "foreign"
            lane.evidence.append(Evidence("run.lock/owner.json", now, "belongs to another run", digest=digest))
            return
        lane.lock_state = runguard.run_lock_state(workspace, lane.run_dir)
        if lane.lock_state not in {"live", "stale"}:
            raise ValueError("unverifiable owner")
        lane.owner_live = lane.lock_state == "live"
        lane.evidence.append(Evidence("run.lock/owner.json", now, lane.lock_state, digest=digest))
    else:
        lane.evidence.append(Evidence("run.lock", now, "absent"))


def _timeout(lane: Lane, now: datetime) -> None:
    assert lane.run_dir is not None and lane.run is not None
    roster_path = lane.run_dir / "roster.json"
    if not roster_path.exists() and not roster_path.is_symlink():
        return
    roster, digest = _read_json(roster_path)
    # Mirror runs list's phase/seat selection without its wall-clock predicate
    # or its unbounded JSON reads. Planning assigns the orchestrator timeout.
    agents = roster.get("agents", {})
    default = runs_cmd._positive_timeout(roster.get("timeout_seconds"))
    status = lane.run.get("status")
    seats: list[str] = []
    if status in {"planning", "result-processing", "synthesizing", "handoff", "started"}:
        if isinstance(roster.get("orchestrator"), str):
            seats.append(roster["orchestrator"])
    elif isinstance(lane.run.get("worker"), str):
        seats.append(lane.run["worker"])
    elif status == "dispatching" and isinstance(lane.run.get("active_seats"), list):
        seats.extend(seat for seat in lane.run["active_seats"] if isinstance(seat, str))
    else:
        plan_path = lane.run_dir / "plan.json"
        if plan_path.exists() or plan_path.is_symlink():
            plan, _ = _read_json(plan_path)
            assignments = plan.get("assignments", [])
            if isinstance(assignments, list):
                seats.extend(
                    item["worker"]
                    for item in assignments
                    if isinstance(item, dict) and isinstance(item.get("worker"), str)
                )
    timeouts: list[float] = []
    for seat in seats:
        agent = agents.get(seat) if isinstance(agents, dict) else None
        value = runs_cmd._positive_timeout(agent.get("timeout_seconds")) if isinstance(agent, dict) else None
        if value is not None or default is not None:
            resolved = value if value is not None else default
            if resolved is not None:
                timeouts.append(resolved)
    lane.timeout_seconds = max(timeouts) if timeouts else default
    lane.evidence.append(Evidence("roster.json", now, "configured seat timeout", digest=digest))


def _collect_run(child: Path, target: Path, now: datetime, worktrees: set[Path]) -> Lane:
    lane = Lane(child.name, run_dir=child)
    bound = run_reap._bind_contained_run_dir(child)
    if bound is None:
        lane.errors.append("unreadable run directory")
        lane.evidence.append(Evidence("run.json", now, "directory could not be bound"))
        return lane
    source = "run.json"
    try:
        with run_reap._bound_transaction(bound):
            raw, meta = run_reap._read_bound_run_bytes(bound)
            if raw is None or meta is None:
                raise ValueError("unreadable or malformed run.json")
            lane.run = meta
            lane.evidence.append(Evidence("run.json", now, "run snapshot", digest=hashlib.sha256(raw).hexdigest()))
            lane.phase = meta.get("status") if isinstance(meta.get("status"), str) else None
            if lane.phase != "paused" and (lane.phase == "orphaned" or runs_cmd._is_terminal(meta)):
                lane.terminal_status = lane.phase
                lane.terminal_at = _timestamp(meta.get("finished_at") or meta.get("orphaned_at"))
            # Worktree isolation records its path in artifact_collection.
            collection = meta.get("artifact_collection")
            recorded = collection.get("worktree") if isinstance(collection, dict) else None
            raw_worktree = meta.get("worktree") or recorded or meta.get("cwd") or str(target)
            if not isinstance(raw_worktree, str) or not Path(raw_worktree).is_absolute():
                raise ValueError("invalid worktree path")
            lane.worktree = Path(raw_worktree)
            source = "events/lifecycle.jsonl"
            _journal(lane, now)
            source = "events"
            _events_progress(lane, now)
            source = "roster.json"
            _timeout(lane, now)
            source = "run.lock/owner.json"
            _lock(lane, now)
            source = "worktree"
            if lane.worktree.exists() and lane.worktree.resolve() not in worktrees:
                raise ValueError("worktree is not linked to target")
            _worktree_progress(lane, now)
    except (OSError, ValueError, RuntimeError, RecursionError, OverflowError) as exc:
        lane.errors.append(str(exc)[:160])
        lane.evidence.append(Evidence(source, now, "evidence unavailable"))
    finally:
        bound.close()
    return lane


def collect_lanes(target: Path, *, now: datetime | None = None, limit: int = 50) -> list[Lane]:
    """Join a bounded local run window, ledger claims and recorded worktrees.

    Malformed directory names and symlinked run directories are skipped. An
    unreadable snapshot in a valid directory is an unknown lane. Hub access,
    dispatch intent reads, and all mutations belong to later slices.
    """
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    target = target.expanduser().resolve()
    if not target.is_dir() or not os.access(target, os.R_OK | os.X_OK):
        raise ValueError("target is not a readable directory")
    clock = run_reap._utc_now(now)
    lanes: list[Lane] = []
    worktrees, worktree_error = _linked_worktrees(target, limit)
    root = target / ".brigade/runs"
    if root.is_symlink() or root.parent.is_symlink():
        raise ValueError("runs root must not be symlinked")
    scan_full = False
    if root.exists():
        if not root.is_dir():
            raise ValueError("runs root is not a directory")
        with os.scandir(root) as entries:
            # Bound directory entries as well as parsed files, including junk.
            for index, entry in enumerate(entries):
                if index >= limit:
                    scan_full = True
                    break
                child = root / entry.name
                if run_reap._classify_child(child, root.resolve()) is not None or not entry.is_dir(
                    follow_symlinks=False
                ):
                    continue
                lane = _collect_run(child, target, clock, worktrees)
                if worktree_error:
                    lane.errors.append(worktree_error)
                lanes.append(lane)
    ledger_path = target / ".brigade/work/tasks.json"
    if ledger_path.parent.is_symlink():
        raise ValueError("ledger directory must not be symlinked")
    if ledger_path.exists() or ledger_path.is_symlink():
        try:
            ledger, digest = _read_json(ledger_path)
            tasks = ledger.get("tasks")
            if not isinstance(tasks, list):
                raise ValueError("ledger tasks must be a list")
            for task in itertools.islice(tasks, limit):
                if not isinstance(task, dict) or not isinstance(task.get("id"), str) or not task["id"]:
                    raise ValueError("invalid ledger task")
                claim = claim_record(task)
                if task.get("claim") is not None and claim is None:
                    raise ValueError("invalid ledger claim")
                matches = [
                    lane
                    for lane in lanes
                    if lane.run
                    and (
                        lane.run.get("task_id") == task.get("id")
                        or (claim and lane.run.get("claim_id") == claim["claim_id"])
                    )
                ]
                if not matches and claim and len(lanes) < limit:
                    matches = [Lane(f"claim:{claim['claim_id']}")]
                    lanes.extend(matches)
                    if scan_full or any(lane.run_dir is not None and lane.errors for lane in lanes):
                        matches[0].errors.append("run scan incomplete, claim join unverified")
                    recorded = task.get("worktree")
                    if recorded is not None:
                        if not isinstance(recorded, str) or not Path(recorded).is_absolute():
                            matches[0].errors.append("invalid task worktree")
                        else:
                            matches[0].worktree = Path(recorded)
                            if matches[0].worktree.exists() and matches[0].worktree.resolve() not in worktrees:
                                matches[0].errors.append("worktree is not linked to target")
                            else:
                                _worktree_progress(matches[0], clock)
                for lane in matches:
                    lane.task, lane.claim = task, claim
                    lane.evidence.append(
                        Evidence(".brigade/work/tasks.json", clock, "ledger task and claim", digest=digest)
                    )
        except (OSError, ValueError, RuntimeError, RecursionError):
            for lane in lanes:
                lane.errors.append("ledger unreadable")
            if not lanes:
                lanes.append(Lane("ledger", errors=["ledger unreadable"]))
            for lane in lanes:
                lane.evidence.append(Evidence(".brigade/work/tasks.json", clock, "unreadable"))
    return sorted(lanes, key=lambda lane: lane.lane_id)


def classify(lane: Lane, now: datetime, thresholds: Thresholds) -> Classification:
    """Return one conservative state using only the collected observation."""
    clock = run_reap._utc_now(now)

    def result(state: str, reason: str) -> Classification:
        infrastructure = None
        if state == "failed" and lane.run is not None:
            kind, phase = worker_failure.resolve_run_failure_taxonomy(lane.run)
            infrastructure = worker_failure.run_failure_is_infrastructure_at_read_time(kind, phase)
        return Classification(state, reason, tuple(lane.evidence), infrastructure)

    if lane.errors:
        return result("unknown", lane.errors[0])
    if not lane.evidence or any(
        clock - item.observed_at > thresholds.evidence_max_age or item.observed_at > clock for item in lane.evidence
    ):
        return result("unknown", "missing or stale observation")
    if any(stamp > clock for stamp in lane.progress):
        return result("unknown", "progress timestamp is in the future")
    if lane.terminal_status == "orphaned":
        return result("orphaned", "run already marked orphaned")
    if lane.terminal_status is not None:
        if lane.terminal_at is not None and lane.terminal_at > clock:
            return result("unknown", "terminal timestamp is in the future")
        if lane.lock_state in {"live", "stale"} and lane.terminal_at is None:
            return result("unknown", "terminal lock has no finish time for grace")
        if (
            lane.lock_state in {"live", "stale"}
            and lane.terminal_at
            and clock - lane.terminal_at > thresholds.zombie_after
        ):
            return result("zombie", "terminal run still owns a live lock beyond grace")
        return result(
            "complete" if lane.terminal_status in runs_cmd._SUCCESS_STATUSES | {"completed", "complete"} else "failed",
            "terminal run outcome",
        )
    if lane.worktree_missing:
        return result("lost-worktree", "recorded worktree path is missing")
    if lane.phase in {"paused", "artifact-collection"}:
        return result("waiting", "run is intentionally waiting")
    meta = lane.run or {}
    started = _timestamp(meta.get("started_at"))
    if lane.run is not None:
        if lane.phase not in runs_cmd._NONTERMINAL_STATUSES or started is None or started > clock:
            return result("unknown", "missing or invalid run phase or start time")
        if lane.lock_state == "stale" and clock - started > thresholds.older_than:
            return result("dead", "nonterminal run has a stale owner lock")
        if lane.owner_live is not True:
            return result("unknown", "owner liveness is unverified or dead run is below age threshold")
        phase_started = _timestamp(meta.get("status_started_at", meta.get("started_at")))
        if lane.timeout_seconds and phase_started and (clock - phase_started).total_seconds() > lane.timeout_seconds:
            return result("stalled", "configured seat timeout exceeded")
        if not lane.progress:
            return result("unknown", "no progress signal available")
        silence = clock - max(lane.progress)
        if silence >= thresholds.stall_after:
            return result("stalled", "progress silence exceeds stall threshold")
        if silence >= thresholds.idle_after:
            return result("idle", "progress silence exceeds idle threshold")
        return result("working", "live owner and recent progress")
    if lane.claim:
        claimed = _timestamp(lane.claim.get("claimed_at"))
        if claimed is None or claimed > clock:
            return result("unknown", "claim time unavailable")
        if (
            lane.dirty
            and lane.task
            and lane.task.get("status") == "in_progress"
            and not lane.task.get("landing")
            and clock - max([claimed, *lane.progress]) >= thresholds.stranded_after
        ):
            return result("stranded", "old claimed worktree has unlanded changes")
        if clock - claimed >= thresholds.claim_grace:
            return result("claim-without-run", "claim has no matching run after grace")
        return result("waiting", "claim is within run-start grace")
    return result("unknown", "insufficient lane evidence")
