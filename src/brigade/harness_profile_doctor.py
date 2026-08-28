"""Expected-state comparison and receipt attestation for harness doctor/--check."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__ as BRIGADE_VERSION

if TYPE_CHECKING:
    from .harness_profile_cmd import SurfacePlan


def _receipt_ownership(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "instruction_fingerprint": state.get("instructions", {}).get("digest"),
        "skills": state.get("skills", {}),
        "generated_fingerprints": {
            name: record.get("digest") or record.get("entry_fingerprint")
            for name, record in sorted(state.get("generated", {}).items())
            if isinstance(record, dict)
        },
        "mcp_fingerprints": {
            name: record.get("projected_fingerprint")
            for name, record in sorted(state["mcp"].items())
            if isinstance(record, dict)
        },
    }


def _uninstall_receipt_conflict(profile, state: dict[str, Any]) -> dict[str, Any] | None:
    """Return a conflict item unless the sync receipt attests to ``state``."""
    path = profile.receipt_path
    item = {"surface": "profile-receipt", "path": str(path), "status": "conflict", "action": "preserve"}
    if not path.exists():
        return item | {"detail": "profile receipt is missing"}
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return item | {"detail": "profile receipt is unreadable"}
    if not isinstance(receipt, dict):
        return item | {"detail": "profile receipt is not an object"}
    if receipt.get("version") != 1 or receipt.get("harness") != profile.harness or receipt.get("operation") != "sync":
        return item | {"detail": "profile receipt does not match this profile"}
    if receipt.get("ownership_fingerprints") != _receipt_ownership(state):
        return item | {"detail": "profile receipt ownership fingerprints drifted"}
    return None


def _state_item(path: Path, *, before: dict[str, Any], after: dict[str, Any], remove: bool = False) -> dict[str, Any]:
    if remove:
        return {
            "surface": "ownership-state",
            "path": str(path),
            "status": "present" if path.exists() else "absent",
            "action": "remove" if path.exists() else "none",
        }
    return {
        "surface": "ownership-state",
        "path": str(path),
        "status": "current" if before == after and path.exists() else "stale",
        "action": "none" if before == after and path.exists() else ("create" if not path.exists() else "update"),
    }


def _receipt_item(path: Path, *, state_action: str, remove: bool = False) -> dict[str, Any]:
    if remove:
        return {
            "surface": "profile-receipt",
            "path": str(path),
            "status": "present" if path.exists() else "absent",
            "action": "remove" if path.exists() else "none",
        }
    action = "none" if state_action == "none" and path.exists() else ("create" if not path.exists() else "update")
    return {
        "surface": "profile-receipt",
        "path": str(path),
        "status": "current" if action == "none" else "stale",
        "action": action,
    }


def _doctor_receipt_item(profile, state: dict[str, Any], *, state_action: str) -> dict[str, Any]:
    """Classify the sync receipt for doctor/--check, including attestation drift."""
    item = _receipt_item(profile.receipt_path, state_action=state_action)
    if item["action"] != "none":
        return item
    conflict = _uninstall_receipt_conflict(profile, state)
    if conflict is None:
        return item
    return {
        "surface": "profile-receipt",
        "path": str(profile.receipt_path),
        "status": "stale",
        "action": "update",
        "detail": conflict["detail"],
    }


def _expected_profile_state(
    state: dict[str, Any],
    *,
    instruction: SurfacePlan,
    instruction_existed: bool,
    depth: dict[str, Any],
    skill_plan: dict[str, Any],
    generated_plan: dict[str, Any],
    hook_plan: dict[str, Any] | None,
    mcp_next: dict[str, Any],
    profile,
) -> dict[str, Any]:
    """Build the ownership state a conflict-free sync would persist."""
    from .harness_profile_cmd import (
        _HOOK_STATE_KEY,
        _INSTRUCTION_EFFECTIVE_PROFILE_KEY,
        _INSTRUCTION_REQUIRED_PROFILE_KEY,
        _LEGACY_MIGRATED,
        _missing_directories,
        _missing_skill_directories,
    )

    proposed = json.loads(json.dumps(state))
    if instruction.status != "conflict":
        old_instruction = state.get("instructions", {})
        created_file = (
            old_instruction.get("created_file", not instruction_existed)
            if isinstance(old_instruction, dict)
            else not instruction_existed
        )
        if instruction.desired_digest:
            instruction_record: dict[str, Any] = {
                "digest": instruction.desired_digest,
                "created_file": created_file,
                _INSTRUCTION_REQUIRED_PROFILE_KEY: depth["required_profile"],
                _INSTRUCTION_EFFECTIVE_PROFILE_KEY: depth["effective_profile"],
            }
            if isinstance(old_instruction, dict) and old_instruction.get("created_directories"):
                instruction_record["created_directories"] = list(old_instruction["created_directories"])
            if isinstance(old_instruction, dict) and old_instruction.get(_LEGACY_MIGRATED):
                instruction_record[_LEGACY_MIGRATED] = True
            proposed["instructions"] = instruction_record
        else:
            proposed["instructions"] = {}
    proposed["skills"] = skill_plan["next"]
    proposed["generated"] = generated_plan["next"]
    if hook_plan is not None:
        proposed["generated"][_HOOK_STATE_KEY] = hook_plan["next"]
    proposed["mcp"] = mcp_next
    proposed["package_version"] = BRIGADE_VERSION
    instruction_dirs = (
        _missing_directories(profile.user_root, instruction.path)
        if instruction.action in {"create", "update"} and profile.instruction_mode == "managed-file"
        else []
    )
    if instruction_dirs and isinstance(proposed.get("instructions"), dict):
        existing = proposed["instructions"].get("created_directories", [])
        proposed["instructions"]["created_directories"] = sorted(set(existing) | set(instruction_dirs))
    created_dirs: dict[str, list[str]] = {}
    for path, _data, skill_id, _relative in skill_plan["writes"]:
        created_dirs.setdefault(skill_id, []).extend(_missing_skill_directories(profile, path))
    for skill_id, directories in created_dirs.items():
        existing = proposed["skills"][skill_id].setdefault("created_directories", [])
        proposed["skills"][skill_id]["created_directories"] = sorted(set(existing) | set(directories))
    generated_dirs: dict[str, list[str]] = {}
    for path, _text, _executable, relative in generated_plan["writes"]:
        generated_dirs.setdefault(relative, []).extend(_missing_directories(profile.user_root, path))
    for relative, directories in generated_dirs.items():
        existing = proposed["generated"][relative].setdefault("created_directories", [])
        proposed["generated"][relative]["created_directories"] = sorted(set(existing) | set(directories))
    return proposed
