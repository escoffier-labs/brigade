"""Doctor classification for Claude work-loop enforcement and adherence."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from .. import localio
from ..config import load_config
from ..templates import template_root
from .install_cmd import status_payload
from .runtime import MAX_RECENT_SESSION_STATES, _receipt_since, _session_fingerprint, iter_session_states

RECENT_WRITE_WINDOW = timedelta(days=7)


def _is_wired_for_claude(target: Path) -> bool:
    try:
        config = load_config(target)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return config is not None and "claude" in config.selection.harnesses


def _instruction_import_current(target: Path) -> bool:
    path = target / "CLAUDE.md"
    try:
        return any(line.strip() == "@AGENTS.md" for line in path.read_text().splitlines())
    except OSError:
        return False


def _skill_current(target: Path) -> tuple[bool, bool]:
    installed = target / ".claude" / "skills" / "brigade-work" / "SKILL.md"
    expected = template_root() / "skills" / "brigade-work" / "SKILL.md"
    try:
        if not installed.is_file():
            return False, False
        return True, installed.read_bytes() == expected.read_bytes()
    except OSError:
        return installed.is_file(), False


def _recent_write_without_receipt(target: Path) -> bool:
    now = localio.utc_now()
    target_label = str(target.expanduser().resolve())
    for state in iter_session_states(
        target,
        modified_since=now - RECENT_WRITE_WINDOW,
        limit=MAX_RECENT_SESSION_STATES,
    ):
        session_id = state.get("session_id")
        if (
            not isinstance(session_id, str)
            or not session_id
            or state.get("target") != target_label
            or state.get("write_observed") is not True
        ):
            continue
        started = localio.parse_iso_datetime(state.get("started_at"))
        last_write = localio.parse_iso_datetime(state.get("last_write_at"))
        receipt_threshold = last_write or started
        if receipt_threshold is None or receipt_threshold > now or now - receipt_threshold > RECENT_WRITE_WINDOW:
            continue
        fingerprint = _session_fingerprint(session_id)
        if not _receipt_since(
            target,
            receipt_threshold,
            session_fingerprint=fingerprint,
        ):
            return True
    return False


def classify(target: Path) -> dict[str, Any] | None:
    target = target.expanduser().resolve()
    if not _is_wired_for_claude(target):
        return None
    hook_status = status_payload(target)
    skill_present, skill_current = _skill_current(target)
    instruction_current = _instruction_import_current(target)
    dormant = _recent_write_without_receipt(target)

    issues: list[str] = []
    if not instruction_current:
        issues.append("missing instruction import @AGENTS.md")
    if not skill_present:
        issues.append("missing brigade-work skill")
    elif not skill_current:
        issues.append("stale skill")
    hook_error = hook_status.get("error")
    if hook_error:
        issues.append(f"hook settings error: {hook_error}")
    elif not hook_status.get("installed"):
        missing = hook_status.get("missing_events") or []
        if missing:
            issues.append(f"missing hook package events={','.join(str(item) for item in missing)}")
        else:
            issues.append("missing hook package sidecar")
    elif not hook_status.get("current"):
        issues.append("stale hook package")

    if dormant:
        state = "dormant"
        issues.append("recent work has no verification receipt")
    elif instruction_current and skill_current and hook_status.get("current"):
        state = "enforced"
    elif instruction_current and skill_current and not hook_error and not hook_status.get("managed_events"):
        state = "advisory-only"
    else:
        state = "partial"

    return {
        "state": state,
        "issues": issues,
        "instruction_import": instruction_current,
        "skill_present": skill_present,
        "skill_current": skill_current,
        "hooks": hook_status,
        "detail": f"state={state}; " + ("; ".join(issues) if issues else "current guidance, skill, and hooks"),
    }
