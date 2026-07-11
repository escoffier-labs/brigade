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
from contextlib import redirect_stdout
from collections import Counter
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
from ..localio import read_json_dict as _read_json, utc_now as _now, write_json as _write_json
from ..render import emit

from . import config as _family_base

globals().update({name: value for name, value in vars(_family_base).items() if not name.startswith("__")})


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


def _read_approvals(target: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    return _iter_receipts(_approvals_root(target), "approval.json")


def _write_approval(target: Path, approval: dict[str, Any]) -> dict[str, Any]:
    approval_id = str(approval["approval_id"])
    approval["path"] = str(_approvals_root(target) / approval_id)
    _write_json(_approval_path(target, approval_id), approval)
    return approval


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
    matches = _matching_approvals(target, action, config)
    for status in ("pending", "approved"):
        existing = next((approval for approval in matches if approval.get("status") == status), None)
        if existing:
            return existing
    for status in ("rejected", "held", "consumed"):
        existing = next((approval for approval in matches if approval.get("status") == status), None)
        if existing:
            return existing
    approval = {
        "schema_version": SCHEMA_VERSION,
        "approval_id": _approval_id(action, config),
        "created_at": _now().isoformat(),
        "status": "pending",
        "source_plan_id": plan_data.get("plan_id"),
        "selected_action_id": action.get("action_id"),
        "selected_action": action,
        "selected_adapter": _adapter_for(action),
        "source_subsystem": action.get("source_subsystem"),
        "source_local_id": action.get("source_local_id"),
        "source_fingerprint": action.get("source_fingerprint"),
        "config_fingerprint": _config_fingerprint(config),
        "acceptance": action.get("acceptance") if isinstance(action.get("acceptance"), list) else [],
        "safe_summary": action.get("safe_summary"),
        "evidence_refs": action.get("evidence_refs") if isinstance(action.get("evidence_refs"), list) else [],
        "risk_level": action.get("risk_level"),
        "approval_reason": action.get("approval_reason") or "explicit approval required",
        "config": config,
        "suggested_next_command": action.get("suggested_next_command"),
        "reviewed_at": None,
        "review_reason": None,
        "consumed_run_id": None,
    }
    return _write_approval(target, approval)


def _current_action_for_approval(target: Path, approval: dict[str, Any]) -> dict[str, Any] | None:
    selected_action = approval.get("selected_action") if isinstance(approval.get("selected_action"), dict) else {}
    action_type = str(selected_action.get("action_type") or "")
    source_id = str(approval.get("source_local_id") or selected_action.get("source_local_id") or "")
    candidate_builders = {
        "run-task": _pending_task_candidates,
        "promote-import": _pending_import_candidates,
        "start-center-action": _center_action_candidates,
        "import-readiness-issues": _readiness_candidates,
        "import-handoff-issues": _handoff_ingest_candidates,
        "build-operator-report": _report_candidate,
    }
    builder = candidate_builders.get(action_type)
    if builder is None:
        return selected_action if selected_action and not _evidence_blockers(target, selected_action) else None
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
    blockers.extend(_evidence_blockers(target, action))
    current = _current_action_for_approval(target, approval)
    if current is None:
        blockers.append("selected action is no longer available")
    elif current.get("source_fingerprint") != approval.get("source_fingerprint"):
        blockers.append("selected action fingerprint changed since approval")
    return list(dict.fromkeys(blockers))


def _consume_approval(target: Path, approval: dict[str, Any], run_id: str) -> None:
    approval["status"] = "consumed"
    approval["consumed_run_id"] = run_id
    approval["consumed_at"] = _now().isoformat()
    _write_approval(target, approval)
