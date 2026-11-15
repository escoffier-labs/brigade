"""Workspace intent for Tier 0 application. The hub remains the authority."""

from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from . import memory_hub_client as hubc, memory_proposal_io as io

PATH = ".brigade/memory/reflect/policy.json"
NEW_WORKSPACE_PATH = ".brigade/new-workspace.v1.json"
CLASSES = ["append_evidence", "merge_exact_duplicates"]
DEFAULT = {"schema": "brigade.memory-reflect-policy.v1", "auto_apply": True, "tier0_op_classes": CLASSES}


def load(target: Path) -> dict[str, Any]:
    value = io.read_object(target / PATH)
    if (
        set(value) != set(DEFAULT)
        or value.get("schema") != DEFAULT["schema"]
        or type(value.get("auto_apply")) is not bool
        or not isinstance(value.get("tier0_op_classes"), list)
        or any(c not in CLASSES for c in value["tier0_op_classes"])
    ):
        raise ValueError("invalid reflect policy")
    return value


def initialize(target: Path) -> None:
    """Record new-workspace defaults once, only from initialization."""
    _seed(target / NEW_WORKSPACE_PATH, {"schema": "brigade.new-workspace.v1", "reflect_auto_apply": True})
    ensure(target)


def ensure(target: Path) -> None:
    """Create new intent without changing any existing policy, including off."""
    _seed(target / PATH, DEFAULT)


def _seed(path: Path, value: dict[str, Any]) -> None:
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        # Init is portable even where the reflection engine itself fails closed.
        # This policy grants intent only. It cannot bypass the engine's IO gates.
        from .managed_block import WRITE_WRITTEN, write_text_nofollow_atomic

        if path.exists() or path.is_symlink():
            return
        result = write_text_nofollow_atomic(path, io.json_bytes(value).decode())
        if result.status != WRITE_WRITTEN:
            raise OSError("reflect policy could not be installed safely")
        return
    try:
        io.write_exclusive(path, io.json_bytes(value))
    except FileExistsError:
        pass


def set_enabled(target: Path, enabled: bool) -> dict[str, Any]:
    try:
        value = load(target)
    except (FileNotFoundError, ValueError):
        if enabled and (target / PATH).exists():
            raise
        value = dict(DEFAULT)
    value["auto_apply"] = enabled
    path = target / PATH
    temporary = path.with_name(f".policy-{uuid.uuid4().hex}.json")
    io.write_exclusive(temporary, io.json_bytes(value))
    with io.directory(path.parent, private=True) as parent:
        try:
            os.replace(temporary.name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary.name, dir_fd=parent)
            except FileNotFoundError:
                pass
    return value


def allowed(target: Path, ops: list[dict[str, Any]]) -> str | None:
    """Re-read immediately before every candidate, failing closed on unsafe IO."""
    try:
        value = load(target)
    except (OSError, ValueError):
        return "reflect_policy_unavailable"
    if not value["auto_apply"]:
        return "auto_apply_disabled"
    if any(op.get("type") not in value["tier0_op_classes"] for op in ops):
        return "class_disabled"
    return None


def _code(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", value) else None


def _checks(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [
        {key: code for key in ("gate", "state", "code") if (code := _code(item.get(key))) is not None}
        for item in value[:32]
        if isinstance(item, dict)
    ]


# Hub replies and HubSettings expose no interval. Match hardening::Limits::default.
AUTO_APPLY_INTERVAL_SECONDS = 1.0
RATE_LIMITED = "memory_auto_apply_rate_limited"


class AutoApplyPacer:
    """One run's automatic calls, spaced from the end of the preceding request."""

    def __init__(self) -> None:
        self.last_attempt: float | None = None

    def wait(self) -> None:
        if self.last_attempt is not None:
            delay = max(0.0, AUTO_APPLY_INTERVAL_SECONDS - (time.monotonic() - self.last_attempt))
            if delay:
                time.sleep(delay)


def attempt(
    target: Path,
    client: hubc.MemoryHubClient,
    proposal_id: str,
    ops: list[dict[str, Any]],
    epoch: str,
    *,
    pacer: AutoApplyPacer | None = None,
) -> dict[str, Any]:
    pacer = pacer if pacer is not None else AutoApplyPacer()
    # At most two calls per candidate and one second per gap. With both lanes
    # capped at 100 candidates, a run adds at most 399 seconds of requested sleep.
    for retry in range(2):
        reason = allowed(target, ops)
        if reason:
            return {"state": "pending", "reason": reason}
        pacer.wait()
        # An operator may turn application off while this run is sleeping.
        reason = allowed(target, ops)
        if reason:
            return {"state": "pending", "reason": reason}
        try:
            # Authority, host ownership, trust, proof, retrieval, blast caps and cooldown
            # are rechecked transactionally by the hub. Never turn a hold into decide.
            try:
                result = client.call(
                    "auto_apply", proposal_id=proposal_id, base_epoch=epoch, client_event_id=hubc.event_id()
                )
            finally:
                pacer.last_attempt = time.monotonic()
            return {
                "state": result.get("state")
                if result.get("state") in ("pending", "applied", "rejected")
                else "pending",
                "checks": _checks(result.get("auto_apply_gates")),
                "reason": _code(result.get("reason")),
            }
        except hubc.HubRefusal as exc:
            code = exc.code if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", exc.code) else "hub_refused"
            if exc.status == 429 and code == RATE_LIMITED and retry == 0:
                continue  # A refused call has no committed result. Retry once with a fresh request id.
            return {"state": "pending", "reason": code}
        except (hubc.HubUnavailable, OSError, ValueError):
            return {"state": "unknown", "reason": "auto_apply_unavailable"}
    raise AssertionError("unreachable")  # pragma: no cover


def local_fallback(target: Path, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Local preflight is observational until its authority contracts are bound."""
    from .memory_tier0_preflight import preflight_payload

    results = []
    for candidate in candidates:
        reason = allowed(target, [])
        checks = [] if reason else preflight_payload(target=target, candidate_id=candidate["candidate_id"])["checks"]
        results.append(
            {
                "candidate_id": candidate["candidate_id"],
                "state": "pending",
                "reason": reason or "local_authority_unbound",
                "checks": checks,
            }
        )
    return results
