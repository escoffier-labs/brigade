"""Explicit, receipted patrol actions using existing mutation fences."""

from __future__ import annotations

import hashlib
import io
import json
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import (
    fleet_claim_release,
    fleet_client,
    fleet_client_messages,
    localio,
    run_reap,
    runguard,
    runs_patrol_receipts,
)
from .run_lane_state import Lane, Thresholds, _journal
from .work_cmd import claiming, ledger

ACTIONS = frozenset({"reap", "release-claim", "escalate"})
TERMINAL = frozenset({"complete", "failed", "orphaned"})
ESCALATE_STATES = frozenset({"dead", "orphaned", "stalled", "zombie", "stranded", "lost-worktree", "unknown"})


def _history(root: Path) -> list[dict[str, Any]]:
    """Fail closed on unreadable history rather than forgetting action fences."""
    actions: list[dict[str, Any]] = []
    for directory in root.iterdir():
        if directory.name.startswith("."):
            continue
        payload = runs_patrol_receipts.read_receipt(directory)
        if payload is not None:
            actions.extend(payload["actions"])
    return actions


def _state(lane: Lane, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": row["state"],
        "run_status": lane.run.get("status") if lane.run else None,
        "claim_id": lane.claim.get("claim_id") if lane.claim else None,
        "lock_state": lane.lock_state,
    }


def _reap(lane: Lane, row: dict[str, Any], thresholds: Thresholds, now: datetime) -> dict[str, Any]:
    if row["state"] not in {"dead", "orphaned"} or lane.run_dir is None:
        return {"status": "refused", "reason": "lane is not dead or orphaned"}
    bound = run_reap._bind_contained_run_dir(lane.run_dir)
    if bound is None:
        return {"status": "refused", "reason": "run directory could not be bound"}
    try:
        raw, meta = run_reap._read_bound_run_bytes(bound)
        expected = next((item.digest for item in lane.evidence if item.source == "run.json"), None)
        if raw is None or meta is None or run_reap._run_fingerprint(raw) != expected:
            return {"status": "refused", "reason": "run changed since observation"}
        won, lost = run_reap._reap_one(
            lane.run_dir, meta, fingerprint=expected, older_than=thresholds.older_than, now=now, bound=bound
        )
        if won is not None:
            return {
                "status": "applied",
                "result": won,
                "after": {"state": "orphaned", "run_status": "orphaned", "lock_state": "absent"},
            }
        return {"status": "refused", "reason": (lost or {}).get("reason", "reap refused")}
    finally:
        bound.close()


def _journal_digest(lane: Lane, now: datetime) -> str | None:
    fresh = Lane(lane.lane_id, run_dir=lane.run_dir, run=lane.run)
    _journal(fresh, now)
    return next((item.digest for item in fresh.evidence if item.source == "events/lifecycle.jsonl"), None)


def _release_local(
    lane: Lane, row: dict[str, Any], thresholds: Thresholds, now: datetime, target: Path
) -> dict[str, Any]:
    if lane.claim is None or lane.task is None:
        return {"status": "skipped", "reason": "no ledger claim"}
    if lane.run_dir is None or row["state"] not in TERMINAL | {"dead"}:
        return {"status": "refused", "reason": "claim has no terminal or dead run proof"}
    bound = run_reap._bind_contained_run_dir(lane.run_dir)
    if bound is None:
        return {"status": "refused", "reason": "run directory could not be bound"}
    try:
        # Ledger CAS and run binding stay held through the write. Taking the run
        # lock also excludes a concurrent resume after the dead-owner proof.
        with ledger._task_ledger_lock(target), run_reap._bound_transaction(bound):
            raw, meta = run_reap._read_bound_run_bytes(bound)
            expected = next((item.digest for item in lane.evidence if item.source == "run.json"), None)
            if raw is None or meta is None or run_reap._run_fingerprint(raw) != expected:
                return {"status": "refused", "reason": "run changed since observation"}
            workspace = runguard.resolve_run_lock_workspace(meta, lane.run_dir)
            if workspace is None or runguard.run_lock_state(workspace, lane.run_dir) not in {"stale", "absent"}:
                return {"status": "refused", "reason": "live or foreign run lock"}
            with ExitStack() as locks:
                try:
                    locks.enter_context(
                        runguard.run_lock(workspace, run_dir=lane.run_dir, wait_seconds=0, stale_action="claim")
                    )
                except runguard.RunLockError:
                    return {"status": "refused", "reason": "run lock acquisition refused"}
                if not bound.still_bound():
                    return {"status": "refused", "reason": "run changed since observation"}
                current_raw, _ = run_reap._read_bound_run_bytes(bound)
                if current_raw is None or run_reap._run_fingerprint(current_raw) != expected:
                    return {"status": "refused", "reason": "run changed after lock acquisition"}
                expected_journal = next(
                    (item.digest for item in lane.evidence if item.source == "events/lifecycle.jsonl"), None
                )
                if _journal_digest(lane, now) != expected_journal:
                    return {"status": "refused", "reason": "journal changed since observation"}
                current = ledger._read_task_ledger(target)
                task = next((task for task in current["tasks"] if task.get("id") == lane.task["id"]), None)
                if task is None:
                    return {"status": "refused", "reason": "ledger task disappeared"}
                result = claiming.apply_release_to_task(current, task, claim_id=lane.claim["claim_id"])
                ledger._write_task_ledger(target, current)
                return {"status": "applied", "result": result, "after": {"claim_id": None, "lock_state": "absent"}}
    finally:
        bound.close()


def _release_hub(lane: Lane, row: dict[str, Any]) -> dict[str, Any]:
    if lane.run_dir is None or row["state"] not in TERMINAL | {"dead"} or lane.run is None:
        return {"status": "refused", "reason": "no terminal or dead run proof"}
    bound = run_reap._bind_contained_run_dir(lane.run_dir)
    if bound is None:
        return {"status": "refused", "reason": "run directory could not be bound"}
    try:
        with run_reap._bound_transaction(bound):
            raw, _ = run_reap._read_bound_run_bytes(bound)
            expected = next((item.digest for item in lane.evidence if item.source == "run.json"), None)
            expected_journal = next(
                (item.digest for item in lane.evidence if item.source == "events/lifecycle.jsonl"), None
            )
            if raw is None or run_reap._run_fingerprint(raw) != expected or not lane.evidence:
                return {"status": "refused", "reason": "run changed since observation"}
            if _journal_digest(lane, lane.evidence[0].observed_at) != expected_journal:
                return {"status": "refused", "reason": "journal changed since observation"}
    finally:
        bound.close()
    if not fleet_client.load_fleet_config().get("hub_url"):
        return {"status": "skipped", "reason": "no Hub configured"}
    workspace = runguard.resolve_run_lock_workspace(lane.run, lane.run_dir)
    if workspace is None:
        return {"status": "refused", "reason": "lock workspace unavailable"}
    output, errors = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        code = fleet_claim_release._release_claim(
            str(workspace),
            as_path=True,
            node_override=None,
            force=False,
            as_json=True,
            expected_run_dir=lane.run_dir,
        )
    if code:
        return {"status": "refused", "reason": "Hub claim release refused"}
    result = json.loads(output.getvalue())
    return {"status": "applied" if result.get("released") else "skipped", "result": result}


def apply_actions(
    *,
    target: Path,
    lanes: list[Lane],
    rows: list[dict[str, Any]],
    payload: dict[str, Any],
    actions: list[str],
    thresholds: Thresholds,
    now: datetime,
    escalate_via: str | None,
    escalate_to: str | None,
) -> None:
    """Reserve each attempt durably before mutation. Never retry uncertain work.

    The caller creates the receipt and holds its shared lock. That lock covers
    history inspection, reservations, side effects, and final receipt updates.
    A pending entry left by a crash fences retries just like an applied entry.
    """
    receipt = target / payload["receipt"]
    root = receipt.parent.parent
    previous = _history(root)
    for lane, row in zip(lanes, rows, strict=True):
        for action in actions:
            scopes = ("ledger", "hub") if action == "release-claim" else ("local",)
            for scope in scopes:
                before = _state(lane, row)
                base = hashlib.sha256(f"{lane.lane_id}|{row['classification_digest']}|{action}".encode()).hexdigest()
                key = base
                escalation_number = None
                if action == "escalate":
                    if row["state"] not in ESCALATE_STATES:
                        continue
                    if not escalate_via and any(
                        item.get("idempotency_key") == base and item.get("status") == "recorded" for item in previous
                    ):
                        continue
                    escalations = [
                        item
                        for item in previous
                        if item.get("lane_id") == lane.lane_id
                        and item.get("action") == "escalate"
                        and item.get("status") in {"pending", "applied", "failed"}
                    ]
                    if len(escalations) >= 2:
                        continue
                    stamps = [run_reap._parse_timestamp(item.get("attempted_at")) for item in escalations]
                    if any(stamp is None or now - stamp < timedelta(hours=4) for stamp in stamps):
                        continue
                    escalation_number = len(escalations) + 1
                    if escalations:
                        key = hashlib.sha256(f"{base}|{escalation_number}".encode()).hexdigest()
                elif any(
                    item.get("idempotency_key") == key
                    and item.get("scope") == scope
                    and item.get("status") in {"pending", "applied", "failed"}
                    for item in previous
                ):
                    continue
                record: dict[str, Any] = {
                    "lane_id": lane.lane_id,
                    "action": action,
                    "scope": scope,
                    "idempotency_key": key,
                    "attempted_at": now.isoformat(),
                    "before": before,
                    "after": dict(before),
                    "status": "pending",
                }
                if action == "escalate":
                    record.update(
                        severity="urgent" if row["state"] in {"dead", "zombie"} else "high",
                        escalation_number=escalation_number,
                    )
                payload["actions"].append(record)
                localio.write_json(receipt, payload)
                try:
                    if action == "reap":
                        result = _reap(lane, row, thresholds, now)
                    elif action == "release-claim":
                        result = (
                            _release_local(lane, row, thresholds, now, target)
                            if scope == "ledger"
                            else _release_hub(lane, row)
                        )
                    else:
                        result = {
                            "status": "applied" if escalate_via else "recorded",
                            "severity": record["severity"],
                            "escalation_number": escalation_number,
                        }
                        if escalate_via:
                            result["delivery"] = fleet_client_messages.send(
                                recipient=escalate_to or "",
                                subject=f"Patrol: {row['state']}",
                                body=f"Lane {row['lane_id']}: {row['reason']}",
                                reference=str(receipt),
                                priority=result["severity"],
                                client_id=key,
                            )
                    after = {**before, **result.pop("after", {})}
                    record.update(result, after=after)
                    if record["status"] == "applied" and action == "reap":
                        # Carry our sanctioned transition forward for a following
                        # release without accepting an unrelated snapshot change.
                        assert lane.run_dir is not None
                        raw, meta = run_reap._read_run_bytes(lane.run_dir)
                        if (
                            raw is None
                            or meta is None
                            or meta.get("status") != "orphaned"
                            or meta.get("orphaned_at") != record["result"]["orphaned_at"]
                            or meta.get("claim_id") != (lane.run or {}).get("claim_id")
                        ):
                            raise ValueError("reaped snapshot changed before refresh")
                        lane.run = meta
                        lane.lock_state = "absent"
                        journal_digest = _journal_digest(lane, now)
                        lane.evidence = [
                            replace(item, digest=run_reap._run_fingerprint(raw))
                            if item.source == "run.json"
                            else replace(item, digest=journal_digest)
                            if item.source == "events/lifecycle.jsonl"
                            else item
                            for item in lane.evidence
                        ]
                        row = {**row, "state": "orphaned"}
                    elif record["status"] == "applied" and scope == "ledger":
                        lane.claim = None
                        lane.lock_state = "absent"
                except (OSError, ValueError, RuntimeError) as exc:
                    record.update(status="failed", reason=type(exc).__name__)
                previous.append(record)
                localio.write_json(receipt, payload)
