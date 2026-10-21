"""Report local lane state, optionally persisting a patrol receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from . import run_reap, runs_cmd
from .run_lane_state import STATES, Thresholds, classify, collect_lanes

SCHEMA = "brigade.runs_patrol.v1"


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("patrol", help="Report local run and claim states without changing them.")
    parser.add_argument("--target", type=Path, default=Path("."), help="Workspace to inspect.")
    for name, default in (
        ("older-than", "2h"),
        ("idle-after", "15m"),
        ("stall-after", "30m"),
        ("stranded-after", "24h"),
    ):
        parser.add_argument(f"--{name}", default=default, help=f"Positive duration (default {default}).")
    parser.add_argument("--limit", type=int, default=50, help="Maximum returned lanes and ledger claims (default 50).")
    parser.add_argument("--json", action="store_true", help=f"Emit the versioned {SCHEMA} contract.")
    parser.add_argument("--receipt", action="store_true", help="Write .brigade/patrol/<id>/patrol.json.")
    parser.add_argument("--fail-on", default="", help="Comma-separated states that return exit status 1.")


def dispatch(args: argparse.Namespace) -> int:
    return patrol(
        target=args.target,
        older_than=args.older_than,
        idle_after=args.idle_after,
        stall_after=args.stall_after,
        stranded_after=args.stranded_after,
        limit=args.limit,
        json_output=args.json,
        receipt=args.receipt,
        fail_on=args.fail_on,
    )


def _write_receipt(target: Path, patrol_id: str, raw: str) -> None:
    # Refuse existing symlinks before any mkdir or file write. The run and
    # ledger inputs stay untouched even when a receipt is explicitly asked for.
    parent = target
    for name in (".brigade", "patrol", patrol_id):
        parent = parent / name
        if parent.is_symlink():
            raise ValueError("receipt path must not be symlinked")
        parent.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(parent / "patrol.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(raw + "\n")


def patrol(
    *,
    target: Path,
    older_than: str = "2h",
    idle_after: str = "15m",
    stall_after: str = "30m",
    stranded_after: str = "24h",
    limit: int = 50,
    json_output: bool = False,
    receipt: bool = False,
    fail_on: str = "",
    now: datetime | None = None,
) -> int:
    try:
        thresholds = Thresholds(
            older_than=run_reap.parse_older_than(older_than),
            idle_after=run_reap.parse_older_than(idle_after),
            stall_after=run_reap.parse_older_than(stall_after),
            stranded_after=run_reap.parse_older_than(stranded_after),
        )
        if thresholds.idle_after >= thresholds.stall_after:
            raise ValueError("idle-after must be less than stall-after")
        selected = {value.strip() for value in fail_on.split(",") if value.strip()}
        if selected - STATES:
            raise ValueError("fail-on contains an unknown state")
        collection_started_at = run_reap._utc_now(now)
        target = target.expanduser().resolve()
        lanes = collect_lanes(target, now=collection_started_at, limit=limit)
        # Progress and starts observed during collection can postdate its start.
        clock = run_reap._utc_now(now)
        rows = []
        for lane in lanes:
            classification = classify(lane, clock, thresholds, observation_started_at=collection_started_at)
            evidence = [asdict(item) for item in classification.evidence]
            for item in evidence:
                item["source"] = runs_cmd._clean_str(item["source"], 240)
                item["observed_at"] = item["observed_at"].isoformat()
                if item["timestamp"] is not None:
                    item["timestamp"] = item["timestamp"].isoformat()
            row = {
                "lane_id": runs_cmd._clean_str(lane.lane_id, 240),
                "run_id": lane.run_dir.name if lane.run_dir else None,
                "task_id": runs_cmd._clean_str(lane.task.get("id"), 240) if lane.task else None,
                "claim_id": runs_cmd._clean_str(lane.claim.get("claim_id"), 240) if lane.claim else None,
                "intent": lane.intent,
                "state": classification.state,
                "reason": runs_cmd._clean_str(classification.reason, 240),
                "infrastructure_failure": classification.infrastructure_failure,
                "evidence": evidence,
            }
            # Observation times change on every poll. Hash the classification and
            # source observations so unchanged evidence keeps a stable identity.
            stable = {
                **row,
                "evidence": [{key: value for key, value in item.items() if key != "observed_at"} for item in evidence],
            }
            row["classification_digest"] = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()
            rows.append(row)
        exit_code = int(any(row["state"] in selected for row in rows) or (lanes.truncated and "unknown" in selected))
        patrol_id = clock.strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:12]
        payload = {
            "schema": SCHEMA,
            "patrol_id": patrol_id,
            "observed_at": clock.isoformat(),
            "scan_duration_seconds": (clock - collection_started_at).total_seconds(),
            "thresholds": thresholds.as_dict(),
            "limit": limit,
            "truncated": lanes.truncated,
            "lanes": rows,
            "actions": [],
            "fail_on": sorted(selected),
            "exit_code": exit_code,
            "receipt": f".brigade/patrol/{patrol_id}/patrol.json" if receipt else None,
        }
        raw = json.dumps(payload, indent=2, sort_keys=True)
        if receipt:
            _write_receipt(target, patrol_id, raw)
    except (OSError, ValueError, RuntimeError, OverflowError) as exc:
        print(f"error: patrol: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(raw)
    elif rows:
        for row in rows:
            print(f"{row['lane_id']}: {row['state']} ({row['reason']})")
    else:
        print("No local lanes found.")
    if lanes.truncated and not json_output:
        print("Patrol scan or lane report truncated. Increase --limit or inspect sources directly.")
    return exit_code
