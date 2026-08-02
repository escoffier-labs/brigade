#!/usr/bin/env python3
"""Measure journal-authority append latency and lifecycle journal volume.

Latency mode drives each run directory through the real Brigade
journal-authority path (``run_lifecycle`` + ``run_checkpoint`` under a real
``runguard.run_lock``), records per-append wall-clock latency with
``time.perf_counter_ns``, and reports deterministic integer percentiles plus
environment metadata.

Volume mode (issue #635) reports event counts and journal bytes for:

- scanned real ``lifecycle.jsonl`` files (optional ``--scan`` roots)
- a synthetic representative run (one seat, no retries, no pause cycles)
- a synthetic worst case (configurable seats, attempts-per-seat, pause cycles)

This is a measurement harness only: it carries no acceptance threshold and
no pass/fail verdict. Standard library plus the in-repo ``brigade`` package;
no third-party runtime dependencies.
"""

from __future__ import annotations

import argparse
import codecs
import json
import math
import os
import platform
import re
import shutil
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brigade import run_checkpoint, run_journal, run_lifecycle, runguard

_MEASURED_STATUS = "planning"

_MOUNTPOINT_ESCAPE_RE = re.compile(r"\\([0-7]{3})")

_DEFAULT_WORST_SEATS = 32
_DEFAULT_WORST_ATTEMPTS = 3
_DEFAULT_WORST_PAUSE_CYCLES = 8


class MeasurementInputError(RuntimeError):
    """Measurement input or scenario state is missing, unreadable, or incomplete."""


@dataclass(frozen=True)
class _JournalMeasurement:
    """Raw volume data plus the validity of the matching bounded parse."""

    event_count: int
    event_types: dict[str, int]
    omitted: bool = False
    error: str | None = None
    integrity_error: str | None = None


def _bootstrap_run(run_dir: Path) -> bytes:
    """Write the minimal canonical run.json carrying both durable request fields.

    Returns the writer-canonical base bytes (the same bytes on disk), which the
    base-stripped checkpoint path requires to be canonical and to carry both
    durable request fields with no journal-derived metadata fields.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    base = {
        "lifecycle_journal_requested": True,
        "run_journal_authority_requested": True,
        "status": "started",
        "task": "journal-authority measurement",
    }
    base_bytes = (json.dumps(base, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (run_dir / "run.json").write_bytes(base_bytes)
    return base_bytes


def _measure_run(run_dir: Path, workspace: Path) -> dict[str, Any]:
    """Drive one run directory through the journal-authority append path.

    Bootstraps with both durable request fields, activates journal authority
    under a real ``runguard.run_lock``, appends one base-stripped checkpoint
    event and one mapped lifecycle status event, timing each append call with
    ``time.perf_counter_ns``.
    """
    base_bytes = _bootstrap_run(run_dir)
    latencies: list[int] = []
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        start = time.perf_counter_ns()
        run_checkpoint.write_checkpoint(
            run_dir,
            base_bytes,
            workspace=workspace,
            paired_event_type=run_lifecycle.STATUS_EVENT_TYPE[_MEASURED_STATUS],
            body_kind=run_checkpoint._BODY_KIND_BASE_STRIPPED,
        )
        latencies.append(time.perf_counter_ns() - start)
        start = time.perf_counter_ns()
        run_lifecycle.record_lifecycle_transition(
            run_dir,
            status=_MEASURED_STATUS,
            workspace=workspace,
        )
        latencies.append(time.perf_counter_ns() - start)
    journal_path = run_dir / "events" / "lifecycle.jsonl"
    journal_entry = _journal_volume_entry(journal_path, label=run_dir.name)
    if journal_entry["omitted"] or journal_entry["integrity_error"]:
        detail = journal_entry["error"] or journal_entry["integrity_error"]
        raise MeasurementInputError(f"measurement journal could not be verified: {detail}")
    return {
        "journal_bytes": journal_entry["journal_bytes"],
        "event_count": journal_entry["event_count"],
        "append_latencies_ns": latencies,
    }


def _percentile_nearest_rank(sorted_values: list[int], percentile: float) -> int:
    """Deterministic nearest-rank percentile on an ascending-sorted nonempty list.

    rank = ceil(p/100 * n), clamped to [1, n]; the value at rank-1 is returned.
    """
    n = len(sorted_values)
    rank = math.ceil(percentile / 100 * n)
    index = max(0, min(n - 1, rank - 1))
    return int(sorted_values[index])


def _decode_mountpoint(raw: str) -> str:
    """Decode mountinfo octal escapes (e.g. ``\\040`` for space) in a mountpoint."""
    return _MOUNTPOINT_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 8)), raw)


def _filesystem_type(root: Path) -> str:
    """Filesystem type hosting ``root``, from /proc/self/mountinfo.

    Picks the deepest mount whose mountpoint is a path-prefix of the resolved
    root directory. Returns "unknown" when mountinfo is absent or unmatched.
    """
    mountinfo = Path("/proc/self/mountinfo")
    try:
        lines = mountinfo.read_text().splitlines()
    except OSError:
        return "unknown"
    resolved = root.expanduser().resolve()
    best_fs: str | None = None
    best_depth = -1
    for line in lines:
        fields, separator, tail = line.partition(" - ")
        if not separator:
            continue
        parts = fields.split()
        if len(parts) < 5:
            continue
        mountpoint = Path(_decode_mountpoint(parts[4]))
        try:
            resolved.relative_to(mountpoint)
        except ValueError:
            continue
        depth = len(mountpoint.parts)
        if depth > best_depth:
            best_depth = depth
            best_fs = tail.split(maxsplit=1)[0] if tail.split() else None
    return best_fs if best_fs is not None else "unknown"


def _directory_fsync_supported(root: Path) -> bool:
    """True when a directory under ``root`` can be opened and fsynced."""
    probe = Path(tempfile.mkdtemp(prefix="dir-fsync-probe-", dir=root))
    try:
        fd = os.open(probe, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        except OSError:
            return False
        finally:
            os.close(fd)
        return True
    except OSError:
        return False
    finally:
        shutil.rmtree(probe, ignore_errors=True)


def _environment(root: Path) -> dict[str, Any]:
    return {
        "python_version": sys.version,
        "platform": platform.platform(),
        "filesystem_type": _filesystem_type(root),
        "directory_fsync_supported": _directory_fsync_supported(root),
    }


def _bounds() -> dict[str, int]:
    return {
        "max_journal_events": int(run_checkpoint.MAX_JOURNAL_EVENTS),
        "max_journal_bytes": int(run_checkpoint.MAX_JOURNAL_BYTES),
    }


def _lock_workspace_parent(root: Path, staging_root: Path) -> Path | None:
    """Pick a parent directory for the runguard lock workspace.

    ``runguard.lock_path`` treats every descendant of a git worktree as the
    same worktree and resolves the lock to ``<git top-level>/.brigade/run.lock``.
    When the requested ``root`` lives inside a git worktree, a lock workspace
    staged under ``root`` would therefore resolve to the very repo lock an
    outer Brigade run already owns and raise ``RunLockError``. Place the lock
    workspace outside the git top-level instead: its parent is never a
    worktree descendant, so ``lock_path`` falls back to the workspace itself.

    For a non-git ``root`` the staging subtree under the requested root is
    already outside any worktree, so an isolated lock workspace there is fine
    and keeps cleanup tied to the staging tree. Returns ``None`` to select the
    standard ``TemporaryDirectory`` location for the impossible case where the
    git top-level is the filesystem root.
    """
    if runguard.is_git_worktree(root):
        git_top = runguard.git_root(root)
        parent = git_top.parent
        if parent != git_top:
            return parent
        return None
    return staging_root


def _stream_count_complete_nonblank_records(journal_path: Path) -> tuple[int, bool]:
    """Return complete raw record count and whether the bytes decode as UTF-8.

    The counter is deliberately independent of JSON parsing. It reads fixed
    chunks, preserves a record's nonblank state across chunk boundaries, and
    counts only records terminated by a newline. A partial tail is therefore
    never treated as a complete event.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    decodable = True
    complete_count = 0
    pending_nonblank = False
    with journal_path.open("rb") as handle:
        while chunk := handle.read(65536):
            if decodable:
                try:
                    decoder.decode(chunk)
                except UnicodeDecodeError:
                    decodable = False
            parts = chunk.split(b"\n")
            for record in parts[:-1]:
                if pending_nonblank or record.strip():
                    complete_count += 1
                pending_nonblank = False
            pending_nonblank = pending_nonblank or bool(parts[-1].strip())
    if decodable:
        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            decodable = False
    return complete_count, decodable


def _measure_journal_events(journal_path: Path) -> _JournalMeasurement:
    """Measure raw volume and parse health without silently converting errors to zero."""
    raw_count, decodable = _stream_count_complete_nonblank_records(journal_path)
    if not decodable:
        return _JournalMeasurement(raw_count, {}, omitted=True, error="undecodable journal")
    try:
        report = run_journal.read_journal_bounded(journal_path)
    except OSError as exc:
        return _JournalMeasurement(
            raw_count,
            {},
            omitted=True,
            error=f"journal read failed: {type(exc).__name__}: {exc}",
        )
    except run_journal.RunJournalError as exc:
        return _JournalMeasurement(raw_count, {}, integrity_error=exc.diagnostic)
    if report.chain_errors:
        return _JournalMeasurement(raw_count, {}, integrity_error="; ".join(report.chain_errors))
    if report.partial_tail is not None:
        return _JournalMeasurement(raw_count, {}, integrity_error="journal has a partial tail")
    return _JournalMeasurement(
        len(report.events),
        dict(sorted(Counter(event.event_type for event in report.events).items())),
    )


def _count_journal_events(journal_path: Path) -> int:
    """Return the measurement count, preserving raw-count fallback for corrupt journals."""
    return _measure_journal_events(journal_path).event_count


def _journal_entry(
    *,
    label: str,
    path: str,
    journal_bytes: int | None,
    measurement: _JournalMeasurement | None,
    error: str | None = None,
) -> dict[str, Any]:
    """Build one stable report entry, including explicit omitted/error state."""
    bounds = _bounds()
    event_count = measurement.event_count if measurement is not None else None
    omitted = error is not None or (measurement is not None and measurement.omitted)
    entry_error = error if error is not None else (measurement.error if measurement is not None else None)
    integrity_error = measurement.integrity_error if measurement is not None else None
    return {
        "label": label,
        "path": path,
        "event_count": event_count,
        "journal_bytes": journal_bytes,
        "event_type_counts": measurement.event_types if measurement is not None else {},
        "headroom_events": bounds["max_journal_events"] - event_count if event_count is not None else None,
        "headroom_bytes": bounds["max_journal_bytes"] - journal_bytes if journal_bytes is not None else None,
        "pct_events": round(100.0 * event_count / bounds["max_journal_events"], 4) if event_count is not None else None,
        "pct_bytes": round(100.0 * journal_bytes / bounds["max_journal_bytes"], 6)
        if journal_bytes is not None
        else None,
        "omitted": omitted,
        "error": entry_error,
        "integrity_error": integrity_error,
    }


def _journal_volume_entry(journal_path: Path, *, label: str, display_path: str | None = None) -> dict[str, Any]:
    """Measure one journal, omitting unreadable or changing files from aggregates."""
    display = display_path or str(journal_path)
    try:
        before = journal_path.stat()
    except OSError as exc:
        return _journal_entry(
            label=label,
            path=display,
            journal_bytes=None,
            measurement=None,
            error=f"journal stat failed: {type(exc).__name__}: {exc}",
        )
    try:
        measurement = _measure_journal_events(journal_path)
    except OSError as exc:
        return _journal_entry(
            label=label,
            path=display,
            journal_bytes=before.st_size,
            measurement=None,
            error=f"journal read failed: {type(exc).__name__}: {exc}",
        )
    try:
        after = journal_path.stat()
    except OSError as exc:
        return _journal_entry(
            label=label,
            path=display,
            journal_bytes=before.st_size,
            measurement=measurement,
            error=f"journal stat failed after read: {type(exc).__name__}: {exc}",
        )
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity:
        return _journal_entry(
            label=label,
            path=display,
            journal_bytes=after.st_size,
            measurement=measurement,
            error="journal changed during measurement",
        )
    return _journal_entry(label=label, path=display, journal_bytes=after.st_size, measurement=measurement)


def scan_lifecycle_journals(roots: list[Path]) -> dict[str, Any]:
    """Collect event-count/byte stats for ``lifecycle.jsonl`` under ``roots``."""
    journals: list[Path] = []
    for root in roots:
        root = root.expanduser().resolve()
        try:
            if not root.is_dir():
                raise MeasurementInputError(f"measurement input missing: {root}")
            discovered = sorted(root.rglob("lifecycle.jsonl"))
        except MeasurementInputError:
            raise
        except OSError as exc:
            raise MeasurementInputError(f"measurement input unreadable: {root}") from exc
        if not discovered:
            raise MeasurementInputError(f"measurement input stale: no lifecycle journals under {root}")
        journals.extend(discovered)
    per_run = [_journal_volume_entry(path, label=path.parent.parent.name) for path in journals]
    healthy_entries = [entry for entry in per_run if not entry["omitted"]]
    event_counts = sorted(entry["event_count"] for entry in healthy_entries)
    byte_counts = sorted(entry["journal_bytes"] for entry in healthy_entries)
    aggregate: dict[str, Any]
    if event_counts:
        aggregate = {
            "runs": len(healthy_entries),
            "omitted_runs": len(per_run) - len(healthy_entries),
            "event_count_p50": _percentile_nearest_rank(event_counts, 50),
            "event_count_p95": _percentile_nearest_rank(event_counts, 95),
            "event_count_max": int(event_counts[-1]),
            "journal_bytes_p50": _percentile_nearest_rank(byte_counts, 50),
            "journal_bytes_p95": _percentile_nearest_rank(byte_counts, 95),
            "journal_bytes_max": int(byte_counts[-1]),
        }
    else:
        aggregate = {
            "runs": 0,
            "omitted_runs": len(per_run),
            "event_count_p50": 0,
            "event_count_p95": 0,
            "event_count_max": 0,
            "journal_bytes_p50": 0,
            "journal_bytes_p95": 0,
            "journal_bytes_max": 0,
        }
    return {"roots": [str(path.expanduser().resolve()) for path in roots], "per_run": per_run, "aggregate": aggregate}


def _set_approval_reference(run_dir: Path, approval_id: str, cycle: int, *, decision_state: str) -> dict[str, Any]:
    snap = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    if not isinstance(snap, dict):
        raise TypeError("run.json must contain an object")
    snap["approval_reference"] = {
        "approval_id": approval_id,
        "source": "daily",
        "fingerprint": f"fp-{cycle}",
        "source_fingerprint": f"sfp-{cycle}",
        "contract_fingerprint": f"cfp-{cycle}",
        "evidence_fingerprint": f"efp-{cycle}",
        "decision_state": decision_state,
    }
    (run_dir / "run.json").write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return snap


def _drive_volume_scenario(
    run_dir: Path,
    workspace: Path,
    *,
    seats: int,
    attempts_per_seat: int,
    pause_cycles: int,
) -> dict[str, Any]:
    """Drive a synthetic authoritative run and return volume stats."""
    if seats < 0 or attempts_per_seat < 1 or pause_cycles < 0:
        raise ValueError("seats>=0, attempts_per_seat>=1, pause_cycles>=0 required")
    _bootstrap_run(run_dir)
    stopped_reason: str | None = None
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        for status in ("planning", "dispatching"):
            try:
                run_lifecycle.record_lifecycle_transition(run_dir, status=status, workspace=workspace)
            except Exception as exc:  # noqa: BLE001 - harness records and continues
                stopped_reason = f"status {status}: {type(exc).__name__}: {exc}"
                break
        if stopped_reason is None:
            for seat_index in range(seats):
                seat = f"seat-{seat_index:02d}"
                for attempt_index in range(attempts_per_seat):
                    try:
                        requested = run_lifecycle.record_dispatch_fact(
                            run_dir,
                            workspace=workspace,
                            event_type="run.dispatch.requested",
                            seat=seat,
                        )
                        if requested is None:
                            raise RuntimeError("dispatch requested returned None")
                        attempt = requested.payload["attempt"]
                        if not isinstance(attempt, int):
                            raise TypeError("dispatch attempt must be int")
                        run_lifecycle.record_dispatch_fact(
                            run_dir,
                            workspace=workspace,
                            event_type="run.dispatch.observed",
                            seat=seat,
                            attempt=attempt,
                        )
                        terminal = (
                            "run.dispatch.completed"
                            if attempt_index == attempts_per_seat - 1
                            else "run.dispatch.failed"
                        )
                        run_lifecycle.record_dispatch_fact(
                            run_dir,
                            workspace=workspace,
                            event_type=terminal,
                            seat=seat,
                            attempt=attempt,
                        )
                    except Exception as exc:  # noqa: BLE001
                        stopped_reason = f"dispatch seat={seat} attempt={attempt_index}: {type(exc).__name__}: {exc}"
                        break
                if stopped_reason is not None:
                    break
        if stopped_reason is None:
            for cycle in range(pause_cycles):
                approval_id = f"approval-{cycle}"
                try:
                    run_lifecycle.record_lifecycle_event(
                        run_dir,
                        event_type="approval.requested",
                        payload={
                            "approval_id": approval_id,
                            "source": "daily",
                            "contract_fingerprint": f"cfp-{cycle}",
                        },
                        idempotency_key=run_lifecycle.approval_idempotency_key(approval_id, "requested"),
                        workspace=workspace,
                    )
                    snap = _set_approval_reference(run_dir, approval_id, cycle, decision_state="pending")
                    run_lifecycle.record_lifecycle_transition(
                        run_dir,
                        status="paused",
                        workspace=workspace,
                        incoming_snapshot=snap,
                    )
                    snap = _set_approval_reference(run_dir, approval_id, cycle, decision_state="approved")
                    run_lifecycle.record_lifecycle_event(
                        run_dir,
                        event_type="approval.consumed",
                        payload={"approval_id": approval_id, "consuming_run_id": run_dir.name},
                        idempotency_key=run_lifecycle.approval_idempotency_key(
                            approval_id, "consumed", scope=run_dir.name
                        ),
                        workspace=workspace,
                    )
                    run_lifecycle.record_lifecycle_transition(
                        run_dir,
                        status="running",
                        workspace=workspace,
                        incoming_snapshot=snap,
                    )
                except Exception as exc:  # noqa: BLE001
                    stopped_reason = f"pause cycle={cycle}: {type(exc).__name__}: {exc}"
                    break
        if stopped_reason is None and seats > 0:
            for status in ("result-processing", "synthesizing", "ok"):
                try:
                    run_lifecycle.record_lifecycle_transition(run_dir, status=status, workspace=workspace)
                except Exception as exc:  # noqa: BLE001
                    stopped_reason = f"terminal {status}: {type(exc).__name__}: {exc}"
                    break

    journal_path = run_dir / "events" / "lifecycle.jsonl"
    entry = _journal_volume_entry(
        journal_path,
        label=run_dir.name,
        display_path=f"runs/{run_dir.name}/events/lifecycle.jsonl",
    )
    entry["config"] = {
        "seats": seats,
        "attempts_per_seat": attempts_per_seat,
        "pause_cycles": pause_cycles,
    }
    entry["stopped_reason"] = stopped_reason
    entry["hit_event_ceiling"] = entry["event_count"] >= run_checkpoint.MAX_JOURNAL_EVENTS
    entry["hit_byte_ceiling"] = entry["journal_bytes"] >= run_checkpoint.MAX_JOURNAL_BYTES
    if stopped_reason is not None:
        raise MeasurementInputError(f"measurement scenario did not complete: {stopped_reason}")
    return entry


def measure_runs(runs: int, root: Path) -> dict[str, Any]:
    """Drive ``runs`` run directories under ``root`` through journal authority.

    Returns a deterministic-schema report dict with keys ``runs``,
    ``per_run``, ``aggregate``, and ``environment``. No pass/fail verdict or
    acceptance threshold is included.

    All measured run directories and journal/checkpoint files live inside a
    unique staging subtree under ``root`` so filesystem measurements remain on
    the requested filesystem. Only the runguard lock workspace is placed in a
    second unique ``TemporaryDirectory`` outside the detected git worktree, so
    an inner measurement run never collides with an outer Brigade run that
    already owns the repo lock. The lock workspace is not part of measured
    append files or timing and is cleaned on both success and exception.
    """
    if isinstance(runs, bool) or not isinstance(runs, int):
        raise TypeError("runs must be an integer")
    if runs <= 0:
        raise ValueError("runs must be a positive integer")
    if not isinstance(root, Path):
        raise TypeError("root must be a pathlib.Path")
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    per_run: list[dict[str, Any]] = []
    all_latencies: list[int] = []
    with tempfile.TemporaryDirectory(prefix="brigade-run-journal-measure-", dir=root) as staging:
        staging_root = Path(staging)
        lock_parent = _lock_workspace_parent(root, staging_root)
        with tempfile.TemporaryDirectory(prefix="brigade-run-journal-lock-", dir=lock_parent) as lock_dir:
            workspace = Path(lock_dir)
            for index in range(runs):
                entry = _measure_run(staging_root / "runs" / f"run-{index:05d}", workspace)
                per_run.append(entry)
                all_latencies.extend(entry["append_latencies_ns"])

    ordered = sorted(all_latencies)
    aggregate = {
        "p50_ns": _percentile_nearest_rank(ordered, 50),
        "p95_ns": _percentile_nearest_rank(ordered, 95),
        "p99_ns": _percentile_nearest_rank(ordered, 99),
        "max_ns": int(ordered[-1]),
    }
    return {
        "runs": runs,
        "per_run": per_run,
        "aggregate": aggregate,
        "environment": _environment(root),
    }


def measure_volume(
    root: Path,
    *,
    scan_roots: list[Path] | None = None,
    roster_seats: int = 11,
    roster_attempts_per_seat: int = 2,
    roster_pause_cycles: int = 3,
    worst_seats: int = _DEFAULT_WORST_SEATS,
    worst_attempts_per_seat: int = _DEFAULT_WORST_ATTEMPTS,
    worst_pause_cycles: int = _DEFAULT_WORST_PAUSE_CYCLES,
) -> dict[str, Any]:
    """Measure lifecycle journal event counts and bytes for issue #635."""
    if not isinstance(root, Path):
        raise TypeError("root must be a pathlib.Path")
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    scanned = scan_lifecycle_journals(scan_roots or [])
    with tempfile.TemporaryDirectory(prefix="brigade-run-journal-volume-", dir=root) as staging:
        staging_root = Path(staging)
        lock_parent = _lock_workspace_parent(root, staging_root)
        with tempfile.TemporaryDirectory(prefix="brigade-run-journal-lock-", dir=lock_parent) as lock_dir:
            workspace = Path(lock_dir)
            representative = _drive_volume_scenario(
                staging_root / "runs" / "representative",
                workspace,
                seats=1,
                attempts_per_seat=1,
                pause_cycles=0,
            )
            roster_sized = _drive_volume_scenario(
                staging_root / "runs" / "roster-sized",
                workspace,
                seats=roster_seats,
                attempts_per_seat=roster_attempts_per_seat,
                pause_cycles=roster_pause_cycles,
            )
            worst_case = _drive_volume_scenario(
                staging_root / "runs" / "worst-case",
                workspace,
                seats=worst_seats,
                attempts_per_seat=worst_attempts_per_seat,
                pause_cycles=worst_pause_cycles,
            )

    for scenario in (representative, roster_sized, worst_case):
        stopped_reason = scenario.get("stopped_reason")
        if stopped_reason is not None:
            raise MeasurementInputError(f"measurement scenario did not complete: {stopped_reason}")

    return {
        "issue": 635,
        "kind": "journal-volume",
        "bounds": _bounds(),
        "environment": _environment(root),
        "scanned_real_runs": scanned,
        "representative_synthetic": representative,
        "roster_sized_synthetic": roster_sized,
        "worst_case_synthetic": worst_case,
    }


def write_report(report: dict[str, Any], output: Path) -> None:
    """Write ``report`` as canonical JSON (indent=2, sort_keys) plus newline."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("latency", "volume", "all"),
        default="latency",
        help="latency (default, issue #568), volume (issue #635), or both",
    )
    parser.add_argument("--runs", type=int, default=1000, help="number of run directories for latency mode")
    parser.add_argument("--output", required=True, help="exact file path for the JSON report")
    parser.add_argument(
        "--scan",
        action="append",
        default=[],
        help="root directory to scan for lifecycle.jsonl (repeatable; volume mode)",
    )
    parser.add_argument("--worst-seats", type=int, default=_DEFAULT_WORST_SEATS)
    parser.add_argument("--worst-attempts-per-seat", type=int, default=_DEFAULT_WORST_ATTEMPTS)
    parser.add_argument("--worst-pause-cycles", type=int, default=_DEFAULT_WORST_PAUSE_CYCLES)
    args = parser.parse_args(argv)
    if args.runs <= 0:
        parser.error("--runs must be a positive integer")
    if args.worst_seats < 0 or args.worst_attempts_per_seat < 1 or args.worst_pause_cycles < 0:
        parser.error("worst-case knobs must be seats>=0, attempts>=1, pause_cycles>=0")

    root = Path.cwd()
    if args.mode == "latency":
        report: dict[str, Any] = measure_runs(args.runs, root=root)
    elif args.mode == "volume":
        report = measure_volume(
            root,
            scan_roots=[Path(path) for path in args.scan],
            worst_seats=args.worst_seats,
            worst_attempts_per_seat=args.worst_attempts_per_seat,
            worst_pause_cycles=args.worst_pause_cycles,
        )
    else:
        report = {
            "latency": measure_runs(args.runs, root=root),
            "volume": measure_volume(
                root,
                scan_roots=[Path(path) for path in args.scan],
                worst_seats=args.worst_seats,
                worst_attempts_per_seat=args.worst_attempts_per_seat,
                worst_pause_cycles=args.worst_pause_cycles,
            ),
        }
    write_report(report, Path(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
