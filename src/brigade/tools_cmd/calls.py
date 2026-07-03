"""Call queue planning, approval, and health helpers for the tools command family."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from ..render import emit
from . import constants, helpers, paths, projections, runtimes, safety


def _describe_payload(target: Path, tool_id: str) -> dict[str, Any]:
    target = target.expanduser().resolve()
    tool, errors = projections._find_tool(target, tool_id)
    summary: dict[str, Any] | None = None
    issues: list[dict[str, Any]] = []
    if tool is not None:
        inspected, inspect_issues = projections._inspect_tool(target, tool)
        summary = inspected
        issues = inspect_issues
    return {
        "target": str(target),
        "config_path": str(paths.config_path(target)),
        "valid": not errors,
        "errors": errors,
        "tool": summary,
        "issues": issues,
        "issue_count": len(issues),
    }


def _load_args(args: str | None, args_json: Path | None) -> tuple[object | None, str | None]:
    if args and args_json:
        return None, "pass only one of --args or --args-json"
    if args_json is not None:
        try:
            return json.loads(args_json.expanduser().read_text()), None
        except OSError as exc:
            return None, str(exc)
        except json.JSONDecodeError as exc:
            return None, f"invalid args JSON: {exc.msg}"
    if args is not None:
        try:
            return json.loads(args), None
        except json.JSONDecodeError as exc:
            return None, f"invalid args JSON: {exc.msg}"
    return {}, None


def _dict_or_empty(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _call_plan_payload(
    target: Path,
    tool_id: str,
    *,
    args: str | None = None,
    args_json: Path | None = None,
) -> dict[str, Any]:
    target = target.expanduser().resolve()
    tool, errors = projections._find_tool(target, tool_id)
    parsed_args, args_error = _load_args(args, args_json)
    blockers: list[str] = list(errors)
    validation_errors: list[str] = []
    if args_error is not None:
        blockers.append(args_error)
    if parsed_args is not None and not isinstance(parsed_args, dict):
        blockers.append("args must be a JSON object")
    mapped_arguments: dict[str, str] = {}
    projection_blockers: list[dict[str, Any]] = []
    schema: object | None = None
    if tool is not None:
        if not tool.get("command"):
            blockers.append("command is required for call planning")
        if tool.get("family") == "mcp":
            if not tool.get("runtime_id"):
                blockers.append("runtime_id is required for MCP call planning")
            if not tool.get("mcp_tool_name"):
                blockers.append("mcp_tool_name is required for MCP call planning")
        auth_label = str(tool.get("auth_label") or "")
        if auth_label and constants.UNSAFE_FIELD_PATTERN.search(auth_label):
            blockers.append("auth_label appears unsafe")
        for label in tool.get("env_labels", []):
            if constants.UNSAFE_FIELD_PATTERN.search(str(label)):
                blockers.append(f"env label appears unsafe: {label}")
        schema, schema_error = safety._load_schema(target, tool, "input_schema_path")
        if schema_error is not None:
            blockers.append(schema_error)
        elif schema is None:
            blockers.append("input_schema_path is required for call planning")
        else:
            shape_errors = safety._schema_shape_errors(schema)
            if shape_errors:
                blockers.extend(shape_errors)
            elif isinstance(parsed_args, dict):
                validation_errors = safety._validate_json_value(parsed_args, schema)  # type: ignore[arg-type]
                blockers.extend(validation_errors)
        for harness in tool.get("supported_harnesses", []):
            projection = projections._projection_item(target, tool, harness)
            if projection.get("status") in {"conflicted", "unmanaged"}:
                projection_blockers.append({key: value for key, value in projection.items() if key != "rendered"})
        if projection_blockers:
            blockers.append("one or more projections are conflicted or unmanaged")
        if isinstance(parsed_args, dict):
            for key, template in tool.get("argument_template", {}).items():
                missing = [
                    var for var in re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", template) if var not in parsed_args
                ]
                for var in missing:
                    blockers.append(f"argument_template {key} references missing arg {var}")
                rendered = safety._render_argument_template(str(template), parsed_args)
                if constants.UNSAFE_FIELD_PATTERN.search(str(key)) or any(
                    constants.UNSAFE_FIELD_PATTERN.search(var)
                    for var in re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", str(template))
                ):
                    mapped_arguments[str(key)] = "[redacted]"
                else:
                    mapped_arguments[str(key)] = rendered
    safe_env_labels = [
        "[redacted]" if constants.UNSAFE_FIELD_PATTERN.search(str(label)) else str(label)
        for label in (tool.get("env_labels", []) if tool is not None else [])
    ]
    safe_args = safety._redact_payload(parsed_args) if parsed_args is not None else None
    plan_payload = {
        "tool_id": tool_id,
        "family": tool.get("family") if tool is not None else None,
        "command": tool.get("command") if tool is not None else None,
        "cwd": tool.get("cwd") if tool is not None else None,
        "timeout": tool.get("timeout") if tool is not None else None,
        "runtime_id": tool.get("runtime_id") if tool is not None else None,
        "requires_runtime": (tool.get("requires_runtime", False) or tool.get("family") == "mcp")
        if tool is not None
        else False,
        "runtime_health_path": tool.get("runtime_health_path") if tool is not None else None,
        "mcp_server_id": tool.get("mcp_server_id") if tool is not None else None,
        "mcp_tool_name": tool.get("mcp_tool_name") if tool is not None else None,
        "auth_label": "[redacted]"
        if tool is not None and constants.UNSAFE_FIELD_PATTERN.search(str(tool.get("auth_label") or ""))
        else (tool.get("auth_label") if tool is not None else None),
        "env_labels": safe_env_labels,
        "arguments": mapped_arguments,
        "args": safe_args,
        "permissions": tool.get("permissions", []) if tool is not None else [],
        "effects": tool.get("effects", []) if tool is not None else [],
        "approval_required": (tool.get("approval_mode") if tool is not None else "never") != "never",
        "approval_mode": tool.get("approval_mode", "never") if tool is not None else "never",
    }
    projection_summary: dict[str, Any] = {"counts": {}, "projections": []}
    contract_fingerprint = None
    source_fingerprint = None
    if tool is not None:
        projection_summary = projections._projection_plan_payload(target, tool_id=tool_id)["counts"]
        projection_items = projections._projection_plan_payload(target, tool_id=tool_id)["projections"]
        projection_summary = {
            "counts": projection_summary,
            "projections": [
                {
                    "harness": item.get("harness"),
                    "status": item.get("status"),
                    "action": item.get("action"),
                    "projection_path": item.get("projection_path"),
                }
                for item in projection_items
            ],
        }
        contract_fingerprint = safety._contract_fingerprint(target, tool)
        source_fingerprint = safety._source_fingerprint(target, tool)
    policy_decision = safety._policy_decision(target, plan_payload)
    blockers.extend(policy_decision["blockers"])
    return {
        "target": str(target),
        "config_path": str(paths.config_path(target)),
        "valid": tool is not None and not blockers,
        "tool_id": tool_id,
        "plan": plan_payload,
        "blockers": blockers,
        "policy": {key: value for key, value in policy_decision.items() if key != "env"},
        "validation_errors": validation_errors,
        "projection_blockers": projection_blockers,
        "projection_summary": projection_summary,
        "contract_fingerprint": contract_fingerprint,
        "source_fingerprint": source_fingerprint,
    }


def _read_calls(target: Path) -> list[dict[str, Any]]:
    path = paths.calls_path(target)
    if not path.is_file():
        return []
    calls: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            calls.append(item)
    return calls


def _write_calls(target: Path, calls: list[dict[str, Any]]) -> None:
    path = paths.calls_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n" for item in calls)
    path.write_text(text)


def _call_fingerprint(plan_payload: dict[str, Any]) -> str:
    return helpers._stable_hash(
        {
            "tool_id": plan_payload.get("tool_id"),
            "plan": plan_payload.get("plan"),
            "contract_fingerprint": plan_payload.get("contract_fingerprint"),
            "source_fingerprint": plan_payload.get("source_fingerprint"),
        }
    )


def _call_plan_from_record(call: dict[str, Any]) -> dict[str, Any]:
    contract = _dict_or_empty(call.get("contract"))
    return {
        "tool_id": call.get("tool_id"),
        "family": call.get("family"),
        "command": call.get("command"),
        "cwd": contract.get("cwd"),
        "timeout": contract.get("timeout"),
        "runtime_id": contract.get("runtime_id"),
        "requires_runtime": contract.get("requires_runtime", False),
        "runtime_health_path": contract.get("runtime_health_path"),
        "mcp_server_id": contract.get("mcp_server_id"),
        "mcp_tool_name": contract.get("mcp_tool_name"),
        "auth_label": contract.get("auth_label"),
        "env_labels": contract.get("env_labels", []),
        "arguments": call.get("arguments"),
        "args": call.get("args"),
        "permissions": contract.get("permissions", []),
        "effects": contract.get("effects", []),
        "approval_required": contract.get("approval_required"),
        "approval_mode": contract.get("approval_mode"),
    }


def _stored_call_fingerprint(call: dict[str, Any]) -> str:
    return helpers._stable_hash(
        {
            "tool_id": call.get("tool_id"),
            "plan": _call_plan_from_record(call),
            "contract_fingerprint": call.get("contract_fingerprint"),
            "source_fingerprint": call.get("source_fingerprint"),
        }
    )


def _approval_fingerprint(call: dict[str, Any]) -> str:
    return helpers._stable_hash(
        {
            "id": call.get("id"),
            "tool_id": call.get("tool_id"),
            "status": call.get("status"),
            "reviewed_at": call.get("reviewed_at"),
            "review_reason": call.get("review_reason"),
            "call_fingerprint": call.get("call_fingerprint"),
            "contract_fingerprint": call.get("contract_fingerprint"),
            "source_fingerprint": call.get("source_fingerprint"),
        }
    )


def _make_call_record(plan_payload: dict[str, Any]) -> dict[str, Any]:
    fingerprint = _call_fingerprint(plan_payload)
    now = helpers._now().isoformat()
    plan = _dict_or_empty(plan_payload.get("plan"))
    return {
        "id": f"call-{fingerprint}",
        "status": "pending",
        "created_at": now,
        "reviewed_at": None,
        "review_reason": None,
        "tool_id": plan_payload.get("tool_id"),
        "family": plan.get("family"),
        "command": plan.get("command"),
        "args": plan.get("args"),
        "arguments": plan.get("arguments"),
        "contract": {
            "approval_mode": plan.get("approval_mode"),
            "approval_required": plan.get("approval_required"),
            "permissions": plan.get("permissions", []),
            "effects": plan.get("effects", []),
            "auth_label": plan.get("auth_label"),
            "env_labels": plan.get("env_labels", []),
            "cwd": plan.get("cwd"),
            "timeout": plan.get("timeout"),
            "runtime_id": plan.get("runtime_id"),
            "requires_runtime": plan.get("requires_runtime", False),
            "runtime_health_path": plan.get("runtime_health_path"),
            "mcp_server_id": plan.get("mcp_server_id"),
            "mcp_tool_name": plan.get("mcp_tool_name"),
        },
        "blockers": plan_payload.get("blockers", []),
        "policy": plan_payload.get("policy", {}),
        "projection_summary": plan_payload.get("projection_summary", {}),
        "contract_fingerprint": plan_payload.get("contract_fingerprint"),
        "source_fingerprint": plan_payload.get("source_fingerprint"),
        "call_fingerprint": fingerprint,
        "approval_fingerprint": None,
        "started_at": None,
        "completed_at": None,
        "run_id": None,
        "receipt_path": None,
        "exit_code": None,
    }


def _queue_call_payload(
    target: Path,
    tool_id: str,
    *,
    args: str | None = None,
    args_json: Path | None = None,
    include_blocked: bool = False,
) -> tuple[dict[str, Any], int]:
    target = target.expanduser().resolve()
    plan_payload = _call_plan_payload(target, tool_id, args=args, args_json=args_json)
    record = _make_call_record(plan_payload)
    if record["blockers"] and not include_blocked:
        return {
            "target": str(target),
            "calls_path": str(paths.calls_path(target)),
            "created": 0,
            "skipped": 0,
            "blocked": 1,
            "call": record,
            "reason": "blocked call plans require --include-blocked",
        }, 1
    calls = _read_calls(target)
    for existing in calls:
        if existing.get("call_fingerprint") != record["call_fingerprint"]:
            continue
        if existing.get("status") in {"pending", "approved"}:
            return {
                "target": str(target),
                "calls_path": str(paths.calls_path(target)),
                "created": 0,
                "skipped": 1,
                "blocked": 0,
                "call": existing,
                "reason": f"equivalent call already {existing.get('status')}",
            }, 0
        if existing.get("status") == "rejected":
            return {
                "target": str(target),
                "calls_path": str(paths.calls_path(target)),
                "created": 0,
                "skipped": 1,
                "blocked": 0,
                "call": existing,
                "reason": "equivalent rejected call requires changed args or contract fingerprint",
            }, 0
    existing_ids = {str(existing.get("id")) for existing in calls}
    if record["id"] in existing_ids:
        record["id"] = (
            f"{record['id']}-queued-{helpers._stable_hash({'created_at': record['created_at'], 'count': len(calls)})}"
        )
    calls.append(record)
    _write_calls(target, calls)
    return {
        "target": str(target),
        "calls_path": str(paths.calls_path(target)),
        "created": 1,
        "skipped": 0,
        "blocked": 0,
        "call": record,
        "reason": None,
    }, 0


def _resolve_call(target: Path, call_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
    calls = _read_calls(target)
    matches = [item for item in calls if str(item.get("id", "")).startswith(call_id)]
    if not matches:
        return None, calls, f"call not found: {call_id}"
    if len(matches) > 1:
        return None, calls, f"call id is ambiguous: {call_id}"
    return matches[0], calls, None


def _call_current_fingerprints(target: Path, call: dict[str, Any]) -> tuple[str | None, str | None]:
    tool_id = str(call.get("tool_id") or "")
    tool, _ = projections._find_tool(target, tool_id)
    if tool is None:
        return None, None
    return safety._contract_fingerprint(target, tool), safety._source_fingerprint(target, tool)


def _call_projection_summary(target: Path, tool_id: str) -> dict[str, Any]:
    payload = projections._projection_plan_payload(target, tool_id=tool_id)
    return {
        "counts": payload.get("counts", {}),
        "projections": [
            {
                "harness": item.get("harness"),
                "status": item.get("status"),
                "action": item.get("action"),
                "projection_path": item.get("projection_path"),
            }
            for item in payload.get("projections", [])
            if isinstance(item, dict)
        ],
    }


def _runtime_snapshot_for_call(target: Path, call: dict[str, Any], *, run_health: bool = True) -> dict[str, Any] | None:
    contract = _dict_or_empty(call.get("contract"))
    runtime_id = contract.get("runtime_id")
    if not isinstance(runtime_id, str) or not runtime_id.strip():
        return None
    runtime, errors = runtimes._find_runtime(target, runtime_id)
    if runtime is None:
        return {
            "id": runtime_id,
            "state": "missing",
            "running": False,
            "health_ok": False,
            "errors": errors,
        }
    return runtimes._runtime_status_item(target, runtime, run_health=run_health)


def _run_id_for_call(call: dict[str, Any], started_at: str) -> str:
    suffix = helpers._stable_hash({"call_id": call.get("id"), "started_at": started_at})
    return f"run-{suffix}"


def _call_run_blockers(target: Path, call: dict[str, Any], *, expected_status: str = "approved") -> list[str]:
    blockers: list[str] = []
    contract = _dict_or_empty(call.get("contract"))
    status = str(call.get("status") or "")
    if status != expected_status:
        if status == "completed":
            blockers.append("completed calls cannot be run again")
        elif status == "failed":
            blockers.append("failed calls are not approved for another run")
        elif status == "running":
            blockers.append("call is already running")
        else:
            if expected_status == "approved":
                blockers.append(f"call must be approved before run: {status or 'unknown'}")
            else:
                blockers.append(f"call must be {expected_status} before run: {status or 'unknown'}")
    if call.get("blockers"):
        blockers.append("blocked calls cannot be run")
    family = call.get("family")
    if family not in {"script", "mcp"}:
        blockers.append("only script and mcp family calls can be run")
    if not isinstance(call.get("command"), str) or not str(call.get("command")).strip():
        blockers.append("command is required")
    if safety._high_risk_command(call.get("command")):
        blockers.append("command shape is high risk")
    approval_fingerprint = call.get("approval_fingerprint")
    if not approval_fingerprint:
        blockers.append("approval fingerprint is missing")
    elif approval_fingerprint != _approval_fingerprint(call):
        blockers.append("approval fingerprint is stale")
    if call.get("call_fingerprint") != _stored_call_fingerprint(call):
        blockers.append("stored args or call metadata fingerprint is stale")
    current_contract, current_source = _call_current_fingerprints(target, call)
    if current_contract != call.get("contract_fingerprint"):
        blockers.append("contract fingerprint is stale")
    if current_source != call.get("source_fingerprint"):
        blockers.append("source fingerprint is stale")
    tool_id = str(call.get("tool_id") or "")
    if not tool_id:
        blockers.append("tool_id is missing")
    else:
        tool, errors = projections._find_tool(target, tool_id)
        if tool is None:
            blockers.extend(errors or [f"tool not found: {tool_id}"])
        else:
            if tool.get("family") != family:
                blockers.append(f"configured tool family changed: {tool.get('family')}")
            current_projection = _call_projection_summary(target, tool_id)
            if current_projection != call.get("projection_summary", {}):
                blockers.append("projection summary is stale")
            cwd_value = contract.get("cwd")
            cwd_path = helpers._as_path(target, cwd_value) if cwd_value else target
            if cwd_path is None or not cwd_path.is_dir():
                blockers.append(f"cwd does not exist: {cwd_path}")
    if not safety._command_parts(call.get("command")):
        blockers.append("command could not be parsed")
    if contract.get("requires_runtime"):
        runtime_snapshot = _runtime_snapshot_for_call(target, call)
        if runtime_snapshot is None:
            blockers.append("runtime is required but runtime_id is missing")
        elif runtime_snapshot.get("state") == "missing":
            blockers.append(f"required runtime is missing: {runtime_snapshot.get('id')}")
        elif not runtime_snapshot.get("running"):
            blockers.append(f"required runtime is not running: {runtime_snapshot.get('id')}")
        elif runtime_snapshot.get("managed") is False:
            blockers.append(f"required runtime is not managed by Brigade: {runtime_snapshot.get('id')}")
        elif runtime_snapshot.get("health_ok") is False:
            blockers.append(f"required runtime is unhealthy: {runtime_snapshot.get('id')}")
    if family == "mcp":
        if not contract.get("runtime_id"):
            blockers.append("runtime_id is required for MCP calls")
        if not contract.get("mcp_tool_name"):
            blockers.append("mcp_tool_name is required for MCP calls")
    policy_decision = safety._policy_decision(target, _call_plan_from_record(call))
    blockers.extend(policy_decision["blockers"])
    return blockers


def _call_health(target: Path) -> dict[str, Any]:
    calls = _read_calls(target)
    now = helpers._now()
    issues: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for call in calls:
        status = str(call.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        created = helpers._parse_iso_datetime(call.get("created_at"))
        if status == "pending" and created is not None:
            age_hours = (now - created).total_seconds() / 3600
            if age_hours > constants.CALL_STALE_HOURS:
                issues.append(
                    {
                        "status": constants.WARN,
                        "name": "tool_call_stale_pending",
                        "issue_type": "call_stale_pending",
                        "tool_id": call.get("tool_id"),
                        "call_id": call.get("id"),
                        "detail": f"{call.get('id')} pending for {age_hours:.1f}h",
                    }
                )
        if status == "pending" and call.get("blockers"):
            issues.append(
                {
                    "status": constants.WARN,
                    "name": "tool_call_blocked",
                    "issue_type": "call_blocked",
                    "tool_id": call.get("tool_id"),
                    "call_id": call.get("id"),
                    "detail": f"{call.get('id')} has {len(call.get('blockers', []))} blocker(s)",
                }
            )
        if status == "approved":
            current_contract, current_source = _call_current_fingerprints(target, call)
            if current_contract != call.get("contract_fingerprint") or current_source != call.get("source_fingerprint"):
                issues.append(
                    {
                        "status": constants.WARN,
                        "name": "tool_call_stale_approved",
                        "issue_type": "call_stale_approved",
                        "tool_id": call.get("tool_id"),
                        "call_id": call.get("id"),
                        "detail": f"{call.get('id')} approved with stale contract or source fingerprint",
                    }
                )
        if status == "running":
            started = helpers._parse_iso_datetime(call.get("started_at"))
            if started is not None:
                age_hours = (now - started).total_seconds() / 3600
                if age_hours > constants.CALL_RUNNING_STALE_HOURS:
                    issues.append(
                        {
                            "status": constants.WARN,
                            "name": "tool_call_running_stale",
                            "issue_type": "call_running_stale",
                            "tool_id": call.get("tool_id"),
                            "call_id": call.get("id"),
                            "detail": f"{call.get('id')} running for {age_hours:.1f}h",
                        }
                    )
        if status == "failed":
            issues.append(
                {
                    "status": constants.WARN,
                    "name": "tool_call_failed",
                    "issue_type": "call_failed",
                    "tool_id": call.get("tool_id"),
                    "call_id": call.get("id"),
                    "detail": f"{call.get('id')} failed with exit_code={call.get('exit_code')}",
                }
            )
        if status in {"held", "rejected"}:
            issues.append(
                {
                    "status": constants.WARN,
                    "name": f"tool_call_{status}",
                    "issue_type": f"call_{status}",
                    "tool_id": call.get("tool_id"),
                    "call_id": call.get("id"),
                    "detail": f"{call.get('id')} is {status}: {call.get('review_reason') or ''}".strip(),
                }
            )
    return {
        "calls_path": str(paths.calls_path(target)),
        "calls": calls,
        "counts": counts,
        "pending_count": counts.get("pending", 0),
        "issue_count": len(issues),
        "issues": issues,
        "top_issue": issues[0] if issues else None,
    }


def call_plan(
    *,
    target: Path,
    tool_id: str,
    args: str | None = None,
    args_json: Path | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _call_plan_payload(target, tool_id, args=args, args_json=args_json)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"tools call plan: {tool_id}")
    print(f"target: {target}")
    blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
    if blockers:
        print(f"blockers: {len(blockers)}")
        for blocker in blockers:
            print(f"- {blocker}")
        return 1
    plan_payload = payload["plan"]
    print(f"command: {plan_payload.get('command')}")
    print(f"approval_mode: {plan_payload.get('approval_mode')}")
    print(f"approval_required: {plan_payload.get('approval_required')}")
    print(f"permissions: {', '.join(plan_payload.get('permissions', []))}")
    print(f"effects: {', '.join(plan_payload.get('effects', []))}")
    print("arguments:")
    for key, value in plan_payload.get("arguments", {}).items():
        print(f"  {key}: {value}")
    return 0


def call_queue(
    *,
    target: Path,
    tool_id: str,
    args: str | None = None,
    args_json: Path | None = None,
    include_blocked: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, rc = _queue_call_payload(
        target,
        tool_id,
        args=args,
        args_json=args_json,
        include_blocked=include_blocked,
    )
    text_lines = [
        f"tools call queue: {tool_id}",
        f"calls_path: {payload['calls_path']}",
        f"created: {payload['created']}",
        f"skipped: {payload['skipped']}",
        f"blocked: {payload['blocked']}",
    ]
    if payload.get("reason"):
        text_lines.append(f"reason: {payload['reason']}")
    call = payload.get("call") if isinstance(payload.get("call"), dict) else {}
    if call:
        text_lines.append(f"call: {call.get('id')}")
        text_lines.append(f"status: {call.get('status')}")
        text_lines.append(f"blockers: {len(call.get('blockers', [])) if isinstance(call.get('blockers'), list) else 0}")
    return emit(payload, json_output, text_lines, rc)


def call_list(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    calls = _read_calls(target)
    counts: dict[str, int] = {}
    for call in calls:
        status = str(call.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    payload = {"target": str(target), "calls_path": str(paths.calls_path(target)), "calls": calls, "counts": counts}
    text_lines = [f"tools call list: {target}", f"calls_path: {paths.calls_path(target)}", f"calls: {len(calls)}"]
    for status, count in sorted(counts.items()):
        text_lines.append(f"{status}: {count}")
    for call in calls:
        text_lines.append(
            f"- {call.get('id')} [{call.get('status')}] {call.get('tool_id')} blockers={len(call.get('blockers', []))}"
        )
    return emit(payload, json_output, text_lines, 0)


def call_show(*, target: Path, call_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    call, _, error = _resolve_call(target, call_id)
    payload = {"target": str(target), "calls_path": str(paths.calls_path(target)), "call": call, "error": error}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if call is not None else 1
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    assert call is not None
    print(f"call: {call.get('id')}")
    print(f"tool_id: {call.get('tool_id')}")
    print(f"status: {call.get('status')}")
    print(f"created_at: {call.get('created_at')}")
    if call.get("reviewed_at"):
        print(f"reviewed_at: {call.get('reviewed_at')}")
    if call.get("review_reason"):
        print(f"review_reason: {call.get('review_reason')}")
    print(f"blockers: {len(call.get('blockers', []))}")
    return 0


def _call_review(
    *,
    target: Path,
    call_id: str,
    status: str,
    reason: str | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    call, calls, error = _resolve_call(target, call_id)
    payload: dict[str, Any]
    if call is None:
        payload = {"target": str(target), "error": error}
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"error: {error}", file=sys.stderr)
        return 1
    if status == "approved" and call.get("blockers"):
        payload = {"target": str(target), "error": "blocked calls cannot be approved", "call": call}
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print("error: blocked calls cannot be approved", file=sys.stderr)
        return 1
    call["status"] = status
    call["reviewed_at"] = helpers._now().isoformat()
    call["review_reason"] = reason
    call["approval_fingerprint"] = _approval_fingerprint(call) if status == "approved" else None
    _write_calls(target, calls)
    payload = {"target": str(target), "calls_path": str(paths.calls_path(target)), "call": call}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"call: {call.get('id')}")
    print(f"status: {call.get('status')}")
    if reason:
        print(f"review_reason: {reason}")
    return 0


def call_approve(*, target: Path, call_id: str, json_output: bool = False) -> int:
    return _call_review(target=target, call_id=call_id, status="approved", json_output=json_output)


def call_reject(*, target: Path, call_id: str, reason: str, json_output: bool = False) -> int:
    return _call_review(target=target, call_id=call_id, status="rejected", reason=reason, json_output=json_output)


def call_hold(*, target: Path, call_id: str, reason: str, json_output: bool = False) -> int:
    return _call_review(target=target, call_id=call_id, status="held", reason=reason, json_output=json_output)
