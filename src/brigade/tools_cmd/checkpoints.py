"""Checkpoint review and resume commands for the tools command family."""

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
from . import calls as calls_mod, checkpoint_store, constants, helpers, paths, runs as runs_mod, safety


def _checkpoint_resume_blockers(
    target: Path, checkpoint: dict[str, Any]
) -> tuple[list[str], dict[str, Any] | None, list[dict[str, Any]]]:
    blockers: list[str] = []
    status = str(checkpoint.get("status") or "")
    if status != "approved":
        blockers.append(f"checkpoint must be approved before resume: {status or 'unknown'}")
    if checkpoint_store._checkpoint_expired(checkpoint):
        blockers.append("checkpoint is expired")
    call_id = str(checkpoint.get("call_id") or "")
    call: dict[str, Any] | None = None
    calls: list[dict[str, Any]] = []
    if not call_id:
        blockers.append("checkpoint call_id is missing")
    else:
        call, calls, error = calls_mod._resolve_call(target, call_id)
        if call is None:
            blockers.append(error or f"call not found: {call_id}")
    if call is not None:
        if checkpoint.get("contract_fingerprint") != call.get("contract_fingerprint"):
            blockers.append("checkpoint contract fingerprint is stale")
        if checkpoint.get("source_fingerprint") != call.get("source_fingerprint"):
            blockers.append("checkpoint source fingerprint is stale")
        if checkpoint.get("call_fingerprint") != call.get("call_fingerprint"):
            blockers.append("checkpoint call fingerprint is stale")
        blockers.extend(calls_mod._call_run_blockers(target, call, expected_status="resume-pending"))
    return blockers, call, calls


def _checkpoint_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    checkpoints: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for path in checkpoint_store._checkpoint_paths(target):
        checkpoint, error = checkpoint_store._read_checkpoint(path)
        if error is not None or checkpoint is None:
            errors.append({"checkpoint_path": str(path), "error": error or "invalid checkpoint"})
            continue
        summary = checkpoint_store._checkpoint_public_summary(checkpoint)
        checkpoints.append(summary)
        status = str(summary.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    checkpoints.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {
        "target": str(target),
        "checkpoints_path": str(paths.checkpoints_path(target)),
        "checkpoints": checkpoints,
        "checkpoint_count": len(checkpoints),
        "counts": counts,
        "errors": errors,
        "error_count": len(errors),
    }


def _checkpoint_health(target: Path) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    checkpoints: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    now = helpers._now()
    for path in checkpoint_store._checkpoint_paths(target):
        checkpoint, error = checkpoint_store._read_checkpoint(path)
        if error is not None or checkpoint is None:
            issues.append(
                {
                    "status": constants.WARN,
                    "name": "tool_checkpoint_invalid",
                    "issue_type": "checkpoint_invalid",
                    "detail": f"{path}: {error or 'invalid checkpoint'}",
                    "checkpoint_id": path.stem,
                }
            )
            continue
        checkpoints.append(checkpoint)
        status = str(checkpoint.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        checkpoint_id = str(checkpoint.get("id") or path.stem)
        base = {
            "tool_id": checkpoint.get("tool_id"),
            "checkpoint_id": checkpoint_id,
            "call_id": checkpoint.get("call_id"),
            "run_id": checkpoint.get("run_id"),
        }
        if checkpoint_store._checkpoint_expired(checkpoint, now=now) and status not in {"rejected", "resumed"}:
            issues.append(
                {
                    **base,
                    "status": constants.WARN,
                    "name": "tool_checkpoint_expired",
                    "issue_type": "checkpoint_expired",
                    "detail": f"{checkpoint_id} expired at {checkpoint.get('expires_at')}",
                }
            )
        created = helpers._parse_iso_datetime(checkpoint.get("created_at"))
        if status in {"pending", "approved"} and created is not None:
            age_hours = (now - created).total_seconds() / 3600
            if age_hours > constants.CALL_STALE_HOURS:
                issues.append(
                    {
                        **base,
                        "status": constants.WARN,
                        "name": "tool_checkpoint_stale",
                        "issue_type": "checkpoint_stale",
                        "detail": f"{checkpoint_id} {status} for {age_hours:.1f}h",
                    }
                )
        if status == "approved":
            blockers, _, _ = _checkpoint_resume_blockers(target, checkpoint)
            if blockers:
                issues.append(
                    {
                        **base,
                        "status": constants.WARN,
                        "name": "tool_checkpoint_blocked",
                        "issue_type": "checkpoint_blocked",
                        "detail": f"{checkpoint_id} resume blocked: {helpers._short('; '.join(blockers))}",
                    }
                )
        if status == "rejected":
            issues.append(
                {
                    **base,
                    "status": constants.WARN,
                    "name": "tool_checkpoint_rejected",
                    "issue_type": "checkpoint_rejected",
                    "detail": f"{checkpoint_id} rejected: {checkpoint.get('review_reason') or ''}".strip(),
                }
            )
        if status == "failed":
            issues.append(
                {
                    **base,
                    "status": constants.WARN,
                    "name": "tool_checkpoint_failed",
                    "issue_type": "checkpoint_failed",
                    "detail": f"{checkpoint_id} resume failed",
                }
            )
    checkpoints.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {
        "checkpoints_path": str(paths.checkpoints_path(target)),
        "checkpoint_count": len(checkpoints),
        "counts": counts,
        "issue_count": len(issues),
        "issues": issues,
        "top_issue": issues[0] if issues else None,
        "latest": checkpoint_store._checkpoint_public_summary(checkpoints[0]) if checkpoints else None,
    }


def _resume_checkpoint_payload(target: Path, checkpoint_id: str) -> tuple[dict[str, Any], int]:
    target = target.expanduser().resolve()
    checkpoint, error = checkpoint_store._resolve_checkpoint(target, checkpoint_id)
    if checkpoint is None:
        return {"target": str(target), "checkpoints_path": str(paths.checkpoints_path(target)), "error": error}, 1
    blockers, call, calls = _checkpoint_resume_blockers(target, checkpoint)
    if blockers or call is None:
        return {
            "target": str(target),
            "checkpoints_path": str(paths.checkpoints_path(target)),
            "checkpoint": checkpoint_store._checkpoint_public_summary(checkpoint),
            "blockers": blockers,
            "error": "checkpoint is not resumable",
        }, 1
    contract = call.get("contract") if isinstance(call.get("contract"), dict) else {}
    cwd_value = contract.get("cwd")
    cwd = helpers._as_path(target, cwd_value) if cwd_value else target
    assert cwd is not None
    argv = safety._command_parts(call.get("command"))
    for key in sorted((call.get("arguments") if isinstance(call.get("arguments"), dict) else {}).keys()):
        value = call["arguments"][key]
        if value is None:
            continue
        argv.extend(shlex.split(str(value)))
    started_at = helpers._now().isoformat()
    run_id = calls_mod._run_id_for_call({**call, "id": f"{call.get('id')}:resume:{checkpoint.get('id')}"}, started_at)
    receipt_path = paths.runs_path(target) / f"{run_id}.json"
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
    run_env["BRIGADE_TOOL_RESUME_CHECKPOINT_ID"] = str(checkpoint.get("id") or "")
    run_env["BRIGADE_TOOL_RESUME_CHOICE"] = str(checkpoint.get("selected_choice") or "")
    start_monotonic = time.monotonic()
    stdout: object = ""
    stderr: object = ""
    exit_code: int | None = None
    timed_out = False
    status = "resumed"
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
    completed_at = helpers._now().isoformat()
    receipt = runs_mod._write_run_receipt(
        target,
        call=call,
        run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=time.monotonic() - start_monotonic,
        status=status,
        exit_code=exit_code,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
        argv=argv,
        cwd=cwd,
        policy_decision=policy_decision,
        extra={
            "checkpoint_id": checkpoint.get("id"),
            "original_call_id": checkpoint.get("call_id"),
            "original_run_id": checkpoint.get("run_id"),
            "resume_run_id": run_id,
            "resume": {
                "checkpoint_id": checkpoint.get("id"),
                "selected_choice": checkpoint.get("selected_choice"),
                "reviewed_at": checkpoint.get("reviewed_at"),
                "review_reason": checkpoint.get("review_reason"),
            },
        },
    )
    call["status"] = status
    call["completed_at"] = completed_at
    call["exit_code"] = exit_code
    call["timed_out"] = timed_out
    call["receipt_path"] = receipt["receipt_path"]
    call["resume_checkpoint_id"] = checkpoint.get("id")
    calls_mod._write_calls(target, calls)
    checkpoint["status"] = "resumed" if status == "resumed" else "failed"
    checkpoint["resume_run_id"] = run_id
    checkpoint_store._write_checkpoint(target, checkpoint)
    return {
        "target": str(target),
        "calls_path": str(paths.calls_path(target)),
        "checkpoints_path": str(paths.checkpoints_path(target)),
        "runs_path": str(paths.runs_path(target)),
        "checkpoint": checkpoint_store._checkpoint_public_summary(checkpoint),
        "call": call,
        "receipt": receipt,
    }, 0 if status == "resumed" else 1


def checkpoint_list(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _checkpoint_payload(target)
    text_lines = [
        f"tools checkpoint list: {target}",
        f"checkpoints_path: {payload['checkpoints_path']}",
        f"checkpoints: {payload['checkpoint_count']}",
    ]
    for status, count in sorted(payload["counts"].items()):
        text_lines.append(f"{status}: {count}")
    for error in payload["errors"]:
        text_lines.append(f"[warn] checkpoint_invalid: {error.get('checkpoint_path')} {error.get('error')}")
    for checkpoint in payload["checkpoints"]:
        text_lines.append(
            f"- {checkpoint.get('id')} [{checkpoint.get('status')}] {checkpoint.get('tool_id')} {checkpoint.get('requested_action')}"
        )
    return emit(payload, json_output, text_lines, 0)


def checkpoint_show(*, target: Path, checkpoint_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    checkpoint, error = checkpoint_store._resolve_checkpoint(target, checkpoint_id)
    payload = {
        "target": str(target),
        "checkpoints_path": str(paths.checkpoints_path(target)),
        "checkpoint": checkpoint_store._checkpoint_public_summary(checkpoint) if checkpoint is not None else None,
        "error": error,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if checkpoint is not None else 1
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    assert checkpoint is not None
    summary = checkpoint_store._checkpoint_public_summary(checkpoint)
    print(f"checkpoint: {summary.get('id')}")
    print(f"status: {summary.get('status')}")
    print(f"tool_id: {summary.get('tool_id')}")
    print(f"call_id: {summary.get('call_id')}")
    print(f"run_id: {summary.get('run_id')}")
    print(f"reason: {summary.get('reason')}")
    print(f"requested_action: {summary.get('requested_action')}")
    print(f"prompt: {summary.get('prompt')}")
    print(f"choices: {', '.join(str(choice) for choice in summary.get('choices', []))}")
    if summary.get("selected_choice"):
        print(f"selected_choice: {summary.get('selected_choice')}")
    return 0


def _checkpoint_review(
    *,
    target: Path,
    checkpoint_id: str,
    status: str,
    choice: str | None = None,
    reason: str | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    checkpoint, error = checkpoint_store._resolve_checkpoint(target, checkpoint_id)
    if checkpoint is None:
        payload = {"target": str(target), "error": error}
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"error: {error}", file=sys.stderr)
        return 1
    if status == "approved":
        choices = [str(item) for item in checkpoint.get("choices", []) if isinstance(item, str)]
        if choices and choice not in choices:
            payload = {
                "target": str(target),
                "error": "choice is not allowed",
                "checkpoint": checkpoint_store._checkpoint_public_summary(checkpoint),
            }
            if json_output:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print("error: choice is not allowed", file=sys.stderr)
            return 1
    checkpoint["status"] = status
    checkpoint["reviewed_at"] = helpers._now().isoformat()
    checkpoint["review_reason"] = reason
    if status == "approved":
        checkpoint["selected_choice"] = choice
    checkpoint_store._write_checkpoint(target, checkpoint)
    call: dict[str, Any] | None = None
    calls: list[dict[str, Any]] = []
    call_id = checkpoint.get("call_id")
    if isinstance(call_id, str) and call_id:
        call, calls, _ = calls_mod._resolve_call(target, call_id)
    if call is not None and status == "approved":
        call["status"] = "resume-pending"
        call["checkpoint_id"] = checkpoint.get("id")
        call["approval_fingerprint"] = calls_mod._approval_fingerprint(call)
        calls_mod._write_calls(target, calls)
    payload = {
        "target": str(target),
        "checkpoints_path": str(paths.checkpoints_path(target)),
        "checkpoint": checkpoint_store._checkpoint_public_summary(checkpoint),
        "call": call,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"checkpoint: {checkpoint.get('id')}")
    print(f"status: {checkpoint.get('status')}")
    if choice:
        print(f"selected_choice: {choice}")
    if reason:
        print(f"review_reason: {reason}")
    return 0


def checkpoint_approve(*, target: Path, checkpoint_id: str, choice: str, json_output: bool = False) -> int:
    return _checkpoint_review(
        target=target,
        checkpoint_id=checkpoint_id,
        status="approved",
        choice=choice,
        json_output=json_output,
    )


def checkpoint_reject(*, target: Path, checkpoint_id: str, reason: str, json_output: bool = False) -> int:
    return _checkpoint_review(
        target=target,
        checkpoint_id=checkpoint_id,
        status="rejected",
        reason=reason,
        json_output=json_output,
    )


def checkpoint_resume(*, target: Path, checkpoint_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, rc = _resume_checkpoint_payload(target, checkpoint_id)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    if payload.get("error"):
        print(f"error: {payload['error']}", file=sys.stderr)
        for blocker in payload.get("blockers", []):
            print(f"- {blocker}", file=sys.stderr)
        return rc
    checkpoint = payload["checkpoint"]
    receipt = payload["receipt"]
    print(f"tools checkpoint resume: {checkpoint.get('id')}")
    print(f"status: {checkpoint.get('status')}")
    print(f"resume_run_id: {receipt.get('id')}")
    print(f"exit_code: {receipt.get('exit_code')}")
    print(f"receipt_path: {receipt.get('receipt_path')}")
    return rc
