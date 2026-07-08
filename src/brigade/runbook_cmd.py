"""Explicit runbook execution with local receipts."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import receipt_signing
from .localio import (
    canonical_json_digest as _canonical_json_digest,
    file_sha256 as _file_sha256,
    stable_hash as _stable_hash,
    utc_now_iso as _now,
    write_json as _write_json,
)

# ADVISORY ONLY. This deny-list catches a few obviously destructive shapes, but
# it is trivially bypassable by destructive filesystem commands or remote-shell wrappers and
# must never be treated as a security boundary. The real boundary is the human
# operator reading the steps before passing --approved. See SECURITY.md.
DANGEROUS_PATTERNS = (
    re.compile(r"\brm\s+-[^;\n]*[rf][^;\n]*[rf]"),
    re.compile(r"\bgit\s+reset\s+--hard\b"),
    re.compile(r"\bgit\s+checkout\s+--\b"),
    re.compile(r"\bmkfs(?:\.\w+)?\b"),
    re.compile(r"\bshutdown\b"),
    re.compile(r"\breboot\b"),
    re.compile(r":\s*\(\s*\)\s*\{"),
)

# Interpreters that, with an inline-script flag, run an arbitrary embedded
# command and so defeat a first-token (or even whole-command) allowlist.
_SHELL_INTERPRETERS = frozenset({"bash", "sh", "dash", "zsh", "ksh", "fish"})
_INLINE_SCRIPT_FLAGS = frozenset({"-c"})


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


def _read_runbook(path: Path, *, require_pin_hashes: bool = True) -> tuple[dict[str, Any] | None, str | None]:
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
    pins = payload.get("pins")
    if pins is not None:
        if not isinstance(pins, list):
            return None, "runbook.pins must be a list when configured"
        for index, pin in enumerate(pins, start=1):
            if not isinstance(pin, dict):
                return None, f"runbook pin {index} must be an object"
            if not isinstance(pin.get("command"), str) or not pin["command"].strip():
                return None, f"runbook pin {index} must contain a non-empty command"
            if require_pin_hashes and (not isinstance(pin.get("sha256"), str) or not pin["sha256"].strip()):
                return None, f"runbook pin {index} must contain a non-empty sha256"
            for field in ("path", "sha256", "version_cmd", "version"):
                if field in pin and pin[field] is not None and not isinstance(pin[field], str):
                    return None, f"runbook pin {index} field {field} must be a string"
    return payload, None


def _command_tokens(command: str) -> list[str] | None:
    try:
        return shlex.split(command)
    except ValueError:
        return None


def _command_name(command: str) -> str:
    parts = _command_tokens(command)
    return Path(parts[0]).name if parts else ""


def _pin_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    pins = payload.get("pins")
    if not isinstance(pins, list):
        return []
    return [pin for pin in pins if isinstance(pin, dict)]


def _pin_matches_argv0(pin: dict[str, Any], argv0: str) -> bool:
    command = str(pin.get("command") or "").strip()
    return bool(command and (argv0 == command or Path(argv0).name == command))


def _pin_for_step(payload: dict[str, Any], step: dict[str, Any]) -> dict[str, Any] | None:
    tokens = _command_tokens(str(step.get("run") or ""))
    if not tokens:
        return None
    for pin in _pin_entries(payload):
        if _pin_matches_argv0(pin, tokens[0]):
            return pin
    return None


def _sha256_file(path: Path) -> str:
    return _file_sha256(path)


def _run_version_cmd(resolved_path: str, version_cmd: str) -> dict[str, Any]:
    # version_cmd is arguments for the resolved pinned binary, never a
    # standalone command: the version in the receipt must describe the same
    # file the hash check verified, not whatever PATH resolves to.
    try:
        argv = [resolved_path, *shlex.split(version_cmd)]
    except ValueError as exc:
        return {"output": f"invalid version_cmd: {exc}", "exit_code": None, "timed_out": False}
    try:
        completed = subprocess.run(
            argv,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
        return {"output": output, "exit_code": completed.returncode, "timed_out": False}
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        output = "\n".join(part.strip() for part in (stdout, stderr) if part.strip())
        return {"output": output, "exit_code": 124, "timed_out": True}
    except OSError as exc:
        return {"output": str(exc), "exit_code": None, "timed_out": False}


def _resolve_pin_path(pin: dict[str, Any], argv0: str | None = None) -> str | None:
    pinned_path = str(pin.get("path") or "").strip()
    if pinned_path:
        path = Path(pinned_path).expanduser()
        if not path.is_absolute():
            return None
        return str(path)
    command = str(pin.get("command") or "").strip()
    if argv0:
        resolved = shutil.which(argv0)
        if resolved:
            return resolved
    return shutil.which(command) if command else None


def _pin_check(pin: dict[str, Any], *, argv0: str | None = None, include_version: bool = False) -> dict[str, Any]:
    resolved = _resolve_pin_path(pin, argv0)
    expected = str(pin.get("sha256") or "").strip()
    observed = None
    status = "missing"
    if resolved is not None:
        try:
            observed = _sha256_file(Path(resolved))
        except OSError:
            observed = None
            status = "missing"
        else:
            status = "ok" if observed == expected else "mismatch"
    check: dict[str, Any] = {
        "command": str(pin.get("command") or ""),
        "resolved_path": resolved,
        "expected_sha256": expected,
        "observed_sha256": observed,
        "status": status,
    }
    if (
        include_version
        and resolved is not None
        and isinstance(pin.get("version_cmd"), str)
        and pin["version_cmd"].strip()
    ):
        check["version_cmd"] = pin["version_cmd"]
        check["expected_version"] = str(pin.get("version") or "")
        version = _run_version_cmd(resolved, pin["version_cmd"])
        check["version_output"] = version["output"]
        check["version_exit_code"] = version["exit_code"]
        check["version_timed_out"] = version["timed_out"]
    return check


def _step_cwd(target: Path, step: dict[str, Any]) -> Path:
    cwd = Path(str(step.get("cwd") or target)).expanduser()
    if not cwd.is_absolute():
        cwd = target / cwd
    return cwd.resolve()


def _resolve_step_argv0(argv0: str, cwd: Path) -> str | None:
    expanded = Path(argv0).expanduser()
    if expanded != Path(expanded.name):
        candidate = expanded if expanded.is_absolute() else cwd / expanded
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
        return None
    return shutil.which(argv0)


def _existing_pin_for_argv0(pins: list[dict[str, Any]], argv0: str) -> dict[str, Any] | None:
    for pin in pins:
        if _pin_matches_argv0(pin, argv0):
            return pin
    return None


def _pin_payload_from_runbook(target: Path, payload: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, str | None]:
    pins: list[dict[str, Any]] = []
    seen: set[str] = set()
    existing_pins = _pin_entries(payload)
    for index, step in enumerate(payload["steps"], start=1):
        tokens = _command_tokens(str(step.get("run") or ""))
        if not tokens:
            return None, f"runbook step {index} command could not be parsed"
        argv0 = tokens[0]
        command = Path(argv0).name
        if command in seen:
            continue
        resolved = _resolve_step_argv0(argv0, _step_cwd(target, step))
        if resolved is None:
            return None, f"runbook step {index} argv[0] could not be resolved: {argv0}"
        try:
            sha256 = _sha256_file(Path(resolved))
        except OSError as exc:
            return None, f"runbook step {index} argv[0] could not be hashed: {exc}"
        entry = {"command": command, "path": resolved, "sha256": sha256}
        existing = _existing_pin_for_argv0(existing_pins, argv0)
        if existing is not None and isinstance(existing.get("version_cmd"), str) and existing["version_cmd"].strip():
            entry["version_cmd"] = existing["version_cmd"]
            entry["version"] = str(_run_version_cmd(resolved, existing["version_cmd"])["output"])
        pins.append(entry)
        seen.add(command)
    return pins, None


def _is_shell_wrapper(tokens: list[str]) -> bool:
    """True when the command shells out to an inline script and so negates the allowlist."""
    if not tokens:
        return False
    if Path(tokens[0]).name not in _SHELL_INTERPRETERS:
        return False
    return any(token in _INLINE_SCRIPT_FLAGS for token in tokens[1:])


def _policy_for_step(payload: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    command = str(step.get("run") or "")
    allowed = payload.get("allowed_commands")
    if allowed is None:
        allowed = step.get("allowed_commands")
    allowed_list = [str(item) for item in allowed] if isinstance(allowed, list) else []
    tokens = _command_tokens(command)
    command_name = Path(tokens[0]).name if tokens else ""
    failures: list[str] = []
    warnings: list[str] = []
    if any(pattern.search(command) for pattern in DANGEROUS_PATTERNS):
        # Advisory deny-list. It is not a security boundary (see SECURITY.md);
        # it only catches a handful of obviously destructive shapes.
        failures.append("command matches an advisory destructive deny-list pattern")
    if not command_name:
        failures.append("command could not be parsed")
    shell_wrapper = tokens is not None and _is_shell_wrapper(tokens)
    if allowed_list:
        # Validate the WHOLE command, not just the first token. A first-token
        # match (e.g. allowed_commands:['bash'] for `bash -c "..."`) does not
        # constrain what actually runs, so an inline-script shell wrapper is
        # rejected outright even when its interpreter is allow-listed.
        if shell_wrapper:
            failures.append(
                f"command {command_name!r} runs an inline script (-c), which negates the allowed_commands allowlist"
            )
        elif command_name not in allowed_list:
            failures.append(f"command {command_name!r} is not in allowed_commands")
    else:
        warnings.append("allowed_commands not configured; advisory destructive deny-list only")
    if shell_wrapper:
        warnings.append("command runs an inline shell script (-c); the allowed_commands allowlist cannot constrain it")
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
    steps = []
    for index, step in enumerate(payload["steps"], start=1):
        plan_step = {
            "index": index,
            "id": str(step.get("id") or f"step-{index}"),
            "run": step["run"],
            "cwd": str(step.get("cwd") or target),
            "timeout_seconds": int(step.get("timeout_seconds") or payload.get("timeout_seconds") or 600),
            "policy": _policy_for_step(payload, step),
        }
        pin = _pin_for_step(payload, step)
        if pin is not None:
            tokens = _command_tokens(step["run"]) or []
            plan_step["pin"] = _pin_check(pin, argv0=tokens[0] if tokens else None)
        steps.append(plan_step)
    policy_failures = [
        {"step": step["id"], "failures": step["policy"]["failures"]} for step in steps if step["policy"]["failures"]
    ]
    plan = {
        "target": str(target),
        "runbook_path": str(runbook),
        "runbook_id": str(payload.get("id") or runbook.stem),
        "description": str(payload.get("description") or ""),
        # file_approved is informational only. It does NOT authorize execution;
        # the operator must pass --approved. A file-embedded flag is ignored.
        "file_approved": bool(payload.get("approved")),
        "policy_valid": not policy_failures,
        "policy_failures": policy_failures,
        "step_count": len(steps),
        "steps": steps,
        "boundaries": [
            "Runs only when the operator passes --approved on the command line; an approved=true baked into the runbook file is ignored.",
            "Executes arbitrary foreground shell commands from the runbook file, which is only as trustworthy as whoever wrote it. Review every step before approving.",
            "The destructive deny-list is advisory, not a security boundary; it is trivially bypassable.",
            "Writes stdout, stderr, and JSON receipts under .brigade/runbooks/runs/.",
        ],
    }
    pins = _pin_entries(payload)
    if pins:
        plan["pins"] = pins
    return plan, None


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


def pin(*, target: Path, runbook: Path, dry_run: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    runbook = runbook.expanduser().resolve()
    payload, error = _read_runbook(runbook, require_pin_hashes=False)
    if payload is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    pins, pin_error = _pin_payload_from_runbook(target, payload)
    if pins is None:
        print(f"error: {pin_error}", file=sys.stderr)
        return 2
    payload["pins"] = pins
    result = {
        "target": str(target),
        "runbook_path": str(runbook),
        "written": not dry_run,
        "dry_run": dry_run,
        "pins": pins,
    }
    if not dry_run:
        _write_json(runbook, payload)
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    verb = "would write" if dry_run else "wrote"
    print(f"runbook_pin: {verb} {runbook}")
    for entry in pins:
        print(f"- {entry['command']}: {entry['path']} sha256={entry['sha256']}")
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
    allow_pin_mismatch: bool = False,
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
    # Approval is a human-in-the-loop gate. It is satisfied ONLY by the operator
    # passing --approved (or the equivalent `approved` argument), never by an
    # "approved": true baked into the runbook file. Brigade is driven by agents
    # that write files, so honoring a file-embedded flag would let any file
    # author authorize arbitrary shell execution without an operator ever seeing
    # the commands. A file-embedded approved=true is intentionally ignored.
    if not approved:
        if json_output:
            print(
                json.dumps(
                    {"target": str(target), "status": "approval-required", "runbook_id": plan_payload["runbook_id"]},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(
                "error: runbook execution requires the operator to pass --approved; "
                "approved=true inside the runbook file is ignored",
                file=sys.stderr,
            )
        return 1
    pin_checks = [_pin_check(pin) for pin in _pin_entries(plan_payload)]
    failed_pin_checks = [check for check in pin_checks if check["status"] in {"missing", "mismatch"}]
    if failed_pin_checks and not allow_pin_mismatch:
        if json_output:
            print(
                json.dumps(
                    {"target": str(target), "status": "pin-check-failed", "pin_checks": pin_checks},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            failures = ", ".join(f"{check['command']}:{check['status']}" for check in failed_pin_checks)
            print(f"error: runbook pin check failed: {failures}", file=sys.stderr)
        return 2
    for check in pin_checks:
        check["override"] = check["status"] in {"missing", "mismatch"} and allow_pin_mismatch
    if failed_pin_checks and allow_pin_mismatch:
        failures = ", ".join(f"{check['command']}:{check['status']}" for check in failed_pin_checks)
        print(f"warning: pin mismatch override allowed; proceeding despite {failures}", file=sys.stderr)
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
    if pin_checks:
        # Re-check with version capture only when steps will actually execute;
        # dry-run must not run version_cmd.
        pin_checks = [_pin_check(pin, include_version=True) for pin in _pin_entries(plan_payload)]
        for check in pin_checks:
            check["override"] = check["status"] in {"missing", "mismatch"} and allow_pin_mismatch
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
    if pin_checks:
        receipt["pin_checks"] = pin_checks
    log_digests: dict[str, str] = {}
    for step in receipt["steps"]:
        if not isinstance(step, dict):
            continue
        for key in ("stdout_log_path", "stderr_log_path"):
            value = step.get(key)
            if not isinstance(value, str) or not value:
                continue
            path = Path(value)
            try:
                log_name = str(path.relative_to(run_dir))
            except ValueError:
                log_name = path.name
            log_digests[log_name] = _file_sha256(path)
    receipt["digests"] = {
        "algorithm": "sha256",
        "logs": dict(sorted(log_digests.items())),
        "receipt_sha256": _canonical_json_digest(receipt, exclude_keys={"digests"}),
    }
    signing_key = receipt_signing.load_key(target)
    if signing_key is not None:
        signing_key_bytes, signing_key_id = signing_key
        receipt["digests"]["signature"] = receipt_signing.sign(receipt["digests"]["receipt_sha256"], signing_key_bytes)
        receipt["digests"]["key_id"] = signing_key_id
    _write_json(run_dir / "receipt.json", receipt)
    if json_output:
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0 if status == "completed" else 1
    print(f"runbook_run: {run_id}")
    print(f"status: {status}")
    print(f"receipt: {receipt['receipt_path']}")
    return 0 if status == "completed" else 1


def run(
    *,
    target: Path,
    runbook: Path,
    approved: bool = False,
    dry_run: bool = False,
    json_output: bool = False,
    allow_pin_mismatch: bool = False,
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
        allow_pin_mismatch=allow_pin_mismatch,
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
    *,
    target: Path,
    run_id: str = "latest",
    approved: bool = False,
    dry_run: bool = False,
    json_output: bool = False,
    allow_pin_mismatch: bool = False,
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
        allow_pin_mismatch=allow_pin_mismatch,
    )


def _failed_step_import_records(receipt: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for step in receipt.get("steps", []):
        if not isinstance(step, dict) or step.get("status") == "completed":
            continue
        step_id = str(step.get("id") or step.get("index") or "step")
        records.append(
            {
                "text": f"Resolve failed runbook step '{step_id}' in run {run_id}",
                "kind": "task",
                "source": "runbook",
                "type": "docs",
                "priority": "normal",
                "template": "docs",
                "acceptance": [
                    "The failed runbook step is fixed or explicitly deferred.",
                    "No private contents or paths are copied into public artifacts.",
                ],
                "metadata": {
                    "run_id": run_id,
                    "step_id": step_id,
                    "safe_summary": f"runbook step {step_id} failed",
                    "source_item_key": f"{run_id}:{step_id}",
                    "source_fingerprint": _stable_hash({"run_id": run_id, "step_id": step_id}),
                },
            }
        )
    return records


def closeout(
    *,
    target: Path,
    run_id: str = "latest",
    status: str = "reviewed",
    reason: str | None = None,
    import_issues: bool = False,
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    receipt, error = _resolve_receipt(target, run_id)
    if receipt is None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    resolved_run_id = str(receipt.get("run_id"))
    closeout_payload = {
        "run_id": receipt.get("run_id"),
        "runbook_id": receipt.get("runbook_id"),
        "status": status,
        "reason": reason or "",
        "created_at": _now(),
    }
    closeout_path = _runs_root(target) / resolved_run_id / "closeout.json"
    _write_json(closeout_path, closeout_payload)
    closeout_payload["closeout_path"] = str(closeout_path)
    if import_issues:
        from .work_cmd import ledger as ledger_mod

        records = _failed_step_import_records(receipt, resolved_run_id)
        imported, skipped, skipped_dismissed = ledger_mod._append_import_records(target, records, dry_run=dry_run)
        closeout_payload["import_issues"] = {
            "failed_step_count": len(records),
            "created": len(imported),
            "skipped": len(skipped),
            "skipped_dismissed": len(skipped_dismissed),
            "dry_run": dry_run,
        }
    if json_output:
        print(json.dumps(closeout_payload, indent=2, sort_keys=True))
        return 0
    print(f"runbook_closeout: {resolved_run_id}")
    print(f"status: {status}")
    print(f"closeout: {closeout_path}")
    if import_issues:
        info = closeout_payload.get("import_issues")
        if isinstance(info, dict):
            print(f"import_issues: {info['created']} created from {info['failed_step_count']} failed step(s)")
    return 0
