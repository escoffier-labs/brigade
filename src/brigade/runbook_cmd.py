"""Explicit runbook execution with local receipts."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any
from .localio import utc_now_iso as _now, write_json as _write_json

DANGEROUS_PATTERNS = (
    re.compile(r"\brm\s+-[^;\n]*[rf][^;\n]*[rf]"),
    re.compile(r"\bgit\s+reset\s+--hard\b"),
    re.compile(r"\bgit\s+checkout\s+--\b"),
    re.compile(r"\bmkfs(?:\.\w+)?\b"),
    re.compile(r"\bshutdown\b"),
    re.compile(r"\breboot\b"),
    re.compile(r":\s*\(\s*\)\s*\{"),
)


def _runbooks_root(target: Path) -> Path:
    return target / ".brigade" / "runbooks"


def _runs_root(target: Path) -> Path:
    return _runbooks_root(target) / "runs"


def _unique_run_dir(target: Path, run_id_base: str) -> tuple[str, Path]:
    root = _runs_root(target)
    root.mkdir(parents=True, exist_ok=True)
    for index in range(0, 100):
        run_id = run_id_base if index == 0 else f"{run_id_base}-{index + 1}"
        run_dir = root / run_id
        try:
            run_dir.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return run_id, run_dir
    raise FileExistsError(f"could not allocate unique runbook run id for {run_id_base}")


def _read_runbook(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"invalid runbook JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "runbook must be a JSON object"
    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        return None, "runbook.steps must be a non-empty list"
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or not isinstance(step.get("run"), str) or not step["run"].strip():
            return None, f"runbook step {index} must contain a non-empty run string"
    return payload, None


def _command_name(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        return ""
    return Path(parts[0]).name if parts else ""


def _policy_for_step(payload: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    command = str(step.get("run") or "")
    allowed = payload.get("allowed_commands")
    if allowed is None:
        allowed = step.get("allowed_commands")
    allowed_list = [str(item) for item in allowed] if isinstance(allowed, list) else []
    command_name = _command_name(command)
    failures: list[str] = []
    warnings: list[str] = []
    if any(pattern.search(command) for pattern in DANGEROUS_PATTERNS):
        failures.append("command matches a destructive default-deny pattern")
    if allowed_list and command_name not in allowed_list:
        failures.append(f"command {command_name!r} is not in allowed_commands")
    if not command_name:
        failures.append("command could not be parsed")
    if not allowed_list:
        warnings.append("allowed_commands not configured; default destructive deny-list only")
    return {
        "command": command,
        "command_name": command_name,
        "allowed_commands": allowed_list,
        "failures": failures,
        "warnings": warnings,
    }


def _plan_payload(target: Path, runbook: Path) -> tuple[dict[str, Any] | None, str | None]:
    payload, error = _read_runbook(runbook)
    if payload is None:
        return None, error
    steps = [
        {
            "index": index,
            "id": str(step.get("id") or f"step-{index}"),
            "run": step["run"],
            "cwd": str(step.get("cwd") or target),
            "timeout_seconds": int(step.get("timeout_seconds") or payload.get("timeout_seconds") or 600),
            "policy": _policy_for_step(payload, step),
        }
        for index, step in enumerate(payload["steps"], start=1)
    ]
    policy_failures = [
        {"step": step["id"], "failures": step["policy"]["failures"]} for step in steps if step["policy"]["failures"]
    ]
    return {
        "target": str(target),
        "runbook_path": str(runbook),
        "runbook_id": str(payload.get("id") or runbook.stem),
        "description": str(payload.get("description") or ""),
        "approved": bool(payload.get("approved")),
        "policy_valid": not policy_failures,
        "policy_failures": policy_failures,
        "step_count": len(steps),
        "steps": steps,
        "boundaries": [
            "Runs only when the operator calls brigade runbook run.",
            "Executes foreground shell commands from the reviewed runbook file.",
            "Writes stdout, stderr, and JSON receipts under .brigade/runbooks/runs/.",
        ],
    }, None


def plan(*, target: Path, runbook: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    runbook = runbook.expanduser().resolve()
    payload, error = _plan_payload(target, runbook)
    if payload is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"runbook plan: {payload['runbook_id']}")
    for step in payload["steps"]:
        print(f"- {step['id']}: {step['run']}")
    return 0


def _execute_plan(
    *,
    target: Path,
    runbook: Path,
    plan_payload: dict[str, Any],
    approved: bool,
    dry_run: bool,
    start_index: int = 1,
    source_run_id: str | None = None,
    json_output: bool = False,
) -> int:
    if not plan_payload["policy_valid"]:
        if json_output:
            print(
                json.dumps(
                    {"target": str(target), "status": "blocked", "policy_failures": plan_payload["policy_failures"]},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print("error: runbook policy failed", file=sys.stderr)
        return 2
    if not (approved or bool(plan_payload.get("approved"))):
        if json_output:
            print(
                json.dumps(
                    {"target": str(target), "status": "approval-required", "runbook_id": plan_payload["runbook_id"]},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print("error: runbook execution requires --approved or approved=true in the runbook", file=sys.stderr)
        return 1
    steps = [step for step in plan_payload["steps"] if int(step["index"]) >= start_index]
    if dry_run:
        payload = {**plan_payload, "dry_run": True, "steps": steps}
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"runbook dry-run: {plan_payload['runbook_id']}")
            for step in steps:
                print(f"- {step['id']}: {step['run']}")
        return 0
    target = target.expanduser().resolve()
    started = _now()
    run_id, run_dir = _unique_run_dir(
        target, f"{started[:19].replace(':', '').replace('-', '')}-{plan_payload['runbook_id']}"
    )
    results: list[dict[str, Any]] = []
    status = "completed"
    for step in steps:
        stdout_path = run_dir / f"{step['index']:02d}-{step['id']}.stdout.log"
        stderr_path = run_dir / f"{step['index']:02d}-{step['id']}.stderr.log"
        try:
            completed = subprocess.run(
                step["run"],
                cwd=step["cwd"],
                shell=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=step["timeout_seconds"],
                check=False,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            exit_code = completed.returncode
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            exit_code = 124
            timed_out = True
        stdout_path.write_text(stdout)
        stderr_path.write_text(stderr)
        row = {
            **step,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "status": "completed" if exit_code == 0 else "failed",
            "stdout_log_path": str(stdout_path),
            "stderr_log_path": str(stderr_path),
        }
        results.append(row)
        if exit_code != 0:
            status = "failed"
            break
    receipt = {
        "run_id": run_id,
        "runbook_id": plan_payload["runbook_id"],
        "target": str(target),
        "runbook_path": str(runbook),
        "started_at": started,
        "completed_at": _now(),
        "status": status,
        "approved": True,
        "source_run_id": source_run_id,
        "start_index": start_index,
        "steps": results,
        "receipt_path": str(run_dir / "receipt.json"),
    }
    _write_json(run_dir / "receipt.json", receipt)
    if json_output:
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0 if status == "completed" else 1
    print(f"runbook_run: {run_id}")
    print(f"status: {status}")
    print(f"receipt: {receipt['receipt_path']}")
    return 0 if status == "completed" else 1


def run(
    *, target: Path, runbook: Path, approved: bool = False, dry_run: bool = False, json_output: bool = False
) -> int:
    target = target.expanduser().resolve()
    runbook = runbook.expanduser().resolve()
    plan_payload, error = _plan_payload(target, runbook)
    if plan_payload is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return _execute_plan(
        target=target,
        runbook=runbook,
        plan_payload=plan_payload,
        approved=approved,
        dry_run=dry_run,
        json_output=json_output,
    )


def _run_receipts(target: Path) -> list[dict[str, Any]]:
    root = _runs_root(target)
    receipts: list[dict[str, Any]] = []
    if not root.is_dir():
        return receipts
    for path in sorted(root.glob("*/receipt.json")):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            receipts.append(payload)
    return sorted(receipts, key=lambda item: str(item.get("started_at") or ""), reverse=True)


def _resolve_receipt(target: Path, run_id: str) -> tuple[dict[str, Any] | None, str | None]:
    receipts = _run_receipts(target)
    if run_id == "latest":
        return (receipts[0], None) if receipts else (None, "no runbook runs found")
    matches = [receipt for receipt in receipts if str(receipt.get("run_id") or "").startswith(run_id)]
    if not matches:
        return None, f"runbook run not found: {run_id}"
    if len(matches) > 1:
        return None, f"runbook run id is ambiguous: {run_id}"
    return matches[0], None


def resume(*, target: Path, run_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    receipt, error = _resolve_receipt(target, run_id)
    if receipt is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    failed = [step for step in receipt.get("steps", []) if isinstance(step, dict) and step.get("status") != "completed"]
    payload = {
        "target": str(target),
        "run": receipt,
        "next": failed[0] if failed else None,
        "suggested_command": f"brigade runbook run {receipt.get('runbook_path')} --target {target} --resume {receipt.get('run_id')} --approved",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"runbook_resume: {receipt.get('run_id')}")
    print(f"status: {receipt.get('status')}")
    print(f"next: {payload['next'].get('id') if isinstance(payload['next'], dict) else 'none'}")
    print(f"suggested_command: {payload['suggested_command']}")
    return 0


def retry(
    *, target: Path, run_id: str = "latest", approved: bool = False, dry_run: bool = False, json_output: bool = False
) -> int:
    target = target.expanduser().resolve()
    receipt, error = _resolve_receipt(target, run_id)
    if receipt is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    failed = [step for step in receipt.get("steps", []) if isinstance(step, dict) and step.get("status") != "completed"]
    if not failed:
        if json_output:
            print(
                json.dumps(
                    {"target": str(target), "status": "no-failed-step", "run_id": receipt.get("run_id")},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(f"runbook_retry: {receipt.get('run_id')}")
            print("status: no-failed-step")
        return 0
    runbook = Path(str(receipt.get("runbook_path") or "")).expanduser().resolve()
    plan_payload, plan_error = _plan_payload(target, runbook)
    if plan_payload is None:
        print(f"error: {plan_error}", file=sys.stderr)
        return 2
    return _execute_plan(
        target=target,
        runbook=runbook,
        plan_payload=plan_payload,
        approved=approved,
        dry_run=dry_run,
        start_index=int(failed[0].get("index") or 1),
        source_run_id=str(receipt.get("run_id") or ""),
        json_output=json_output,
    )


def closeout(
    *,
    target: Path,
    run_id: str = "latest",
    status: str = "reviewed",
    reason: str | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    receipt, error = _resolve_receipt(target, run_id)
    if receipt is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    closeout_payload = {
        "run_id": receipt.get("run_id"),
        "runbook_id": receipt.get("runbook_id"),
        "status": status,
        "reason": reason or "",
        "created_at": _now(),
    }
    closeout_path = _runs_root(target) / str(receipt.get("run_id")) / "closeout.json"
    _write_json(closeout_path, closeout_payload)
    closeout_payload["closeout_path"] = str(closeout_path)
    if json_output:
        print(json.dumps(closeout_payload, indent=2, sort_keys=True))
        return 0
    print(f"runbook_closeout: {receipt.get('run_id')}")
    print(f"status: {status}")
    print(f"closeout: {closeout_path}")
    return 0
