"""Read-only memory reflection runs and retained candidate inspection."""

from __future__ import annotations

import argparse
import heapq
import json
import os
import re
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import memory_proposal_io as io
from . import memory_reflect_candidates as derive
from .memory_evidence_log import LOG_PATH, MAX_SCAN_BYTES, EvidenceLog

ROOT = ".brigade/memory/reflect/runs"
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
RETAIN_RUNS = 50
RETAIN_DAYS = 30
RUN_NAME = re.compile(r"\d{8}-\d{6}-\d{6}-[0-9a-f]{8}")


def _run_started(name: str) -> datetime | None:
    if not RUN_NAME.fullmatch(name):
        return None
    try:
        return datetime.strptime(name[:22], "%Y%m%d-%H%M%S-%f").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _prune_runs(target: Path, now: datetime) -> None:
    """Bound derived history without following links or deleting unknown files."""
    root = target / ROOT
    newest: list[str] = []
    successful: str | None = None
    cutoff = now - timedelta(days=RETAIN_DAYS)
    with io.directory(root) as parent:
        with os.scandir(parent) as entries:
            for entry in entries:
                started = _run_started(entry.name)
                if started is None or not entry.is_dir(follow_symlinks=False):
                    continue
                if started >= cutoff:
                    heapq.heappush(newest, entry.name)
                    if len(newest) > RETAIN_RUNS:
                        heapq.heappop(newest)
                try:
                    receipt = io.read_object(root / entry.name / "receipt.json", private=True)
                except (OSError, ValueError, UnicodeError, RecursionError):
                    continue
                if (
                    receipt.get("schema") == "brigade.memory-reflect-run.v1"
                    and receipt.get("run_id") == entry.name
                    and receipt.get("status") == "completed"
                    and type(receipt.get("candidate_count")) is int
                    and 0 <= receipt["candidate_count"] <= MAX_LIMIT
                    and (successful is None or entry.name > successful)
                ):
                    successful = entry.name
        if successful is not None and successful not in newest:
            if len(newest) == RETAIN_RUNS:
                heapq.heappop(newest)
            newest.append(successful)
        keep = set(newest)
        with os.scandir(parent) as entries:
            for entry in entries:
                started = _run_started(entry.name)
                if (
                    started is None
                    or started > now
                    or entry.name in keep
                    or not entry.is_dir(follow_symlinks=False)
                    or entry.stat(follow_symlinks=False).st_mtime > now.timestamp()
                ):
                    continue
                with io.directory(root / entry.name) as run:
                    names = os.listdir(run)
                    if "receipt.json" not in names and started >= cutoff:
                        continue  # Leave recent in-progress runs to publish their receipts.
                    if set(names) - {"receipt.json", "candidates.jsonl"}:
                        raise OSError("unexpected files in reflect run; retention stopped")
                    for name in names:
                        metadata = os.stat(name, dir_fd=run, follow_symlinks=False)
                        if (
                            not stat.S_ISREG(metadata.st_mode)
                            or metadata.st_nlink != 1
                            or metadata.st_uid != os.getuid()
                        ):
                            raise OSError("unsafe reflect run file; retention stopped")
                    for name in names:
                        os.unlink(name, dir_fd=run)
                os.rmdir(entry.name, dir_fd=parent)


def _evidence_log(target: Path) -> EvidenceLog:
    """Separate the existing reader's window warning from invalid-row warnings.

    Bind the classification to an unchanged regular file so concurrent appends
    cannot disguise an invalid-row warning as a window warning.
    """
    path = target / LOG_PATH
    try:
        with io.directory(path.parent) as parent:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                before = io._regular(fd)
                ledger = EvidenceLog(target)
                after = io._regular(fd)
                live = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                observations = {
                    (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns) for s in (before, after, live)
                }
                if len(observations) != 1:
                    raise ValueError("evidence log changed during reflection; retry the run")
                truncated = before.st_size > MAX_SCAN_BYTES
            finally:
                os.close(fd)
    except FileNotFoundError:
        ledger = EvidenceLog(target)
        truncated = False
    ledger.summary["scan_window_truncated"] = truncated
    warning_count = ledger.summary["warning_count"]
    assert isinstance(warning_count, int)
    ledger.summary["invalid_warning_count"] = warning_count - int(truncated)
    return ledger


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("reflect", help="Derive cited memory candidates without editing cards.")
    commands = parser.add_subparsers(dest="memory_reflect_command", required=True)
    for name in ("run", "status", "candidates", "show"):
        command = commands.add_parser(name, help=f"{name.capitalize()} deterministic memory reflection candidates.")
        command.add_argument("--target", "-t", type=Path, default=Path("."))
        command.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
        if name == "run":
            command.add_argument("--scope", help="Exact card frontmatter scope (missing scope means repo).")
            command.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Candidate limit from 1 to 100.")
            command.add_argument("--dry-run", action="store_true", help="Print metadata diffs and duplicate summaries.")
        elif name == "show":
            command.add_argument("candidate_id")


def run_payload(
    *, target: Path, scope: str | None = None, limit: int = DEFAULT_LIMIT, dry_run: bool = False
) -> dict[str, Any]:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError("--limit must be from 1 to 100")
    if scope is not None and (not scope.strip() or len(scope) > 1024 or any(ord(c) < 32 for c in scope)):
        raise ValueError("invalid scope")
    now = datetime.now(timezone.utc)
    run_id = now.strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid.uuid4().hex[:8]
    relative = f"{ROOT}/{run_id}"
    payload: dict[str, Any] = {
        "schema": "brigade.memory-reflect-run.v1",
        "run_id": run_id,
        "started_at": now.isoformat(),
        "scope": scope,
        "limit": limit,
        "dry_run": dry_run,
        "receipt_path": f"{relative}/receipt.json",
        "candidates_path": f"{relative}/candidates.jsonl",
        "candidates": [],
        "status": "failed",
        "stage": "configuration",
        "skipped_cards": [],
        "total_candidates": 0,
        "truncated": False,
    }
    try:
        care = derive.configuration(target)
        payload["stage"] = "evidence-log"
        ledger = _evidence_log(target)
        payload["evidence_log"] = ledger.summary
        if ledger.summary["unsupported"] or ledger.summary["invalid_warning_count"]:
            raise ValueError("evidence log is incomplete or unreadable")
        payload["stage"] = "cards"
        cards, payload["skipped_cards"] = derive.read_cards(target, care, ledger, scope)
        payload["stage"] = "candidates"
        candidates = derive.generate(target, care, ledger, cards)
        payload["total_candidates"] = len(candidates)
        payload["truncated"] = len(candidates) > limit
        candidates = candidates[:limit]
        payload["stage"] = "persist"
        io.write_exclusive(target / payload["candidates_path"], b"".join(io.json_bytes(c) for c in candidates))
        payload["candidates"] = candidates
        payload["status"] = "completed"
        payload["stage"] = "completed"
    except derive.UnsafeState:
        # A failure receipt here would itself violate the canonical-root boundary.
        raise
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        payload["error"] = str(exc)
    payload["candidate_count"] = len(payload["candidates"])
    # The receipt is the visibility boundary. Partial candidate output from a
    # failed run is never returned by the inspection commands.
    receipt = {key: value for key, value in payload.items() if key != "candidates"}
    io.write_exclusive(target / payload["receipt_path"], io.json_bytes(receipt))
    try:
        _prune_runs(target, now)
    except (OSError, ValueError):
        payload["retention_warning"] = "reflect history could not be pruned safely"
    return payload


def _runs(target: Path) -> tuple[list[dict[str, Any]], int]:
    root = target / ROOT
    try:
        with io.directory(root):
            paths = heapq.nlargest(
                io.MAX_FILES,
                (path for path in root.iterdir() if re.fullmatch(r"\d{8}-\d{6}-\d{6}-[0-9a-f]{8}", path.name)),
            )
    except FileNotFoundError:
        return [], 0
    runs: list[dict[str, Any]] = []
    invalid = 0
    for path in paths:
        if not re.fullmatch(r"\d{8}-\d{6}-\d{6}-[0-9a-f]{8}", path.name):
            continue
        try:
            receipt = io.read_object(path / "receipt.json", private=True)
        except FileNotFoundError:
            continue  # A concurrent or interrupted run has not published a receipt.
        except (OSError, ValueError, UnicodeError, RecursionError):
            invalid += 1
            continue
        count = receipt.get("candidate_count")
        if (
            receipt.get("run_id") != path.name
            or receipt.get("schema") != "brigade.memory-reflect-run.v1"
            or receipt.get("status") not in ("completed", "failed")
            or type(count) is not int
            or not 0 <= count <= MAX_LIMIT
        ):
            invalid += 1
            continue
        runs.append(receipt)
    return runs, invalid


def _candidates(target: Path, run: dict[str, Any]) -> list[dict[str, Any]]:
    # Construct from the validated directory name, never from stored paths.
    path = target / ROOT / run["run_id"] / "candidates.jsonl"
    raw = io.text(path, limit=io.MAX_STATE_BYTES, private=True) or ""
    candidates = [json.loads(line) for line in raw.splitlines()]
    if len(candidates) != run["candidate_count"] or len(candidates) > MAX_LIMIT:
        raise ValueError("reflect candidate count mismatch")
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("schema") != "brigade.memory-reflect-candidate.v1":
            raise ValueError("invalid retained reflect candidate")
        stored_id = candidate.get("candidate_id")
        content = {k: v for k, v in candidate.items() if k != "candidate_id"}
        if stored_id != "candidate-" + io.sha256(io.json_bytes(content))[:24]:
            raise ValueError("reflect candidate digest mismatch")
    return candidates


def inspect_payload(*, target: Path, command: str, candidate_id: str | None = None) -> dict[str, Any]:
    if command == "show" and not re.fullmatch(r"candidate-[0-9a-f]{24}", candidate_id or ""):
        raise ValueError("invalid candidate id")
    runs, invalid = _runs(target)
    if command == "status":
        return {
            "status": "ok",
            "run_count": len(runs),
            "invalid_runs": invalid,
            "latest_run": runs[0] if runs else None,
        }
    successful = [run for run in runs if run["status"] == "completed"]
    for run in successful:
        try:
            candidates = _candidates(target, run)
        except (OSError, ValueError, UnicodeError, RecursionError):
            invalid += 1
            continue
        if command == "candidates":
            return {"status": "ok", "run_id": run["run_id"], "candidates": candidates, "invalid_runs": invalid}
        for candidate in candidates:
            if candidate["candidate_id"] == candidate_id:
                return candidate
    if command == "candidates":
        return {"status": "ok", "run_id": None, "candidates": [], "invalid_runs": invalid}
    raise ValueError("candidate not found")


def _terminal_text(value: str) -> str:
    return "".join(
        f"\\x{ord(char):02x}" if (ord(char) < 32 or 127 <= ord(char) <= 159) and char not in "\n\r\t" else char
        for char in value
    )


def dispatch(args: argparse.Namespace) -> int:
    target = Path(args.target.absolute())
    try:
        with io.directory(target):
            pass
        if args.memory_reflect_command == "run":
            payload = run_payload(target=target, scope=args.scope, limit=args.limit, dry_run=args.dry_run)
        else:
            payload = inspect_payload(
                target=target, command=args.memory_reflect_command, candidate_id=getattr(args, "candidate_id", None)
            )
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        payload = {"status": "refused", "error": str(exc)}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(f"memory reflect: {payload.get('status', payload.get('kind', 'ok'))}")
        if "error" in payload:
            print(_terminal_text(f"error: {payload['error']}"))
        if "latest_run" in payload:
            print(json.dumps(payload, sort_keys=True, indent=2))
        candidates = payload.get("candidates", [payload] if "candidate_id" in payload else [])
        for candidate in candidates:
            print(_terminal_text(f"{candidate['candidate_id']} {candidate['kind']}: {candidate['summary']}"))
            if getattr(args, "dry_run", False) or args.memory_reflect_command == "show":
                if candidate["diff"]:
                    print(_terminal_text(candidate["diff"]), end="" if candidate["diff"].endswith("\n") else "\n")
    return 1 if payload.get("status") == "failed" else 2 if payload.get("status") == "refused" else 0
