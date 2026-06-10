"""Code review run and finding operations."""

from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4
from .. import dogfood_cmd
from ..install import apply_gitignore
from . import constants, helpers, ledger as ledger_mod, config as config_mod
from . import scanners as scanners_mod


def _review_read_receipt(path: Path) -> dict[str, Any] | None:
    receipt = path / "receipt.json" if path.is_dir() else path
    if not receipt.is_file():
        return None
    try:
        data = json.loads(receipt.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("path", str(receipt.parent))
    return data


def _review_receipts(target: Path) -> list[dict[str, Any]]:
    root = helpers._review_runs_root(target)
    if not root.is_dir():
        return []
    receipts = [_review_read_receipt(path) for path in root.iterdir() if path.is_dir()]
    valid = [item for item in receipts if isinstance(item, dict)]
    valid.sort(key=lambda item: str(item.get("started_at") or item.get("run_id") or ""), reverse=True)
    return valid


def _review_latest_success(target: Path, reviewer_id: str | None = None) -> dict[str, Any] | None:
    for receipt in _review_receipts(target):
        if reviewer_id and receipt.get("reviewer_id") != reviewer_id:
            continue
        if receipt.get("status") == "completed" and receipt.get("exit_code") == 0:
            return receipt
    return None


def _review_receipt_path(run: dict[str, Any]) -> str | None:
    value = run.get("path")
    if isinstance(value, str) and value:
        return str(Path(value) / "receipt.json")
    return None


def _review_redact(value: object) -> object:
    if isinstance(value, dict):
        redacted: dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.casefold() in constants.REVIEW_UNSAFE_FIELD_NAMES:
                redacted[key_text] = "[redacted]"
            else:
                redacted[key_text] = _review_redact(item)
        return redacted
    if isinstance(value, list):
        return [_review_redact(item) for item in value]
    if isinstance(value, str):
        return constants.REVIEW_UNSAFE_VALUE_RE.sub("[redacted]", value)
    return value


def _review_safe_text(value: object, *, limit: int = 600) -> str:
    if not isinstance(value, str):
        return ""
    return helpers._short(str(_review_redact(value)).strip(), limit)


def _review_finding_fingerprint(finding: dict[str, Any], *, reviewer_id: str) -> str:
    existing = finding.get("source_fingerprint")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    return helpers._stable_hash(
        {
            "reviewer_id": reviewer_id,
            "path": finding.get("path"),
            "line": finding.get("line"),
            "severity": finding.get("severity"),
            "category": finding.get("category"),
            "rationale": finding.get("rationale"),
            "suggested_fix": finding.get("suggested_fix"),
        }
    )


def _normalize_review_finding(
    value: object, *, reviewer_id: str, run_id: str, run: dict[str, Any], label: str
) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(value, dict):
        return None, [f"{label}: expected JSON object"]
    errors: list[str] = []
    path_value = value.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        errors.append(f"{label}: path must be a non-empty string")
    severity = str(value.get("severity") or "medium").strip().lower()
    if severity not in constants.REVIEW_SEVERITIES:
        errors.append(f"{label}: severity must be one of: {', '.join(constants.REVIEW_SEVERITIES)}")
    category = str(value.get("category") or "maintainability").strip().lower()
    if category not in constants.REVIEW_CATEGORIES:
        errors.append(f"{label}: category must be one of: {', '.join(constants.REVIEW_CATEGORIES)}")
    line = value.get("line")
    if line is not None and (not isinstance(line, int) or isinstance(line, bool) or line < 1):
        errors.append(f"{label}: line must be a positive integer when present")
    confidence = str(value.get("confidence") or "medium").strip().lower()
    if confidence not in {"low", "medium", "high"}:
        errors.append(f"{label}: confidence must be low, medium, or high")
    rationale = _review_safe_text(value.get("rationale") or value.get("summary") or value.get("text"), limit=800)
    suggested_fix = _review_safe_text(value.get("suggested_fix") or value.get("fix"), limit=800)
    safe_excerpt = _review_safe_text(value.get("safe_excerpt") or value.get("excerpt"), limit=400)
    if not rationale:
        errors.append(f"{label}: rationale must be a non-empty string")
    if errors:
        return None, errors
    normalized: dict[str, Any] = {
        "reviewer_id": reviewer_id,
        "run_id": run_id,
        "severity": severity,
        "category": category,
        "path": str(path_value).strip(),
        "line": line,
        "safe_excerpt": safe_excerpt,
        "rationale": rationale,
        "suggested_fix": suggested_fix,
        "confidence": confidence,
    }
    finding_id = value.get("finding_id") or value.get("id")
    if isinstance(finding_id, str) and finding_id.strip():
        normalized["finding_id"] = finding_id.strip()
    else:
        normalized["finding_id"] = helpers._stable_hash(normalized)[:12]
    source_fingerprint = value.get("source_fingerprint")
    if isinstance(source_fingerprint, str) and source_fingerprint.strip():
        normalized["source_fingerprint"] = source_fingerprint.strip()
    else:
        normalized["source_fingerprint"] = _review_finding_fingerprint(normalized, reviewer_id=reviewer_id)
    normalized["receipt_path"] = _review_receipt_path(run)
    if run.get("findings_path"):
        normalized["findings_path"] = run.get("findings_path")
    return normalized, []


def _load_review_findings(
    path: Path, *, reviewer_id: str, run_id: str, run: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        payload = json.loads(path.read_text())
    except OSError as exc:
        return [], [f"{path}: {exc}"]
    except json.JSONDecodeError as exc:
        return [], [f"{path}: invalid JSON: {exc.msg}"]
    if isinstance(payload, list):
        raw_findings = payload
    elif isinstance(payload, dict):
        raw_findings = payload.get("findings", [])
    else:
        return [], [f"{path}: expected JSON object or list"]
    if not isinstance(raw_findings, list):
        return [], [f"{path}: findings must be a list"]
    findings: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, item in enumerate(raw_findings, start=1):
        finding, item_errors = _normalize_review_finding(
            _review_redact(item),
            reviewer_id=reviewer_id,
            run_id=run_id,
            run=run,
            label=f"finding {index}",
        )
        errors.extend(item_errors)
        if finding is not None:
            findings.append(finding)
    return findings, errors


def _review_import_record(finding: dict[str, Any]) -> dict[str, Any]:
    location = str(finding.get("path") or "")
    if finding.get("line"):
        location = f"{location}:{finding.get('line')}"
    text = (
        f"Review finding {finding.get('severity')} {finding.get('category')} in {location}: {finding.get('rationale')}"
    )
    metadata = {
        "reviewer_id": finding.get("reviewer_id"),
        "review_run_id": finding.get("run_id"),
        "review_finding_id": finding.get("finding_id"),
        "severity": finding.get("severity"),
        "category": finding.get("category"),
        "path": finding.get("path"),
        "line": finding.get("line"),
        "safe_excerpt": finding.get("safe_excerpt"),
        "rationale": finding.get("rationale"),
        "suggested_fix": finding.get("suggested_fix"),
        "confidence": finding.get("confidence"),
        "receipt_path": finding.get("receipt_path"),
        "findings_path": finding.get("findings_path"),
        "source_item_key": f"code-review:{finding.get('reviewer_id')}:{finding.get('finding_id')}",
        "source_fingerprint": finding.get("source_fingerprint"),
    }
    return {
        "text": text,
        "kind": "task" if finding.get("severity") in {"high", "critical"} else "finding",
        "source": "code-review",
        "type": "bug" if finding.get("category") == "bug" else "workflow",
        "priority": "high" if finding.get("severity") in {"high", "critical"} else "normal",
        "template": "bugfix",
        "acceptance": [
            f"The code review finding {finding.get('finding_id')} is resolved or dismissed with rationale.",
            f"`brigade work review import-findings {finding.get('run_id')}` does not create a duplicate unresolved finding.",
        ],
        "metadata": metadata,
    }


def _review_stamp_completed_tasks(target: Path, run_id: str) -> list[str]:
    ledger = ledger_mod._read_task_ledger(target)
    stamped: list[str] = []
    changed = False
    for task in ledger.get("tasks", []):
        if not isinstance(task, dict) or task.get("status") != "done":
            continue
        completion = task.setdefault("completion", {})
        if not isinstance(completion, dict):
            completion = {}
            task["completion"] = completion
        review_run_ids = completion.get("review_run_ids")
        if not isinstance(review_run_ids, list):
            review_run_ids = []
            completion["review_run_ids"] = review_run_ids
        if run_id not in review_run_ids:
            review_run_ids.append(run_id)
            stamped.append(str(task.get("id")))
            changed = True
    if changed:
        ledger_mod._write_task_ledger(target, ledger)
    return stamped


def _review_run_one(target: Path, reviewer: dict[str, Any]) -> dict[str, Any]:
    reviewer_id = str(reviewer.get("id") or "reviewer")
    command = str(reviewer.get("command") or "")
    argv, blocker = config_mod._review_argv(command)
    output_path = config_mod._review_output_path(target, reviewer)
    findings_path = config_mod._review_findings_path(target, reviewer)
    cwd = config_mod._review_cwd(target, reviewer)
    started = helpers._now()
    run_id = f"{started.strftime('%Y%m%d-%H%M%S')}-{helpers._slug(reviewer_id)}-{uuid4().hex[:6]}"
    run_dir = helpers._review_runs_root(target) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    receipt_path = run_dir / "receipt.json"
    receipt: dict[str, Any] = {
        "run_id": run_id,
        "reviewer_id": reviewer_id,
        "name": reviewer.get("name"),
        "status": "running",
        "path": str(run_dir),
        "target": str(target),
        "cwd": str(cwd),
        "command_label": command,
        "argv": argv or [],
        "started_at": started.isoformat(),
        "timeout": reviewer.get("timeout"),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "output_path": str(output_path) if output_path is not None else None,
        "output_before": scanners_mod._scanner_output_snapshot(output_path),
        "findings_path": str(findings_path) if findings_path is not None else None,
        "findings_before": scanners_mod._scanner_output_snapshot(findings_path),
        "target_paths": reviewer.get("target_paths") or [],
        "base_ref": reviewer.get("base_ref"),
        "supported_modes": reviewer.get("supported_modes") or [],
        "privacy_mode": reviewer.get("privacy_mode"),
    }
    helpers._write_json(receipt_path, receipt)
    if blocker is not None:
        completed = helpers._now()
        receipt.update(
            {
                "status": "failed",
                "completed_at": completed.isoformat(),
                "duration_seconds": (completed - started).total_seconds(),
                "exit_code": None,
                "timed_out": False,
                "error": blocker,
                "stdout_summary": "",
                "stderr_summary": blocker,
                "output_after": scanners_mod._scanner_output_snapshot(output_path),
                "findings_after": scanners_mod._scanner_output_snapshot(findings_path),
            }
        )
        helpers._write_json(receipt_path, receipt)
        return receipt
    if not cwd.is_dir():
        completed = helpers._now()
        receipt.update(
            {
                "status": "failed",
                "completed_at": completed.isoformat(),
                "duration_seconds": (completed - started).total_seconds(),
                "exit_code": None,
                "timed_out": False,
                "error": f"review cwd not found: {cwd}",
                "stdout_summary": "",
                "stderr_summary": f"review cwd not found: {cwd}",
                "output_after": scanners_mod._scanner_output_snapshot(output_path),
                "findings_after": scanners_mod._scanner_output_snapshot(findings_path),
            }
        )
        helpers._write_json(receipt_path, receipt)
        return receipt
    try:
        completed_process = subprocess.run(
            argv,
            cwd=cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=float(reviewer.get("timeout", 600)),
        )
        stdout = completed_process.stdout or ""
        stderr = completed_process.stderr or ""
        stdout_path.write_text(stdout)
        stderr_path.write_text(stderr)
        completed = helpers._now()
        status = "completed" if completed_process.returncode == 0 else "failed"
        receipt.update(
            {
                "status": status,
                "completed_at": completed.isoformat(),
                "duration_seconds": (completed - started).total_seconds(),
                "exit_code": completed_process.returncode,
                "timed_out": False,
                "stdout_summary": scanners_mod._scanner_run_summary(stdout),
                "stderr_summary": scanners_mod._scanner_run_summary(stderr),
                "output_after": scanners_mod._scanner_output_snapshot(output_path),
                "findings_after": scanners_mod._scanner_output_snapshot(findings_path),
            }
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        stdout_path.write_text(stdout)
        stderr_path.write_text(stderr)
        completed = helpers._now()
        receipt.update(
            {
                "status": "failed",
                "completed_at": completed.isoformat(),
                "duration_seconds": (completed - started).total_seconds(),
                "exit_code": None,
                "timed_out": True,
                "error": f"review timed out after {reviewer.get('timeout')} seconds",
                "stdout_summary": scanners_mod._scanner_run_summary(stdout),
                "stderr_summary": scanners_mod._scanner_run_summary(stderr),
                "output_after": scanners_mod._scanner_output_snapshot(output_path),
                "findings_after": scanners_mod._scanner_output_snapshot(findings_path),
            }
        )
    if receipt.get("status") == "completed":
        receipt["completed_task_ids_reviewed"] = _review_stamp_completed_tasks(target, run_id)
    helpers._write_json(receipt_path, receipt)
    return receipt


def _review_plan_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    reviewers, errors = config_mod._load_review_config(target)
    planned: list[dict[str, Any]] = []
    for reviewer in reviewers:
        argv, blocker = config_mod._review_argv(str(reviewer.get("command") or ""))
        planned.append(
            {
                "id": reviewer.get("id"),
                "name": reviewer.get("name"),
                "enabled": reviewer.get("enabled", True),
                "command": reviewer.get("command"),
                "argv": argv or [],
                "blocker": blocker,
                "cwd": str(config_mod._review_cwd(target, reviewer)),
                "timeout": reviewer.get("timeout"),
                "target_paths": reviewer.get("target_paths") or [],
                "base_ref": reviewer.get("base_ref"),
                "output_path": str(config_mod._review_output_path(target, reviewer))
                if config_mod._review_output_path(target, reviewer)
                else None,
                "findings_path": str(config_mod._review_findings_path(target, reviewer))
                if config_mod._review_findings_path(target, reviewer)
                else None,
                "supported_modes": reviewer.get("supported_modes") or [],
                "privacy_mode": reviewer.get("privacy_mode"),
            }
        )
    return {
        "target": str(target),
        "config_path": str(helpers._review_config_path(target)),
        "valid": not errors,
        "errors": errors,
        "reviewers": reviewers,
        "planned": planned,
    }


def _review_pending_finding(target: Path) -> dict[str, Any] | None:
    candidates = [item for item in ledger_mod._pending_imports(target) if item.get("source") == "code-review"]
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            constants.PRIORITY_RANK.get(str(item.get("priority") or "normal"), 9),
            str(item.get("created_at") or ""),
        )
    )
    return ledger_mod._import_summary(candidates[0])


def _review_imports(target: Path, *, run_id: str | None = None) -> list[dict[str, Any]]:
    items = [
        item
        for item in ledger_mod._read_imports(target)
        if isinstance(item, dict) and item.get("source") == "code-review"
    ]
    if run_id is None:
        return items
    filtered: list[dict[str, Any]] = []
    for item in items:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if metadata.get("review_run_id") == run_id:
            filtered.append(item)
    return filtered


def _review_tasks_by_id(target: Path) -> dict[str, dict[str, Any]]:
    return {
        str(task.get("id")): task
        for task in ledger_mod._read_task_ledger(target).get("tasks", [])
        if isinstance(task, dict) and isinstance(task.get("id"), str)
    }


def _review_current_fingerprints(findings: list[dict[str, Any]]) -> dict[str, str]:
    values: dict[str, str] = {}
    for finding in findings:
        finding_id = finding.get("finding_id")
        fingerprint = finding.get("source_fingerprint")
        if isinstance(finding_id, str) and isinstance(fingerprint, str):
            values[finding_id] = fingerprint
    return values


def _review_finding_resolution(
    item: dict[str, Any],
    *,
    tasks_by_id: dict[str, dict[str, Any]],
    current_fingerprints: dict[str, str] | None = None,
) -> dict[str, Any]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    finding_id = str(metadata.get("review_finding_id") or "")
    source_fingerprint = metadata.get("source_fingerprint")
    current_fingerprint = current_fingerprints.get(finding_id) if current_fingerprints else None
    source_changed = bool(
        isinstance(current_fingerprint, str)
        and isinstance(source_fingerprint, str)
        and current_fingerprint
        and source_fingerprint
        and current_fingerprint != source_fingerprint
    )
    task_id = item.get("task_id")
    task = tasks_by_id.get(str(task_id)) if isinstance(task_id, str) else None
    status = str(item.get("status", "pending"))
    dismiss_reason = item.get("dismiss_reason")
    task_done = bool(task and task.get("status") == "done")
    if source_changed:
        state = "re_review"
        resolved = False
    elif status == "dismissed" and isinstance(dismiss_reason, str) and dismiss_reason.strip():
        state = "dismissed"
        resolved = True
    elif status == "promoted" and task_done:
        state = "completed"
        resolved = True
    elif status == "promoted":
        state = "promoted"
        resolved = False
    elif status == "dismissed":
        state = "dismissed_without_reason"
        resolved = False
    else:
        state = "pending"
        resolved = False
    return {
        "resolved": resolved,
        "resolution_state": state,
        "source_changed": source_changed,
        "current_source_fingerprint": current_fingerprint,
        "task": task,
    }


def _review_finding_summary(
    item: dict[str, Any],
    *,
    tasks_by_id: dict[str, dict[str, Any]],
    current_fingerprints: dict[str, str] | None = None,
) -> dict[str, Any]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    resolution = _review_finding_resolution(item, tasks_by_id=tasks_by_id, current_fingerprints=current_fingerprints)
    task = resolution.get("task") if isinstance(resolution.get("task"), dict) else None
    return {
        "import_id": item.get("id"),
        "finding_id": metadata.get("review_finding_id"),
        "reviewer_id": metadata.get("reviewer_id"),
        "review_run_id": metadata.get("review_run_id"),
        "severity": metadata.get("severity"),
        "category": metadata.get("category"),
        "path": metadata.get("path"),
        "line": metadata.get("line"),
        "status": item.get("status", "pending"),
        "resolution_state": resolution["resolution_state"],
        "resolved": resolution["resolved"],
        "source_changed": resolution["source_changed"],
        "source_fingerprint": metadata.get("source_fingerprint"),
        "current_source_fingerprint": resolution.get("current_source_fingerprint"),
        "task_id": item.get("task_id"),
        "task_status": task.get("status") if task else None,
        "dismiss_reason": item.get("dismiss_reason"),
        "completed_at": task.get("completed_at") if task else None,
        "text": item.get("text"),
        "metadata": metadata,
    }


def _review_findings_payload(target: Path, *, run_id: str | None = None) -> dict[str, Any]:
    target = target.expanduser().resolve()
    tasks_by_id = _review_tasks_by_id(target)
    imports = _review_imports(target, run_id=run_id)
    current_fingerprints_by_run: dict[str, dict[str, str]] = {}
    wanted_run_ids = {
        str(metadata.get("review_run_id"))
        for item in imports
        if isinstance((metadata := item.get("metadata")), dict) and isinstance(metadata.get("review_run_id"), str)
    }
    for run in _review_receipts(target):
        review_run_id = run.get("run_id")
        findings_path = run.get("findings_path")
        if not isinstance(review_run_id, str) or review_run_id not in wanted_run_ids:
            continue
        if not isinstance(findings_path, str) or not Path(findings_path).is_file():
            continue
        findings, _ = _load_review_findings(
            Path(findings_path),
            reviewer_id=str(run.get("reviewer_id") or ""),
            run_id=review_run_id,
            run=run,
        )
        current_fingerprints_by_run[review_run_id] = _review_current_fingerprints(findings)
    summaries = []
    for item in imports:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        item_run_id = metadata.get("review_run_id")
        current_fingerprints = (
            current_fingerprints_by_run.get(str(item_run_id)) if isinstance(item_run_id, str) else None
        )
        summaries.append(
            _review_finding_summary(item, tasks_by_id=tasks_by_id, current_fingerprints=current_fingerprints)
        )
    groups: dict[str, dict[str, int]] = {
        "by_reviewer": {},
        "by_run": {},
        "by_severity": {},
        "by_category": {},
        "by_path": {},
        "by_status": {},
        "by_resolution": {},
    }
    for item in summaries:
        for group_name, key_name in (
            ("by_reviewer", "reviewer_id"),
            ("by_run", "review_run_id"),
            ("by_severity", "severity"),
            ("by_category", "category"),
            ("by_path", "path"),
            ("by_status", "status"),
            ("by_resolution", "resolution_state"),
        ):
            value = str(item.get(key_name) or "unknown")
            groups[group_name][value] = groups[group_name].get(value, 0) + 1
    unresolved = [item for item in summaries if not item["resolved"]]
    return {
        "target": str(target),
        "count": len(summaries),
        "unresolved_count": len(unresolved),
        "findings": summaries,
        "groups": groups,
        "top_unresolved": unresolved[0] if unresolved else None,
    }


def _find_review_finding(target: Path, finding_id_or_import_id: str) -> tuple[dict[str, Any] | None, str | None]:
    payload = _review_findings_payload(target)
    matches = [
        item
        for item in payload["findings"]
        if item.get("import_id") == finding_id_or_import_id
        or item.get("finding_id") == finding_id_or_import_id
        or (isinstance(item.get("import_id"), str) and item["import_id"].startswith(finding_id_or_import_id))
        or (isinstance(item.get("finding_id"), str) and item["finding_id"].startswith(finding_id_or_import_id))
    ]
    if not matches:
        return None, f"review finding not found: {finding_id_or_import_id}"
    if len(matches) > 1:
        return None, f"review finding id is ambiguous: {finding_id_or_import_id}"
    return matches[0], None


def _review_malformed_findings(target: Path, runs: list[dict[str, Any]], reviewers: list[dict[str, Any]]) -> list[str]:
    items: list[tuple[str, Path, dict[str, Any]]] = []
    for run in runs[:20]:
        value = run.get("findings_path")
        if isinstance(value, str) and value:
            items.append((str(run.get("run_id")), Path(value), run))
    for reviewer in reviewers:
        path = config_mod._review_findings_path(target, reviewer)
        if path is not None and path.is_file():
            items.append(
                (str(reviewer.get("id")), path, {"run_id": str(reviewer.get("id")), "findings_path": str(path)})
            )
    malformed: list[str] = []
    seen: set[str] = set()
    for label, path, run in items:
        if str(path) in seen or not path.is_file():
            continue
        seen.add(str(path))
        _, errors = _load_review_findings(
            path,
            reviewer_id=str(run.get("reviewer_id") or label),
            run_id=str(run.get("run_id") or label),
            run=run,
        )
        if errors:
            malformed.append(f"{label}:{errors[0]}")
    return malformed


def _review_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    plan = _review_plan_payload(target)
    reviewers = plan["reviewers"] if isinstance(plan.get("reviewers"), list) else []
    receipts = _review_receipts(target)
    checks: list[dict[str, Any]] = []
    if not helpers._review_config_path(target).is_file():
        checks.append(
            {
                "status": constants.WARN,
                "name": "review_config",
                "detail": f"missing, run `brigade work review init --target {target}`",
            }
        )
    elif plan.get("valid"):
        checks.append({"status": constants.OK, "name": "review_config", "detail": plan["config_path"]})
    else:
        checks.append({"status": constants.FAIL, "name": "review_config", "detail": "; ".join(plan.get("errors", []))})
    blocked = [
        f"{item.get('id')}:{item.get('blocker')}"
        for item in plan.get("planned", [])
        if isinstance(item, dict) and item.get("enabled", True) and item.get("blocker")
    ]
    if blocked:
        checks.append({"status": constants.WARN, "name": "review_commands", "detail": ", ".join(blocked[:5])})
    elif plan.get("valid"):
        checks.append(
            {"status": constants.OK, "name": "review_commands", "detail": "enabled reviewer commands are resolvable"}
        )
    failed = [run for run in receipts if run.get("status") == "failed" or run.get("timed_out")][:5]
    if failed:
        checks.append(
            {
                "status": constants.WARN,
                "name": "review_runs_failed",
                "detail": ", ".join(str(run.get("run_id")) for run in failed),
            }
        )
    elif receipts:
        checks.append({"status": constants.OK, "name": "review_runs_failed", "detail": "none"})
    missing_logs: list[str] = []
    for run in receipts[:20]:
        for key in ("stdout_path", "stderr_path"):
            value = run.get(key)
            if isinstance(value, str) and value and not Path(value).is_file():
                missing_logs.append(f"{run.get('run_id')}:{key}")
    if missing_logs:
        checks.append({"status": constants.WARN, "name": "review_run_logs", "detail": ", ".join(missing_logs[:5])})
    elif receipts:
        checks.append({"status": constants.OK, "name": "review_run_logs", "detail": "receipt logs exist"})
    malformed = _review_malformed_findings(target, receipts, reviewers)
    if malformed:
        checks.append(
            {"status": constants.WARN, "name": "review_findings_malformed", "detail": "; ".join(malformed[:3])}
        )
    latest_success = _review_latest_success(target)
    enabled = [reviewer for reviewer in reviewers if reviewer.get("enabled", True)]
    if enabled and latest_success is None:
        checks.append({"status": constants.WARN, "name": "review_runs_missing", "detail": "no successful review runs"})
    elif latest_success is not None:
        completed = helpers._parse_iso_datetime(latest_success.get("completed_at") or latest_success.get("started_at"))
        if completed is not None:
            age_hours = (helpers._now() - completed).total_seconds() / 3600
            if age_hours > constants.REVIEW_RUN_STALE_HOURS:
                checks.append(
                    {
                        "status": constants.WARN,
                        "name": "review_runs_stale",
                        "detail": f"{latest_success.get('run_id')}={age_hours:.1f}h",
                    }
                )
            else:
                checks.append(
                    {"status": constants.OK, "name": "review_runs_stale", "detail": "latest review run is fresh"}
                )
    ledger = ledger_mod._read_task_ledger(target)
    done_tasks = [task for task in ledger.get("tasks", []) if isinstance(task, dict) and task.get("status") == "done"]
    if enabled and done_tasks and latest_success is None:
        checks.append(
            {
                "status": constants.WARN,
                "name": "review_completed_tasks",
                "detail": f"{len(done_tasks)} completed task(s) have no successful review receipt",
            }
        )
    unclosed = [
        run for run in receipts if run.get("status") == "completed" and not isinstance(run.get("closeout"), dict)
    ]
    if unclosed:
        checks.append(
            {
                "status": constants.WARN,
                "name": "review_runs_unclosed",
                "detail": ", ".join(str(run.get("run_id")) for run in unclosed[:5]),
            }
        )
    findings_payload = _review_findings_payload(target)
    top_pending = _review_pending_finding(target)
    return {
        "target": str(target),
        "config_path": str(helpers._review_config_path(target)),
        "checks": checks,
        "plan": plan,
        "latest_run": receipts[0] if receipts else None,
        "latest_success": latest_success,
        "latest_unclosed_run": unclosed[0] if unclosed else None,
        "top_pending_finding": top_pending,
        "top_unresolved_finding": findings_payload["top_unresolved"],
        "pending_finding_count": len(
            [item for item in ledger_mod._pending_imports(target) if item.get("source") == "code-review"]
        ),
        "unresolved_finding_count": findings_payload["unresolved_count"],
    }


def _review_closeout_path(run: dict[str, Any]) -> Path | None:
    value = run.get("path")
    if isinstance(value, str) and value:
        return Path(value) / "closeout.json"
    return None


def _resolve_review_run(target: Path, run_id: str) -> tuple[dict[str, Any] | None, str | None]:
    receipts = _review_receipts(target)
    if run_id == "latest":
        return (receipts[0], None) if receipts else (None, "review run not found: latest")
    matches = [run for run in receipts if str(run.get("run_id") or "").startswith(run_id)]
    if not matches:
        return None, f"review run not found: {run_id}"
    if len(matches) > 1:
        return None, f"review run id is ambiguous: {run_id}"
    return matches[0], None


def _review_stamp_task_closeouts(target: Path, closeout: dict[str, Any]) -> list[str]:
    ledger = ledger_mod._read_task_ledger(target)
    wanted_task_ids = {
        str(item.get("task_id"))
        for item in closeout.get("findings", [])
        if isinstance(item, dict) and isinstance(item.get("task_id"), str)
    }
    wanted_task_ids.update(
        str(item) for item in closeout.get("completed_task_ids_reviewed", []) if isinstance(item, str)
    )
    stamped: list[str] = []
    changed = False
    for task in ledger.get("tasks", []):
        if not isinstance(task, dict) or task.get("status") != "done" or task.get("id") not in wanted_task_ids:
            continue
        metadata = task.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
            task["metadata"] = metadata
        closeouts = metadata.get("review_closeouts")
        if not isinstance(closeouts, list):
            closeouts = []
            metadata["review_closeouts"] = closeouts
        if any(isinstance(item, dict) and item.get("review_run_id") == closeout.get("run_id") for item in closeouts):
            continue
        closeouts.append(
            {
                "review_run_id": closeout.get("run_id"),
                "closed_at": closeout.get("closed_at"),
                "finding_count": closeout.get("finding_count"),
                "unresolved_count": closeout.get("unresolved_count"),
                "resolved": closeout.get("resolved"),
            }
        )
        stamped.append(str(task.get("id")))
        changed = True
    if changed:
        ledger_mod._write_task_ledger(target, ledger)
    return stamped


def _review_stamp_latest_session(target: Path, closeout: dict[str, Any]) -> str | None:
    sessions, _ = helpers._collect_sessions(helpers._work_root(target))
    if not sessions:
        return None
    session_dir, payload = sessions[0]
    closeouts = payload.get("review_closeouts")
    if not isinstance(closeouts, list):
        closeouts = []
        payload["review_closeouts"] = closeouts
    if not any(isinstance(item, dict) and item.get("review_run_id") == closeout.get("run_id") for item in closeouts):
        closeouts.append(
            {
                "review_run_id": closeout.get("run_id"),
                "closed_at": closeout.get("closed_at"),
                "finding_count": closeout.get("finding_count"),
                "unresolved_count": closeout.get("unresolved_count"),
                "resolved": closeout.get("resolved"),
            }
        )
        helpers._write_json(session_dir / "session.json", payload)
    return str(session_dir)


def _review_closeout_payload(target: Path, run_id: str, *, write: bool = False) -> tuple[dict[str, Any] | None, int]:
    target = target.expanduser().resolve()
    run, error = _resolve_review_run(target, run_id)
    if run is None:
        print(f"error: {error}", file=sys.stderr)
        return None, 1 if error and "not found" in error else 2
    findings: list[dict[str, Any]] = []
    current_errors: list[str] = []
    findings_path = run.get("findings_path")
    if isinstance(findings_path, str) and findings_path and Path(findings_path).is_file():
        findings, current_errors = _load_review_findings(
            Path(findings_path),
            reviewer_id=str(run.get("reviewer_id") or ""),
            run_id=str(run.get("run_id") or ""),
            run=run,
        )
    tasks_by_id = _review_tasks_by_id(target)
    current_fingerprints = _review_current_fingerprints(findings)
    imported = _review_imports(target, run_id=str(run.get("run_id") or ""))
    summaries = [
        _review_finding_summary(item, tasks_by_id=tasks_by_id, current_fingerprints=current_fingerprints)
        for item in imported
    ]
    imported_finding_ids = {str(item.get("finding_id")) for item in summaries if item.get("finding_id")}
    for finding in findings:
        finding_id = str(finding.get("finding_id") or "")
        if finding_id and finding_id in imported_finding_ids:
            continue
        summaries.append(
            {
                "import_id": None,
                "finding_id": finding.get("finding_id"),
                "reviewer_id": finding.get("reviewer_id"),
                "review_run_id": finding.get("run_id"),
                "severity": finding.get("severity"),
                "category": finding.get("category"),
                "path": finding.get("path"),
                "line": finding.get("line"),
                "status": "not_imported",
                "resolution_state": "not_imported",
                "resolved": False,
                "source_changed": False,
                "source_fingerprint": finding.get("source_fingerprint"),
                "current_source_fingerprint": finding.get("source_fingerprint"),
                "task_id": None,
                "task_status": None,
                "dismiss_reason": None,
                "completed_at": None,
                "text": finding.get("rationale"),
                "metadata": finding,
            }
        )
    pending = [item for item in summaries if item["status"] == "pending"]
    dismissed = [item for item in summaries if item["status"] == "dismissed"]
    promoted = [item for item in summaries if item["status"] == "promoted"]
    completed = [item for item in summaries if item["resolution_state"] == "completed"]
    unresolved = [item for item in summaries if not item["resolved"]]
    now = helpers._now().isoformat()
    closeout = {
        "run_id": run.get("run_id"),
        "reviewer_id": run.get("reviewer_id"),
        "closed_at": now,
        "status": "unresolved" if unresolved or current_errors else "resolved",
        "resolved": not unresolved and not current_errors,
        "finding_count": len(findings),
        "imported_finding_count": len(summaries),
        "pending_imports": len(pending),
        "dismissed_findings": len(dismissed),
        "promoted_tasks": len(promoted),
        "completed_tasks": len(completed),
        "unresolved_count": len(unresolved),
        "changed_source_count": len([item for item in summaries if item.get("source_changed")]),
        "current_findings_errors": current_errors,
        "findings": summaries,
        "unresolved_findings": unresolved,
        "completed_task_ids_reviewed": run.get("completed_task_ids_reviewed")
        if isinstance(run.get("completed_task_ids_reviewed"), list)
        else [],
    }
    if write:
        stamped_tasks = _review_stamp_task_closeouts(target, closeout)
        stamped_session = _review_stamp_latest_session(target, closeout)
        closeout["stamped_task_ids"] = stamped_tasks
        closeout["stamped_session_path"] = stamped_session
        run["closeout"] = {
            key: closeout[key]
            for key in (
                "closed_at",
                "status",
                "resolved",
                "finding_count",
                "imported_finding_count",
                "unresolved_count",
                "changed_source_count",
            )
        }
        if _review_closeout_path(run) is not None:
            helpers._write_json(_review_closeout_path(run), closeout)
        if run.get("path"):
            helpers._write_json(Path(str(run["path"])) / "receipt.json", run)
    return {
        "target": str(target),
        "run": run,
        "closeout": closeout,
    }, 0 if closeout["resolved"] else 1


def review_init(*, target: Path, force: bool = False, update_gitignore: bool = True) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = helpers._review_config_path(target)
    if path.exists() and not force:
        print(f"error: review config already exists: {path}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config_mod._format_review_toml())
    print(f"review_config: {path}")
    print(f"reviewers: {len(constants.REVIEW_DEFAULTS)}")
    if update_gitignore:
        result = apply_gitignore(target, helpers._work_selection(target, dogfood_cmd.default_handoff_inbox(target)))
        print(f"gitignore: {result}")
    else:
        print("gitignore: skipped")
    print("next_command: brigade work review plan")
    return 0


def review_plan(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _review_plan_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"work review plan: {target}")
    print(f"config_path: {payload['config_path']}")
    if payload["errors"]:
        print(f"errors: {len(payload['errors'])}")
        for error in payload["errors"]:
            print(f"- {error}")
        return 1
    planned = payload.get("planned") if isinstance(payload.get("planned"), list) else []
    if not planned:
        print("reviewers: none")
    for item in planned:
        status = "enabled" if item.get("enabled", True) else "disabled"
        blocker = f" blocker={item.get('blocker')}" if item.get("blocker") else ""
        print(f"- {item.get('id')} [{status}] cwd={item.get('cwd')} timeout={item.get('timeout')}{blocker}")
        print(f"  command: {item.get('command')}")
        print(f"  findings: {item.get('findings_path')}")
    return 0


def _select_reviewers_for_run(
    target: Path,
    *,
    reviewer_id: str | None,
    all_matching: bool,
    include_disabled: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    reviewers, errors = config_mod._load_review_config(target)
    if errors:
        return [], [], errors
    if reviewer_id:
        selected = [item for item in reviewers if item.get("id") == reviewer_id]
        if not selected:
            return [], [], [f"reviewer not found: {reviewer_id}"]
    elif all_matching:
        selected = list(reviewers)
    else:
        return [], [], ["reviewer id or --all is required"]
    runnable: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for reviewer in selected:
        if not reviewer.get("enabled", True) and not include_disabled:
            if reviewer_id:
                return [], [], [f"reviewer disabled: {reviewer_id}"]
            skipped.append({"reviewer": reviewer, "reason": "disabled"})
            continue
        runnable.append(reviewer)
    return runnable, skipped, []


def review_run(
    *,
    target: Path,
    reviewer_id: str | None = None,
    all_matching: bool = False,
    include_disabled: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if bool(reviewer_id) == bool(all_matching):
        print("error: pass exactly one reviewer id or --all", file=sys.stderr)
        return 2
    if not helpers._review_config_path(target).is_file():
        print(f"error: review config missing: {helpers._review_config_path(target)}", file=sys.stderr)
        return 2
    selected, skipped, errors = _select_reviewers_for_run(
        target,
        reviewer_id=reviewer_id,
        all_matching=all_matching,
        include_disabled=include_disabled,
    )
    if errors:
        if json_output:
            print(
                json.dumps(
                    {"target": str(target), "errors": errors, "runs": [], "skipped": []}, indent=2, sort_keys=True
                )
            )
        else:
            for error in errors:
                print(f"error: {error}", file=sys.stderr)
        return 2
    runs = [_review_run_one(target, reviewer) for reviewer in selected]
    payload = {
        "target": str(target),
        "runs_root": str(helpers._review_runs_root(target)),
        "selected": len(selected),
        "completed": len([run for run in runs if run.get("status") == "completed"]),
        "failed": len([run for run in runs if run.get("status") != "completed"]),
        "skipped": [
            {"reviewer_id": item["reviewer"].get("id"), "reason": item["reason"]}
            for item in skipped
            if isinstance(item.get("reviewer"), dict)
        ],
        "runs": runs,
    }
    rc = 0 if payload["failed"] == 0 else 1
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    print(f"work review run: {target}")
    print(f"runs_root: {payload['runs_root']}")
    print(f"selected: {payload['selected']}")
    print(f"completed: {payload['completed']}")
    print(f"failed: {payload['failed']}")
    for item in payload["skipped"]:
        print(f"skipped: {item['reviewer_id']} {item['reason']}")
    for run in runs:
        print(
            f"- {run.get('run_id')} {run.get('reviewer_id')} "
            f"[{run.get('status')}] exit={run.get('exit_code')} timed_out={run.get('timed_out')}"
        )
        if run.get("error"):
            print(f"  error: {run.get('error')}")
        print(f"  logs: {run.get('stdout_path')} {run.get('stderr_path')}")
    return rc


def review_runs(*, target: Path, json_output: bool = False, limit: int = 20) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    receipts = _review_receipts(target)[:limit]
    payload = {"target": str(target), "runs_root": str(helpers._review_runs_root(target)), "runs": receipts}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"work review runs: {target}")
    print(f"runs_root: {payload['runs_root']}")
    if not receipts:
        print("runs: none")
        return 0
    for receipt in receipts:
        print(
            f"- {receipt.get('run_id')} {receipt.get('reviewer_id')} "
            f"[{receipt.get('status')}] exit={receipt.get('exit_code')} {receipt.get('started_at')}"
        )
    return 0


def review_show(*, target: Path, run_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    matches = [receipt for receipt in _review_receipts(target) if str(receipt.get("run_id") or "").startswith(run_id)]
    if not matches:
        print(f"error: review run not found: {run_id}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"error: review run id is ambiguous: {run_id}", file=sys.stderr)
        return 2
    receipt = matches[0]
    if json_output:
        print(json.dumps({"target": str(target), "run": receipt}, indent=2, sort_keys=True))
        return 0
    print(f"review_run: {receipt.get('run_id')}")
    print(f"reviewer: {receipt.get('reviewer_id')}")
    print(f"status: {receipt.get('status')}")
    print(f"started_at: {receipt.get('started_at')}")
    if receipt.get("completed_at"):
        print(f"completed_at: {receipt.get('completed_at')}")
    print(f"duration_seconds: {receipt.get('duration_seconds')}")
    print(f"exit_code: {receipt.get('exit_code')}")
    print(f"timed_out: {receipt.get('timed_out')}")
    print(f"stdout: {receipt.get('stdout_path')}")
    print(f"stderr: {receipt.get('stderr_path')}")
    print(f"findings: {receipt.get('findings_path')}")
    if receipt.get("stdout_summary"):
        print(f"stdout_summary: {helpers._short(str(receipt.get('stdout_summary')))}")
    if receipt.get("stderr_summary"):
        print(f"stderr_summary: {helpers._short(str(receipt.get('stderr_summary')))}")
    return 0


def review_import_findings(*, target: Path, run_id: str, json_output: bool = False, dry_run: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    matches = [receipt for receipt in _review_receipts(target) if str(receipt.get("run_id") or "").startswith(run_id)]
    if not matches:
        print(f"error: review run not found: {run_id}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"error: review run id is ambiguous: {run_id}", file=sys.stderr)
        return 2
    run = matches[0]
    findings_path_value = run.get("findings_path")
    if not isinstance(findings_path_value, str) or not findings_path_value:
        print(f"error: review run has no findings_path: {run.get('run_id')}", file=sys.stderr)
        return 2
    findings_path = Path(findings_path_value)
    if not findings_path.is_file():
        print(f"error: review findings file not found: {findings_path}", file=sys.stderr)
        return 1
    findings, errors = _load_review_findings(
        findings_path,
        reviewer_id=str(run.get("reviewer_id") or ""),
        run_id=str(run.get("run_id") or ""),
        run=run,
    )
    if errors:
        if json_output:
            print(
                json.dumps(
                    {"target": str(target), "run_id": run.get("run_id"), "errors": errors}, indent=2, sort_keys=True
                )
            )
        else:
            for error in errors:
                print(f"error: {error}", file=sys.stderr)
        return 2
    records = [_review_import_record(finding) for finding in findings]
    imported, skipped, skipped_dismissed = ledger_mod._append_import_records(target, records, dry_run=dry_run)
    payload = {
        "target": str(target),
        "run_id": run.get("run_id"),
        "reviewer_id": run.get("reviewer_id"),
        "findings_path": str(findings_path),
        "findings": len(findings),
        "created": len(imported),
        "skipped": len(skipped),
        "dismissed": len(skipped_dismissed),
        "imports": imported,
        "dry_run": dry_run,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"review findings import: {target}")
    print(f"run_id: {payload['run_id']}")
    print(f"findings: {payload['findings']}")
    print(f"created: {payload['created']}")
    print(f"skipped: {payload['skipped']}")
    print(f"dismissed: {payload['dismissed']}")
    for item in imported:
        print(f"- {item.get('id')} [{item.get('kind')}] {helpers._short(str(item.get('text', '')))}")
    return 0


def review_findings(*, target: Path, json_output: bool = False, run_id: str | None = None) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _review_findings_payload(target, run_id=run_id)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"review findings: {target}")
    print(f"findings: {payload['count']}")
    print(f"unresolved: {payload['unresolved_count']}")
    groups = payload["groups"]
    for group_name in ("by_reviewer", "by_run", "by_severity", "by_category", "by_status", "by_resolution"):
        values = groups.get(group_name) if isinstance(groups.get(group_name), dict) else {}
        if not values:
            continue
        print(f"{group_name}:")
        for key, count in values.items():
            print(f"  {key}: {count}")
    for item in payload["findings"][:20]:
        print(
            f"- {item.get('finding_id')} import={item.get('import_id')} "
            f"[{item.get('severity')} {item.get('category')}] "
            f"{item.get('resolution_state')} {item.get('path')}"
        )
    return 0


def review_finding_show(*, target: Path, finding_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    finding, error = _find_review_finding(target, finding_id)
    if finding is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    payload = {"target": str(target), "finding": finding}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"review_finding: {finding.get('finding_id')}")
    print(f"import: {finding.get('import_id')}")
    print(f"reviewer: {finding.get('reviewer_id')}")
    print(f"run: {finding.get('review_run_id')}")
    print(f"severity: {finding.get('severity')}")
    print(f"category: {finding.get('category')}")
    print(f"path: {finding.get('path')}")
    if finding.get("line"):
        print(f"line: {finding.get('line')}")
    print(f"status: {finding.get('status')}")
    print(f"resolution_state: {finding.get('resolution_state')}")
    print(f"resolved: {finding.get('resolved')}")
    print(f"source_changed: {finding.get('source_changed')}")
    if finding.get("task_id"):
        print(f"task: {finding.get('task_id')}")
        print(f"task_status: {finding.get('task_status')}")
    if finding.get("dismiss_reason"):
        print(f"dismiss_reason: {finding.get('dismiss_reason')}")
    print(f"text: {finding.get('text')}")
    return 0


def review_closeout(*, target: Path, run_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, rc = _review_closeout_payload(target, run_id, write=True)
    if payload is None:
        return rc
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    closeout = payload["closeout"]
    print(f"review closeout: {closeout.get('run_id')}")
    print(f"reviewer: {closeout.get('reviewer_id')}")
    print(f"status: {closeout.get('status')}")
    print(f"resolved: {closeout.get('resolved')}")
    print(f"findings: {closeout.get('finding_count')}")
    print(f"imported_findings: {closeout.get('imported_finding_count')}")
    print(f"pending_imports: {closeout.get('pending_imports')}")
    print(f"dismissed_findings: {closeout.get('dismissed_findings')}")
    print(f"promoted_tasks: {closeout.get('promoted_tasks')}")
    print(f"completed_tasks: {closeout.get('completed_tasks')}")
    print(f"unresolved: {closeout.get('unresolved_count')}")
    if closeout.get("changed_source_count"):
        print(f"changed_sources: {closeout.get('changed_source_count')}")
    for item in closeout.get("unresolved_findings", [])[:10]:
        if isinstance(item, dict):
            print(f"- unresolved {item.get('finding_id')} {item.get('resolution_state')} {item.get('path')}")
    return rc
