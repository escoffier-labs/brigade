"""Workspace intent for Tier 0 application. The hub remains the authority."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any

from . import memory_hub_client as hubc, memory_proposal_io as io

PATH = ".brigade/memory/reflect/policy.json"
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


def ensure(target: Path) -> None:
    """Create new intent without changing any existing policy, including off."""
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        # Init is portable even where the reflection engine itself fails closed.
        # This policy grants intent only. It cannot bypass the engine's IO gates.
        from .managed_block import WRITE_WRITTEN, write_text_nofollow_atomic

        path = target / PATH
        if path.exists() or path.is_symlink():
            return
        result = write_text_nofollow_atomic(path, io.json_bytes(DEFAULT).decode())
        if result.status != WRITE_WRITTEN:
            raise OSError("reflect policy could not be installed safely")
        return
    try:
        io.write_exclusive(target / PATH, io.json_bytes(DEFAULT))
    except FileExistsError:
        pass


def set_enabled(target: Path, enabled: bool) -> dict[str, Any]:
    try:
        value = load(target)
    except FileNotFoundError:
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


def attempt(
    target: Path, client: hubc.MemoryHubClient, proposal_id: str, ops: list[dict[str, Any]], epoch: str
) -> dict[str, Any]:
    reason = allowed(target, ops)
    if reason:
        return {"state": "pending", "reason": reason}
    try:
        # Authority, host ownership, trust, proof, retrieval, blast caps and cooldown
        # are rechecked transactionally by the hub. Never turn a hold into decide.
        result = client.call("auto_apply", proposal_id=proposal_id, base_epoch=epoch, client_event_id=hubc.event_id())
        return {
            "state": result.get("state", "pending"),
            "checks": result.get("auto_apply_gates", []),
            "reason": result.get("reason"),
        }
    except hubc.HubRefusal as exc:
        code = exc.code if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", exc.code) else "hub_refused"
        return {"state": "pending", "reason": code}
    except (hubc.HubUnavailable, OSError, ValueError):
        return {"state": "unknown", "reason": "auto_apply_unavailable"}


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
