"""MCP JSON-RPC execution helpers for the tools command family."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from . import helpers, paths, safety


def _mcp_jsonrpc_requests(call: dict[str, Any]) -> list[dict[str, Any]]:
    raw_contract = call.get("contract")
    contract = raw_contract if isinstance(raw_contract, dict) else {}
    tool_name = str(contract.get("mcp_tool_name") or call.get("tool_id") or "")
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    return [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "brigade", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": args},
        },
    ]


def _parse_mcp_responses(stdout: object) -> tuple[list[dict[str, Any]], list[str]]:
    responses: list[dict[str, Any]] = []
    errors: list[str] = []
    for line in str(stdout or "").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"invalid JSON-RPC response: {exc.msg}")
            continue
        if not isinstance(payload, dict):
            errors.append("JSON-RPC response must be an object")
            continue
        responses.append(payload)
        if payload.get("error"):
            errors.append(f"JSON-RPC error for id={payload.get('id')}: {helpers._short(str(payload.get('error')))}")
    return responses, errors


def _mcp_response_by_id(responses: list[dict[str, Any]], request_id: int) -> dict[str, Any] | None:
    for response in responses:
        if response.get("id") == request_id:
            return response
    return None


def _mcp_tool_list_contains(response: dict[str, Any] | None, tool_name: str) -> bool:
    result = response.get("result") if isinstance(response, dict) else None
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return False
    for item in tools:
        if isinstance(item, dict) and item.get("name") == tool_name:
            return True
    return False


def _run_mcp_call(
    target: Path,
    *,
    call: dict[str, Any],
    run_id: str,
    cwd: Path,
    policy_decision: dict[str, Any],
    timeout_value: float | None,
) -> tuple[object, object, int | None, bool, str, dict[str, Any]]:
    raw_contract = call.get("contract")
    contract = raw_contract if isinstance(raw_contract, dict) else {}
    raw_env_values = policy_decision.get("env")
    env_values = raw_env_values if isinstance(raw_env_values, dict) else {}
    run_env = os.environ.copy()
    for label, value in env_values.items():
        run_env[str(label)] = str(value)
    run_env["BRIGADE_TOOL_CHECKPOINT_DIR"] = str(paths.checkpoints_path(target))
    run_env["BRIGADE_TOOL_CALL_ID"] = str(call.get("id") or "")
    run_env["BRIGADE_TOOL_RUN_ID"] = run_id
    tool_name = str(contract.get("mcp_tool_name") or "")
    requests = _mcp_jsonrpc_requests(call)
    request_text = "".join(json.dumps(request, sort_keys=True) + "\n" for request in requests)
    started_request_id = requests[-1]["id"]
    status = "completed"
    exit_code: int | None = None
    timed_out = False
    stdout: object = ""
    stderr: object = ""
    try:
        completed = subprocess.run(
            safety._command_parts(call.get("command")),
            input=request_text,
            cwd=cwd,
            env=run_env,
            text=True,
            capture_output=True,
            timeout=timeout_value,
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        exit_code = completed.returncode
        if completed.returncode != 0:
            status = "failed"
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        timed_out = True
        status = "failed"
    except OSError as exc:
        stderr = str(exc)
        status = "failed"
    responses, response_errors = _parse_mcp_responses(stdout)
    if response_errors and status == "completed":
        status = "failed"
    if status == "completed" and _mcp_response_by_id(responses, 1) is None:
        response_errors.append("missing initialize response")
        status = "failed"
    list_response = _mcp_response_by_id(responses, 2)
    if status == "completed" and not _mcp_tool_list_contains(list_response, tool_name):
        response_errors.append(f"MCP tool not listed by server: {tool_name}")
        status = "failed"
    call_response = _mcp_response_by_id(responses, 3)
    if status == "completed" and call_response is None:
        response_errors.append("missing tools/call response")
        status = "failed"
    if response_errors:
        stderr = (str(stderr or "") + "\n" + "\n".join(response_errors)).strip()
    extra = {
        "mcp_server_id": contract.get("mcp_server_id") or contract.get("runtime_id"),
        "mcp_tool_name": tool_name,
        "mcp_request_id": started_request_id,
        "mcp_request_payload": safety._redact_payload(requests[-1]),
        "mcp_response_summary": safety._redact_payload(call_response or {}),
        "mcp_response_count": len(responses),
        "mcp_error_type": "mcp_execution_failed" if status == "failed" else None,
    }
    return stdout, stderr, exit_code, timed_out, status, extra
