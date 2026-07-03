"""Run execution and history commands for the tools command family."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..render import emit
from . import calls as calls_mod, checkpoint_store, constants, helpers, mcp, paths, safety


def _write_run_receipt(
    target: Path,
    *,
    call: dict[str, Any],
    run_id: str,
    started_at: str,
    completed_at: str,
    duration_seconds: float,
    status: str,
    exit_code: int | None,
    timed_out: bool,
    stdout: object,
    stderr: object,
    argv: list[str],
    cwd: Path,
    policy_decision: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_dir = paths.runs_path(target)
    run_dir.mkdir(parents=True, exist_ok=True)
    stdout_text = "" if stdout is None else str(stdout)
    stderr_text = "" if stderr is None else str(stderr)
    contract = call.get("contract") if isinstance(call.get("contract"), dict) else {}
    runtime_snapshot = calls_mod._runtime_snapshot_for_call(target, call, run_health=False)
    if policy_decision is None:
        policy_decision = safety._policy_decision(target, calls_mod._call_plan_from_record(call))
    safe_policy = {key: value for key, value in policy_decision.items() if key != "env"}
    env_values = policy_decision.get("env") if isinstance(policy_decision.get("env"), dict) else {}
    for value in env_values.values():
        if value:
            stdout_text = stdout_text.replace(str(value), "[redacted]")
            stderr_text = stderr_text.replace(str(value), "[redacted]")
    if extra:
        extra = safety._redact_known_values(extra, [str(value) for value in env_values.values() if value])
    stdout_path = run_dir / f"{run_id}.stdout.log"
    stderr_path = run_dir / f"{run_id}.stderr.log"
    receipt_path = run_dir / f"{run_id}.json"
    stdout_path.write_text(stdout_text)
    stderr_path.write_text(stderr_text)
    receipt = {
        "id": run_id,
        "call_id": call.get("id"),
        "tool_id": call.get("tool_id"),
        "family": call.get("family"),
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_seconds": round(duration_seconds, 3),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "timeout": contract.get("timeout"),
        "command_label": call.get("command"),
        "argv": [safety._redact_text(part, 160) for part in argv],
        "cwd": str(cwd),
        "args": call.get("args"),
        "arguments": call.get("arguments"),
        "stdout_summary": safety._redact_text(stdout_text),
        "stderr_summary": safety._redact_text(stderr_text),
        "stdout_log_path": str(stdout_path),
        "stderr_log_path": str(stderr_path),
        "receipt_path": str(receipt_path),
        "contract_fingerprint": call.get("contract_fingerprint"),
        "source_fingerprint": call.get("source_fingerprint"),
        "call_fingerprint": call.get("call_fingerprint"),
        "approval_fingerprint": call.get("approval_fingerprint"),
        "approval": {
            "reviewed_at": call.get("reviewed_at"),
            "review_reason": call.get("review_reason"),
        },
        "permissions": contract.get("permissions", []),
        "effects": contract.get("effects", []),
        "runtime_id": contract.get("runtime_id"),
        "mcp_server_id": contract.get("mcp_server_id"),
        "mcp_tool_name": contract.get("mcp_tool_name"),
        "runtime": runtime_snapshot,
        "policy": safe_policy,
        "env_labels_used": policy_decision.get("env_labels_used", []),
        "projection_summary": call.get("projection_summary", {}),
    }
    if extra:
        receipt.update(extra)
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt


def _run_receipt_paths(target: Path) -> list[Path]:
    run_dir = paths.runs_path(target)
    if not run_dir.is_dir():
        return []
    return sorted(path for path in run_dir.glob("*.json") if path.is_file())


def _read_run_receipt(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text())
    except OSError as exc:
        return None, str(exc)
    except json.JSONDecodeError as exc:
        return None, f"invalid run receipt JSON: {exc.msg}"
    if not isinstance(payload, dict):
        return None, "run receipt must be a JSON object"
    payload.setdefault("receipt_path", str(path))
    return payload, None


def _run_sort_key(receipt: dict[str, Any]) -> str:
    return str(receipt.get("started_at") or receipt.get("completed_at") or receipt.get("id") or "")


def _run_public_summary(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": receipt.get("id"),
        "status": receipt.get("status"),
        "call_id": receipt.get("call_id"),
        "tool_id": receipt.get("tool_id"),
        "family": receipt.get("family"),
        "started_at": receipt.get("started_at"),
        "completed_at": receipt.get("completed_at"),
        "duration_seconds": receipt.get("duration_seconds"),
        "exit_code": receipt.get("exit_code"),
        "timed_out": receipt.get("timed_out"),
        "timeout": receipt.get("timeout"),
        "policy": receipt.get("policy", {}),
        "runtime": receipt.get("runtime"),
        "mcp_server_id": receipt.get("mcp_server_id"),
        "mcp_tool_name": receipt.get("mcp_tool_name"),
        "mcp_request_id": receipt.get("mcp_request_id"),
        "mcp_response_summary": receipt.get("mcp_response_summary"),
        "stdout_summary": receipt.get("stdout_summary"),
        "stderr_summary": receipt.get("stderr_summary"),
        "stdout_log_path": receipt.get("stdout_log_path"),
        "stderr_log_path": receipt.get("stderr_log_path"),
        "receipt_path": receipt.get("receipt_path"),
    }


def _run_history_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    runs: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for path in _run_receipt_paths(target):
        receipt, error = _read_run_receipt(path)
        if error is not None or receipt is None:
            errors.append({"receipt_path": str(path), "error": error or "invalid run receipt"})
            continue
        summary = _run_public_summary(receipt)
        runs.append(summary)
        status = str(summary.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    runs.sort(key=_run_sort_key, reverse=True)
    return {
        "target": str(target),
        "runs_path": str(paths.runs_path(target)),
        "runs": runs,
        "run_count": len(runs),
        "counts": counts,
        "errors": errors,
        "error_count": len(errors),
        "latest": runs[0] if runs else None,
    }


def _resolve_run_receipt(target: Path, run_id: str) -> tuple[dict[str, Any] | None, str | None]:
    matches: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    for path in _run_receipt_paths(target):
        receipt, error = _read_run_receipt(path)
        if error is not None or receipt is None:
            parse_errors.append(f"{path}: {error}")
            continue
        candidate_id = str(receipt.get("id") or path.stem)
        if candidate_id.startswith(run_id) or path.stem.startswith(run_id):
            matches.append(receipt)
    if not matches:
        suffix = f"; skipped malformed receipts: {'; '.join(parse_errors)}" if parse_errors else ""
        return None, f"run not found: {run_id}{suffix}"
    if len(matches) > 1:
        return None, f"run id is ambiguous: {run_id}"
    return matches[0], None


def _replay_plan_payload(target: Path, receipt: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    tool_id = receipt.get("tool_id")
    args = receipt.get("args")
    blockers: list[str] = []
    if not isinstance(tool_id, str) or not tool_id.strip():
        blockers.append("receipt tool_id is missing")
        tool_id = ""
    if not isinstance(args, dict):
        blockers.append("receipt does not contain replay args")
        args = {}
    plan_payload = calls_mod._call_plan_payload(target, tool_id, args=json.dumps(args, sort_keys=True))
    blockers.extend(plan_payload.get("blockers", []))
    if not blockers:
        candidate = calls_mod._make_call_record(plan_payload)
        candidate["status"] = "approved"
        candidate["reviewed_at"] = helpers._now().isoformat()
        candidate["review_reason"] = f"replay validation for {receipt.get('id')}"
        candidate["approval_fingerprint"] = calls_mod._approval_fingerprint(candidate)
        blockers.extend(calls_mod._call_run_blockers(target, candidate))
    return plan_payload, blockers


def _replay_call_payload(target: Path, run_id: str) -> tuple[dict[str, Any], int]:
    target = target.expanduser().resolve()
    receipt, error = _resolve_run_receipt(target, run_id)
    if receipt is None:
        return {"target": str(target), "runs_path": str(paths.runs_path(target)), "error": error}, 1
    plan_payload, blockers = _replay_plan_payload(target, receipt)
    payload: dict[str, Any] = {
        "target": str(target),
        "runs_path": str(paths.runs_path(target)),
        "calls_path": str(paths.calls_path(target)),
        "run": _run_public_summary(receipt),
        "plan": plan_payload,
        "blockers": blockers,
        "created": 0,
        "executed": 0,
    }
    if blockers:
        payload["error"] = "run replay is blocked"
        return payload, 1
    record = calls_mod._make_call_record(plan_payload)
    record["replay_of_run_id"] = receipt.get("id")
    record["replay_source_call_id"] = receipt.get("call_id")
    record["replay_created_at"] = helpers._now().isoformat()
    calls = calls_mod._read_calls(target)
    existing_ids = {str(call.get("id")) for call in calls}
    if record["id"] in existing_ids:
        record["id"] = (
            f"{record['id']}-replay-{helpers._stable_hash({'run_id': receipt.get('id'), 'created_at': record['replay_created_at']})}"
        )
    calls.append(record)
    calls_mod._write_calls(target, calls)
    payload["call"] = record
    payload["created"] = 1
    return payload, 0


def _log_path_exists(target: Path, value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    path = Path(value)
    if not path.is_absolute():
        path = target / path
    return path.is_file()


def _run_history_health(target: Path) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for path in _run_receipt_paths(target):
        receipt, error = _read_run_receipt(path)
        if error is not None or receipt is None:
            issues.append(
                {
                    "status": constants.WARN,
                    "name": "tool_run_receipt_invalid",
                    "issue_type": "run_receipt_invalid",
                    "detail": f"{path}: {error or 'invalid run receipt'}",
                    "run_id": path.stem,
                }
            )
            continue
        runs.append(receipt)
        status = str(receipt.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        run_id = str(receipt.get("id") or path.stem)
        tool_id = receipt.get("tool_id")
        if status == "failed":
            issues.append(
                {
                    "status": constants.WARN,
                    "name": "tool_run_failed",
                    "issue_type": "run_failed",
                    "tool_id": tool_id,
                    "run_id": run_id,
                    "call_id": receipt.get("call_id"),
                    "detail": f"{run_id} failed with exit_code={receipt.get('exit_code')}",
                }
            )
            if receipt.get("family") == "mcp":
                issues.append(
                    {
                        "status": constants.WARN,
                        "name": "tool_mcp_execution_failed",
                        "issue_type": str(receipt.get("mcp_error_type") or "mcp_execution_failed"),
                        "tool_id": tool_id,
                        "run_id": run_id,
                        "call_id": receipt.get("call_id"),
                        "detail": f"{run_id} MCP execution failed: {helpers._short(str(receipt.get('stderr_summary') or receipt.get('mcp_response_summary') or ''))}",
                    }
                )
        if receipt.get("timed_out") is True:
            issues.append(
                {
                    "status": constants.WARN,
                    "name": "tool_run_timed_out",
                    "issue_type": "run_timed_out",
                    "tool_id": tool_id,
                    "run_id": run_id,
                    "call_id": receipt.get("call_id"),
                    "detail": f"{run_id} timed out",
                }
            )
        for key in ("stdout_log_path", "stderr_log_path"):
            if not _log_path_exists(target, receipt.get(key)):
                issues.append(
                    {
                        "status": constants.WARN,
                        "name": "tool_run_missing_log",
                        "issue_type": "run_missing_log",
                        "tool_id": tool_id,
                        "run_id": run_id,
                        "call_id": receipt.get("call_id"),
                        "detail": f"{run_id} missing {key}",
                    }
                )
        _, blockers = _replay_plan_payload(target, receipt)
        if blockers:
            issues.append(
                {
                    "status": constants.WARN,
                    "name": "tool_run_replay_blocked",
                    "issue_type": "run_replay_blocked",
                    "tool_id": tool_id,
                    "run_id": run_id,
                    "call_id": receipt.get("call_id"),
                    "detail": f"{run_id} replay blocked: {helpers._short('; '.join(blockers))}",
                }
            )
    runs.sort(key=_run_sort_key, reverse=True)
    return {
        "runs_path": str(paths.runs_path(target)),
        "run_count": len(runs),
        "counts": counts,
        "issue_count": len(issues),
        "issues": issues,
        "top_issue": issues[0] if issues else None,
        "latest": _run_public_summary(runs[0]) if runs else None,
    }


def _next_approved_call(calls: list[dict[str, Any]]) -> dict[str, Any] | None:
    approved = [call for call in calls if call.get("status") == "approved"]
    approved.sort(key=lambda call: str(call.get("created_at") or ""))
    return approved[0] if approved else None


def _run_call_payload(
    target: Path, *, call_id: str | None = None, next_call: bool = False
) -> tuple[dict[str, Any], int]:
    target = target.expanduser().resolve()
    calls = calls_mod._read_calls(target)
    call: dict[str, Any] | None
    error: str | None = None
    if next_call:
        call = _next_approved_call(calls)
        if call is None:
            error = "no approved calls available"
    elif call_id:
        call, calls, error = calls_mod._resolve_call(target, call_id)
    else:
        call = None
        error = "pass a call id or --next"
    if call is None:
        return {"target": str(target), "calls_path": str(paths.calls_path(target)), "error": error}, 1
    blockers = calls_mod._call_run_blockers(target, call)
    if blockers:
        return {
            "target": str(target),
            "calls_path": str(paths.calls_path(target)),
            "call": call,
            "blockers": blockers,
            "error": "call is not runnable",
        }, 1
    contract = call.get("contract") if isinstance(call.get("contract"), dict) else {}
    cwd_value = contract.get("cwd")
    cwd = helpers._as_path(target, cwd_value) if cwd_value else target
    assert cwd is not None
    argv = safety._command_parts(call.get("command"))
    if call.get("family") != "mcp":
        for key in sorted((call.get("arguments") if isinstance(call.get("arguments"), dict) else {}).keys()):
            value = call["arguments"][key]
            if value is None:
                continue
            argv.extend(shlex.split(str(value)))
    started_at = helpers._now().isoformat()
    run_id = calls_mod._run_id_for_call(call, started_at)
    receipt_path = paths.runs_path(target) / f"{run_id}.json"
    paths.checkpoints_path(target).mkdir(parents=True, exist_ok=True)
    call["status"] = "running"
    call["started_at"] = started_at
    call["completed_at"] = None
    call["run_id"] = run_id
    call["receipt_path"] = str(receipt_path)
    call["exit_code"] = None
    calls_mod._write_calls(target, calls)

    timeout = contract.get("timeout")
    timeout_value = float(timeout) if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) else None
    policy_decision = safety._policy_decision(target, calls_mod._call_plan_from_record(call), include_env_values=True)
    run_env = os.environ.copy()
    env_values = policy_decision.get("env") if isinstance(policy_decision.get("env"), dict) else {}
    for label, value in env_values.items():
        run_env[str(label)] = str(value)
    run_env["BRIGADE_TOOL_CHECKPOINT_DIR"] = str(paths.checkpoints_path(target))
    run_env["BRIGADE_TOOL_CALL_ID"] = str(call.get("id") or "")
    run_env["BRIGADE_TOOL_RUN_ID"] = run_id
    start_monotonic = time.monotonic()
    started_epoch = time.time()
    stdout: object = ""
    stderr: object = ""
    exit_code: int | None = None
    timed_out = False
    status = "completed"
    extra_receipt: dict[str, Any] = {}
    if call.get("family") == "mcp":
        stdout, stderr, exit_code, timed_out, status, extra_receipt = mcp._run_mcp_call(
            target,
            call=call,
            run_id=run_id,
            cwd=cwd,
            policy_decision=policy_decision,
            timeout_value=timeout_value,
        )
    else:
        try:
            completed = subprocess.run(
                argv,
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
    duration_seconds = time.monotonic() - start_monotonic
    completed_at = helpers._now().isoformat()
    checkpoints = checkpoint_store._collect_run_checkpoints(
        target,
        call=call,
        run_id=run_id,
        fallback_created_at=completed_at,
        started_epoch=started_epoch,
    )
    checkpoint = checkpoints[0] if checkpoints else None
    if checkpoint is not None:
        status = "waiting"
        extra_receipt["checkpoint_id"] = checkpoint.get("id")
        extra_receipt["checkpoint"] = checkpoint_store._checkpoint_public_summary(checkpoint)
    receipt = _write_run_receipt(
        target,
        call=call,
        run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration_seconds,
        status=status,
        exit_code=exit_code,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
        argv=argv,
        cwd=cwd,
        policy_decision=policy_decision,
        extra=extra_receipt,
    )
    call["status"] = status
    call["completed_at"] = completed_at
    call["exit_code"] = exit_code
    call["timed_out"] = timed_out
    call["receipt_path"] = receipt["receipt_path"]
    if checkpoint is not None:
        call["checkpoint_id"] = checkpoint.get("id")
    calls_mod._write_calls(target, calls)
    return {
        "target": str(target),
        "calls_path": str(paths.calls_path(target)),
        "runs_path": str(paths.runs_path(target)),
        "call": call,
        "receipt": receipt,
    }, 0 if status in {"completed", "waiting", "resumed"} else 1


def call_run(*, target: Path, call_id: str | None = None, next_call: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if bool(call_id) == bool(next_call):
        print("error: pass exactly one call id or --next", file=sys.stderr)
        return 2
    payload, rc = _run_call_payload(target, call_id=call_id, next_call=next_call)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    if payload.get("error"):
        print(f"error: {payload['error']}", file=sys.stderr)
        for blocker in payload.get("blockers", []):
            print(f"- {blocker}", file=sys.stderr)
        return rc
    call = payload["call"]
    receipt = payload["receipt"]
    print(f"tools call run: {call.get('id')}")
    print(f"status: {call.get('status')}")
    print(f"exit_code: {call.get('exit_code')}")
    print(f"receipt_path: {receipt.get('receipt_path')}")
    print(f"stdout_summary: {receipt.get('stdout_summary')}")
    print(f"stderr_summary: {receipt.get('stderr_summary')}")
    return rc


def run_list(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _run_history_payload(target)
    text_lines = [f"tools run list: {target}", f"runs_path: {payload['runs_path']}", f"runs: {payload['run_count']}"]
    for status, count in sorted(payload["counts"].items()):
        text_lines.append(f"{status}: {count}")
    for error in payload["errors"]:
        text_lines.append(f"[warn] run_receipt_invalid: {error.get('receipt_path')} {error.get('error')}")
    for run in payload["runs"]:
        text_lines.append(
            f"- {run.get('id')} [{run.get('status')}] {run.get('tool_id')} exit_code={run.get('exit_code')}"
        )
    return emit(payload, json_output, text_lines, 0)


def run_show(*, target: Path, run_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    receipt, error = _resolve_run_receipt(target, run_id)
    payload = {
        "target": str(target),
        "runs_path": str(paths.runs_path(target)),
        "run": _run_public_summary(receipt) if receipt is not None else None,
        "error": error,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if receipt is not None else 1
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    assert receipt is not None
    run = _run_public_summary(receipt)
    print(f"run: {run.get('id')}")
    print(f"tool_id: {run.get('tool_id')}")
    print(f"call_id: {run.get('call_id')}")
    print(f"status: {run.get('status')}")
    print(f"started_at: {run.get('started_at')}")
    print(f"completed_at: {run.get('completed_at')}")
    print(f"duration_seconds: {run.get('duration_seconds')}")
    print(f"exit_code: {run.get('exit_code')}")
    print(f"timed_out: {run.get('timed_out')}")
    print(f"stdout_summary: {run.get('stdout_summary')}")
    print(f"stderr_summary: {run.get('stderr_summary')}")
    print(f"stdout_log_path: {run.get('stdout_log_path')}")
    print(f"stderr_log_path: {run.get('stderr_log_path')}")
    return 0


def run_latest(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _run_history_payload(target)
    latest = payload["latest"]
    output = {"target": str(target), "runs_path": payload["runs_path"], "run": latest}
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0 if latest is not None else 1
    if latest is None:
        print(f"tools run latest: none ({payload['runs_path']})")
        return 1
    print(f"run: {latest.get('id')}")
    print(f"tool_id: {latest.get('tool_id')}")
    print(f"status: {latest.get('status')}")
    print(f"started_at: {latest.get('started_at')}")
    print(f"exit_code: {latest.get('exit_code')}")
    print(f"receipt_path: {latest.get('receipt_path')}")
    return 0


def run_replay(*, target: Path, run_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, rc = _replay_call_payload(target, run_id)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    if payload.get("error"):
        print(f"error: {payload['error']}", file=sys.stderr)
        for blocker in payload.get("blockers", []):
            print(f"- {blocker}", file=sys.stderr)
        return rc
    call = payload["call"]
    run = payload["run"]
    print(f"tools run replay: {run.get('id')}")
    print(f"call: {call.get('id')}")
    print(f"status: {call.get('status')}")
    print("executed: 0")
    print(f"next_command: brigade tools call approve {call.get('id')}")
    return rc
