"""Report local lane state, optionally persisting a patrol receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from . import localio, run_reap, runs_cmd
from .run_lane_state import STATES, Thresholds, classify, collect_lanes
from . import runs_patrol_actions, runs_patrol_receipts

SCHEMA = "brigade.runs_patrol.v1"


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("patrol", help="Report local run and claim states, with explicit optional actions.")
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
    parser.add_argument("--apply", default="", help="Explicit actions: reap,release-claim,escalate. Writes a receipt.")
    parser.add_argument("--escalate-via", choices=("fleet-msg",), help="Send escalation through fleet messages.")
    parser.add_argument("--escalate-to", help="Fleet message recipient principal. Requires --escalate-via.")
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
        apply=args.apply,
        escalate_via=args.escalate_via,
        escalate_to=args.escalate_to,
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
    path = parent / "patrol.json"
    if path.exists() or path.is_symlink():
        raise ValueError("patrol receipt already exists")
    localio.write_text_atomic(path, raw)


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
    apply: str = "",
    escalate_via: str | None = None,
    escalate_to: str | None = None,
    now: datetime | None = None,
) -> int:
    written_receipt: Path | None = None
    payload: dict | None = None
    try:
        actions = list(dict.fromkeys(value.strip() for value in apply.split(",") if value.strip()))
        if set(actions) - runs_patrol_actions.ACTIONS:
            raise ValueError("apply contains an unknown action")
        if apply and not actions:
            raise ValueError("apply requires explicit action names")
        if escalate_via not in {None, "fleet-msg"}:
            raise ValueError("unknown escalation transport")
        if (escalate_via or escalate_to) and (
            "escalate" not in actions or not escalate_via or not escalate_to or not escalate_to.strip()
        ):
            raise ValueError("fleet-msg requires --apply escalate, --escalate-via and --escalate-to")
        receipt = receipt or bool(actions)
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
                "hub_claim": lane.hub_claim,
                "display_only": lane.display_only,
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
            with runs_patrol_receipts.receipt_lock(target, report_only=not actions) as locked:
                _write_receipt(target, patrol_id, runs_patrol_receipts.bounded_receipt_text(payload))
                written_receipt = target / payload["receipt"]
                if locked:
                    runs_patrol_receipts.prune_reports(
                        written_receipt.parent.parent, clock, keep=written_receipt.parent, strict=bool(actions)
                    )
                if actions:
                    runs_patrol_actions.apply_actions(
                        target=target,
                        lanes=lanes,
                        rows=rows,
                        payload=payload,
                        actions=actions,
                        thresholds=thresholds,
                        now=clock,
                        escalate_via=escalate_via,
                        escalate_to=escalate_to,
                    )
                    raw = json.dumps(payload, indent=2, sort_keys=True)
    except (OSError, ValueError, RuntimeError, OverflowError) as exc:
        if written_receipt is not None and payload is not None:
            payload.update(exit_code=2, error=type(exc).__name__)
            try:
                runs_patrol_receipts.write_receipt(written_receipt, payload)
            except OSError:
                pass  # The original failure still determines the exit disposition.
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


def brief_summary(target: Path) -> dict[str, object]:
    """Bound the brief to ten run snapshots and bounded claim rows, with no Hub reads or receipt."""
    started = run_reap._utc_now(None)
    try:
        lanes = collect_lanes(target, now=started, limit=20, scan_limit=10, claim_limit=10, include_hub=False)
        clock = run_reap._utc_now(None)
        counts = Counter(classify(lane, clock, Thresholds(), observation_started_at=started).state for lane in lanes)
        return {"counts": dict(sorted(counts.items())), "truncated": lanes.truncated}
    except (OSError, ValueError, RuntimeError, OverflowError):
        return {"counts": {"unknown": 1}, "truncated": False}
