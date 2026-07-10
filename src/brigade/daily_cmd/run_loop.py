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


def _latest_run(target: Path) -> dict[str, Any] | None:
    root = _runs_root(target)
    if not root.is_dir():
        return None
    runs: list[dict[str, Any]] = []
    for child in root.iterdir():
        if child.is_dir():
            payload = _read_json(child / "run.json")
            if payload is not None:
                runs.append(payload)
    runs.sort(key=lambda item: str(item.get("started_at") or item.get("run_id") or ""), reverse=True)
    return runs[0] if runs else None


def _iter_receipts(root: Path, filename: str) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    receipts: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    if not root.is_dir():
        return receipts, errors
    for child in root.iterdir():
        if not child.is_dir():
            continue
        path = child / filename
        payload = _read_json(path)
        if payload is None:
            errors.append({"path": str(path), "error": "missing or invalid JSON"})
            continue
        receipts.append(payload)
    receipts.sort(
        key=lambda item: str(
            item.get("started_at") or item.get("created_at") or item.get("run_id") or item.get("plan_id") or ""
        ),
        reverse=True,
    )
    return receipts, errors


def _latest_plan(target: Path) -> dict[str, Any] | None:
    plans, _ = _iter_receipts(_plans_root(target), "plan.json")
    return plans[0] if plans else None


def _resolve_plan(target: Path, plan_id: str | None) -> dict[str, Any] | None:
    if plan_id in (None, "", "latest"):
        return _latest_plan(target)
    path = _plans_root(target) / str(plan_id) / "plan.json"
    return _read_json(path)


def _record_run(target: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    run_id = str(receipt["run_id"])
    run_dir = _runs_root(target) / run_id
    receipt["path"] = str(run_dir)
    _write_json(run_dir / "run.json", receipt)
    return receipt


def _record_telemetry_event(target: Path, event: dict[str, Any]) -> None:
    event_id = str(event.get("event_id") or f"{_now().strftime('%Y%m%d-%H%M%S')}-telemetry-{uuid4().hex[:6]}")
    event["event_id"] = event_id
    event.setdefault("created_at", _now().isoformat())
    _write_json(_telemetry_root(target) / "events" / event_id / "event.json", event)


def _invoke_context_build(target: Path, action: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    if not action.get("context_kind"):
        return None, []
    task_id = (action.get("metadata") or {}).get("task_id")
    before = {str(pack.get("pack_id")) for pack in context_cmd._packs(target)}
    with redirect_stdout(StringIO()):
        rc = context_cmd.build(
            target=target,
            kind=str(action.get("context_kind")),
            task_id=str(task_id) if task_id else None,
            json_output=False,
        )
    after = context_cmd._packs(target)
    created = next((pack for pack in after if str(pack.get("pack_id")) not in before), None)
    if isinstance(created, dict):
        pack_id = str(created.get("pack_id") or "")
        context_path = context_cmd._packs_root(target) / pack_id / "context.json"
        context_payload = _read_json(context_path)
        if isinstance(context_payload, dict):
            context_payload["daily_action"] = {
                "action_id": action.get("action_id"),
                "safe_summary": action.get("safe_summary"),
                "acceptance": action.get("acceptance") if isinstance(action.get("acceptance"), list) else [],
                "evidence_refs": action.get("evidence_refs") if isinstance(action.get("evidence_refs"), list) else [],
                "approval_required": bool(action.get("approval_required")),
                "approval_reason": action.get("approval_reason"),
            }
            context_payload["daily_recent_failed_runs"] = [
                {"run_id": run.get("run_id"), "status": run.get("status"), "blockers": run.get("blockers", [])}
                for run in (_iter_receipts(_runs_root(target), "run.json")[0])
                if run.get("status") in {"failed", "blocked"}
            ][:5]
            excluded = (
                context_payload.get("excluded_private_evidence")
                if isinstance(context_payload.get("excluded_private_evidence"), list)
                else []
            )
            for item in (
                "raw scanner output",
                "raw chat text",
                "private repo names",
                "owner names",
                "org names",
                "hostnames",
            ):
                if item not in excluded:
                    excluded.append(item)
            context_payload["excluded_private_evidence"] = excluded
            _write_json(context_path, context_payload)
    return (str(created.get("pack_id")) if isinstance(created, dict) else None), [
        {"command": "brigade context build", "exit_code": rc}
    ]


def _blocked_run(
    *,
    target: Path,
    receipt: dict[str, Any],
    blockers: list[str],
    json_output: bool,
    next_command: str = "brigade daily review",
    approval: dict[str, Any] | None = None,
) -> int:
    receipt["status"] = "blocked"
    receipt["completed_at"] = _now().isoformat()
    receipt["next_recommended_command"] = next_command
    receipt["blockers"].extend(blockers)
    adapter = (
        receipt.get("adapter_result")
        if isinstance(receipt.get("adapter_result"), dict)
        else _adapter_result(
            receipt.get("selected_action") if isinstance(receipt.get("selected_action"), dict) else None
        )
    )
    adapter["status"] = "blocked"
    adapter["blockers"] = list(dict.fromkeys([*(adapter.get("blockers") or []), *blockers]))
    adapter["next_recommended_command"] = receipt["next_recommended_command"]
    receipt["adapter_result"] = adapter
    if approval is not None:
        receipt["approval_id"] = approval.get("approval_id")
        receipt["approval_request"] = approval
        receipt["next_recommended_command"] = f"brigade daily approvals show {approval.get('approval_id')}"
    _record_run(target, receipt)
    _record_telemetry_event(
        target,
        {
            "type": "daily-run",
            "run_id": receipt.get("run_id"),
            "status": "blocked",
            "action_type": (receipt.get("selected_action") or {}).get("action_type")
            if isinstance(receipt.get("selected_action"), dict)
            else None,
            "blockers": blockers,
            "approval_id": receipt.get("approval_id"),
        },
    )
    lines: list[str] = [
        f"daily run: {receipt['run_id']}",
        "status: blocked",
    ]
    lines.extend(f"blocker: {blocker}" for blocker in blockers)
    return emit(receipt, json_output, lines, 1)


def run(
    *,
    target: Path,
    approved: bool = False,
    approval_id: str | None = None,
    plan_id: str | None = None,
    replan: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    config, _ = _load_config(target)
    approval: dict[str, Any] | None = None
    if approval_id:
        approval = _find_approval(target, approval_id)
        if approval is not None and isinstance(approval.get("selected_action"), dict):
            plan_data = {
                "plan_id": approval.get("source_plan_id"),
                "selected_action": approval.get("selected_action"),
                "path": None,
            }
        else:
            plan_data = plan_payload(target, record=True)
            plan_data["approval_load_error"] = f"approval not found: {approval_id}"
    elif plan_id and not replan:
        plan_data = _resolve_plan(target, plan_id)
        if plan_data is None:
            plan_data = plan_payload(target, record=True)
            plan_data["plan_load_error"] = f"plan not found: {plan_id}"
    else:
        plan_data = plan_payload(target, record=True)
    action = plan_data.get("selected_action") if isinstance(plan_data.get("selected_action"), dict) else None
    run_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-daily-run-{uuid4().hex[:6]}"
    started = _now().isoformat()
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("daily-run"),
        "target": str(target),
        "run_id": run_id,
        "plan_id": plan_data.get("plan_id"),
        "selected_action_id": action.get("action_id") if action else None,
        "selected_action": action,
        "status": "running" if action else "blocked",
        "started_at": started,
        "completed_at": None,
        "commands_invoked": [],
        "receipts_created": [str(Path(str(plan_data.get("path") or "")) / "plan.json")]
        if plan_data.get("path")
        else [],
        "adapter_result": _adapter_result(action, status="running" if action else "blocked"),
        "work_session_id": None,
        "task_id": None,
        "context_pack_id": None,
        "verification_receipt": None,
        "handoff_path": None,
        "approval_id": approval.get("approval_id") if approval else None,
        "blockers": [],
        "next_recommended_command": "brigade daily status",
        "config": config,
    }
    if plan_data.get("approval_load_error"):
        return _blocked_run(
            target=target, receipt=receipt, blockers=[str(plan_data["approval_load_error"])], json_output=json_output
        )
    if plan_data.get("plan_load_error"):
        return _blocked_run(
            target=target, receipt=receipt, blockers=[str(plan_data["plan_load_error"])], json_output=json_output
        )
    if (
        plan_id
        and not replan
        and _is_stale(plan_data.get("created_at"), int(config.get("stale_plan_threshold_hours") or 12))
    ):
        return _blocked_run(
            target=target,
            receipt=receipt,
            blockers=[f"recorded plan is stale: {plan_data.get('plan_id')}"],
            json_output=json_output,
            next_command="brigade daily plan --record",
        )
    if action is None:
        return _blocked_run(
            target=target,
            receipt=receipt,
            blockers=["no daily action selected"],
            json_output=json_output,
            next_command="brigade daily plan",
        )
    approval_granted = approved or approval is not None
    config_blockers = _config_blockers(config, action, approved=approval_granted)
    if approval is not None:
        approval_blockers = _approval_blockers(target, approval, config)
        if config_blockers or approval_blockers:
            return _blocked_run(
                target=target, receipt=receipt, blockers=[*config_blockers, *approval_blockers], json_output=json_output
            )
    evidence_blockers = _evidence_blockers(target, action)
    if config_blockers or evidence_blockers:
        return _blocked_run(
            target=target, receipt=receipt, blockers=[*config_blockers, *evidence_blockers], json_output=json_output
        )
    if action.get("approval_required") and not approval_granted:
        approval = _ensure_approval(target, plan_data, action, config)
        blockers = [str(action.get("approval_reason") or "explicit approval required")]
        if approval.get("status") not in {"pending", "approved"}:
            blockers.append(f"approval status is {approval.get('status')}")
        return _blocked_run(
            target=target, receipt=receipt, blockers=blockers, json_output=json_output, approval=approval
        )
    if action.get("context_kind") and config.get("allow_context_pack_build", True):
        context_pack_id, context_commands = _invoke_context_build(target, action)
    else:
        context_pack_id, context_commands = None, []
    receipt["context_pack_id"] = context_pack_id
    receipt["commands_invoked"].extend(context_commands)
    receipt["adapter_result"]["commands_invoked"].extend(context_commands)
    if context_pack_id:
        receipt["receipts_created"].append(str(context_cmd._packs_root(target) / context_pack_id / "context.json"))
        receipt["adapter_result"]["receipts_created"].append(
            str(context_cmd._packs_root(target) / context_pack_id / "context.json")
        )
    if approval is not None:
        _consume_approval(target, approval, run_id)
    rc = 0
    action_type = str(action.get("action_type"))
    if action_type == "run-task":
        task_id = str((action.get("metadata") or {}).get("task_id") or "")
        with redirect_stdout(StringIO()):
            rc = work_cmd.run(None, target=target, task_id=task_id or None, inspect=False)
        receipt["task_id"] = task_id or None
        receipt["commands_invoked"].append({"command": "brigade work run", "exit_code": rc})
        receipt["adapter_result"]["commands_invoked"].append({"command": "brigade work run", "exit_code": rc})
        active = work_cmd._active_session_info(target)
        receipt["work_session_id"] = active.get("id") if isinstance(active, dict) else None
    elif action_type == "promote-import":
        import_id = str((action.get("metadata") or {}).get("import_id") or action.get("source_local_id"))
        with redirect_stdout(StringIO()):
            rc = work_cmd.import_promote(target=target, import_id=import_id)
        receipt["commands_invoked"].append({"command": f"brigade work import promote {import_id}", "exit_code": rc})
        receipt["adapter_result"]["commands_invoked"].append(
            {"command": f"brigade work import promote {import_id}", "exit_code": rc}
        )
    elif action_type == "start-center-action":
        action_id = str((action.get("metadata") or {}).get("action_id") or action.get("source_local_id"))
        with redirect_stdout(StringIO()):
            rc = center_cmd.actions_start(target=target, action_id=action_id)
        receipt["commands_invoked"].append({"command": f"brigade center actions start {action_id}", "exit_code": rc})
        receipt["adapter_result"]["commands_invoked"].append(
            {"command": f"brigade center actions start {action_id}", "exit_code": rc}
        )
    elif action_type == "import-readiness-issues":
        with redirect_stdout(StringIO()):
            rc = center_cmd.readiness_import_issues(target=target)
        receipt["commands_invoked"].append({"command": "brigade center readiness import-issues", "exit_code": rc})
        receipt["adapter_result"]["commands_invoked"].append(
            {"command": "brigade center readiness import-issues", "exit_code": rc}
        )
    elif action_type == "import-handoff-issues":
        with redirect_stdout(StringIO()):
            rc = handoff_cmd.import_issues(target=target)
        receipt["commands_invoked"].append({"command": "brigade handoff import-issues", "exit_code": rc})
        receipt["adapter_result"]["commands_invoked"].append(
            {"command": "brigade handoff import-issues", "exit_code": rc}
        )
    elif action_type == "build-operator-report":
        with redirect_stdout(StringIO()):
            rc = center_cmd.report_build(target=target)
        receipt["commands_invoked"].append({"command": "brigade center report build", "exit_code": rc})
        receipt["adapter_result"]["commands_invoked"].append(
            {"command": "brigade center report build", "exit_code": rc}
        )
    elif action_type == "start-phase-action":
        action_id = str((action.get("metadata") or {}).get("action_id") or action.get("source_local_id"))
        with redirect_stdout(StringIO()):
            rc = phases_cmd.actions_start(target=target, action_id=action_id)
        receipt["commands_invoked"].append(
            {"command": f"brigade work phases actions start {action_id}", "exit_code": rc}
        )
        receipt["adapter_result"]["commands_invoked"].append(
            {"command": f"brigade work phases actions start {action_id}", "exit_code": rc}
        )
    elif action_type == "build-phase-report":
        with redirect_stdout(StringIO()):
            rc = phases_cmd.report_build(target=target)
        receipt["commands_invoked"].append({"command": "brigade work phases report build", "exit_code": rc})
        receipt["adapter_result"]["commands_invoked"].append(
            {"command": "brigade work phases report build", "exit_code": rc}
        )
    elif action_type == "write-phase-session-checkpoint":
        session_id = str((action.get("metadata") or {}).get("session_id") or action.get("source_local_id") or "latest")
        with redirect_stdout(StringIO()):
            rc = phases_cmd.session_checkpoint(
                target=target,
                session_id=session_id,
                status="noted",
                summary="Daily driver checkpoint before continuing AFK session.",
            )
        receipt["commands_invoked"].append(
            {"command": f"brigade work phases session checkpoint {session_id}", "exit_code": rc}
        )
        receipt["adapter_result"]["commands_invoked"].append(
            {"command": f"brigade work phases session checkpoint {session_id}", "exit_code": rc}
        )
        latest_checkpoint = phases_cmd._latest_checkpoint_for_session(target, session_id)
        if isinstance(latest_checkpoint, dict) and latest_checkpoint.get("path"):
            receipt["receipts_created"].append(str(latest_checkpoint["path"]))
            receipt["adapter_result"]["receipts_created"].append(str(latest_checkpoint["path"]))
    elif action_type == "build-phase-session-report":
        session_id = str((action.get("metadata") or {}).get("session_id") or action.get("source_local_id") or "latest")
        with redirect_stdout(StringIO()):
            rc = phases_cmd.session_report_build(target=target, session_id=session_id)
        receipt["commands_invoked"].append(
            {"command": f"brigade work phases session report build {session_id}", "exit_code": rc}
        )
        receipt["adapter_result"]["commands_invoked"].append(
            {"command": f"brigade work phases session report build {session_id}", "exit_code": rc}
        )
    elif action_type == "closeout-phase-session":
        session_id = str((action.get("metadata") or {}).get("session_id") or action.get("source_local_id") or "latest")
        with redirect_stdout(StringIO()):
            rc = phases_cmd.session_closeout(
                target=target,
                session_id=session_id,
                status="reviewed",
                reason="Daily driver reviewed completed phase session.",
            )
        receipt["commands_invoked"].append(
            {"command": f"brigade work phases session closeout {session_id}", "exit_code": rc}
        )
        receipt["adapter_result"]["commands_invoked"].append(
            {"command": f"brigade work phases session closeout {session_id}", "exit_code": rc}
        )
    else:
        receipt["blockers"].append(f"selected action is review-only: {action_type}")
        rc = 1
    receipt["status"] = "completed" if rc == 0 else "failed"
    receipt["adapter_result"]["status"] = receipt["status"]
    receipt["adapter_result"]["blockers"] = receipt["blockers"]
    receipt["adapter_result"]["next_recommended_command"] = (
        "brigade daily closeout" if rc == 0 else "brigade daily repair"
    )
    receipt["completed_at"] = _now().isoformat()
    receipt["next_recommended_command"] = "brigade daily closeout"
    _record_run(target, receipt)
    _record_telemetry_event(
        target,
        {
            "type": "daily-run",
            "run_id": run_id,
            "status": receipt["status"],
            "action_type": action_type,
            "adapter_id": receipt["adapter_result"].get("adapter_id"),
            "blockers": receipt["blockers"],
            "approval_id": receipt.get("approval_id"),
        },
    )
    lines: list[str] = [
        f"daily run: {run_id}",
        f"status: {receipt['status']}",
        f"selected: {receipt['selected_action_id']}",
        f"next: {receipt['next_recommended_command']}",
    ]
    return emit(receipt, json_output, lines, rc)
