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


def _changed_files_summary(target: Path) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["git", "status", "--short"],
            cwd=target,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        return {"available": False, "tracked_dirty_count": None, "untracked_count": None, "files": []}
    files = []
    tracked = 0
    untracked = 0
    for line in proc.stdout.splitlines():
        if not line:
            continue
        status = line[:2]
        path = line[3:] if len(line) > 3 else ""
        files.append({"status": status.strip(), "path": _safe_text(target, path)})
        if status == "??":
            untracked += 1
        else:
            tracked += 1
    return {
        "available": proc.returncode == 0,
        "tracked_dirty_count": tracked,
        "untracked_count": untracked,
        "files": files[:50],
    }


def _verification_expectation(config: dict[str, Any], run_receipt: dict[str, Any]) -> dict[str, Any]:
    action = run_receipt.get("selected_action") if isinstance(run_receipt.get("selected_action"), dict) else {}
    action_type = str(action.get("action_type") or "")
    required = False
    if action_type == "run-task":
        required = bool(config.get("verification_required_for_work_run"))
    elif action_type == "promote-import":
        required = bool(config.get("verification_required_for_import_promotion"))
    elif action_type in {"import-readiness-issues", "build-operator-report"}:
        required = bool(config.get("verification_required_for_release_actions"))
    return {
        "required": required,
        "action_type": action_type,
        "allowed_commands": config.get("allowed_verification_commands"),
        "timeout": config.get("verification_timeout"),
    }


def _write_handoff(target: Path, run_receipt: dict[str, Any], status: str, reason: str | None) -> Path:
    inbox = target / ".claude" / "memory-handoffs"
    inbox.mkdir(parents=True, exist_ok=True)
    stamp = _now().strftime("%Y-%m-%d-%H%M")
    path = inbox / f"{stamp}-brigade-daily-closeout.md"
    content = f"""# Memory Handoff

## Type
workflow

## Title
Brigade daily loop closeout

## Summary
Brigade daily closeout recorded status `{status}` for daily run `{run_receipt.get("run_id")}`. The receipt preserves the selected action, invoked local commands, blockers, and next recommendation for future operator review.

## Durable facts
- Daily run id: `{run_receipt.get("run_id")}`
- Selected action: `{run_receipt.get("selected_action_id")}`
- Closeout status: `{status}`
- Reason: `{_safe_text(target, reason or "not provided")}`

## Evidence
- daily receipt: `{run_receipt.get("path")}`
- next command: `{run_receipt.get("next_recommended_command")}`

## Recommended memory action
no-card

## Target document
.learnings/LEARNINGS.md

## Suggested document content
### Brigade daily loop closeout

Daily run `{run_receipt.get("run_id")}` closed with status `{status}`. Review the local daily receipt for selected action, commands invoked, blockers, and next recommendation.
"""
    path.write_text(content)
    lint = handoff_cmd.lint_file(path)
    if not lint.valid:
        raise RuntimeError("; ".join(lint.errors))
    return path


def closeout(
    *,
    target: Path,
    status: str = "reviewed",
    reason: str | None = None,
    handoff: bool = False,
    json_output: bool = False,
) -> int:
    if status not in RUN_STATUSES:
        print(f"error: invalid daily closeout status: {status}", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    receipt = _latest_run(target)
    if receipt is None:
        print("error: no daily run receipt found", file=sys.stderr)
        return 1
    config, _ = _load_config(target)
    latest_verification = work_cmd._latest_verify_receipt(target)
    verification_expectation = _verification_expectation(config, receipt)
    verification_blockers: list[str] = []
    if verification_expectation["required"] and not latest_verification:
        verification_blockers.append("verification receipt required by daily config")
    elif verification_expectation["required"] and latest_verification.get("status") != "completed":
        verification_blockers.append(f"latest verification did not complete: {latest_verification.get('run_id')}")
    receipt["closeout_status"] = status
    receipt["closeout_reason"] = reason
    receipt["reviewed_at"] = _now().isoformat()
    receipt["latest_work_closeout"] = work_cmd._latest_work_closeout_payload(target)
    receipt["latest_verification"] = latest_verification
    receipt["verification_status"] = (
        "missing" if latest_verification is None else str(latest_verification.get("status") or "unknown")
    )
    receipt["verification_expectation"] = verification_expectation
    receipt["verification_blockers"] = verification_blockers
    receipt["changed_files_summary"] = _changed_files_summary(target)
    receipt["review_closeout_state"] = work_cmd._review_health(target).get("latest_closeout")
    receipt["handoff_drafts"] = center_cmd.status_payload(target).get("handoff_drafts")
    receipt["center_report"] = center_cmd.latest_report(target)
    receipt["center_readiness"] = center_cmd._latest_readiness(target)
    receipt["release_readiness_impact"] = {
        "latest_release_readiness": center_cmd.status_payload(target).get("release_readiness"),
        "improved": not verification_blockers and status == "reviewed",
    }
    if handoff:
        try:
            handoff_path = _write_handoff(target, receipt, status, reason)
        except RuntimeError as exc:
            print(f"error: handoff lint failed: {exc}", file=sys.stderr)
            return 1
        receipt["handoff_path"] = str(handoff_path)
    _record_run(target, receipt)
    _record_telemetry_event(
        target,
        {
            "type": "daily-closeout",
            "run_id": receipt.get("run_id"),
            "status": status,
            "verification_status": receipt.get("verification_status"),
            "blockers": verification_blockers,
        },
    )
    if json_output:
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0
    print(f"daily closeout: {receipt.get('run_id')}")
    print(f"status: {status}")
    print(f"run_status: {receipt.get('status')}")
    if receipt.get("handoff_path"):
        print(f"handoff: {receipt['handoff_path']}")
    return 0
