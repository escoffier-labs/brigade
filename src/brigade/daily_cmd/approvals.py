"""Agent-facing daily driver over local Brigade operator state."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import json
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, redirect_stdout
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import (
    center_cmd,
    context_cmd,
    handoff_cmd,
    memory_cmd,
    notifications_cmd,
    phases_cmd,
    security_cmd,
    toml_compat as tomllib,
    tools_cmd,
    work_cmd,
)
from .. import runguard
from ..localio import read_json_dict as _read_json, utc_now as _now, write_json as _write_json
from ..render import emit

from .config import (
    DEFAULT_CONFIG,
    SCHEMA_VERSION,
    _approvals_root,
    _fingerprint,
    _load_config,
    _runs_root,
    _slug,
)
from . import candidates as _candidates_mod
from . import run_loop as _run_loop_mod
from . import status_plan as _status_plan_mod

_APPROVAL_REFERENCE_FIELDS = (
    "approval_id",
    "source",
    "fingerprint",
    "source_fingerprint",
    "contract_fingerprint",
    "evidence_fingerprint",
)


class ApprovalClaimError(RuntimeError):
    """A Daily approval could not be claimed from its current stored state."""


@dataclass(frozen=True)
class ApprovalClaim:
    decided_at: str
    claimed_at: str
    already_claimed: bool


def _config_fingerprint(config: dict[str, Any]) -> str:
    stable = {key: config.get(key) for key in sorted(DEFAULT_CONFIG)}
    return _fingerprint(stable)


def _approval_id(action: dict[str, Any], config: dict[str, Any]) -> str:
    source = _slug(action.get("source_subsystem"))
    local = _slug(action.get("source_local_id"))
    digest = _fingerprint(
        {
            "action_id": action.get("action_id"),
            "source_fingerprint": action.get("source_fingerprint"),
            "config_fingerprint": _config_fingerprint(config),
        }
    )[:12]
    return f"approval-{source}-{local}-{digest}"


def _approval_path(target: Path, approval_id: str) -> Path:
    return _approvals_root(target) / approval_id / "approval.json"


def _approval_lock_path(target: Path, approval_id: str) -> Path:
    return _approvals_root(target).parent / ".approval-locks" / f"{approval_id}.lock"


def _acquire_approval_store_lock(path: Path) -> runguard._LockOwnership:
    deadline = time.monotonic() + 5.0
    while True:
        try:
            return runguard._acquire_lock(path)
        except runguard.RunLockError as exc:
            if "another brigade run appears active" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


@contextmanager
def _approval_store_lock(target: Path, approval_id: str) -> Iterator[None]:
    path = _approval_lock_path(target, approval_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    ownership = _acquire_approval_store_lock(path)
    try:
        yield
    finally:
        runguard._release_lock(path, ownership)


def _read_approvals(target: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    return _run_loop_mod._iter_receipts(_approvals_root(target), "approval.json")


def _write_approval_unlocked(target: Path, approval: dict[str, Any]) -> dict[str, Any]:
    approval_id = str(approval["approval_id"])
    approval["path"] = str(_approvals_root(target) / approval_id)
    _write_json(_approval_path(target, approval_id), approval)
    return approval


def _write_approval(target: Path, approval: dict[str, Any]) -> dict[str, Any]:
    approval_id = str(approval["approval_id"])
    with _approval_store_lock(target, approval_id):
        return _write_approval_unlocked(target, approval)


def _find_approval(target: Path, approval_id: str) -> dict[str, Any] | None:
    return _read_json(_approval_path(target, approval_id))


def _matching_approvals(target: Path, action: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    approvals, _ = _read_approvals(target)
    config_fp = _config_fingerprint(config)
    return [
        approval
        for approval in approvals
        if approval.get("selected_action_id") == action.get("action_id")
        and approval.get("source_fingerprint") == action.get("source_fingerprint")
        and approval.get("config_fingerprint") == config_fp
    ]


def _ensure_approval(
    target: Path, plan_data: dict[str, Any], action: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    approval_id = _approval_id(action, config)
    with _approval_store_lock(target, approval_id):
        matches = _matching_approvals(target, action, config)
        for status in ("pending", "approved", "rejected", "held", "consumed"):
            existing = next((approval for approval in matches if approval.get("status") == status), None)
            if existing is not None:
                return existing
        evidence_refs = action.get("evidence_refs")
        approval = {
            "schema_version": SCHEMA_VERSION,
            "approval_id": approval_id,
            "created_at": _now().isoformat(),
            "status": "pending",
            "source_plan_id": plan_data.get("plan_id"),
            "selected_action_id": action.get("action_id"),
            "selected_action": action,
            "selected_adapter": _candidates_mod._adapter_for(action),
            "source_subsystem": action.get("source_subsystem"),
            "source_local_id": action.get("source_local_id"),
            "source_fingerprint": action.get("source_fingerprint"),
            "config_fingerprint": _config_fingerprint(config),
            "acceptance": action.get("acceptance") if isinstance(action.get("acceptance"), list) else [],
            "safe_summary": action.get("safe_summary"),
            "evidence_refs": evidence_refs if isinstance(evidence_refs, list) else [],
            "risk_level": action.get("risk_level"),
            "approval_reason": action.get("approval_reason") or "explicit approval required",
            "config": config,
            "suggested_next_command": action.get("suggested_next_command"),
            "reviewed_at": None,
            "review_reason": None,
            "consumed_run_id": None,
        }
        return _write_approval_unlocked(target, approval)


def _current_action_for_approval(target: Path, approval: dict[str, Any]) -> dict[str, Any] | None:
    selected_action = raw_action if isinstance((raw_action := approval.get("selected_action")), dict) else {}
    action_type = str(selected_action.get("action_type") or "")
    source_id = str(approval.get("source_local_id") or selected_action.get("source_local_id") or "")
    candidate_builders = {
        "run-task": _candidates_mod._pending_task_candidates,
        "promote-import": _candidates_mod._pending_import_candidates,
        "start-center-action": _candidates_mod._center_action_candidates,
        "import-readiness-issues": _candidates_mod._readiness_candidates,
        "import-handoff-issues": _candidates_mod._handoff_ingest_candidates,
        "build-operator-report": _candidates_mod._report_candidate,
    }
    builder = candidate_builders.get(action_type)
    if builder is None:
        return (
            selected_action
            if selected_action and not _status_plan_mod._evidence_blockers(target, selected_action)
            else None
        )
    for action in builder(target):
        if action.get("source_local_id") == source_id:
            return action
    return None


def _approval_blockers(target: Path, approval: dict[str, Any], config: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    status = str(approval.get("status") or "")
    if status != "approved":
        blockers.append(f"approval status is {status or 'unknown'}")
    if approval.get("consumed_run_id"):
        blockers.append(f"approval already consumed by {approval.get('consumed_run_id')}")
    if approval.get("config_fingerprint") != _config_fingerprint(config):
        blockers.append("daily config changed since approval")
    action = approval.get("selected_action") if isinstance(approval.get("selected_action"), dict) else None
    blockers.extend(_status_plan_mod._evidence_blockers(target, action))
    current = _current_action_for_approval(target, approval)
    if current is None:
        blockers.append("selected action is no longer available")
    elif current.get("source_fingerprint") != approval.get("source_fingerprint"):
        blockers.append("selected action fingerprint changed since approval")
    return list(dict.fromkeys(blockers))


def _approval_claim_reference(approval: Mapping[str, Any]) -> dict[str, str]:
    components = {
        "approval_id": approval.get("approval_id"),
        "source": "daily",
        "source_fingerprint": approval.get("source_fingerprint"),
        "contract_fingerprint": approval.get("config_fingerprint"),
        "evidence_fingerprint": _fingerprint(approval.get("evidence_refs", [])),
    }
    if any(not isinstance(value, str) or not value for value in components.values()):
        raise ApprovalClaimError("daily approval record is missing required fingerprints")
    return {
        **{key: str(value) for key, value in components.items()},
        "fingerprint": _fingerprint(components),
    }


def claim_for_run(
    target: Path,
    approval_id: str,
    reference: Mapping[str, str],
    run_id: str,
) -> ApprovalClaim:
    """Atomically validate and claim one approved Daily record for a run."""
    with _approval_store_lock(target, approval_id):
        approval = _find_approval(target, approval_id)
        if approval is None:
            raise ApprovalClaimError(f"daily approval not found: {approval_id}")
        actual = _approval_claim_reference(approval)
        if any(reference.get(field) != actual[field] for field in _APPROVAL_REFERENCE_FIELDS):
            raise ApprovalClaimError("daily approval fingerprints changed before claim")
        status = str(approval.get("status") or "")
        consumed_by = approval.get("consumed_run_id")
        if status == "consumed" and consumed_by == run_id:
            decided_at = approval.get("reviewed_at")
            if not isinstance(decided_at, str) or not decided_at:
                raise ApprovalClaimError("daily approval decision has no review timestamp")
            claimed_at = approval.get("consumed_at")
            if not isinstance(claimed_at, str) or not claimed_at:
                raise ApprovalClaimError("daily approval claim has no consumption timestamp")
            return ApprovalClaim(
                decided_at=decided_at,
                claimed_at=claimed_at,
                already_claimed=True,
            )
        if status != "approved":
            raise ApprovalClaimError(f"daily approval is {status or 'pending'}")
        config, _ = _load_config(target)
        blockers = _approval_blockers(target, approval, config)
        if blockers:
            raise ApprovalClaimError(f"daily approval is stale or blocked: {blockers[0]}")
        decided_at = approval.get("reviewed_at")
        if not isinstance(decided_at, str) or not decided_at:
            raise ApprovalClaimError("daily approval decision has no review timestamp")
        claimed_at = _now().isoformat()
        approval["status"] = "consumed"
        approval["consumed_run_id"] = run_id
        approval["consumed_at"] = claimed_at
        _write_approval_unlocked(target, approval)
        return ApprovalClaim(
            decided_at=decided_at,
            claimed_at=claimed_at,
            already_claimed=False,
        )


def reserve_for_run(
    target: Path,
    approval_id: str,
    reference: Mapping[str, str],
    run_id: str,
    token_fingerprint: str,
) -> ApprovalClaim:
    """Reserve a rotatable one-shot approval claim without consuming it."""
    with _approval_store_lock(target, approval_id):
        approval = _find_approval(target, approval_id)
        if approval is None:
            raise ApprovalClaimError(f"daily approval not found: {approval_id}")
        actual = _approval_claim_reference(approval)
        if any(reference.get(field) != actual[field] for field in _APPROVAL_REFERENCE_FIELDS):
            raise ApprovalClaimError("daily approval fingerprints changed before reservation")
        existing = approval.get("approval_claim")
        if existing is not None:
            if not isinstance(existing, Mapping):
                raise ApprovalClaimError("daily approval claim is malformed")
            existing_state = existing.get("state")
            existing_run_id = existing.get("run_id")
            if existing_state == "redeemed":
                raise ApprovalClaimError("daily approval claim is already redeemed")
            if existing_state != "reserved" or not isinstance(existing_run_id, str):
                raise ApprovalClaimError("daily approval claim is malformed")
            if existing_run_id != run_id:
                raise ApprovalClaimError(f"daily approval claim is reserved by {existing_run_id}")
        if approval.get("status") != "approved":
            raise ApprovalClaimError(f"daily approval is {approval.get('status') or 'pending'}")
        config, _ = _load_config(target)
        blockers = _approval_blockers(target, approval, config)
        if blockers:
            raise ApprovalClaimError(f"daily approval is stale or blocked: {blockers[0]}")
        decided_at = approval.get("reviewed_at")
        if not isinstance(decided_at, str) or not decided_at:
            raise ApprovalClaimError("daily approval decision has no review timestamp")
        reserved_at = _now().isoformat()
        approval["approval_claim"] = {
            "run_id": run_id,
            "state": "reserved",
            "reserved_at": reserved_at,
            "token_fingerprint": token_fingerprint,
            "redeemed_at": None,
        }
        _write_approval_unlocked(target, approval)
        return ApprovalClaim(
            decided_at=decided_at,
            claimed_at=reserved_at,
            already_claimed=isinstance(existing, Mapping),
        )


def redeem_for_run(target: Path, approval_id: str, reference: Mapping[str, str], token: str) -> ApprovalClaim:
    """CAS one reserved claim to redeemed immediately before its action."""
    from .. import runs_cmd

    with _approval_store_lock(target, approval_id):
        approval = _find_approval(target, approval_id)
        if approval is None:
            raise ApprovalClaimError(f"daily approval not found: {approval_id}")
        actual = _approval_claim_reference(approval)
        if any(reference.get(field) != actual[field] for field in _APPROVAL_REFERENCE_FIELDS):
            raise ApprovalClaimError("daily approval fingerprints changed before redemption")
        claim = approval.get("approval_claim")
        if not isinstance(claim, dict) or claim.get("state") != "reserved":
            raise ApprovalClaimError("daily approval claim is not reserved")
        if claim.get("token_fingerprint") != runs_cmd._request_digest(token):
            raise ApprovalClaimError("daily approval claim token does not match")
        run_id = claim.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ApprovalClaimError("daily approval claim has no run id")
        status = str(approval.get("status") or "")
        if status != "approved":
            raise ApprovalClaimError(f"daily approval is {status or 'pending'}")
        config, _ = _load_config(target)
        blockers = _approval_blockers(target, approval, config)
        if blockers:
            raise ApprovalClaimError(f"daily approval is stale or blocked: {blockers[0]}")
        redeemed_at = _now().isoformat()
        claim["state"] = "redeemed"
        claim["redeemed_at"] = redeemed_at
        approval["status"] = "consumed"
        approval["consumed_run_id"] = run_id
        approval["consumed_at"] = redeemed_at
        _write_approval_unlocked(target, approval)
        decided_at = approval.get("reviewed_at")
        if not isinstance(decided_at, str) or not decided_at:
            raise ApprovalClaimError("daily approval decision has no review timestamp")
        return ApprovalClaim(decided_at=decided_at, claimed_at=redeemed_at, already_claimed=False)


def record_redeemed_action_completed(
    target: Path,
    approval_id: str,
    reference: Mapping[str, str],
    run_receipt: Mapping[str, Any],
) -> bool:
    """Bind a successful Daily action receipt to its redeemed approval claim."""
    with _approval_store_lock(target, approval_id):
        approval = _find_approval(target, approval_id)
        if approval is None:
            raise ApprovalClaimError(f"daily approval not found: {approval_id}")
        actual = _approval_claim_reference(approval)
        if any(reference.get(field) != actual[field] for field in _APPROVAL_REFERENCE_FIELDS):
            raise ApprovalClaimError("daily approval fingerprints changed after action")
        claim = approval.get("approval_claim")
        if claim is None:
            return False
        if not isinstance(claim, Mapping) or claim.get("state") != "redeemed":
            raise ApprovalClaimError("daily approval claim changed after action")
        owner_run_id = claim.get("run_id")
        redeemed_at = claim.get("redeemed_at")
        if not isinstance(owner_run_id, str) or not owner_run_id:
            raise ApprovalClaimError("daily approval redeemed claim has no run id")
        if not isinstance(redeemed_at, str) or not redeemed_at:
            raise ApprovalClaimError("daily approval redeemed claim has no redemption timestamp")
        if (
            approval.get("status") != "consumed"
            or approval.get("consumed_run_id") != owner_run_id
            or approval.get("consumed_at") != redeemed_at
        ):
            raise ApprovalClaimError(f"daily approval redeemed source state is {approval.get('status') or 'unknown'}")

        daily_run_id = run_receipt.get("run_id")
        completed_at = run_receipt.get("completed_at")
        action_id = approval.get("selected_action_id")
        selected_action = raw_action if isinstance((raw_action := run_receipt.get("selected_action")), Mapping) else {}
        if (
            run_receipt.get("status") != "completed"
            or run_receipt.get("approval_id") != approval_id
            or run_receipt.get("selected_action_id") != action_id
            or selected_action.get("source_fingerprint") != approval.get("source_fingerprint")
            or not isinstance(daily_run_id, str)
            or not daily_run_id
            or not isinstance(completed_at, str)
            or not completed_at
        ):
            raise ApprovalClaimError("daily approval action receipt does not prove successful completion")

        completed = {
            "state": "completed",
            "owner_run_id": owner_run_id,
            "daily_run_id": daily_run_id,
            "action_id": action_id,
            "source_fingerprint": approval.get("source_fingerprint"),
            "completed_at": completed_at,
        }
        existing = approval.get("approval_action_receipt")
        if existing is not None and existing != completed:
            raise ApprovalClaimError("daily approval action receipt conflicts with stored completion")
        approval["approval_action_receipt"] = completed
        _write_approval_unlocked(target, approval)
        return True


def _redeemed_reconciliation_blockers(
    approval: Mapping[str, Any],
    config: dict[str, Any],
) -> list[str]:
    blockers: list[str] = []
    if approval.get("config_fingerprint") != _config_fingerprint(config):
        blockers.append("daily config changed since approval")
    selected_action = approval.get("selected_action")
    if not isinstance(selected_action, dict) or not selected_action:
        blockers.append("daily approval selected action is malformed")
    elif approval.get("selected_adapter") != _candidates_mod._adapter_for(selected_action):
        blockers.append("daily approval adapter changed since approval")
    return blockers


def _validate_redeemed_for_run_unlocked(
    target: Path,
    approval_id: str,
    reference: Mapping[str, str],
    run_id: str,
) -> ApprovalClaim:
    """Validate one redeemed Daily claim while its source-store lock is held."""
    approval = _find_approval(target, approval_id)
    if approval is None:
        raise ApprovalClaimError(f"daily approval not found: {approval_id}")
    actual = _approval_claim_reference(approval)
    if any(reference.get(field) != actual[field] for field in _APPROVAL_REFERENCE_FIELDS):
        raise ApprovalClaimError("daily approval fingerprints changed before reconciliation")
    claim = approval.get("approval_claim")
    if not isinstance(claim, Mapping) or claim.get("state") != "redeemed":
        raise ApprovalClaimError("daily approval has no redeemed claim to reconcile")
    claim_run_id = claim.get("run_id")
    if claim_run_id != run_id:
        owner = claim_run_id if isinstance(claim_run_id, str) and claim_run_id else "another run"
        raise ApprovalClaimError(f"daily approval redeemed claim belongs to {owner}")
    redeemed_at = claim.get("redeemed_at")
    if not isinstance(redeemed_at, str) or not redeemed_at:
        raise ApprovalClaimError("daily approval redeemed claim has no redemption timestamp")
    if (
        approval.get("status") != "consumed"
        or approval.get("consumed_run_id") != run_id
        or approval.get("consumed_at") != redeemed_at
    ):
        raise ApprovalClaimError(f"daily approval redeemed source state is {approval.get('status') or 'unknown'}")

    action_receipt = approval.get("approval_action_receipt")
    if not isinstance(action_receipt, Mapping) or action_receipt.get("state") != "completed":
        raise ApprovalClaimError("daily approval has no completed action receipt to reconcile")
    if action_receipt.get("owner_run_id") != run_id:
        raw_owner = action_receipt.get("owner_run_id")
        owner_text = str(raw_owner) if raw_owner else "another run"
        raise ApprovalClaimError(f"daily approval completed action receipt belongs to {owner_text}")
    daily_run_id = action_receipt.get("daily_run_id")
    completed_at = action_receipt.get("completed_at")
    if (
        action_receipt.get("action_id") != approval.get("selected_action_id")
        or action_receipt.get("source_fingerprint") != approval.get("source_fingerprint")
        or not isinstance(daily_run_id, str)
        or not daily_run_id
        or not isinstance(completed_at, str)
        or not completed_at
    ):
        raise ApprovalClaimError("daily approval completed action receipt is malformed")
    run_receipt = _read_json(_runs_root(target) / daily_run_id / "run.json")
    if not isinstance(run_receipt, Mapping):
        raise ApprovalClaimError("daily approval completed action receipt no longer matches its run")
    selected_action = (
        raw_sel_action if isinstance((raw_sel_action := run_receipt.get("selected_action")), Mapping) else {}
    )
    if (
        run_receipt.get("status") != "completed"
        or run_receipt.get("run_id") != daily_run_id
        or run_receipt.get("approval_id") != approval_id
        or run_receipt.get("selected_action_id") != approval.get("selected_action_id")
        or selected_action.get("source_fingerprint") != approval.get("source_fingerprint")
        or run_receipt.get("completed_at") != completed_at
    ):
        raise ApprovalClaimError("daily approval completed action receipt no longer matches its run")
    config, _ = _load_config(target)
    blockers = _redeemed_reconciliation_blockers(approval, config)
    if blockers:
        raise ApprovalClaimError(f"daily approval is stale or blocked: {blockers[0]}")
    decided_at = approval.get("reviewed_at")
    if not isinstance(decided_at, str) or not decided_at:
        raise ApprovalClaimError("daily approval decision has no review timestamp")
    return ApprovalClaim(
        decided_at=decided_at,
        claimed_at=redeemed_at,
        already_claimed=True,
    )


@contextmanager
def redeemed_reconciliation_guard(
    target: Path,
    approval_id: str,
    reference: Mapping[str, str],
    run_id: str,
) -> Iterator[ApprovalClaim]:
    """Hold the Daily source-store lock through outcome reconciliation."""
    with _approval_store_lock(target, approval_id):
        yield _validate_redeemed_for_run_unlocked(target, approval_id, reference, run_id)


def validate_redeemed_for_run(
    target: Path,
    approval_id: str,
    reference: Mapping[str, str],
    run_id: str,
) -> ApprovalClaim:
    """Validate one redeemed Daily claim for outcome-only reconciliation."""
    with redeemed_reconciliation_guard(target, approval_id, reference, run_id) as claim:
        return claim


def _consume_approval(target: Path, approval: dict[str, Any], run_id: str) -> None:
    claim = approval.get("approval_claim")
    if isinstance(claim, Mapping) and claim.get("state") == "reserved":
        from .. import runs_cmd

        token = runs_cmd._approval_marker_from_env(runs_cmd._APPROVAL_RESUME_TOKEN_ENV)
        if token is None:
            raise ApprovalClaimError("daily approval resume token is missing")
        redeem_for_run(
            target,
            str(approval.get("approval_id") or ""),
            _approval_claim_reference(approval),
            token,
        )
        return
    claim_for_run(
        target,
        str(approval.get("approval_id") or ""),
        _approval_claim_reference(approval),
        run_id,
    )
