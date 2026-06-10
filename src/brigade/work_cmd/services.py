"""Backup, review, verify, scanner, sweep, and inbox operations."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from .. import dogfood_cmd, scrub
from ..install import apply_gitignore
from ..untrusted import scan_untrusted, wrap_untrusted
from . import constants, helpers, ledger as ledger_mod, config as config_mod


def _scanner_read_receipt(path: Path) -> dict[str, Any] | None:
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


def _scanner_receipts(target: Path) -> list[dict[str, Any]]:
    root = helpers._scanner_runs_root(target)
    if not root.is_dir():
        return []
    receipts = [_scanner_read_receipt(path) for path in root.iterdir() if path.is_dir()]
    valid = [item for item in receipts if isinstance(item, dict)]
    valid.sort(key=lambda item: str(item.get("started_at") or item.get("run_id") or ""), reverse=True)
    return valid


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


def _scanner_read_sweep(path: Path) -> dict[str, Any] | None:
    report = path / "sweep.json" if path.is_dir() else path
    if not report.is_file():
        return None
    try:
        data = json.loads(report.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("path", str(report.parent))
    return data


def _scanner_sweeps(target: Path) -> list[dict[str, Any]]:
    root = helpers._scanner_sweeps_root(target)
    if not root.is_dir():
        return []
    sweeps = [_scanner_read_sweep(path) for path in root.iterdir() if path.is_dir()]
    valid = [item for item in sweeps if isinstance(item, dict)]
    valid.sort(key=lambda item: str(item.get("started_at") or item.get("sweep_id") or ""), reverse=True)
    return valid


def _scanner_latest_sweep(target: Path) -> dict[str, Any] | None:
    sweeps = _scanner_sweeps(target)
    return sweeps[0] if sweeps else None


def _scanner_latest_success(target: Path, scanner_id: str) -> dict[str, Any] | None:
    for receipt in _scanner_receipts(target):
        if (
            receipt.get("scanner_id") == scanner_id
            and receipt.get("status") == "completed"
            and receipt.get("exit_code") == 0
        ):
            return receipt
    return None


def _scanner_is_due(target: Path, scanner: dict[str, Any], *, now: datetime | None = None) -> bool:
    now = now or helpers._now()
    scanner_id = str(scanner.get("id") or "")
    latest = _scanner_latest_success(target, scanner_id)
    if latest is None:
        return True
    started = helpers._parse_iso_datetime(latest.get("completed_at") or latest.get("started_at"))
    if started is None:
        return True
    cadence = str(scanner.get("cadence") or "")
    if cadence.startswith("hourly@"):
        return (now - started).total_seconds() >= 3600
    if cadence.startswith("daily@"):
        return now.date() > started.date()
    return False


def _scanner_due_items(target: Path, scanners: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [scanner for scanner in scanners if scanner.get("enabled", True) and _scanner_is_due(target, scanner)]


def _scanner_running_receipts(target: Path) -> list[dict[str, Any]]:
    return [receipt for receipt in _scanner_receipts(target) if receipt.get("status") == "running"]


def _scanner_output_snapshot(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if not path.exists():
        return {"path": str(path), "exists": False}
    stat = path.stat()
    return {
        "path": str(path),
        "exists": True,
        "is_dir": path.is_dir(),
        "size": stat.st_size if path.is_file() else None,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def _scanner_run_summary(text: str, limit: int = 1200) -> str:
    rendered = text.strip()
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


def _scanner_run_receipt_path(run: dict[str, Any]) -> str | None:
    path = run.get("path")
    if isinstance(path, str) and path.strip():
        return str(Path(path) / "receipt.json")
    return None


def _scanner_import_fingerprint(record: dict[str, Any], *, scanner: dict[str, Any]) -> str:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    existing = metadata.get("source_fingerprint")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    return helpers._stable_hash(
        {
            "scanner_id": scanner.get("id"),
            "scanner_source": scanner.get("source"),
            "source_item_key": ledger_mod._import_source_key(record),
            "text": record.get("text"),
            "kind": record.get("kind"),
            "type": record.get("type"),
            "priority": record.get("priority"),
            "template": record.get("template"),
            "acceptance": record.get("acceptance"),
        }
    )


def _scanner_import_provenance(
    *,
    target: Path,
    scanner: dict[str, Any],
    run: dict[str, Any],
    record: dict[str, Any],
) -> dict[str, Any]:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    output_after = run.get("output_after") if isinstance(run.get("output_after"), dict) else None
    provenance = {
        "scanner_id": scanner.get("id"),
        "scanner_source": scanner.get("source"),
        "scanner_run_id": run.get("run_id"),
        "scanner_receipt_path": _scanner_run_receipt_path(run),
        "scanner_output_path_snapshot": output_after,
        "source_fingerprint": _scanner_import_fingerprint(record, scanner=scanner),
    }
    import_path = config_mod._scanner_import_path(target, scanner)
    if import_path is not None:
        provenance["scanner_import_path"] = str(import_path)
    return {key: value for key, value in {**metadata, **provenance}.items() if value is not None}


def _scanner_enrich_import_records(
    *,
    target: Path,
    scanner: dict[str, Any],
    run: dict[str, Any],
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for record in records:
        item = dict(record)
        item["metadata"] = _scanner_import_provenance(target=target, scanner=scanner, run=run, record=record)
        enriched.append(item)
    return enriched


def _scanner_stamp_new_imports(
    *,
    target: Path,
    scanner: dict[str, Any],
    run: dict[str, Any],
    before_ids: set[str],
) -> list[str]:
    imports = ledger_mod._read_imports(target)
    changed = 0
    stamped_ids: list[str] = []
    for item in imports:
        import_id = item.get("id")
        if not isinstance(import_id, str) or import_id in before_ids:
            continue
        if item.get("source") != scanner.get("source"):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if metadata.get("scanner_run_id"):
            continue
        item["metadata"] = _scanner_import_provenance(target=target, scanner=scanner, run=run, record=item)
        item["updated_at"] = helpers._now().isoformat()
        changed += 1
        stamped_ids.append(import_id)
    if changed:
        ledger_mod._write_imports(target, imports)
    return stamped_ids


def _scanner_validate_import_output(
    target: Path,
    scanner: dict[str, Any],
) -> tuple[Path | None, list[dict[str, Any]], list[str]]:
    import_path = config_mod._scanner_import_path(target, scanner)
    if import_path is None:
        return None, [], [f"{scanner.get('id')}: import_path is not configured"]
    if scanner.get("import_format", "jsonl") != "jsonl":
        return import_path, [], [f"{scanner.get('id')}: import_format must be jsonl"]
    if not import_path.is_file():
        return import_path, [], [f"{scanner.get('id')}: import file not found: {import_path}"]
    records, errors = ledger_mod._load_import_jsonl(import_path)
    return import_path, records, [f"{scanner.get('id')}: {error}" for error in errors]


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


def _scanner_run_one(
    target: Path,
    scanner: dict[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any]:
    scanner_id = str(scanner.get("id") or "scanner")
    command = str(scanner.get("command") or "")
    argv, blocker = config_mod._scanner_argv(command)
    output_path = config_mod._scanner_output_path(target, scanner)
    import_path = config_mod._scanner_import_path(target, scanner)
    cwd = config_mod._scanner_cwd(target, scanner)
    started = helpers._now()
    run_id = f"{started.strftime('%Y%m%d-%H%M%S')}-{helpers._slug(scanner_id)}-{uuid4().hex[:6]}"
    run_dir = helpers._scanner_runs_root(target) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    receipt_path = run_dir / "receipt.json"
    receipt: dict[str, Any] = {
        "run_id": run_id,
        "scanner_id": scanner_id,
        "source": scanner.get("source"),
        "status": "running",
        "path": str(run_dir),
        "target": str(target),
        "cwd": str(cwd),
        "command": command,
        "argv": argv or [],
        "started_at": started.isoformat(),
        "timeout": scanner.get("timeout"),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "output_path": str(output_path) if output_path is not None else None,
        "output_before": _scanner_output_snapshot(output_path),
        "import_path": str(import_path) if import_path is not None else None,
        "import_format": scanner.get("import_format", "jsonl") if import_path is not None else None,
        "forced": force,
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
                "output_after": _scanner_output_snapshot(output_path),
            }
        )
        stdout_path.write_text("")
        stderr_path.write_text(blocker + "\n")
        helpers._write_json(receipt_path, receipt)
        return receipt
    if not cwd.is_dir():
        completed = helpers._now()
        error = f"scanner cwd does not exist: {cwd}"
        receipt.update(
            {
                "status": "failed",
                "completed_at": completed.isoformat(),
                "duration_seconds": (completed - started).total_seconds(),
                "exit_code": None,
                "timed_out": False,
                "error": error,
                "stdout_summary": "",
                "stderr_summary": error,
                "output_after": _scanner_output_snapshot(output_path),
            }
        )
        stdout_path.write_text("")
        stderr_path.write_text(error + "\n")
        helpers._write_json(receipt_path, receipt)
        return receipt
    try:
        completed_process = subprocess.run(
            argv,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=float(scanner.get("timeout") or 300),
            shell=False,
        )
        stdout = completed_process.stdout or ""
        stderr = completed_process.stderr or ""
        stdout_path.write_text(stdout)
        stderr_path.write_text(stderr)
        completed = helpers._now()
        receipt.update(
            {
                "status": "completed" if completed_process.returncode == 0 else "failed",
                "completed_at": completed.isoformat(),
                "duration_seconds": (completed - started).total_seconds(),
                "exit_code": completed_process.returncode,
                "timed_out": False,
                "stdout_summary": _scanner_run_summary(stdout),
                "stderr_summary": _scanner_run_summary(stderr),
                "output_after": _scanner_output_snapshot(output_path),
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
                "error": f"scanner timed out after {scanner.get('timeout')} seconds",
                "stdout_summary": _scanner_run_summary(stdout),
                "stderr_summary": _scanner_run_summary(stderr),
                "output_after": _scanner_output_snapshot(output_path),
            }
        )
    helpers._write_json(receipt_path, receipt)
    return receipt


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
        "output_before": _scanner_output_snapshot(output_path),
        "findings_path": str(findings_path) if findings_path is not None else None,
        "findings_before": _scanner_output_snapshot(findings_path),
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
                "output_after": _scanner_output_snapshot(output_path),
                "findings_after": _scanner_output_snapshot(findings_path),
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
                "output_after": _scanner_output_snapshot(output_path),
                "findings_after": _scanner_output_snapshot(findings_path),
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
                "stdout_summary": _scanner_run_summary(stdout),
                "stderr_summary": _scanner_run_summary(stderr),
                "output_after": _scanner_output_snapshot(output_path),
                "findings_after": _scanner_output_snapshot(findings_path),
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
                "stdout_summary": _scanner_run_summary(stdout),
                "stderr_summary": _scanner_run_summary(stderr),
                "output_after": _scanner_output_snapshot(output_path),
                "findings_after": _scanner_output_snapshot(findings_path),
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


def _scanner_plan_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    scanners, errors = config_mod._load_scanner_config(target)
    enabled = [scanner for scanner in scanners if scanner.get("enabled", True)]
    planned: list[dict[str, Any]] = []
    for scanner in enabled:
        start = config_mod._scanner_start_minute(str(scanner.get("cadence", "")))
        if start is None:
            continue
        duration = config_mod._scanner_duration_minutes(scanner)
        planned.append(
            {
                "id": scanner.get("id"),
                "source": scanner.get("source"),
                "command": scanner.get("command"),
                "cadence": scanner.get("cadence"),
                "start_minute": start,
                "start": config_mod._format_clock_minutes(start),
                "duration_minutes": duration,
                "end": config_mod._format_clock_minutes(start + duration),
                "conflict_window": scanner.get("conflict_window"),
                "output_path": scanner.get("output_path"),
                "import_path": scanner.get("import_path"),
                "import_format": scanner.get("import_format", "jsonl") if scanner.get("import_path") else None,
            }
        )
    planned.sort(key=lambda item: int(item.get("start_minute", 0)))

    conflicts: list[dict[str, Any]] = []
    for index, left in enumerate(planned):
        left_start = int(left["start_minute"])
        left_end = left_start + int(left["duration_minutes"])
        left_window = config_mod._scanner_window_minutes(str(left.get("conflict_window") or ""))
        for right in planned[index + 1 :]:
            right_start = int(right["start_minute"])
            right_end = right_start + int(right["duration_minutes"])
            right_window = config_mod._scanner_window_minutes(str(right.get("conflict_window") or ""))
            if left_start < right_end and right_start < left_end:
                conflicts.append({"type": "run_overlap", "scanners": [left["id"], right["id"]]})
            if left_window and right_window and left_window[0] < right_window[1] and right_window[0] < left_window[1]:
                conflicts.append({"type": "window_overlap", "scanners": [left["id"], right["id"]]})
            if abs(right_start - left_start) < 15:
                conflicts.append({"type": "clustered_runs", "scanners": [left["id"], right["id"]]})

    suggestions: list[dict[str, Any]] = []
    next_start: int | None = None
    for item in planned:
        current = int(item["start_minute"])
        suggested = current if next_start is None else max(current, next_start)
        suggestions.append(
            {
                "id": item["id"],
                "current": item["cadence"],
                "suggested_start": config_mod._format_clock_minutes(suggested),
                "suggested_cadence": f"daily@{config_mod._format_clock_minutes(suggested)}"
                if str(item.get("cadence", "")).startswith("daily@")
                else f"hourly@{suggested % 60:02d}",
            }
        )
        next_start = suggested + 15

    return {
        "target": str(target),
        "config_path": str(helpers._scanner_config_path(target)),
        "valid": not errors,
        "errors": errors,
        "scanners": scanners,
        "planned": planned,
        "conflicts": conflicts,
        "suggestions": suggestions,
    }


def _scanner_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    plan = _scanner_plan_payload(target)
    scanners = plan["scanners"] if isinstance(plan.get("scanners"), list) else []
    checks: list[dict[str, Any]] = []
    if not helpers._scanner_config_path(target).is_file():
        checks.append(
            {
                "status": constants.WARN,
                "name": "scanner_config",
                "detail": f"missing, run `brigade work scanners init --target {target}`",
            }
        )
    elif plan.get("valid"):
        checks.append({"status": constants.OK, "name": "scanner_config", "detail": plan["config_path"]})
    else:
        checks.append({"status": constants.FAIL, "name": "scanner_config", "detail": "; ".join(plan.get("errors", []))})

    by_id = {scanner.get("id"): scanner for scanner in scanners if isinstance(scanner, dict)}
    missing_required = [scanner_id for scanner_id in constants.SCANNER_REQUIRED_IDS if scanner_id not in by_id]
    disabled_required = [
        scanner_id
        for scanner_id in constants.SCANNER_REQUIRED_IDS
        if isinstance(by_id.get(scanner_id), dict) and not by_id[scanner_id].get("enabled", True)
    ]
    if missing_required or disabled_required:
        detail_parts = []
        if missing_required:
            detail_parts.append(f"missing={','.join(missing_required)}")
        if disabled_required:
            detail_parts.append(f"disabled={','.join(disabled_required)}")
        checks.append({"status": constants.WARN, "name": "scanner_required", "detail": "; ".join(detail_parts)})
    else:
        checks.append(
            {"status": constants.OK, "name": "scanner_required", "detail": "required local producers are enabled"}
        )

    bad_commands = []
    for scanner in scanners:
        if not scanner.get("enabled", True):
            continue
        _, blocker = config_mod._scanner_argv(str(scanner.get("command") or ""))
        if blocker is not None:
            bad_commands.append(str(scanner.get("id")))
    if bad_commands:
        checks.append({"status": constants.WARN, "name": "scanner_commands", "detail": ", ".join(bad_commands)})
    else:
        checks.append(
            {"status": constants.OK, "name": "scanner_commands", "detail": "enabled scanner commands are resolvable"}
        )

    stale_outputs: list[str] = []
    missing_outputs: list[str] = []
    now = helpers._now() if scanners else None
    for scanner in scanners:
        if not scanner.get("enabled", True):
            continue
        output = scanner.get("output_path")
        if not isinstance(output, str) or not output.strip():
            continue
        path = Path(output).expanduser()
        path = path if path.is_absolute() else target / path
        if not path.exists():
            missing_outputs.append(str(scanner.get("id")))
            continue
        if now is None:
            continue
        age_hours = (now.timestamp() - path.stat().st_mtime) / 3600
        if age_hours > constants.SCANNER_OUTPUT_STALE_HOURS:
            stale_outputs.append(f"{scanner.get('id')}={age_hours:.1f}h")
    if missing_outputs or stale_outputs:
        parts = []
        if missing_outputs:
            parts.append(f"missing={','.join(missing_outputs)}")
        if stale_outputs:
            parts.append(f"stale={','.join(stale_outputs)}")
        checks.append({"status": constants.WARN, "name": "scanner_outputs", "detail": "; ".join(parts)})
    else:
        checks.append(
            {"status": constants.OK, "name": "scanner_outputs", "detail": "enabled scanner outputs exist and are fresh"}
        )

    conflicts = plan.get("conflicts") if isinstance(plan.get("conflicts"), list) else []
    if conflicts:
        rendered = ", ".join(
            f"{item.get('type')}:{'/'.join(str(v) for v in item.get('scanners', []))}" for item in conflicts[:5]
        )
        checks.append({"status": constants.WARN, "name": "scanner_schedule", "detail": rendered})
    elif plan.get("valid"):
        checks.append({"status": constants.OK, "name": "scanner_schedule", "detail": "no scanner schedule conflicts"})

    receipts = _scanner_receipts(target)
    malformed_receipts = []
    runs_root = helpers._scanner_runs_root(target)
    if runs_root.is_dir():
        for path in runs_root.iterdir():
            if path.is_dir() and _scanner_read_receipt(path) is None:
                malformed_receipts.append(path.name)
    if malformed_receipts:
        checks.append(
            {"status": constants.FAIL, "name": "scanner_run_receipts", "detail": ", ".join(malformed_receipts[:5])}
        )

    running = [receipt for receipt in receipts if receipt.get("status") == "running"]
    if running:
        checks.append(
            {
                "status": constants.WARN,
                "name": "scanner_runs_running",
                "detail": ", ".join(str(item.get("run_id")) for item in running[:5]),
            }
        )

    recent_failed = [receipt for receipt in receipts if receipt.get("status") == "failed" or receipt.get("timed_out")][
        :5
    ]
    if recent_failed:
        rendered = ", ".join(f"{item.get('scanner_id')}:{item.get('run_id')}" for item in recent_failed)
        checks.append({"status": constants.WARN, "name": "scanner_runs_failed", "detail": rendered})
    elif receipts:
        checks.append({"status": constants.OK, "name": "scanner_runs_failed", "detail": "none"})

    missing_logs = []
    for receipt in receipts[:20]:
        for key in ("stdout_path", "stderr_path"):
            value = receipt.get(key)
            if isinstance(value, str) and value and not Path(value).is_file():
                missing_logs.append(f"{receipt.get('run_id')}:{key}")
    if missing_logs:
        checks.append({"status": constants.WARN, "name": "scanner_run_logs", "detail": ", ".join(missing_logs[:5])})
    elif receipts:
        checks.append({"status": constants.OK, "name": "scanner_run_logs", "detail": "receipt logs exist"})

    stale_successes: list[str] = []
    if scanners:
        now = helpers._now()
        for scanner in scanners:
            if not scanner.get("enabled", True):
                continue
            latest_success = _scanner_latest_success(target, str(scanner.get("id") or ""))
            if latest_success is None:
                continue
            completed = helpers._parse_iso_datetime(
                latest_success.get("completed_at") or latest_success.get("started_at")
            )
            if completed is None:
                stale_successes.append(str(scanner.get("id")))
                continue
            age_hours = (now - completed).total_seconds() / 3600
            if age_hours > constants.SCANNER_RUN_STALE_HOURS:
                stale_successes.append(f"{scanner.get('id')}={age_hours:.1f}h")
    if stale_successes:
        checks.append(
            {"status": constants.WARN, "name": "scanner_runs_stale", "detail": ", ".join(stale_successes[:5])}
        )
    elif receipts and plan.get("valid"):
        checks.append({"status": constants.OK, "name": "scanner_runs_stale", "detail": "none"})

    due = _scanner_due_items(target, scanners)
    if due:
        checks.append(
            {
                "status": constants.WARN,
                "name": "scanner_runs_due",
                "detail": ", ".join(str(item.get("id")) for item in due[:5]),
            }
        )
    elif plan.get("valid"):
        checks.append({"status": constants.OK, "name": "scanner_runs_due", "detail": "none"})

    next_run = plan.get("planned", [None])[0] if plan.get("planned") else None
    latest_run = receipts[0] if receipts else None
    return {
        "target": str(target),
        "config_path": str(helpers._scanner_config_path(target)),
        "checks": checks,
        "plan": plan,
        "next_run": next_run,
        "latest_run": latest_run,
        "due": due,
    }


def _scanner_sweep_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    checks: list[dict[str, Any]] = []
    latest = _scanner_latest_sweep(target)
    due = _scanner_health(target).get("due")
    due_count = len(due) if isinstance(due, list) else 0
    review: dict[str, Any] | None = None
    if latest is None:
        checks.append({"status": constants.WARN, "name": "scanner_sweeps", "detail": "none, run `brigade work sweep`"})
    else:
        status = str(latest.get("status") or "unknown")
        if status == "failed":
            checks.append({"status": constants.WARN, "name": "scanner_sweep_failed", "detail": latest.get("sweep_id")})
        else:
            checks.append(
                {
                    "status": constants.OK,
                    "name": "scanner_sweep_latest",
                    "detail": f"{latest.get('sweep_id')} [{status}]",
                }
            )
        completed = helpers._parse_iso_datetime(latest.get("completed_at") or latest.get("started_at"))
        if completed is not None:
            age_hours = (helpers._now() - completed).total_seconds() / 3600
            if age_hours > constants.SCANNER_SWEEP_STALE_HOURS:
                checks.append(
                    {
                        "status": constants.WARN,
                        "name": "scanner_sweep_stale",
                        "detail": f"{latest.get('sweep_id')}={age_hours:.1f}h",
                    }
                )
        review, _ = _sweep_review_payload(target, str(latest.get("sweep_id") or "latest"))
        if isinstance(review, dict):
            checks.extend(review["issues"])
    return {
        "target": str(target),
        "sweeps_root": str(helpers._scanner_sweeps_root(target)),
        "latest": latest,
        "checks": checks,
        "due_count": due_count,
        "suggested_command": "brigade work sweep" if due_count else None,
        "review": {
            "top_pending_import": review.get("top_pending_import") if isinstance(review, dict) else None,
            "issue_count": len(review.get("issues", [])) if isinstance(review, dict) else 0,
            "issues": review.get("issues", []) if isinstance(review, dict) else [],
        },
    }


def _default_verify_commands(target: Path) -> list[str]:
    if (target / "pyproject.toml").is_file() and (target / "tests").is_dir():
        if (target / "src").is_dir():
            return ["PYTHONPATH=src python3 -m pytest -q"]
        return ["python3 -m pytest -q"]
    if (target / "pytest.ini").is_file() or (target / "tests").is_dir():
        return ["python3 -m pytest -q"]
    if (target / "package.json").is_file():
        return ["npm test"]
    return []


def _verify_parse_command(command: str) -> tuple[list[str] | None, dict[str, str], str | None]:
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return None, {}, f"invalid command: {exc}"
    if not parts:
        return None, {}, "empty command"
    env: dict[str, str] = {}
    argv = list(parts)
    while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
        key, value = argv.pop(0).split("=", 1)
        env[key] = value
    if not argv:
        return None, env, "command contains only environment assignments"
    executable = Path(argv[0]).name
    if executable in constants.SCANNER_HIGH_RISK_COMMANDS:
        return None, env, f"high-risk verification command: {executable}"
    if any(constants.SCANNER_SHELL_META_RE.search(part) for part in argv):
        return None, env, "high-risk verification command contains shell metacharacters"
    if "/" in argv[0]:
        if not Path(argv[0]).expanduser().exists():
            return None, env, f"verification command is not resolvable: {argv[0]}"
    elif shutil.which(argv[0]) is None:
        return None, env, f"verification command is not resolvable: {argv[0]}"
    return argv, env, None


def _latest_verify_receipt(target: Path) -> dict[str, Any] | None:
    receipts = _verify_receipts(target)
    return receipts[0] if receipts else None


def _verify_read_receipt(path: Path) -> dict[str, Any] | None:
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


def _verify_receipts(target: Path) -> list[dict[str, Any]]:
    root = helpers._verify_runs_root(target)
    if not root.is_dir():
        return []
    receipts = [_verify_read_receipt(path) for path in root.iterdir() if path.is_dir()]
    valid = [item for item in receipts if isinstance(item, dict)]
    valid.sort(key=lambda item: str(item.get("started_at") or item.get("run_id") or ""), reverse=True)
    return valid


def _resolve_verify_receipt(target: Path, run_id: str) -> tuple[dict[str, Any] | None, str | None]:
    receipts = _verify_receipts(target)
    if run_id == "latest":
        return (receipts[0], None) if receipts else (None, "verification run not found: latest")
    matches = [run for run in receipts if str(run.get("run_id") or "").startswith(run_id)]
    if not matches:
        return None, f"verification run not found: {run_id}"
    if len(matches) > 1:
        return None, f"verification run id is ambiguous: {run_id}"
    return matches[0], None


def _verification_task_from_session(payload: dict[str, Any]) -> dict[str, Any] | None:
    task = payload.get("task")
    return task if isinstance(task, dict) else None


def _verification_evidence_payload(target: Path, session: tuple[Path, dict[str, Any]] | None = None) -> dict[str, Any]:
    from .. import handoff_cmd

    target = target.expanduser().resolve()
    sessions, _ = helpers._collect_sessions(helpers._work_root(target))
    latest_session = session or (sessions[0] if sessions else None)
    session_info = helpers._session_info(latest_session[0], latest_session[1]) if latest_session else None
    task = _verification_task_from_session(latest_session[1]) if latest_session else None
    latest_verify = _latest_verify_receipt(target)
    sweep_health = _scanner_sweep_health(target)
    review_health = _review_health(target)
    handoff_drafts = handoff_cmd.draft_queue_payload(target)
    return {
        "target": str(target),
        "session": session_info,
        "task": task,
        "task_acceptance": task.get("acceptance")
        if isinstance(task, dict) and isinstance(task.get("acceptance"), list)
        else [],
        "latest_verify": latest_verify,
        "scanner_sweep": {
            "latest": sweep_health.get("latest"),
            "issue_count": sweep_health.get("review", {}).get("issue_count")
            if isinstance(sweep_health.get("review"), dict)
            else 0,
            "top_issue": sweep_health.get("review", {}).get("top_issue")
            if isinstance(sweep_health.get("review"), dict)
            else None,
            "due_count": sweep_health.get("due_count"),
        },
        "code_review": {
            "latest_run": review_health.get("latest_run"),
            "latest_unclosed_run": review_health.get("latest_unclosed_run"),
            "unresolved_finding_count": review_health.get("unresolved_finding_count"),
            "top_unresolved_finding": review_health.get("top_unresolved_finding"),
        },
        "handoff_drafts": {
            "counts": handoff_drafts.get("counts"),
            "issue_count": handoff_drafts.get("issue_count"),
            "top_issue": handoff_drafts.get("top_issue"),
            "latest_ingest_run": handoff_drafts.get("latest_ingest_run"),
        },
    }


def _verify_plan_payload(target: Path, commands: list[str] | None = None) -> dict[str, Any]:
    target = target.expanduser().resolve()
    planned_commands = commands if commands is not None else _default_verify_commands(target)
    evidence = _verification_evidence_payload(target)
    blockers: list[str] = []
    if not planned_commands:
        blockers.append("no verification commands found; pass --command")
    for command in planned_commands:
        _, _, error = _verify_parse_command(command)
        if error:
            blockers.append(f"{command}: {error}")
    return {
        "target": str(target),
        "verify_runs_root": str(helpers._verify_runs_root(target)),
        "commands": planned_commands,
        "blockers": blockers,
        "evidence": evidence,
        "suggested_command": "brigade work verify run"
        if planned_commands
        else 'brigade work verify run --command "..."',
    }


def _write_verify_markdown(run_dir: Path, receipt: dict[str, Any]) -> None:
    lines = [
        "# Brigade Work Verification",
        "",
        f"- Run: `{receipt.get('run_id')}`",
        f"- Status: {receipt.get('status')}",
        f"- Target: `{receipt.get('target')}`",
        f"- Started: {receipt.get('started_at')}",
        f"- Completed: {receipt.get('completed_at')}",
        "",
        "## Commands",
        "",
    ]
    for command in receipt.get("commands", []):
        if not isinstance(command, dict):
            continue
        lines.append(f"- `{command.get('command')}`: exit={command.get('exit_code')} status={command.get('status')}")
    lines.extend(["", "## Evidence", ""])
    evidence = receipt.get("evidence") if isinstance(receipt.get("evidence"), dict) else {}
    session = evidence.get("session") if isinstance(evidence.get("session"), dict) else None
    latest_verify = evidence.get("latest_verify") if isinstance(evidence.get("latest_verify"), dict) else None
    if session:
        lines.append(f"- Session: `{session.get('id')}` status={session.get('status')}")
    if latest_verify:
        lines.append(f"- Previous verification: `{latest_verify.get('run_id')}` status={latest_verify.get('status')}")
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n")


def _run_verify_commands(target: Path, commands: list[str], timeout: int) -> tuple[dict[str, Any], int]:
    started = helpers._now()
    run_id = f"{started.strftime('%Y%m%d-%H%M%S')}-work-verify-{uuid4().hex[:6]}"
    run_dir = helpers._verify_runs_root(target) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    receipt: dict[str, Any] = {
        "run_id": run_id,
        "target": str(target),
        "status": "running",
        "started_at": started.isoformat(),
        "timeout": timeout,
        "path": str(run_dir),
        "evidence": _verification_evidence_payload(target),
        "commands": [],
    }
    rc = 0
    for index, command in enumerate(commands, start=1):
        argv, env_assignments, error = _verify_parse_command(command)
        command_result: dict[str, Any] = {
            "command": command,
            "env": sorted(env_assignments),
            "started_at": helpers._now().isoformat(),
        }
        stdout_path = run_dir / f"command-{index}-stdout.log"
        stderr_path = run_dir / f"command-{index}-stderr.log"
        if error or argv is None:
            command_result.update(
                {
                    "status": "failed",
                    "exit_code": 2,
                    "stderr_summary": error,
                    "stdout_summary": "",
                    "stdout_log_path": str(stdout_path),
                    "stderr_log_path": str(stderr_path),
                }
            )
            stdout_path.write_text("")
            stderr_path.write_text(str(error or "invalid command") + "\n")
            rc = 2
            receipt["commands"].append(command_result)
            continue
        run_env = os.environ.copy()
        run_env.update(env_assignments)
        command_started = helpers._now()
        try:
            completed = subprocess.run(
                argv,
                cwd=target,
                env=run_env,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
            command_completed = helpers._now()
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            stdout_path.write_text(stdout)
            stderr_path.write_text(stderr)
            status = "completed" if completed.returncode == 0 else "failed"
            if completed.returncode != 0 and rc == 0:
                rc = completed.returncode
            command_result.update(
                {
                    "status": status,
                    "exit_code": completed.returncode,
                    "completed_at": command_completed.isoformat(),
                    "duration_seconds": (command_completed - command_started).total_seconds(),
                    "argv": argv,
                    "stdout_summary": _scanner_run_summary(stdout),
                    "stderr_summary": _scanner_run_summary(stderr),
                    "stdout_log_path": str(stdout_path),
                    "stderr_log_path": str(stderr_path),
                }
            )
        except subprocess.TimeoutExpired as exc:
            command_completed = helpers._now()
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            stdout_path.write_text(stdout)
            stderr_path.write_text(stderr)
            command_result.update(
                {
                    "status": "timed_out",
                    "exit_code": None,
                    "completed_at": command_completed.isoformat(),
                    "duration_seconds": (command_completed - command_started).total_seconds(),
                    "argv": argv,
                    "stdout_summary": _scanner_run_summary(stdout),
                    "stderr_summary": _scanner_run_summary(stderr),
                    "stdout_log_path": str(stdout_path),
                    "stderr_log_path": str(stderr_path),
                }
            )
            rc = 124
        receipt["commands"].append(command_result)
    completed_at = helpers._now()
    receipt["completed_at"] = completed_at.isoformat()
    receipt["duration_seconds"] = (completed_at - started).total_seconds()
    receipt["status"] = "completed" if rc == 0 else "failed"
    helpers._write_json(run_dir / "receipt.json", receipt)
    _write_verify_markdown(run_dir, receipt)
    return receipt, rc


def _resolve_closeout_session(target: Path, session_id: str) -> tuple[Path | None, dict[str, Any] | None, str | None]:
    sessions, _ = helpers._collect_sessions(helpers._work_root(target))
    if session_id == "latest":
        return (sessions[0][0], sessions[0][1], None) if sessions else (None, None, "work session not found: latest")
    matches: list[tuple[Path, dict[str, Any]]] = []
    for path, payload in sessions:
        payload_id = str(payload.get("id") or path.name)
        if (
            payload_id == session_id
            or path.name == session_id
            or payload_id.startswith(session_id)
            or path.name.startswith(session_id)
        ):
            matches.append((path, payload))
    if not matches:
        path = helpers._resolve_session(target, session_id)
        payload = helpers._read_session(path)
        if payload is not None:
            return path, payload, None
        return None, None, f"work session not found: {session_id}"
    if len(matches) > 1:
        return None, None, f"work session id is ambiguous: {session_id}"
    return matches[0][0], matches[0][1], None


def _work_closeout_path(target: Path, closeout_id: str) -> Path:
    return helpers._work_closeouts_root(target) / closeout_id / "closeout.json"


def _latest_work_closeout_payload(target: Path) -> dict[str, Any] | None:
    root = helpers._work_closeouts_root(target)
    if not root.is_dir():
        return None
    closeouts: list[dict[str, Any]] = []
    for child in root.iterdir():
        payload = helpers._read_json(child / "closeout.json") if child.is_dir() else None
        if isinstance(payload, dict):
            payload.setdefault("path", str(child / "closeout.json"))
            closeouts.append(payload)
    closeouts.sort(key=lambda item: str(item.get("created_at") or item.get("closeout_id") or ""), reverse=True)
    return closeouts[0] if closeouts else None


def _write_work_closeout_markdown(path: Path, closeout: dict[str, Any]) -> None:
    lines = [
        "# Brigade Work Closeout",
        "",
        f"- Closeout: `{closeout.get('closeout_id')}`",
        f"- Status: {closeout.get('status')}",
        f"- Ready: {closeout.get('ready')}",
        f"- Session: `{closeout.get('session', {}).get('id') if isinstance(closeout.get('session'), dict) else ''}`",
        f"- Verification: `{closeout.get('verification', {}).get('run_id') if isinstance(closeout.get('verification'), dict) else ''}`",
        "",
        "## Blockers",
        "",
    ]
    blockers = closeout.get("blockers") if isinstance(closeout.get("blockers"), list) else []
    lines.extend(f"- {item}" for item in blockers) if blockers else lines.append("- none")
    lines.extend(["", "## Evidence", ""])
    for key in ("task", "scanner_sweep", "code_review", "handoff_drafts"):
        value = closeout.get(key)
        lines.append(f"- {key}: `{json.dumps(value, sort_keys=True, default=str)[:500]}`")
    path.with_name("closeout.md").write_text("\n".join(lines) + "\n")


def _work_closeout_payload(target: Path, session_id: str, *, write: bool = False) -> tuple[dict[str, Any] | None, int]:
    target = target.expanduser().resolve()
    session_path, session_payload, error = _resolve_closeout_session(target, session_id)
    if session_path is None or session_payload is None:
        print(f"error: {error}", file=sys.stderr)
        return None, 1 if error and "not found" in error else 2
    evidence = _verification_evidence_payload(target, (session_path, session_payload))
    latest_verify = evidence.get("latest_verify") if isinstance(evidence.get("latest_verify"), dict) else None
    task = evidence.get("task") if isinstance(evidence.get("task"), dict) else None
    task_acceptance = evidence.get("task_acceptance") if isinstance(evidence.get("task_acceptance"), list) else []
    scanner_sweep = evidence.get("scanner_sweep") if isinstance(evidence.get("scanner_sweep"), dict) else {}
    code_review = evidence.get("code_review") if isinstance(evidence.get("code_review"), dict) else {}
    handoff_drafts = evidence.get("handoff_drafts") if isinstance(evidence.get("handoff_drafts"), dict) else {}
    blockers: list[str] = []
    if session_payload.get("status") != "ended":
        blockers.append(f"work session is not ended: {session_payload.get('status')}")
    if latest_verify is None:
        blockers.append("no verification receipt found")
    elif latest_verify.get("status") != "completed":
        blockers.append(
            f"latest verification did not complete: {latest_verify.get('run_id')} [{latest_verify.get('status')}]"
        )
    if task is not None and not task_acceptance:
        blockers.append(f"task has no acceptance criteria: {task.get('id')}")
    latest_sweep = scanner_sweep.get("latest") if isinstance(scanner_sweep.get("latest"), dict) else None
    if latest_sweep and latest_sweep.get("status") == "failed":
        blockers.append(f"latest scanner sweep failed: {latest_sweep.get('sweep_id')}")
    if int(scanner_sweep.get("issue_count") or 0) > 0:
        blockers.append(f"scanner sweep has unresolved review issue(s): {scanner_sweep.get('issue_count')}")
    if code_review.get("latest_unclosed_run"):
        run = code_review["latest_unclosed_run"]
        if isinstance(run, dict):
            blockers.append(f"review run is not closed out: {run.get('run_id')}")
    if int(code_review.get("unresolved_finding_count") or 0) > 0:
        blockers.append(f"code review has unresolved finding(s): {code_review.get('unresolved_finding_count')}")
    if int(handoff_drafts.get("issue_count") or 0) > 0:
        blockers.append(f"handoff draft queue has issue(s): {handoff_drafts.get('issue_count')}")
    now = helpers._now()
    closeout_id = f"{now.strftime('%Y%m%d-%H%M%S')}-work-closeout-{uuid4().hex[:6]}"
    closeout = {
        "closeout_id": closeout_id,
        "target": str(target),
        "status": "ready" if not blockers else "blocked",
        "ready": not blockers,
        "created_at": now.isoformat(),
        "session": helpers._session_info(session_path, session_payload),
        "session_path": str(session_path),
        "task": ledger_mod._task_summary(task) if task else None,
        "acceptance_criteria": task_acceptance,
        "verification": {
            "run_id": latest_verify.get("run_id"),
            "status": latest_verify.get("status"),
            "path": latest_verify.get("path"),
            "command_count": len(latest_verify.get("commands") or []),
        }
        if latest_verify
        else None,
        "scanner_sweep": scanner_sweep,
        "code_review": code_review,
        "handoff_drafts": handoff_drafts,
        "blockers": blockers,
    }
    if write:
        path = _work_closeout_path(target, closeout_id)
        helpers._write_json(path, closeout)
        _write_work_closeout_markdown(path, closeout)
        session_payload["closeout"] = {
            "closeout_id": closeout_id,
            "status": closeout["status"],
            "ready": closeout["ready"],
            "path": str(path),
            "created_at": closeout["created_at"],
        }
        helpers._write_json(session_path / "session.json", session_payload)
        closeout["path"] = str(path)
    return closeout, 0 if closeout["ready"] else 1


def _scanner_health_issue_records(target: Path) -> list[dict[str, Any]]:
    health = _scanner_health(target)
    records: list[dict[str, Any]] = []
    for check in health["checks"]:
        if check.get("status") == constants.OK:
            continue
        name = str(check.get("name"))
        detail = str(check.get("detail"))
        records.append(
            {
                "text": f"Repair scanner health issue {name}: {detail}",
                "kind": "task",
                "source": "scanner-health",
                "type": "workflow",
                "priority": "normal",
                "template": "bugfix",
                "acceptance": [f"`brigade work scanners doctor` no longer reports {name}."],
                "metadata": {
                    "scanner_health_check": name,
                    "scanner_health_status": check.get("status"),
                    "scanner_health_detail": detail,
                    "source_item_key": f"scanner-health:{name}",
                    "source_fingerprint": helpers._stable_hash({"name": name, "detail": detail}),
                },
            }
        )
    return records


def import_add(
    *,
    target: Path,
    text: str,
    kind: str = "task",
    source: str = "manual",
    metadata: list[str] | None = None,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    rendered = text.strip()
    if not rendered:
        print("error: import text is required", file=sys.stderr)
        return 2
    if kind not in constants.IMPORT_KINDS:
        print(f"error: --kind must be one of: {', '.join(constants.IMPORT_KINDS)}", file=sys.stderr)
        return 2
    source_text = source.strip() or "manual"
    try:
        parsed_metadata = ledger_mod._parse_metadata(metadata)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    imports = ledger_mod._read_imports(target)
    item = ledger_mod._make_import(rendered, kind=kind, source=source_text, metadata=parsed_metadata)
    imports.append(item)
    ledger_mod._write_imports(target, imports)
    print(f"import: {item['id']}")
    print(f"status: {item['status']}")
    print(f"kind: {item['kind']}")
    print(f"source: {item['source']}")
    print(f"text: {item['text']}")
    return 0


def import_context(
    *,
    target,
    text,
    source="manual",
    context_kind="note",
    from_file=None,
    max_chars=20000,
    json_output=False,
) -> int:
    target = Path(target).expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if context_kind not in constants.CONTEXT_KINDS:
        print(
            f"error: --kind must be one of: {', '.join(constants.CONTEXT_KINDS)}",
            file=sys.stderr,
        )
        return 2

    if from_file is not None:
        body_path = Path(from_file).expanduser()
        try:
            raw = body_path.read_text()
        except OSError as exc:
            print(f"error: cannot read --from-file: {exc}", file=sys.stderr)
            return 2
    else:
        raw = text

    body = (raw or "").strip()
    if not body:
        print("error: context body is required", file=sys.stderr)
        return 2

    sig = scan_untrusted(body)
    framed = wrap_untrusted(body, source_kind="tool-output", max_chars=max_chars)
    metadata = {
        "context_kind": context_kind,
        "injection_flagged": sig.flagged,
        "injection_count": sig.count,
        "needs_review": sig.flagged,
        "source_chars": len(body),
        "truncated": len(body) > max_chars,
    }
    source_text = source.strip() or "manual"

    imports = ledger_mod._read_imports(target)
    item = ledger_mod._make_import(framed, kind="context", source=source_text, metadata=metadata)
    imports.append(item)
    ledger_mod._write_imports(target, imports)

    if json_output:
        print(json.dumps(item, indent=2, sort_keys=True))
        return 0

    print(f"import: {item['id']}")
    print(f"status: {item['status']}")
    print(f"kind: {item['kind']}")
    print(f"source: {item['source']}")
    print(f"context_kind: {context_kind}")
    if sig.flagged:
        print(f"needs_review: injection signal ({sig.count})")
    return 0


def import_list(
    *,
    target: Path,
    all_imports: bool = False,
    json_output: bool = False,
    limit: int = 20,
    source: str | None = None,
    kind: str | None = None,
    metadata: list[str] | None = None,
) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    if kind is not None and kind not in constants.IMPORT_KINDS:
        print(f"error: --kind must be one of: {', '.join(constants.IMPORT_KINDS)}", file=sys.stderr)
        return 2
    metadata_filters, rc = ledger_mod._parse_or_report_metadata_filters(metadata)
    if rc:
        return rc
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    imports = [item for item in ledger_mod._read_imports(target) if isinstance(item, dict)]
    imports.sort(key=ledger_mod._import_sort_key)
    if not all_imports:
        imports = [item for item in imports if item.get("status", "pending") == "pending"]
    if source:
        imports = [item for item in imports if item.get("source") == source]
    if kind:
        imports = [item for item in imports if item.get("kind") == kind]
    if metadata_filters:
        imports = [item for item in imports if ledger_mod._import_metadata_matches(item, metadata_filters)]
    imports = imports[:limit]

    if json_output:
        print(
            json.dumps(
                {"imports_path": str(helpers._imports_path(target)), "imports": imports}, indent=2, sort_keys=True
            )
        )
        return 0

    print(f"work imports: {target}")
    print(f"imports_path: {helpers._imports_path(target)}")
    if not imports:
        print("imports: none")
        return 0
    for item in imports:
        status_text = item.get("status", "pending")
        kind = item.get("kind", "task")
        source = item.get("source", "manual")
        print(f"- {item.get('id')} [{status_text}] {kind} from {source}: {helpers._short(str(item.get('text', '')))}")
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if metadata:
            rendered = ", ".join(f"{key}={metadata[key]}" for key in sorted(metadata))
            print(f"  metadata: {rendered}")
        if item.get("task_id"):
            print(f"  task: {item['task_id']}")
    return 0


def import_validate(*, input_path: Path, json_output: bool = False) -> int:
    path = input_path.expanduser().resolve()
    if not path.is_file():
        print(f"error: import file not found: {path}", file=sys.stderr)
        return 2
    records, errors = ledger_mod._load_import_jsonl(path)
    payload = {
        "path": str(path),
        "valid": not errors,
        "records": len(records),
        "errors": errors,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if not errors else 1
    print(f"import file: {path}")
    print(f"records: {len(records)}")
    if errors:
        print(f"errors: {len(errors)}")
        for error in errors:
            print(f"- {error}")
        return 1
    print("status: valid")
    return 0


def import_ingest(
    *,
    target: Path,
    input_path: Path,
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = input_path.expanduser().resolve()
    if not path.is_file():
        print(f"error: import file not found: {path}", file=sys.stderr)
        return 2
    records, errors = ledger_mod._load_import_jsonl(path)
    if errors:
        if json_output:
            print(
                json.dumps(
                    {
                        "path": str(path),
                        "imports_path": str(helpers._imports_path(target)),
                        "valid": False,
                        "errors": errors,
                        "created": 0,
                        "skipped": 0,
                        "dismissed": 0,
                        "invalid": len(errors),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(f"error: import file is invalid: {path}", file=sys.stderr)
            for error in errors:
                print(f"- {error}", file=sys.stderr)
        return 2

    imported, skipped, skipped_dismissed = ledger_mod._append_import_records(target, records, dry_run=dry_run)
    payload = {
        "path": str(path),
        "imports_path": str(helpers._imports_path(target)),
        "dry_run": dry_run,
        "created": len(imported),
        "imported": len(imported),
        "skipped": len(skipped),
        "skipped_duplicates": len(skipped),
        "dismissed": len(skipped_dismissed),
        "skipped_dismissed": len(skipped_dismissed),
        "invalid": 0,
        "imports": imported,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"import file: {path}")
    print(f"imports_path: {helpers._imports_path(target)}")
    print(f"dry_run: {dry_run}")
    print(f"imported: {len(imported)}")
    print(f"skipped_duplicates: {len(skipped)}")
    if skipped_dismissed:
        print(f"skipped_dismissed: {len(skipped_dismissed)}")
    for item in imported:
        print(
            f"- {item.get('id')} [{item.get('kind')}] {item.get('source')}: {helpers._short(str(item.get('text', '')))}"
        )
    return 0


def import_issue_repairs(
    *,
    target: Path,
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    records = ledger_mod._issue_repair_records(target)
    imported, skipped, skipped_dismissed = ledger_mod._append_import_records(target, records, dry_run=dry_run)
    payload = {
        "target": str(target),
        "imports_path": str(helpers._imports_path(target)),
        "dry_run": dry_run,
        "candidate_count": len(records),
        "created": len(imported),
        "imported": len(imported),
        "skipped": len(skipped),
        "skipped_duplicates": len(skipped),
        "dismissed": len(skipped_dismissed),
        "skipped_dismissed": len(skipped_dismissed),
        "invalid": 0,
        "imports": imported,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"issue repair imports: {target}")
    print(f"imports_path: {helpers._imports_path(target)}")
    print(f"dry_run: {dry_run}")
    print(f"candidates: {len(records)}")
    print(f"imported: {len(imported)}")
    print(f"skipped_duplicates: {len(skipped)}")
    if skipped_dismissed:
        print(f"skipped_dismissed: {len(skipped_dismissed)}")
    for item in imported:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        print(f"- {item.get('id')} {metadata.get('issue_type')}: {helpers._short(str(item.get('text', '')))}")
    return 0


def import_memory_care(
    *,
    target: Path,
    queue: Path | None = None,
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    return _import_memory_refresh_queue(
        target=target,
        queue=queue,
        dry_run=dry_run,
        json_output=json_output,
        source="memory-care",
        command_name="memory-care",
    )


def import_memory_refresh(
    *,
    target: Path,
    queue: Path | None = None,
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    return _import_memory_refresh_queue(
        target=target,
        queue=queue,
        dry_run=dry_run,
        json_output=json_output,
        source="memory-refresh",
        command_name="memory-refresh",
    )


def _memory_refresh_cards(payload: dict[str, Any], *, queue_path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    cards = payload.get("cards")
    if cards is None:
        cards = payload.get("candidates")
    if cards is None:
        cards = payload.get("refresh_candidates", [])
    if not isinstance(cards, list):
        return [], [f"memory-refresh queue `cards` must be a list: {queue_path}"]

    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, card in enumerate(cards, start=1):
        label = f"memory-refresh card entry {index}"
        if not isinstance(card, dict):
            errors.append(f"{label} must be an object")
            continue
        card_file = (
            ledger_mod._string_field(card.get("file"))
            or ledger_mod._string_field(card.get("path"))
            or ledger_mod._string_field(card.get("card_file"))
        )
        card_id = ledger_mod._string_field(card.get("id")) or ledger_mod._string_field(card.get("card_id")) or card_file
        if not card_file:
            errors.append(f"{label} requires file")
            continue
        reason = (
            ledger_mod._string_field(card.get("refresh_reason"))
            or ledger_mod._string_field(card.get("reason"))
            or ledger_mod._string_field(card.get("category"))
            or "stale memory card"
        )
        acceptance = ledger_mod._normalize_acceptance(card.get("acceptance"))
        if not acceptance:
            acceptance = [
                f"Review {card_file} against current source evidence.",
                "Update the memory card or document why no change is needed.",
            ]
        metadata: dict[str, Any] = {
            "card_file": card_file,
            "card_id": card_id,
            "refresh_reason": reason,
            "reason": reason,
            "queue_path": str(queue_path),
        }
        for key in (
            "confidence",
            "evidence_references",
            "evidence_summary",
            "issue_type",
            "review_after",
            "last_reviewed_at",
            "freshness",
            "safe_summary",
            "source",
            "suggested_refresh_action",
            "safe_autofix_plan",
        ):
            value = card.get(key)
            if value not in (None, ""):
                metadata[key] = value
        source_item_key = ledger_mod._string_field(card.get("source_item_key")) or f"memory-refresh:{card_id}"
        record = {
            "text": f"Refresh memory card {card_file}: {reason}",
            "kind": "task",
            "source": "memory-refresh",
            "type": card.get("type") if isinstance(card.get("type"), str) else "docs",
            "priority": card.get("priority") if isinstance(card.get("priority"), str) else "normal",
            "template": card.get("template") if isinstance(card.get("template"), str) else "docs",
            "acceptance": acceptance,
            "metadata": metadata,
        }
        fingerprint = ledger_mod._string_field(card.get("source_fingerprint")) or helpers._stable_hash(
            {
                "card_id": card_id,
                "card_file": card_file,
                "reason": reason,
                "acceptance": acceptance,
                "evidence_summary": metadata.get("evidence_summary"),
                "issue_type": metadata.get("issue_type"),
            }
        )
        metadata["source_item_key"] = source_item_key
        metadata["source_fingerprint"] = fingerprint
        records.append(record)
    return records, errors


def _import_memory_refresh_queue(
    *,
    target: Path,
    queue: Path | None,
    dry_run: bool,
    json_output: bool,
    source: str,
    command_name: str,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    queue_path = (
        queue.expanduser().resolve()
        if queue is not None
        else target / "memory" / "cards" / "decay" / "refresh-queue.json"
    )
    if not queue_path.is_file():
        print(f"error: memory-care refresh queue not found: {queue_path}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(queue_path.read_text())
    except json.JSONDecodeError as exc:
        print(f"error: invalid memory-care refresh queue JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print(f"error: memory-care refresh queue must be an object: {queue_path}", file=sys.stderr)
        return 2
    records, errors = _memory_refresh_cards(payload, queue_path=queue_path)
    if source != "memory-refresh":
        for record in records:
            record["source"] = source
            metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            if isinstance(metadata.get("source_item_key"), str):
                metadata["source_item_key"] = metadata["source_item_key"].replace("memory-refresh:", f"{source}:", 1)
    if errors:
        if json_output:
            print(
                json.dumps(
                    {
                        "queue": str(queue_path),
                        "imports_path": str(helpers._imports_path(target)),
                        "valid": False,
                        "errors": errors,
                        "created": 0,
                        "skipped": 0,
                        "dismissed": 0,
                        "invalid": len(errors),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            for error in errors:
                print(f"error: {error}", file=sys.stderr)
        return 2
    imported, skipped, skipped_dismissed = ledger_mod._append_import_records(target, records, dry_run=dry_run)
    output = {
        "queue": str(queue_path),
        "imports_path": str(helpers._imports_path(target)),
        "dry_run": dry_run,
        "valid": True,
        "queued_cards": len(records),
        "created": len(imported),
        "imported": len(imported),
        "skipped": len(skipped),
        "skipped_duplicates": len(skipped),
        "dismissed": len(skipped_dismissed),
        "skipped_dismissed": len(skipped_dismissed),
        "invalid": 0,
        "imports": imported,
    }
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    print(f"{command_name} queue: {queue_path}")
    print(f"imports_path: {helpers._imports_path(target)}")
    print(f"dry_run: {dry_run}")
    print(f"queued_cards: {len(records)}")
    print(f"imported: {len(imported)}")
    print(f"skipped_duplicates: {len(skipped)}")
    if skipped_dismissed:
        print(f"skipped_dismissed: {len(skipped_dismissed)}")
    for item in imported:
        print(f"- {item.get('id')} {helpers._short(str(item.get('text', '')))}")
    return 0


def _safe_chat_metadata(issue: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    metadata = issue.get("metadata", {})
    if metadata is None:
        metadata = {}
    safe: dict[str, Any] = {}
    omitted: list[str] = []
    if isinstance(metadata, dict):
        for key, value in metadata.items():
            normalized = str(key).strip().casefold()
            if normalized in constants.RAW_CHAT_FIELDS or normalized.startswith("raw_"):
                omitted.append(str(key))
                continue
            safe[str(key)] = value
    for source_key, dest_key in (
        ("provider", "provider"),
        ("surface", "surface"),
        ("workspace", "workspace"),
        ("channel", "channel"),
        ("thread", "thread"),
        ("message_range", "message_range"),
        ("confidence", "confidence"),
        ("evidence_summary", "evidence_summary"),
        ("local_locator", "local_locator"),
    ):
        value = issue.get(source_key)
        if value not in (None, ""):
            safe[dest_key] = value
    for key in constants.RAW_CHAT_FIELDS:
        if key in issue:
            omitted.append(key)
    return safe, sorted(set(omitted))


def _chat_sweep_records(payload: dict[str, Any], *, sweep_path: Path) -> tuple[list[dict[str, Any]], list[str], int]:
    issues = payload.get("issues", [])
    if not isinstance(issues, list):
        return [], [f"chat memory sweep `issues` must be a list: {sweep_path}"], 0

    generated_at = payload.get("generated_at")
    sweep_id = (
        ledger_mod._string_field(payload.get("sweep_id"))
        or ledger_mod._string_field(payload.get("id"))
        or helpers._stable_hash({"path": str(sweep_path), "generated_at": generated_at})
    )
    provider = ledger_mod._string_field(payload.get("provider"))
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, issue in enumerate(issues, start=1):
        label = f"chat memory sweep issue {index}"
        if not isinstance(issue, dict):
            errors.append(f"{label} must be an object")
            continue
        title = ledger_mod._string_field(issue.get("title"))
        if not title:
            errors.append(f"{label} requires title")
            continue
        issue_id = (
            ledger_mod._string_field(issue.get("id"))
            or ledger_mod._string_field(issue.get("issue_id"))
            or helpers._stable_hash({"sweep_id": sweep_id, "title": title, "index": index})
        )
        actionable = bool(issue.get("actionable")) or bool(issue.get("task")) or issue.get("kind") == "task"
        kind = "task" if actionable else issue.get("kind", "incident")
        if not isinstance(kind, str) or kind not in constants.IMPORT_KINDS:
            errors.append(f"{label} kind must be one of: {', '.join(constants.IMPORT_KINDS)}")
            continue
        metadata = issue.get("metadata", {})
        if metadata is not None and not isinstance(metadata, dict):
            errors.append(f"{label} metadata must be an object")
            continue

        safe_metadata, omitted_fields = _safe_chat_metadata(issue)
        if provider and "provider" not in safe_metadata:
            safe_metadata["provider"] = provider
        summary = ledger_mod._string_field(issue.get("summary"))
        evidence_summary = ledger_mod._string_field(issue.get("evidence_summary"))
        severity = ledger_mod._string_field(issue.get("severity"))
        issue_source = ledger_mod._string_field(issue.get("source"))
        rendered_title = title
        severity_prefix = f" [{severity}]" if severity else ""
        if actionable:
            text = f"Review chat memory sweep task{severity_prefix} {rendered_title}"
        else:
            text = f"Review memory sweep issue{severity_prefix} {rendered_title}"
        if summary:
            text = f"{text}: {summary}"

        record_metadata = dict(safe_metadata)
        record_metadata.update(
            {
                "sweep_id": sweep_id,
                "sweep_issue_id": issue_id,
                "source_item_key": f"chat-memory-sweep:{sweep_id}:{issue_id}",
                "sweep_path": str(sweep_path),
                "issue_title": rendered_title,
            }
        )
        if issue_source:
            record_metadata["issue_source"] = issue_source
        if severity:
            record_metadata["severity"] = severity
        if evidence_summary:
            record_metadata["evidence_summary"] = evidence_summary
        if isinstance(generated_at, str) and generated_at.strip():
            record_metadata["generated_at"] = generated_at.strip()
        if omitted_fields:
            record_metadata["private_fields_omitted"] = omitted_fields
        acceptance = ledger_mod._normalize_acceptance(issue.get("acceptance"))
        if actionable and not acceptance:
            acceptance = [
                "Review the sweep summary and local evidence locator.",
                "Promote only public-safe conclusions or create a memory handoff.",
            ]
        fingerprint_payload = {
            "title": title,
            "summary": summary,
            "kind": kind,
            "severity": severity,
            "source": issue_source,
            "acceptance": acceptance,
            "evidence_summary": evidence_summary,
            "metadata": {
                key: value
                for key, value in record_metadata.items()
                if key not in {"sweep_path", "source_fingerprint", "private_fields_omitted"}
            },
        }
        record_metadata["source_fingerprint"] = helpers._stable_hash(fingerprint_payload)
        record: dict[str, Any] = {
            "text": text,
            "kind": kind,
            "source": "chat-memory-sweep",
            "metadata": record_metadata,
        }
        if kind == "task":
            record["type"] = issue.get("type") if isinstance(issue.get("type"), str) else "workflow"
            record["priority"] = issue.get("priority") if isinstance(issue.get("priority"), str) else "normal"
            record["template"] = issue.get("template") if isinstance(issue.get("template"), str) else "vertical-slice"
            record["acceptance"] = acceptance
        records.append(record)
    return records, errors, len(issues)


def import_chat_sweep(
    *,
    target: Path,
    input_path: Path | None = None,
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    sweep_path = (
        input_path.expanduser().resolve()
        if input_path is not None
        else target / ".brigade" / "chat-memory-sweeps" / "latest.json"
    )
    if not sweep_path.is_file():
        print(f"error: chat memory sweep not found: {sweep_path}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(sweep_path.read_text())
    except json.JSONDecodeError as exc:
        print(f"error: invalid chat memory sweep JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print(f"error: chat memory sweep must be an object: {sweep_path}", file=sys.stderr)
        return 2
    records, errors, issue_count = _chat_sweep_records(payload, sweep_path=sweep_path)
    if errors:
        output = {
            "input": str(sweep_path),
            "imports_path": str(helpers._imports_path(target)),
            "valid": False,
            "errors": errors,
            "created": 0,
            "skipped": 0,
            "dismissed": 0,
            "invalid": len(errors),
        }
        if json_output:
            print(json.dumps(output, indent=2, sort_keys=True))
        else:
            for error in errors:
                print(f"error: {error}", file=sys.stderr)
        return 2

    imported, skipped, skipped_dismissed = ledger_mod._append_import_records(target, records, dry_run=dry_run)
    output = {
        "input": str(sweep_path),
        "imports_path": str(helpers._imports_path(target)),
        "dry_run": dry_run,
        "valid": True,
        "issues": issue_count,
        "created": len(imported),
        "imported": len(imported),
        "skipped": len(skipped),
        "skipped_duplicates": len(skipped),
        "dismissed": len(skipped_dismissed),
        "skipped_dismissed": len(skipped_dismissed),
        "invalid": 0,
        "imports": imported,
    }
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    print(f"chat memory sweep: {sweep_path}")
    print(f"imports_path: {helpers._imports_path(target)}")
    print(f"dry_run: {dry_run}")
    print(f"issues: {issue_count}")
    print(f"imported: {len(imported)}")
    print(f"skipped_duplicates: {len(skipped)}")
    if skipped_dismissed:
        print(f"skipped_dismissed: {len(skipped_dismissed)}")
    for item in imported:
        print(f"- {item.get('id')} [{item.get('kind')}] {helpers._short(str(item.get('text', '')))}")
    return 0


def _content_guard_import_records(result: dict[str, Any]) -> list[dict[str, Any]]:
    exit_code = int(result.get("exit_code") or 0)
    if exit_code == 0:
        return []
    target = str(result.get("target") or "")
    policy = str(result.get("policy") or "public-repo")
    stdout_summary = _scanner_run_summary(str(result.get("stdout") or ""), limit=12)
    stderr_summary = _scanner_run_summary(str(result.get("stderr") or ""), limit=8)
    detail = str(result.get("detail") or "content-guard reported findings")
    metadata = {
        "scanner_id": "content-guard",
        "scanner_source": "content-guard",
        "policy": policy,
        "scan_target": target,
        "exit_code": exit_code,
        "detail": detail,
        "stdout_summary": stdout_summary,
        "stderr_summary": stderr_summary,
        "source_item_key": f"content-guard:{policy}:{target}",
        "source_fingerprint": helpers._stable_hash(
            {
                "policy": policy,
                "target": target,
                "exit_code": exit_code,
                "stdout": stdout_summary,
                "stderr": stderr_summary,
            }
        ),
    }
    return [
        {
            "text": f"Review Content Guard findings for {target} using policy {policy}: {detail}",
            "kind": "finding",
            "source": "content-guard",
            "metadata": metadata,
        }
    ]


def import_content_guard(
    *,
    target: Path,
    scan_target: Path | None = None,
    policy: str = "public-repo",
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    effective_scan_target = scan_target.expanduser().resolve() if scan_target is not None else target
    result = scrub.run_scan(effective_scan_target, repo_target=target, policy=policy)
    records = _content_guard_import_records(result)
    imported, skipped, skipped_dismissed = ledger_mod._append_import_records(target, records, dry_run=dry_run)
    output = {
        "target": str(target),
        "scan_target": str(effective_scan_target),
        "policy": policy,
        "dry_run": dry_run,
        "scan": {
            "available": result.get("available"),
            "status": result.get("status"),
            "exit_code": result.get("exit_code"),
            "detail": result.get("detail"),
            "stdout_summary": _scanner_run_summary(str(result.get("stdout") or ""), limit=12),
            "stderr_summary": _scanner_run_summary(str(result.get("stderr") or ""), limit=8),
        },
        "imports_path": str(helpers._imports_path(target)),
        "created": len(imported),
        "imported": len(imported),
        "skipped": len(skipped),
        "dismissed": len(skipped_dismissed),
        "imports": imported,
    }
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0 if result.get("available") else 2
    print(f"content-guard import: {effective_scan_target}")
    print(f"policy: {policy}")
    print(f"scan: {result.get('status')} ({result.get('detail')})")
    print(f"imported: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    if skipped_dismissed:
        print(f"skipped_dismissed: {len(skipped_dismissed)}")
    for item in imported:
        print(f"- {item.get('id')} [{item.get('kind')}] {helpers._short(str(item.get('text', '')))}")
    return 0 if result.get("available") else 2


def import_triage(
    *,
    target: Path,
    json_output: bool = False,
    limit: int = 50,
    source: str | None = None,
    kind: str | None = None,
    metadata: list[str] | None = None,
) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    if kind is not None and kind not in constants.IMPORT_KINDS:
        print(f"error: --kind must be one of: {', '.join(constants.IMPORT_KINDS)}", file=sys.stderr)
        return 2
    metadata_filters, rc = ledger_mod._parse_or_report_metadata_filters(metadata)
    if rc:
        return rc
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    pending = ledger_mod._matching_pending_imports(target, kind=kind, source=source, metadata_filters=metadata_filters)
    counts = ledger_mod._import_counts(pending)
    groups: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for item in pending:
        source = str(item.get("source") or "manual")
        kind = str(item.get("kind") or "task")
        groups.setdefault(source, {}).setdefault(kind, []).append(item)

    if json_output:
        print(
            json.dumps(
                {
                    "imports_path": str(helpers._imports_path(target)),
                    "counts": counts,
                    "groups": groups,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    print(f"work import triage: {target}")
    print(f"imports_path: {helpers._imports_path(target)}")
    print(f"pending_imports: {counts['total']}")
    if not pending:
        return 0
    print("sources:")
    for source, by_kind in sorted(groups.items()):
        source_count = sum(len(items) for items in by_kind.values())
        print(f"- {source}: {source_count}")
        for kind, items in sorted(by_kind.items()):
            print(f"  {kind}: {len(items)}")
            for item in items[:limit]:
                print(f"    - {item.get('id')} {helpers._short(str(item.get('text', '')))}")
            if len(items) > limit:
                print(f"    ... {len(items) - limit} more")
    return 0


def _metadata_has_any(metadata: dict[str, Any], keys: set[str]) -> bool:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, list) and value:
            return True
        if isinstance(value, dict) and value:
            return True
        if isinstance(value, (int, float, bool)):
            return True
    return False


def _provenance_audit_sources(target: Path) -> set[str]:
    sources = set(constants.PROVENANCE_AUDIT_SOURCES)
    sources.update(_scanner_source_map(target))
    return sources


def _provenance_audit_item(
    item: dict[str, Any],
    *,
    scanner_sources: dict[str, dict[str, Any]],
    audited_sources: set[str],
) -> dict[str, Any] | None:
    source = str(item.get("source") or "manual")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    is_configured_scanner = source in scanner_sources
    if source not in audited_sources and not is_configured_scanner:
        return None

    missing: list[str] = []
    source_identity = ledger_mod._import_source_identity(item)
    fingerprint = ledger_mod._import_fingerprint(item)
    explicit_fingerprint = metadata.get("source_fingerprint")
    has_explicit_fingerprint = isinstance(explicit_fingerprint, str) and bool(explicit_fingerprint.strip())
    if source_identity is None:
        missing.append("source_item_key")
    if not has_explicit_fingerprint:
        missing.append("source_fingerprint")
    if not _metadata_has_any(metadata, constants.PROVENANCE_SAFE_SUMMARY_KEYS):
        missing.append("safe_summary")
    if not _metadata_has_any(metadata, constants.PROVENANCE_EVIDENCE_KEYS):
        missing.append("evidence_reference")

    if is_configured_scanner:
        for key in ("scanner_id", "scanner_source", "scanner_run_id"):
            if not metadata.get(key):
                missing.append(key)

    missing = sorted(set(missing))
    return {
        "id": item.get("id"),
        "source": source,
        "kind": item.get("kind", "task"),
        "status": item.get("status", "pending"),
        "producer": "scanner" if is_configured_scanner else source,
        "source_identity": list(source_identity) if source_identity else None,
        "source_fingerprint": explicit_fingerprint.strip() if has_explicit_fingerprint else None,
        "effective_source_fingerprint": fingerprint,
        "has_source_identity": source_identity is not None,
        "has_source_fingerprint": has_explicit_fingerprint,
        "has_safe_summary": "safe_summary" not in missing,
        "has_evidence_reference": "evidence_reference" not in missing,
        "dismissed_until_changed_ready": source_identity is not None and has_explicit_fingerprint,
        "provenance_complete": not missing,
        "missing_fields": missing,
    }


def _import_provenance_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    imports = [item for item in ledger_mod._read_imports(target) if isinstance(item, dict)]
    scanner_sources = _scanner_source_map(target)
    audited_sources = _provenance_audit_sources(target)
    items = [
        audit
        for item in imports
        if (audit := _provenance_audit_item(item, scanner_sources=scanner_sources, audited_sources=audited_sources))
        is not None
    ]
    missing_by_field: dict[str, int] = {}
    missing_by_source: dict[str, int] = {}
    incomplete = [item for item in items if not item["provenance_complete"]]
    for item in incomplete:
        source = str(item.get("source") or "manual")
        missing_by_source[source] = missing_by_source.get(source, 0) + 1
        for field in item.get("missing_fields", []):
            missing_by_field[field] = missing_by_field.get(field, 0) + 1
    return {
        "target": str(target),
        "imports_path": str(helpers._imports_path(target)),
        "audited_source_count": len(audited_sources),
        "import_count": len(imports),
        "audited_import_count": len(items),
        "complete_count": len(items) - len(incomplete),
        "incomplete_count": len(incomplete),
        "missing_by_field": dict(sorted(missing_by_field.items())),
        "missing_by_source": dict(sorted(missing_by_source.items())),
        "items": items,
        "issues": incomplete,
    }


def import_provenance(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _import_provenance_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"work import provenance: {target}")
    print(f"imports_path: {payload['imports_path']}")
    print(f"audited_imports: {payload['audited_import_count']}")
    print(f"complete: {payload['complete_count']}")
    print(f"incomplete: {payload['incomplete_count']}")
    if payload["missing_by_field"]:
        print("missing_by_field:")
        for field, count in payload["missing_by_field"].items():
            print(f"  {field}: {count}")
    if payload["missing_by_source"]:
        print("missing_by_source:")
        for source, count in payload["missing_by_source"].items():
            print(f"  {source}: {count}")
    for item in payload["issues"][:20]:
        fields = ", ".join(str(field) for field in item.get("missing_fields", []))
        print(f"- {item.get('id')} {item.get('source')} {item.get('kind')} missing={fields}")
    return 0


def _inbox_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    pending = ledger_mod._pending_imports(target)
    now = helpers._now()
    summaries = [ledger_mod._import_summary(item, now=now) for item in pending]
    by_source: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    acceptance = {"ready": 0, "missing": 0}
    handoff_ready = 0
    stale: list[dict[str, Any]] = []
    for summary in summaries:
        source = str(summary.get("source") or "manual")
        kind = str(summary.get("kind") or "task")
        by_source[source] = by_source.get(source, 0) + 1
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if kind == "task":
            priority = str(summary.get("priority") or "normal")
            by_priority[priority] = by_priority.get(priority, 0) + 1
            if summary.get("acceptance_missing"):
                acceptance["missing"] += 1
            else:
                acceptance["ready"] += 1
        elif kind in constants.HANDOFF_READY_KINDS:
            handoff_ready += 1
        age_hours = summary.get("age_hours")
        if isinstance(age_hours, (int, float)) and age_hours > constants.IMPORT_STALE_HOURS:
            stale.append(summary)
    candidate = ledger_mod._scanner_candidate(pending)
    handoff_candidate = ledger_mod._handoff_candidate(pending)
    return {
        "target": str(target),
        "imports_path": str(helpers._imports_path(target)),
        "counts": {
            "total": len(summaries),
            "by_source": dict(sorted(by_source.items())),
            "by_kind": dict(sorted(by_kind.items())),
            "by_priority": dict(sorted(by_priority.items())),
            "acceptance": acceptance,
            "handoff_ready": handoff_ready,
            "stale": len(stale),
        },
        "candidate": ledger_mod._import_summary(candidate, now=now) if candidate else None,
        "handoff_candidate": ledger_mod._import_summary(handoff_candidate, now=now) if handoff_candidate else None,
        "imports": summaries,
    }


def _scanner_source_map(target: Path) -> dict[str, dict[str, Any]]:
    scanners, errors = config_mod._load_scanner_config(target)
    if errors:
        return {}
    by_source: dict[str, dict[str, Any]] = {}
    for scanner in scanners:
        for key in ("source", "id"):
            value = scanner.get(key)
            if isinstance(value, str) and value.strip():
                by_source[value.strip()] = scanner
    return by_source


def _import_hygiene_issue(status: str, name: str, detail: str, items: list[str] | None = None) -> dict[str, Any]:
    issue: dict[str, Any] = {"status": status, "name": name, "detail": detail}
    if items is not None:
        issue["items"] = items
    return issue


def _inbox_hygiene_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    imports = [item for item in ledger_mod._read_imports(target) if isinstance(item, dict)]
    scanner_sources = _scanner_source_map(target)
    checks: list[dict[str, Any]] = []
    now: datetime | None = None

    def current_now() -> datetime:
        nonlocal now
        if now is None:
            now = helpers._now()
        return now

    missing_provenance: list[str] = []
    for item in imports:
        if item.get("status", "pending") != "pending":
            continue
        source = str(item.get("source") or "")
        if source not in scanner_sources:
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        required = ("scanner_id", "scanner_source", "source_fingerprint")
        if any(not metadata.get(key) for key in required):
            missing_provenance.append(str(item.get("id")))
    checks.append(
        _import_hygiene_issue(
            constants.WARN if missing_provenance else constants.OK,
            "inbox_missing_provenance",
            f"{len(missing_provenance)} pending scanner import(s) missing provenance"
            if missing_provenance
            else "pending scanner imports have provenance",
            missing_provenance[:10],
        )
    )

    stale_pending = [
        str(item.get("id"))
        for item in imports
        if item.get("status", "pending") == "pending"
        and (created := helpers._parse_iso_datetime(item.get("created_at"))) is not None
        and (current_now() - created).total_seconds() / 3600 > constants.IMPORT_STALE_HOURS
    ]
    checks.append(
        _import_hygiene_issue(
            constants.WARN if stale_pending else constants.OK,
            "inbox_stale_pending",
            f"{len(stale_pending)} pending import(s) older than {constants.IMPORT_STALE_HOURS}h"
            if stale_pending
            else "none",
            stale_pending[:10],
        )
    )
    stale_handoff_ready = [
        str(item.get("id"))
        for item in imports
        if item.get("status", "pending") == "pending"
        and item.get("kind") in constants.HANDOFF_READY_KINDS
        and (created := helpers._parse_iso_datetime(item.get("created_at"))) is not None
        and (current_now() - created).total_seconds() / 3600 > constants.IMPORT_STALE_HOURS
    ]
    checks.append(
        _import_hygiene_issue(
            constants.WARN if stale_handoff_ready else constants.OK,
            "inbox_stale_handoff_ready",
            f"{len(stale_handoff_ready)} handoff-ready import(s) older than {constants.IMPORT_STALE_HOURS}h"
            if stale_handoff_ready
            else "none",
            stale_handoff_ready[:10],
        )
    )

    task_ids = {
        str(task.get("id"))
        for task in ledger_mod._read_task_ledger(target).get("tasks", [])
        if isinstance(task, dict) and isinstance(task.get("id"), str)
    }
    broken_promoted = [
        str(item.get("id"))
        for item in imports
        if item.get("status") == "promoted"
        and isinstance(item.get("task_id"), str)
        and item.get("task_id") not in task_ids
    ]
    checks.append(
        _import_hygiene_issue(
            constants.WARN if broken_promoted else constants.OK,
            "inbox_promoted_task_missing",
            f"{len(broken_promoted)} promoted import(s) point at missing ledger tasks" if broken_promoted else "none",
            broken_promoted[:10],
        )
    )
    missing_handoff_drafts = [
        str(item.get("id"))
        for item in imports
        if item.get("status") == "promoted"
        and isinstance(item.get("handoff_path"), str)
        and not Path(item["handoff_path"]).expanduser().exists()
    ]
    checks.append(
        _import_hygiene_issue(
            constants.WARN if missing_handoff_drafts else constants.OK,
            "inbox_promoted_handoff_missing",
            f"{len(missing_handoff_drafts)} promoted import(s) point at missing handoff drafts"
            if missing_handoff_drafts
            else "none",
            missing_handoff_drafts[:10],
        )
    )

    by_identity: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in imports:
        identity = ledger_mod._import_source_identity(item)
        if identity is not None:
            by_identity.setdefault(identity, []).append(item)
    changed_dismissed: list[str] = []
    for items in by_identity.values():
        dismissed = [item for item in items if item.get("status") == "dismissed"]
        active = [item for item in items if item.get("status", "pending") in {"pending", "promoted"}]
        active_fingerprints = {
            ledger_mod._import_fingerprint(item) for item in active if ledger_mod._import_fingerprint(item)
        }
        for item in dismissed:
            fingerprint = ledger_mod._import_fingerprint(item)
            if fingerprint and active_fingerprints and fingerprint not in active_fingerprints:
                changed_dismissed.append(str(item.get("id")))
    checks.append(
        _import_hygiene_issue(
            constants.WARN if changed_dismissed else constants.OK,
            "inbox_dismissed_changed",
            f"{len(changed_dismissed)} dismissed import(s) have changed source fingerprints"
            if changed_dismissed
            else "none",
            changed_dismissed[:10],
        )
    )

    by_source: dict[str, dict[str, int]] = {}
    for item in imports:
        source = str(item.get("source") or "manual")
        status = str(item.get("status") or "pending")
        by_source.setdefault(source, {"dismissed": 0, "promoted": 0})
        if status in by_source[source]:
            by_source[source][status] += 1
    noisy_sources = [
        f"{source}=dismissed:{counts['dismissed']},promoted:{counts['promoted']}"
        for source, counts in sorted(by_source.items())
        if counts["dismissed"] >= constants.DISMISSED_SOURCE_WARN_THRESHOLD
        and counts["dismissed"] > max(1, counts["promoted"]) * 2
    ]
    checks.append(
        _import_hygiene_issue(
            constants.WARN if noisy_sources else constants.OK,
            "inbox_noisy_sources",
            ", ".join(noisy_sources) if noisy_sources else "none",
            noisy_sources[:10],
        )
    )

    provenance = _import_provenance_payload(target)
    provenance_missing = [
        str(item.get("id")) for item in provenance["issues"] if item.get("status", "pending") == "pending"
    ]
    checks.append(
        _import_hygiene_issue(
            constants.WARN if provenance_missing else constants.OK,
            "inbox_provenance_contract",
            f"{len(provenance_missing)} pending producer import(s) missing provenance contract fields"
            if provenance_missing
            else "producer imports satisfy the provenance contract",
            provenance_missing[:10],
        )
    )

    no_import_runs: list[str] = []
    scanners, errors = config_mod._load_scanner_config(target)
    scanner_by_id = {str(scanner.get("id")): scanner for scanner in scanners if isinstance(scanner.get("id"), str)}
    if not errors:
        imports_by_run = {
            str(metadata.get("scanner_run_id"))
            for item in imports
            if isinstance((metadata := item.get("metadata")), dict) and metadata.get("scanner_run_id")
        }
        for receipt in _scanner_receipts(target):
            run_id = str(receipt.get("run_id") or "")
            scanner = scanner_by_id.get(str(receipt.get("scanner_id") or ""))
            if not run_id or scanner is None or not scanner.get("import_path"):
                continue
            if receipt.get("status") != "completed":
                continue
            ingest = receipt.get("ingest_output") if isinstance(receipt.get("ingest_output"), dict) else {}
            created = int(ingest.get("created", 0) or 0) if ingest else 0
            stamped = int(receipt.get("provenance_imports_stamped", 0) or 0)
            if run_id not in imports_by_run and created == 0 and stamped == 0:
                no_import_runs.append(run_id)
    checks.append(
        _import_hygiene_issue(
            constants.WARN if no_import_runs else constants.OK,
            "inbox_scanner_run_no_imports",
            f"{len(no_import_runs)} scanner run(s) produced no imports despite configured import_path"
            if no_import_runs
            else "none",
            no_import_runs[:10],
        )
    )

    imports_by_id = {str(item.get("id")): item for item in imports if isinstance(item.get("id"), str)}
    sweep_missing_refs: list[str] = []
    sweep_lost_provenance: list[str] = []
    sweep_unclosed: list[str] = []
    for sweep_report in _scanner_sweeps(target):
        sweep_id = str(sweep_report.get("sweep_id") or "unknown")
        references = _sweep_import_references(sweep_report)
        referenced_pending = False
        for import_id in references.get("created_import_ids", []):
            if not isinstance(import_id, str) or not import_id.strip():
                continue
            item = imports_by_id.get(import_id)
            if item is None:
                sweep_missing_refs.append(f"{sweep_id}:{import_id}")
                continue
            if item.get("status", "pending") == "pending":
                referenced_pending = True
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            required = ("scanner_id", "scanner_source", "scanner_run_id", "source_fingerprint")
            if any(not metadata.get(key) for key in required):
                sweep_lost_provenance.append(f"{sweep_id}:{import_id}")
        if referenced_pending and not _sweep_is_closed(sweep_report):
            sweep_unclosed.append(sweep_id)
    checks.append(
        _import_hygiene_issue(
            constants.WARN if sweep_missing_refs else constants.OK,
            "inbox_sweep_import_missing",
            f"{len(sweep_missing_refs)} sweep import reference(s) missing from inbox" if sweep_missing_refs else "none",
            sweep_missing_refs[:10],
        )
    )
    checks.append(
        _import_hygiene_issue(
            constants.WARN if sweep_lost_provenance else constants.OK,
            "inbox_sweep_import_provenance",
            f"{len(sweep_lost_provenance)} sweep import reference(s) lost provenance"
            if sweep_lost_provenance
            else "none",
            sweep_lost_provenance[:10],
        )
    )
    checks.append(
        _import_hygiene_issue(
            constants.WARN if sweep_unclosed else constants.OK,
            "inbox_sweep_unclosed",
            f"{len(sweep_unclosed)} sweep(s) have pending imports without review closeout"
            if sweep_unclosed
            else "none",
            sweep_unclosed[:10],
        )
    )

    issues = [check for check in checks if check.get("status") != constants.OK]
    return {
        "target": str(target),
        "imports_path": str(helpers._imports_path(target)),
        "archive_path": str(helpers._imports_archive_path(target)),
        "checks": checks,
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def _inbox_quality_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    imports = [item for item in ledger_mod._read_imports(target) if isinstance(item, dict)]
    pending = [item for item in imports if item.get("status", "pending") == "pending"]
    dismissed_by_source = Counter(
        str(item.get("source") or "unknown") for item in imports if item.get("status") == "dismissed"
    )
    promoted_by_source = Counter(
        str(item.get("source") or "unknown") for item in imports if item.get("status") == "promoted"
    )
    noisy_sources = {
        source for source, count in dismissed_by_source.items() if count >= max(3, promoted_by_source[source] * 3)
    }
    by_identity: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in imports:
        identity = ledger_mod._import_source_identity(item)
        if identity is not None:
            by_identity.setdefault(identity, []).append(item)
    changed_dismissed: list[str] = []
    duplicate_pending: list[str] = []
    for items in by_identity.values():
        pending_items = [item for item in items if item.get("status", "pending") == "pending"]
        if len(pending_items) > 1:
            duplicate_pending.extend(str(item.get("id")) for item in pending_items[1:])
        dismissed_items = [item for item in items if item.get("status") == "dismissed"]
        active_fingerprints = {
            ledger_mod._import_fingerprint(item) for item in pending_items if ledger_mod._import_fingerprint(item)
        }
        for item in dismissed_items:
            fingerprint = ledger_mod._import_fingerprint(item)
            if fingerprint and active_fingerprints and fingerprint not in active_fingerprints:
                changed_dismissed.append(str(item.get("id")))

    scored: list[dict[str, Any]] = []
    now = helpers._now()
    for item in pending:
        import_id = str(item.get("id") or "")
        source = str(item.get("source") or "unknown")
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        acceptance = item.get("acceptance") if isinstance(item.get("acceptance"), list) else []
        has_acceptance = bool(acceptance)
        has_provenance = bool(
            metadata.get("source_fingerprint") or metadata.get("scanner_run_id") or item.get("source")
        )
        created = helpers._parse_iso_datetime(item.get("created_at"))
        age_hours = (now - created).total_seconds() / 3600 if created is not None else None
        flags: list[str] = []
        score = 100
        if has_acceptance:
            flags.append("acceptance-ready")
        else:
            flags.append("missing-acceptance")
            score -= 30
        if has_provenance:
            flags.append("provenance-ready")
        else:
            flags.append("missing-provenance")
            score -= 35
        if age_hours is not None and age_hours > constants.IMPORT_STALE_HOURS:
            flags.append("stale")
            score -= 20
        if bool(metadata.get("deferred") or metadata.get("deferred_at") or item.get("deferred_at")):
            flags.append("deferred")
            score -= 45
        if source in noisy_sources:
            flags.append("noisy-source")
            score -= 40
        if import_id in duplicate_pending:
            flags.append("duplicate-pending")
            score -= 30
        scored.append(
            {
                "import_id": import_id,
                "source": source,
                "kind": item.get("kind", "task"),
                "priority": item.get("priority", "normal"),
                "quality_score": max(0, score),
                "quality_flags": flags,
                "acceptance_count": len(acceptance),
                "has_acceptance": has_acceptance,
                "has_provenance": has_provenance,
                "age_hours": round(age_hours, 2) if age_hours is not None else None,
                "source_fingerprint": metadata.get("source_fingerprint"),
            }
        )
    scored.sort(key=lambda item: (int(item.get("quality_score") or 0), str(item.get("import_id") or "")), reverse=True)
    issue_counts = {
        "missing_acceptance": sum(1 for item in scored if "missing-acceptance" in item["quality_flags"]),
        "missing_provenance": sum(1 for item in scored if "missing-provenance" in item["quality_flags"]),
        "stale": sum(1 for item in scored if "stale" in item["quality_flags"]),
        "deferred": sum(1 for item in scored if "deferred" in item["quality_flags"]),
        "noisy_source": sum(1 for item in scored if "noisy-source" in item["quality_flags"]),
        "duplicate_pending": sum(1 for item in scored if "duplicate-pending" in item["quality_flags"]),
        "changed_dismissed": len(changed_dismissed),
    }
    issues = [
        {"status": constants.WARN, "name": f"inbox_quality_{name}", "detail": str(count)}
        for name, count in issue_counts.items()
        if count
    ]
    return {
        "schema_version": 1,
        "schema": {"name": "work-inbox-quality", "version": 1},
        "target": str(target),
        "pending_count": len(pending),
        "scored_imports": scored,
        "best_import": scored[0] if scored else None,
        "issue_counts": issue_counts,
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "noisy_sources": sorted(noisy_sources),
        "changed_dismissed_import_ids": sorted(set(changed_dismissed)),
    }


def inbox(*, target: Path, json_output: bool = False, limit: int = 20) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _inbox_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    counts = payload["counts"]
    print(f"work inbox: {target}")
    print(f"imports_path: {payload['imports_path']}")
    print(f"pending_imports: {counts['total']}")
    if counts["by_source"]:
        print("by_source:")
        for source, count in counts["by_source"].items():
            print(f"  {source}: {count}")
    if counts["by_kind"]:
        print("by_kind:")
        for kind, count in counts["by_kind"].items():
            print(f"  {kind}: {count}")
    if counts["by_priority"]:
        print("task_priorities:")
        for priority, count in counts["by_priority"].items():
            print(f"  {priority}: {count}")
    acceptance = counts["acceptance"]
    print(f"task_acceptance_ready: {acceptance['ready']}")
    print(f"task_acceptance_missing: {acceptance['missing']}")
    print(f"handoff_ready: {counts.get('handoff_ready', 0)}")
    candidate = payload.get("candidate") or payload.get("handoff_candidate")
    if isinstance(candidate, dict):
        print("next:")
        print(f"  import: {candidate.get('id')}")
        print(f"  source: {candidate.get('source')}")
        print(f"  kind: {candidate.get('kind')}")
        if candidate.get("kind") == "task":
            print(f"  priority: {candidate.get('priority')}")
            print(f"  acceptance: {candidate.get('acceptance_count')}")
        print(f"  text: {helpers._short(str(candidate.get('text', '')))}")
        context = candidate.get("context") if isinstance(candidate.get("context"), dict) else {}
        if context:
            rendered = ", ".join(f"{key}={context[key]}" for key in sorted(context))
            print(f"  context: {rendered}")
        print(f"  plan: brigade work import plan {candidate.get('id')}")
        if candidate.get("kind") == "task":
            print(f"  promote: brigade work import promote {candidate.get('id')}")
            print(f"  run: brigade work import promote --run {candidate.get('id')}")
        elif candidate.get("kind") in constants.HANDOFF_READY_KINDS:
            print(f"  plan_handoff: brigade work import plan-handoff {candidate.get('id')}")
            print(f"  promote_handoff: brigade work import promote-handoff {candidate.get('id')}")
        print(f'  dismiss: brigade work import dismiss {candidate.get("id")} --reason "..."')
    imports = payload.get("imports") if isinstance(payload.get("imports"), list) else []
    if imports:
        print("items:")
        for item in imports[:limit]:
            detail = f"[{item.get('kind')}] {item.get('source')}"
            if item.get("kind") == "task":
                detail += f" {item.get('priority')} acceptance={item.get('acceptance_count')}"
            print(f"- {item.get('id')} {detail}: {helpers._short(str(item.get('text', '')))}")
            context = item.get("context") if isinstance(item.get("context"), dict) else {}
            if context:
                rendered = ", ".join(f"{key}={context[key]}" for key in sorted(context))
                print(f"  context: {rendered}")
        if len(imports) > limit:
            print(f"... {len(imports) - limit} more")
    return 0


def inbox_doctor(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _inbox_hygiene_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"work inbox doctor: {target}")
    print(f"imports_path: {payload['imports_path']}")
    print(f"archive_path: {payload['archive_path']}")
    for check in payload["checks"]:
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    return 0


def _archive_import_cutoff(item: dict[str, Any]) -> datetime | None:
    for key in ("updated_at", "dismissed_at", "promoted_at", "created_at"):
        parsed = helpers._parse_iso_datetime(item.get(key))
        if parsed is not None:
            return parsed
    return None


def inbox_archive(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    now = helpers._now()
    imports = [item for item in ledger_mod._read_imports(target) if isinstance(item, dict)]
    archived: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for item in imports:
        status = str(item.get("status", "pending"))
        timestamp = _archive_import_cutoff(item)
        age_hours = (now - timestamp).total_seconds() / 3600 if timestamp is not None else 0
        if status in {"promoted", "dismissed", "superseded"} and age_hours >= constants.IMPORT_ARCHIVE_STALE_HOURS:
            archived_item = dict(item)
            archived_item["archived_at"] = now.isoformat()
            archived_item["archive_reason"] = f"{status}_older_than_{constants.IMPORT_ARCHIVE_STALE_HOURS}h"
            archived.append(archived_item)
        else:
            kept.append(item)
    if archived:
        ledger_mod._append_archived_imports(target, archived)
        ledger_mod._write_imports(target, kept)
    payload = {
        "target": str(target),
        "imports_path": str(helpers._imports_path(target)),
        "archive_path": str(helpers._imports_archive_path(target)),
        "archived": len(archived),
        "kept": len(kept),
        "archived_imports": archived,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"work inbox archive: {target}")
    print(f"imports_path: {payload['imports_path']}")
    print(f"archive_path: {payload['archive_path']}")
    print(f"archived: {payload['archived']}")
    print(f"kept: {payload['kept']}")
    for item in archived[:20]:
        print(f"- {item.get('id')} [{item.get('status')}] {helpers._short(str(item.get('text', '')))}")
    return 0


def import_show(*, target: Path, import_id: str) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    item, _ = ledger_mod._find_import(target, import_id)
    if item is None:
        print(f"error: import not found: {import_id}", file=sys.stderr)
        return 1
    print(f"import: {item.get('id')}")
    print(f"status: {item.get('status', 'pending')}")
    print(f"kind: {item.get('kind', '')}")
    print(f"source: {item.get('source', '')}")
    print(f"created_at: {item.get('created_at', '')}")
    print(f"updated_at: {item.get('updated_at', '')}")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    if metadata:
        print("metadata:")
        for key in sorted(metadata):
            print(f"  {key}: {metadata[key]}")
    if item.get("promoted_at"):
        print(f"promoted_at: {item['promoted_at']}")
    if item.get("task_id"):
        print(f"task: {item['task_id']}")
    if item.get("handoff_path"):
        print(f"handoff: {item['handoff_path']}")
    if item.get("handoff_target_document"):
        print(f"handoff_target_document: {item['handoff_target_document']}")
    print(f"text: {item.get('text', '')}")
    return 0


def _import_plan_payload(target: Path, import_id: str) -> tuple[dict[str, Any] | None, int]:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return None, 2
    item, _ = ledger_mod._find_import(target, import_id)
    if item is None:
        print(f"error: import not found: {import_id}", file=sys.stderr)
        return None, 1
    summary = ledger_mod._import_summary(item)
    payload: dict[str, Any] = {
        "target": str(target),
        "imports_path": str(helpers._imports_path(target)),
        "import": summary,
        "suggested_promote_command": f"brigade work import promote {item.get('id')}",
        "suggested_dismiss_command": f'brigade work import dismiss {item.get("id")} --reason "..."',
    }
    if item.get("kind") == "task":
        task = ledger_mod._task_preview_from_import(item)
        template = task.get("template") if isinstance(task.get("template"), str) else None
        payload["task"] = task
        if template:
            payload["guidance"] = list(constants.TASK_TEMPLATES.get(template, {}).get("guidance", ()))
        payload["suggested_run_command"] = f"brigade work import promote --run {item.get('id')}"
        payload["recommended_action"] = "promote-task"
    elif item.get("kind") in constants.HANDOFF_READY_KINDS:
        handoff = ledger_mod._import_handoff_plan_payload(target, item)
        payload["handoff"] = {
            "ready": handoff["handoff_ready"],
            "target_document": handoff["target_document"],
            "handoff_type": handoff["handoff_type"],
            "handoff_inbox": handoff["handoff_inbox"],
            "blockers": handoff["blockers"],
            "provenance": handoff["provenance"],
        }
        payload["recommended_action"] = "promote-handoff" if handoff["handoff_ready"] else "dismiss-or-fix"
        payload["suggested_promote_handoff_command"] = handoff["suggested_promote_handoff_command"]
    else:
        payload["recommended_action"] = "dismiss-or-fix"
    return payload, 0


def import_plan(*, target: Path, import_id: str, json_output: bool = False) -> int:
    payload, rc = _import_plan_payload(target, import_id)
    if payload is None:
        return rc
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    item = payload["import"]
    print(f"import: {item.get('id')}")
    print(f"status: {item.get('status')}")
    print(f"kind: {item.get('kind')}")
    print(f"source: {item.get('source')}")
    print(f"text: {item.get('text')}")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    if metadata:
        print("metadata:")
        for key in sorted(metadata):
            print(f"  {key}: {metadata[key]}")
    task = payload.get("task")
    if isinstance(task, dict):
        print("task:")
        print(f"  type: {task.get('type')}")
        print(f"  priority: {task.get('priority')}")
        if task.get("template"):
            print(f"  template: {task['template']}")
        acceptance = task.get("acceptance") if isinstance(task.get("acceptance"), list) else []
        print(f"  acceptance: {len(acceptance)}")
        for criterion in acceptance:
            print(f"    - {criterion}")
    if payload.get("guidance"):
        print("guidance:")
        for item in payload["guidance"]:
            print(f"  - {item}")
    handoff = payload.get("handoff")
    if isinstance(handoff, dict):
        print("handoff:")
        print(f"  ready: {handoff.get('ready')}")
        print(f"  target_document: {handoff.get('target_document')}")
        print(f"  type: {handoff.get('handoff_type')}")
        blockers = handoff.get("blockers") if isinstance(handoff.get("blockers"), list) else []
        if blockers:
            print("  blockers:")
            for blocker in blockers:
                print(f"    - {blocker}")
    if payload.get("recommended_action"):
        print(f"recommended: {payload['recommended_action']}")
    print(f"promote: {payload['suggested_promote_command']}")
    if payload.get("suggested_promote_handoff_command"):
        print(f"handoff: {payload['suggested_promote_handoff_command']}")
    if payload.get("suggested_run_command"):
        print(f"run: {payload['suggested_run_command']}")
    print(f"dismiss: {payload['suggested_dismiss_command']}")
    return 0


def import_plan_handoff(*, target: Path, import_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    item, _ = ledger_mod._find_import(target, import_id)
    if item is None:
        print(f"error: import not found: {import_id}", file=sys.stderr)
        return 1
    payload = ledger_mod._import_handoff_plan_payload(target, item)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["handoff_ready"] else 2
    source = payload["import"].get("source") if isinstance(payload.get("import"), dict) else item.get("source")
    kind = payload["import"].get("kind") if isinstance(payload.get("import"), dict) else item.get("kind")
    print(f"import: {item.get('id')}")
    print(f"status: {item.get('status', 'pending')}")
    print(f"kind: {kind}")
    print(f"source: {source}")
    print(f"text: {ledger_mod._handoff_safe_text(item.get('text') or '')}")
    print(f"handoff_ready: {payload['handoff_ready']}")
    print(f"handoff_inbox: {payload['handoff_inbox']}")
    print(f"target_document: {payload['target_document']}")
    print(f"type: {payload['handoff_type']}")
    if payload["blockers"]:
        print("blockers:")
        for blocker in payload["blockers"]:
            print(f"  - {blocker}")
    print(f"promote_handoff: {payload['suggested_promote_handoff_command']}")
    print(f"dismiss: {payload['suggested_dismiss_command']}")
    return 0 if payload["handoff_ready"] else 2


def import_promote_handoff(
    *,
    target: Path,
    import_id: str,
    run_after: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    item, imports = ledger_mod._find_import(target, import_id)
    if item is None:
        print(f"error: import not found: {import_id}", file=sys.stderr)
        return 1
    if run_after:
        if item.get("kind") != "task":
            print(f"error: --run requires a task import: {item.get('id')}", file=sys.stderr)
            return 2
        return import_promote(target=target, import_id=str(item.get("id")), run_after=True)
    payload = ledger_mod._import_handoff_plan_payload(target, item)
    if payload["blockers"]:
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            for blocker in payload["blockers"]:
                print(f"error: {blocker}", file=sys.stderr)
        return 2
    target_document = str(payload["target_document"])
    handoff_path = ledger_mod._write_import_handoff(target, item, target_document)
    from .. import handoff_cmd

    lint_result = handoff_cmd.lint_file(handoff_path)
    if not lint_result.valid:
        try:
            handoff_path.unlink()
        except OSError:
            pass
        failure_payload = dict(payload)
        failure_payload.update(
            {
                "handoff_path": str(handoff_path),
                "lint": lint_result.as_dict(),
                "handoff_ready": False,
                "blockers": [*payload["blockers"], *lint_result.errors],
            }
        )
        if json_output:
            print(json.dumps(failure_payload, indent=2, sort_keys=True))
        else:
            for error in lint_result.errors:
                print(f"error: handoff lint failed: {error}", file=sys.stderr)
        return 2
    ledger_mod._mark_import_handoff_promoted(target, item, handoff_path=handoff_path, target_document=target_document)
    ledger_mod._write_imports(target, imports)
    output = dict(payload)
    output.update(
        {
            "handoff_ready": True,
            "handoff_path": str(handoff_path),
            "lint": lint_result.as_dict(),
            "import": ledger_mod._import_summary(item),
        }
    )
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    print(f"import: {item.get('id')}")
    print(f"status: {item.get('status')}")
    print(f"handoff: {handoff_path}")
    print(f"target_document: {target_document}")
    print("lint: ok")
    return 0


def import_promote(
    *,
    target: Path,
    import_id: str | None = None,
    all_matching: bool = False,
    kind: str | None = None,
    source: str | None = None,
    metadata: list[str] | None = None,
    run_after: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if kind is not None and kind not in constants.IMPORT_KINDS:
        print(f"error: --kind must be one of: {', '.join(constants.IMPORT_KINDS)}", file=sys.stderr)
        return 2
    metadata_filters, rc = ledger_mod._parse_or_report_metadata_filters(metadata)
    if rc:
        return rc
    if all_matching and import_id:
        print("error: pass an import id or --all, not both", file=sys.stderr)
        return 2
    if run_after and all_matching:
        print("error: --run can only be used with one import id", file=sys.stderr)
        return 2
    if all_matching:
        imports = ledger_mod._read_imports(target)
        wanted_ids = {
            item.get("id")
            for item in ledger_mod._matching_pending_imports(
                target,
                kind=kind,
                source=source,
                metadata_filters=metadata_filters,
            )
        }
        promoted: list[tuple[dict[str, Any], dict[str, Any], bool]] = []
        for item in imports:
            if item.get("id") not in wanted_ids:
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            task, created = ledger_mod._mark_import_promoted(target, item)
            promoted.append((item, task, created))
        ledger_mod._write_imports(target, imports)
        created_count = len([item for item in promoted if item[2]])
        print(f"promoted: {len(promoted)}")
        print(f"created: {created_count}")
        print(f"existing: {len(promoted) - created_count}")
        for item, task, created in promoted:
            status = "created" if created else "existing"
            print(
                f"- {item.get('id')} -> {task['id']} [{status} acceptance={len(ledger_mod._task_acceptance(task))}] "
                f"{helpers._short(str(task.get('text', '')))}"
            )
        return 0
    if not import_id:
        print("error: import id is required unless --all is passed", file=sys.stderr)
        return 2
    item, imports = ledger_mod._find_import(target, import_id)
    if item is None:
        print(f"error: import not found: {import_id}", file=sys.stderr)
        return 1
    if item.get("status", "pending") != "pending":
        print(f"error: import is not pending: {item.get('id')} ({item.get('status')})", file=sys.stderr)
        return 2
    if run_after and item.get("kind") != "task":
        print(f"error: --run requires a task import: {item.get('id')}", file=sys.stderr)
        return 2
    text = str(item.get("text") or "").strip()
    if not text:
        print(f"error: import has no text: {import_id}", file=sys.stderr)
        return 2
    task, created = ledger_mod._mark_import_promoted(target, item)
    ledger_mod._write_imports(target, imports)
    print(f"import: {item.get('id')}")
    print(f"status: {item.get('status')}")
    print(f"task: {task['id']}")
    print(f"created: {created}")
    print(f"acceptance: {len(ledger_mod._task_acceptance(task))}")
    print(f"text: {task['text']}")
    if run_after:
        print("run: starting")
        from brigade import work_cmd as _facade

        return _facade.run(None, target=target, task_id=str(task["id"]))
    return 0


def import_dismiss(
    *,
    target: Path,
    import_id: str | None = None,
    reason: str | None = None,
    all_matching: bool = False,
    kind: str | None = None,
    source: str | None = None,
    metadata: list[str] | None = None,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if kind is not None and kind not in constants.IMPORT_KINDS:
        print(f"error: --kind must be one of: {', '.join(constants.IMPORT_KINDS)}", file=sys.stderr)
        return 2
    metadata_filters, rc = ledger_mod._parse_or_report_metadata_filters(metadata)
    if rc:
        return rc
    if all_matching and import_id:
        print("error: pass an import id or --all, not both", file=sys.stderr)
        return 2
    if all_matching:
        imports = ledger_mod._read_imports(target)
        wanted_ids = {
            item.get("id")
            for item in ledger_mod._matching_pending_imports(
                target,
                kind=kind,
                source=source,
                metadata_filters=metadata_filters,
            )
        }
        now = helpers._now().isoformat()
        dismissed: list[dict[str, Any]] = []
        for item in imports:
            if item.get("id") not in wanted_ids:
                continue
            item["status"] = "dismissed"
            item["updated_at"] = now
            item["dismissed_at"] = now
            if reason and reason.strip():
                item["dismiss_reason"] = reason.strip()
            dismissed.append(item)
        ledger_mod._write_imports(target, imports)
        print(f"dismissed: {len(dismissed)}")
        if reason and reason.strip():
            print(f"reason: {reason.strip()}")
        for item in dismissed:
            print(f"- {item.get('id')} {helpers._short(str(item.get('text', '')))}")
        return 0
    if not import_id:
        print("error: import id is required unless --all is passed", file=sys.stderr)
        return 2
    item, imports = ledger_mod._find_import(target, import_id)
    if item is None:
        print(f"error: import not found: {import_id}", file=sys.stderr)
        return 1
    if item.get("status", "pending") != "pending":
        print(f"error: import is not pending: {item.get('id')} ({item.get('status')})", file=sys.stderr)
        return 2
    now = helpers._now().isoformat()
    item["status"] = "dismissed"
    item["updated_at"] = now
    item["dismissed_at"] = now
    if reason and reason.strip():
        item["dismiss_reason"] = reason.strip()
    ledger_mod._write_imports(target, imports)
    print(f"import: {item.get('id')}")
    print("status: dismissed")
    if item.get("dismiss_reason"):
        print(f"reason: {item['dismiss_reason']}")
    return 0


def backup_init(*, target: Path, force: bool = False, update_gitignore: bool = True) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = helpers._backup_config_path(target)
    if path.exists() and not force:
        print(f"error: backup config already exists: {path}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config_mod._format_backup_toml())
    print(f"backup_config: {path}")
    print(f"destinations: {len(constants.BACKUP_DEFAULTS)}")
    if update_gitignore:
        result = apply_gitignore(target, helpers._work_selection(target, dogfood_cmd.default_handoff_inbox(target)))
        print(f"gitignore: {result}")
    else:
        print("gitignore: skipped")
    print("next_command: brigade work backup status")
    return 0


def backup_contract(*, target: Path, destination_id: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, rc = config_mod._backup_contract_payload(target, destination_id=destination_id)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    print(f"work backup contract: {target}")
    print(f"config_path: {payload['config_path']}")
    print(f"config_loaded: {payload['config_loaded']}")
    if payload.get("config_errors"):
        for error in payload["config_errors"]:
            print(f"config_warning: {error}")
    for error in payload.get("errors", []):
        print(f"error: {error}", file=sys.stderr)
    print(f"destinations: {payload['destination_count']}")
    for destination in payload.get("destinations", []):
        print(f"- {destination.get('id')} [{destination.get('kind')}]")
        print(f"  summary_path: {destination.get('summary_path')}")
        print(f"  command_label: {destination.get('command_label')}")
        print(f"  required_fields: {', '.join(destination.get('required_fields', []))}")
        print(f"  accepted_success_results: {', '.join(destination.get('accepted_success_results', []))}")
    print("would_write: false")
    return rc


def backup_status(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    health = config_mod._backup_health(target)
    if json_output:
        print(json.dumps(health, indent=2, sort_keys=True))
        return 0 if health["valid"] else 1
    print(f"work backup status: {target}")
    print(f"config_path: {health['config_path']}")
    if not health["valid"]:
        for check in health["checks"]:
            if check.get("name") == "backup_config":
                print(f"error: {check.get('detail')}")
        return 1
    destinations = health.get("destinations") if isinstance(health.get("destinations"), list) else []
    print(f"destinations: {len(destinations)}")
    print(f"operator_summary: {health.get('operator_summary')}")
    for destination in destinations:
        if not isinstance(destination, dict):
            continue
        status = "enabled" if destination.get("enabled", True) else "disabled"
        destination_issues = [issue for issue in health["issues"] if issue.get("destination") == destination.get("id")]
        print(f"- {destination.get('id')} [{status}] {destination.get('kind')} issues={len(destination_issues)}")
        print(f"  summary: {destination.get('summary_path')}")
    top_issue = health.get("top_issue")
    if isinstance(top_issue, dict):
        print(f"top_issue: {top_issue.get('destination')}/{top_issue.get('issue_type')} {top_issue.get('detail')}")
    else:
        print("top_issue: none")
    print(f"raw_issues: {health.get('raw_issue_count')}")
    print(f"quieted_issues: {health.get('quieted_issue_count')}")
    print(f"restore_rehearsal_issues: {health.get('restore_rehearsal_issue_count')}")
    return 0


def backup_doctor(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    health = config_mod._backup_health(target)
    if json_output:
        print(json.dumps(health, indent=2, sort_keys=True))
        return 0 if not any(check.get("status") == constants.FAIL for check in health["checks"]) else 1
    print(f"work backup doctor: {target}")
    print(f"config_path: {health['config_path']}")
    for check in health.get("active_checks", health["checks"]):
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    print(f"backup_issues: {health['issue_count']}")
    return 0 if not any(check.get("status") == constants.FAIL for check in health["checks"]) else 1


def backup_import_issues(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    records = config_mod._backup_issue_records(target)
    imported, skipped, skipped_dismissed = ledger_mod._append_import_records(target, records)
    payload = {
        "target": str(target),
        "imports_path": str(helpers._imports_path(target)),
        "issues": len(records),
        "created": len(imported),
        "skipped": len(skipped),
        "dismissed": len(skipped_dismissed),
        "imports": imported,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"backup issue imports: {target}")
    print(f"imports_path: {payload['imports_path']}")
    print(f"issues: {len(records)}")
    print(f"created: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    print(f"dismissed: {len(skipped_dismissed)}")
    for item in imported:
        print(f"- {item.get('id')} [{item.get('kind')}] {helpers._short(str(item.get('text', '')))}")
    return 0


def scanners_init(*, target: Path, force: bool = False, update_gitignore: bool = True) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = helpers._scanner_config_path(target)
    if path.exists() and not force:
        print(f"error: scanner config already exists: {path}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config_mod._format_scanner_toml())
    print(f"scanner_config: {path}")
    print(f"scanners: {len(constants.SCANNER_DEFAULTS)}")
    if update_gitignore:
        result = apply_gitignore(target, helpers._work_selection(target, dogfood_cmd.default_handoff_inbox(target)))
        print(f"gitignore: {result}")
    else:
        print("gitignore: skipped")
    print("next_command: brigade work scanners plan")
    return 0


def backup_closeout(*, target: Path, reason: str | None = None, defer: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    raw_health = config_mod._backup_health(target)
    source_issues = (
        raw_health.get("raw_issues") if isinstance(raw_health.get("raw_issues"), list) else raw_health["issues"]
    )
    fingerprints = [config_mod._backup_issue_fingerprint(issue) for issue in source_issues if isinstance(issue, dict)]
    closeout_id = f"{helpers._now().strftime('%Y%m%d-%H%M%S')}-backup-closeout"
    payload = {
        "closeout_id": closeout_id,
        "created_at": helpers._now().isoformat(),
        "status": "deferred" if defer else "reviewed",
        "reason": reason or "",
        "issue_count": len(source_issues),
        "source_fingerprints": fingerprints,
        "restore_rehearsal_issue_count": raw_health.get("restore_rehearsal_issue_count", 0),
        "safe_summary": f"{len(fingerprints)} backup issue(s) {'deferred' if defer else 'reviewed'}",
    }
    helpers._write_json(config_mod._backup_closeouts_root(target) / closeout_id / "closeout.json", payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"backup_closeout: {closeout_id}")
    print(f"status: {payload['status']}")
    print(f"issues: {payload['issue_count']}")
    return 0


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


def verify_plan(
    *,
    target: Path,
    commands: list[str] | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _verify_plan_payload(target, commands)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if not payload["blockers"] else 1
    print(f"work verify plan: {target}")
    print(f"verify_runs_root: {payload['verify_runs_root']}")
    commands = payload.get("commands") if isinstance(payload.get("commands"), list) else []
    print(f"commands: {len(commands)}")
    for command in commands:
        print(f"- {command}")
    blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
    if blockers:
        print("blockers:")
        for blocker in blockers:
            print(f"  - {blocker}")
    print(f"run: {payload['suggested_command']}")
    return 0 if not blockers else 1


def verify_run(
    *,
    target: Path,
    commands: list[str] | None = None,
    timeout: int = 900,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if timeout < 1:
        print("error: --timeout must be a positive integer", file=sys.stderr)
        return 2
    planned = commands if commands is not None else _default_verify_commands(target)
    if not planned:
        print("error: no verification commands found; pass --command", file=sys.stderr)
        return 2
    receipt, rc = _run_verify_commands(target, planned, timeout)
    if json_output:
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return rc
    print(f"work verify run: {target}")
    print(f"run: {receipt['run_id']}")
    print(f"status: {receipt['status']}")
    print(f"commands: {len(receipt['commands'])}")
    for command in receipt["commands"]:
        if isinstance(command, dict):
            print(f"- {command.get('command')} [{command.get('status')}] exit={command.get('exit_code')}")
    print(f"receipt: {Path(str(receipt['path'])) / 'receipt.json'}")
    return rc


def verify_runs(*, target: Path, json_output: bool = False, limit: int = 20) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    runs = _verify_receipts(target)[:limit]
    payload = {"target": str(target), "verify_runs_root": str(helpers._verify_runs_root(target)), "runs": runs}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"work verify runs: {target}")
    print(f"verify_runs_root: {payload['verify_runs_root']}")
    if not runs:
        print("runs: none")
        return 0
    for run in runs:
        print(
            f"- {run.get('run_id')} [{run.get('status')}] commands={len(run.get('commands') or [])} {run.get('started_at')}"
        )
    return 0


def verify_show(*, target: Path, run_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    run, error = _resolve_verify_receipt(target, run_id)
    if run is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    if json_output:
        print(json.dumps(run, indent=2, sort_keys=True))
        return 0
    print(f"work verify run: {run.get('run_id')}")
    print(f"status: {run.get('status')}")
    print(f"target: {run.get('target')}")
    print(f"started: {run.get('started_at')}")
    print(f"completed: {run.get('completed_at')}")
    for command in run.get("commands", []):
        if isinstance(command, dict):
            print(f"- {command.get('command')} [{command.get('status')}] exit={command.get('exit_code')}")
            if command.get("stdout_summary"):
                print(f"  stdout: {helpers._short(str(command.get('stdout_summary')), 140)}")
            if command.get("stderr_summary"):
                print(f"  stderr: {helpers._short(str(command.get('stderr_summary')), 140)}")
    return 0


def closeout(*, target: Path, session_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, rc = _work_closeout_payload(target, session_id, write=True)
    if payload is None:
        return rc
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    print(f"work closeout: {payload['closeout_id']}")
    print(f"status: {payload['status']}")
    print(f"ready: {payload['ready']}")
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    print(f"session: {session.get('id')}")
    verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else None
    if verification:
        print(f"verification: {verification.get('run_id')} [{verification.get('status')}]")
    blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
    if blockers:
        print("blockers:")
        for blocker in blockers:
            print(f"  - {blocker}")
    if payload.get("path"):
        print(f"receipt: {payload['path']}")
    return rc


def _acceptance_payload(target: Path) -> dict[str, Any]:
    tasks = [task for task in ledger_mod._read_task_ledger(target).get("tasks", []) if isinstance(task, dict)]
    pending = [task for task in tasks if task.get("status", "pending") == "pending"]
    done = [task for task in tasks if task.get("status") == "done"]
    pending_with_acceptance = [task for task in pending if ledger_mod._task_acceptance(task)]
    pending_missing = [task for task in pending if not ledger_mod._task_acceptance(task)]
    done_with_completion = [task for task in done if task.get("completion")]
    done_missing_completion = [task for task in done if not task.get("completion")]
    done_missing_completed_acceptance = [
        task
        for task in done
        if ledger_mod._task_acceptance(task) and not ledger_mod._normalize_acceptance(task.get("completed_acceptance"))
    ]
    review_payload = _review_findings_payload(target)
    review_groups = review_payload.get("groups") if isinstance(review_payload.get("groups"), dict) else {}
    review_outcomes = review_groups.get("by_resolution") if isinstance(review_groups.get("by_resolution"), dict) else {}
    latest_closeout = _latest_work_closeout_payload(target)
    closeout_summary = None
    if latest_closeout is not None:
        closeout_summary = {
            "closeout_id": latest_closeout.get("closeout_id"),
            "status": latest_closeout.get("status"),
            "ready": latest_closeout.get("ready"),
            "path": latest_closeout.get("path"),
            "acceptance_count": len(latest_closeout.get("acceptance_criteria") or []),
            "blocker_count": len(latest_closeout.get("blockers") or []),
        }
    issues: list[dict[str, Any]] = []
    if pending_missing:
        issues.append(
            {
                "status": constants.WARN,
                "name": "acceptance_pending_missing",
                "detail": f"{len(pending_missing)} pending task(s) missing acceptance",
            }
        )
    if done_missing_completion:
        issues.append(
            {
                "status": constants.WARN,
                "name": "acceptance_done_missing_completion",
                "detail": f"{len(done_missing_completion)} done task(s) missing completion evidence",
            }
        )
    if done_missing_completed_acceptance:
        issues.append(
            {
                "status": constants.WARN,
                "name": "acceptance_done_missing_completed_acceptance",
                "detail": f"{len(done_missing_completed_acceptance)} done task(s) missing completion-time acceptance evidence",
            }
        )
    if int(review_payload.get("unresolved_count") or 0) > 0:
        issues.append(
            {
                "status": constants.WARN,
                "name": "acceptance_review_findings_unresolved",
                "detail": f"{review_payload.get('unresolved_count')} review finding(s) unresolved",
            }
        )
    if done and latest_closeout is None:
        issues.append(
            {
                "status": constants.WARN,
                "name": "acceptance_work_closeout_missing",
                "detail": "completed tasks exist but no work closeout receipt was found",
            }
        )
    elif latest_closeout is not None and not latest_closeout.get("ready"):
        issues.append(
            {
                "status": constants.WARN,
                "name": "acceptance_work_closeout_blocked",
                "detail": f"latest work closeout is not ready: {latest_closeout.get('closeout_id')}",
            }
        )
    return {
        "target": str(target),
        "task_count": len(tasks),
        "pending_count": len(pending),
        "done_count": len(done),
        "pending_with_acceptance": [task.get("id") for task in pending_with_acceptance],
        "pending_missing_acceptance": [task.get("id") for task in pending_missing],
        "done_with_completion": [task.get("id") for task in done_with_completion],
        "done_missing_completion": [task.get("id") for task in done_missing_completion],
        "done_missing_completed_acceptance": [task.get("id") for task in done_missing_completed_acceptance],
        "review_findings": {
            "count": review_payload.get("count"),
            "unresolved_count": review_payload.get("unresolved_count"),
            "outcomes": dict(sorted(review_outcomes.items())),
            "top_unresolved": review_payload.get("top_unresolved"),
        },
        "review_finding_pending_count": int(review_outcomes.get("pending") or 0),
        "latest_work_closeout": closeout_summary,
        "coverage": {
            "pending_with_acceptance": len(pending_with_acceptance),
            "pending_missing_acceptance": len(pending_missing),
            "done_with_completion": len(done_with_completion),
            "done_missing_completion": len(done_missing_completion),
            "done_with_completed_acceptance": len(done) - len(done_missing_completed_acceptance),
            "done_missing_completed_acceptance": len(done_missing_completed_acceptance),
            "review_findings_resolved": int(review_payload.get("count") or 0)
            - int(review_payload.get("unresolved_count") or 0),
            "review_findings_unresolved": int(review_payload.get("unresolved_count") or 0),
            "work_closeout_ready": 1 if latest_closeout is not None and latest_closeout.get("ready") else 0,
            "work_closeout_missing": 1 if latest_closeout is None else 0,
        },
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def acceptance(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _acceptance_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"work acceptance: {target}")
    print(f"tasks: {payload['task_count']}")
    print(f"pending_missing_acceptance: {len(payload['pending_missing_acceptance'])}")
    print(f"done_missing_completion: {len(payload['done_missing_completion'])}")
    print(f"done_missing_completed_acceptance: {len(payload['done_missing_completed_acceptance'])}")
    print(f"review_findings_pending: {payload['review_finding_pending_count']}")
    review_findings = payload.get("review_findings") if isinstance(payload.get("review_findings"), dict) else {}
    print(f"review_findings_unresolved: {review_findings.get('unresolved_count', 0)}")
    closeout = payload.get("latest_work_closeout") if isinstance(payload.get("latest_work_closeout"), dict) else None
    print(f"work_closeout: {closeout.get('closeout_id') if closeout else 'none'}")
    return 0


def scanners_list(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    scanners, errors = config_mod._load_scanner_config(target)
    payload = {
        "target": str(target),
        "config_path": str(helpers._scanner_config_path(target)),
        "valid": not errors,
        "errors": errors,
        "scanners": scanners,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if not errors else 1
    print(f"work scanners: {target}")
    print(f"config_path: {helpers._scanner_config_path(target)}")
    if errors:
        print(f"errors: {len(errors)}")
        for error in errors:
            print(f"- {error}")
        return 1
    if not scanners:
        print("scanners: none")
        return 0
    for scanner in scanners:
        status = "enabled" if scanner.get("enabled", True) else "disabled"
        print(f"- {scanner.get('id')} [{status}] {scanner.get('cadence')} source={scanner.get('source')}")
        print(f"  command: {scanner.get('command')}")
        print(f"  output: {scanner.get('output_path')}")
        if scanner.get("import_path"):
            print(f"  import: {scanner.get('import_path')} ({scanner.get('import_format', 'jsonl')})")
    return 0


def scanners_show(*, target: Path, scanner_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    scanners, errors = config_mod._load_scanner_config(target)
    scanner = None
    for item in scanners:
        if item.get("id") == scanner_id:
            scanner = item
            break
    payload = {
        "target": str(target),
        "config_path": str(helpers._scanner_config_path(target)),
        "valid": not errors,
        "errors": errors,
        "scanner": scanner,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if scanner is not None and not errors else 1
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    if scanner is None:
        print(f"error: scanner not found: {scanner_id}", file=sys.stderr)
        return 1
    print(f"scanner: {scanner.get('id')}")
    print(f"enabled: {scanner.get('enabled')}")
    print(f"source: {scanner.get('source')}")
    print(f"cadence: {scanner.get('cadence')}")
    print(f"timeout: {scanner.get('timeout')}")
    print(f"output_path: {scanner.get('output_path')}")
    if scanner.get("import_path"):
        print(f"import_path: {scanner.get('import_path')}")
        print(f"import_format: {scanner.get('import_format', 'jsonl')}")
    print(f"conflict_window: {scanner.get('conflict_window')}")
    print(f"command: {scanner.get('command')}")
    return 0


def scanners_plan(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _scanner_plan_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"work scanners plan: {target}")
    print(f"config_path: {payload['config_path']}")
    if payload["errors"]:
        print(f"errors: {len(payload['errors'])}")
        for error in payload["errors"]:
            print(f"- {error}")
        return 1
    planned = payload.get("planned") if isinstance(payload.get("planned"), list) else []
    if not planned:
        print("planned: none")
    else:
        print("planned:")
        for item in planned:
            print(
                f"- {item.get('id')} {item.get('start')}-{item.get('end')} "
                f"{item.get('cadence')} output={item.get('output_path')}"
            )
    conflicts = payload.get("conflicts") if isinstance(payload.get("conflicts"), list) else []
    if conflicts:
        print("conflicts:")
        for item in conflicts:
            print(f"- {item.get('type')}: {', '.join(str(v) for v in item.get('scanners', []))}")
    else:
        print("conflicts: none")
    suggestions = payload.get("suggestions") if isinstance(payload.get("suggestions"), list) else []
    if suggestions:
        print("suggested_schedule:")
        for item in suggestions:
            print(f"- {item.get('id')}: {item.get('suggested_cadence')}")
    return 0


def scanners_doctor(*, target: Path, json_output: bool = False, import_issues: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    health = _scanner_health(target)
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    skipped_dismissed: list[dict[str, Any]] = []
    if import_issues:
        records = _scanner_health_issue_records(target)
        imported, skipped, skipped_dismissed = ledger_mod._append_import_records(target, records)
        health["import_issues"] = {
            "created": len(imported),
            "skipped": len(skipped),
            "dismissed": len(skipped_dismissed),
            "imports": imported,
        }
    if json_output:
        print(json.dumps(health, indent=2, sort_keys=True))
        return 0 if not any(check.get("status") == constants.FAIL for check in health["checks"]) else 1
    print(f"work scanners doctor: {target}")
    print(f"config_path: {health['config_path']}")
    for check in health["checks"]:
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    next_run = health.get("next_run")
    if isinstance(next_run, dict):
        print(f"next_scanner: {next_run.get('id')} {next_run.get('start')} {next_run.get('cadence')}")
    if import_issues:
        print(f"imported_issues: {len(imported)}")
        print(f"skipped_issues: {len(skipped)}")
        print(f"dismissed_issues: {len(skipped_dismissed)}")
    return 0 if not any(check.get("status") == constants.FAIL for check in health["checks"]) else 1


def _select_scanners_for_run(
    target: Path,
    *,
    scanner_id: str | None,
    all_matching: bool,
    due: bool,
    include_disabled: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    scanners, errors = config_mod._load_scanner_config(target)
    if errors:
        return [], [], errors
    if scanner_id:
        selected = [item for item in scanners if item.get("id") == scanner_id]
        if not selected:
            return [], [], [f"scanner not found: {scanner_id}"]
    elif all_matching or due:
        selected = list(scanners)
    else:
        return [], [], ["scanner id, --all, or --due is required"]
    runnable: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for scanner in selected:
        if not scanner.get("enabled", True) and not include_disabled:
            if scanner_id:
                return [], [], [f"scanner disabled: {scanner_id}"]
            skipped.append({"scanner": scanner, "reason": "disabled"})
            continue
        if due and not _scanner_is_due(target, scanner):
            skipped.append({"scanner": scanner, "reason": "not_due"})
            continue
        runnable.append(scanner)
    return runnable, skipped, []


def _scanners_run_payload(
    *,
    target: Path,
    scanner_id: str | None = None,
    all_matching: bool = False,
    due: bool = False,
    include_disabled: bool = False,
    force: bool = False,
    ingest_output: bool = False,
    require_selector: bool = True,
) -> tuple[dict[str, Any], int]:
    target = target.expanduser().resolve()
    if not target.is_dir():
        return {
            "target": str(target),
            "errors": [f"--target is not a directory: {target}"],
            "runs": [],
            "skipped": [],
        }, 2
    selector_count = sum(1 for item in (scanner_id, all_matching, due) if bool(item))
    if require_selector and selector_count != 1:
        error = "pass exactly one of scanner id, --all, or --due"
        return {"target": str(target), "errors": [error], "runs": [], "skipped": []}, 2
    if not require_selector and selector_count > 1:
        error = "pass only one of scanner id, --all, or --due"
        return {"target": str(target), "errors": [error], "runs": [], "skipped": []}, 2
    if not helpers._scanner_config_path(target).is_file():
        error = f"scanner config missing: {helpers._scanner_config_path(target)}"
        return {"target": str(target), "errors": [error], "runs": [], "skipped": []}, 2
    running = _scanner_running_receipts(target)
    if running and not force:
        error = f"scanner run already in progress: {running[0].get('run_id')}"
        return {"target": str(target), "errors": [error], "runs": [], "skipped": []}, 2
    selected, skipped, errors = _select_scanners_for_run(
        target,
        scanner_id=scanner_id,
        all_matching=all_matching,
        due=due,
        include_disabled=include_disabled,
    )
    if errors:
        return {"target": str(target), "errors": errors, "runs": [], "skipped": skipped}, 2
    before_counts = ledger_mod._import_counts(ledger_mod._pending_imports(target))
    runs: list[dict[str, Any]] = []
    contexts: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for scanner in selected:
        before_ids = {
            str(item.get("id"))
            for item in ledger_mod._read_imports(target)
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        run = _scanner_run_one(target, scanner, force=force)
        stamped_ids = _scanner_stamp_new_imports(target=target, scanner=scanner, run=run, before_ids=before_ids)
        run["provenance_imports_stamped"] = len(stamped_ids)
        if stamped_ids:
            run["stamped_import_ids"] = stamped_ids
        if run.get("path"):
            helpers._write_json(Path(str(run["path"])) / "receipt.json", run)
        runs.append(run)
        contexts.append((scanner, run))
    ingest_errors: list[str] = []
    ingest_payloads: list[tuple[dict[str, Any], dict[str, Any], Path, list[dict[str, Any]]]] = []
    if ingest_output:
        for scanner, run in contexts:
            if run.get("status") != "completed":
                continue
            path, records, errors = _scanner_validate_import_output(target, scanner)
            if errors:
                ingest_errors.extend(errors)
                continue
            if path is not None:
                ingest_payloads.append(
                    (
                        scanner,
                        run,
                        path,
                        _scanner_enrich_import_records(target=target, scanner=scanner, run=run, records=records),
                    )
                )
        if ingest_errors:
            after_counts = ledger_mod._import_counts(ledger_mod._pending_imports(target))
            payload = {
                "target": str(target),
                "runs_root": str(helpers._scanner_runs_root(target)),
                "selected": len(selected),
                "completed": len([run for run in runs if run.get("status") == "completed"]),
                "failed": len([run for run in runs if run.get("status") != "completed"]),
                "skipped": [
                    {"scanner_id": item["scanner"].get("id"), "reason": item["reason"]}
                    for item in skipped
                    if isinstance(item.get("scanner"), dict)
                ],
                "imports_before": before_counts,
                "imports_after": after_counts,
                "ingest_output": True,
                "ingest_errors": ingest_errors,
                "runs": runs,
            }
            return payload, 2
        for scanner, run, path, records in ingest_payloads:
            imported, skipped_records, skipped_dismissed = ledger_mod._append_import_records(target, records)
            run["ingest_output"] = {
                "path": str(path),
                "created": len(imported),
                "skipped": len(skipped_records),
                "dismissed": len(skipped_dismissed),
                "records": len(records),
                "created_import_ids": [str(item.get("id")) for item in imported if isinstance(item.get("id"), str)],
                "skipped_source_fingerprints": [
                    fingerprint for record in skipped_records if (fingerprint := ledger_mod._import_fingerprint(record))
                ],
                "dismissed_source_fingerprints": [
                    fingerprint
                    for record in skipped_dismissed
                    if (fingerprint := ledger_mod._import_fingerprint(record))
                ],
            }
            if run.get("path"):
                helpers._write_json(Path(str(run["path"])) / "receipt.json", run)
    after_counts = ledger_mod._import_counts(ledger_mod._pending_imports(target))
    payload = {
        "target": str(target),
        "runs_root": str(helpers._scanner_runs_root(target)),
        "selected": len(selected),
        "completed": len([run for run in runs if run.get("status") == "completed"]),
        "failed": len([run for run in runs if run.get("status") != "completed"]),
        "skipped": [
            {"scanner_id": item["scanner"].get("id"), "reason": item["reason"]}
            for item in skipped
            if isinstance(item.get("scanner"), dict)
        ],
        "imports_before": before_counts,
        "imports_after": after_counts,
        "ingest_output": ingest_output,
        "ingest_errors": ingest_errors,
        "runs": runs,
    }
    return payload, 0 if payload["failed"] == 0 else 1


def scanners_run(
    *,
    target: Path,
    scanner_id: str | None = None,
    all_matching: bool = False,
    due: bool = False,
    include_disabled: bool = False,
    force: bool = False,
    ingest_output: bool = False,
    json_output: bool = False,
) -> int:
    payload, rc = _scanners_run_payload(
        target=target,
        scanner_id=scanner_id,
        all_matching=all_matching,
        due=due,
        include_disabled=include_disabled,
        force=force,
        ingest_output=ingest_output,
    )
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return rc
    print(f"work scanners run: {payload.get('target')}")
    print(f"runs_root: {payload['runs_root']}")
    print(f"selected: {payload['selected']}")
    print(f"completed: {payload['completed']}")
    print(f"failed: {payload['failed']}")
    for item in payload["skipped"]:
        print(f"skipped: {item['scanner_id']} {item['reason']}")
    runs = payload.get("runs") if isinstance(payload.get("runs"), list) else []
    for run in runs:
        print(
            f"- {run.get('run_id')} {run.get('scanner_id')} "
            f"[{run.get('status')}] exit={run.get('exit_code')} timed_out={run.get('timed_out')}"
        )
        if run.get("error"):
            print(f"  error: {run.get('error')}")
        if run.get("ingest_output"):
            ingest = run["ingest_output"]
            print(
                "  ingest_output: "
                f"created={ingest.get('created')} skipped={ingest.get('skipped')} dismissed={ingest.get('dismissed')}"
            )
        if run.get("provenance_imports_stamped"):
            print(f"  provenance_imports_stamped: {run.get('provenance_imports_stamped')}")
        print(f"  logs: {run.get('stdout_path')} {run.get('stderr_path')}")
    before_counts = payload.get("imports_before") if isinstance(payload.get("imports_before"), dict) else {}
    after_counts = payload.get("imports_after") if isinstance(payload.get("imports_after"), dict) else {}
    print(f"pending_imports_before: {before_counts.get('total', 0)}")
    print(f"pending_imports_after: {after_counts.get('total', 0)}")
    return rc


def scanners_runs(*, target: Path, json_output: bool = False, limit: int = 20) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    receipts = _scanner_receipts(target)[:limit]
    payload = {"target": str(target), "runs_root": str(helpers._scanner_runs_root(target)), "runs": receipts}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"work scanner runs: {target}")
    print(f"runs_root: {payload['runs_root']}")
    if not receipts:
        print("runs: none")
        return 0
    for receipt in receipts:
        print(
            f"- {receipt.get('run_id')} {receipt.get('scanner_id')} "
            f"[{receipt.get('status')}] exit={receipt.get('exit_code')} {receipt.get('started_at')}"
        )
    return 0


def scanners_run_show(*, target: Path, run_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    matches = [receipt for receipt in _scanner_receipts(target) if str(receipt.get("run_id") or "").startswith(run_id)]
    if not matches:
        print(f"error: scanner run not found: {run_id}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"error: scanner run id is ambiguous: {run_id}", file=sys.stderr)
        return 2
    receipt = matches[0]
    if json_output:
        print(json.dumps({"target": str(target), "run": receipt}, indent=2, sort_keys=True))
        return 0
    print(f"scanner_run: {receipt.get('run_id')}")
    print(f"scanner: {receipt.get('scanner_id')}")
    print(f"source: {receipt.get('source')}")
    print(f"status: {receipt.get('status')}")
    print(f"started_at: {receipt.get('started_at')}")
    if receipt.get("completed_at"):
        print(f"completed_at: {receipt.get('completed_at')}")
    print(f"duration_seconds: {receipt.get('duration_seconds')}")
    print(f"exit_code: {receipt.get('exit_code')}")
    print(f"timed_out: {receipt.get('timed_out')}")
    print(f"stdout: {receipt.get('stdout_path')}")
    print(f"stderr: {receipt.get('stderr_path')}")
    if receipt.get("stdout_summary"):
        print(f"stdout_summary: {helpers._short(str(receipt.get('stdout_summary')))}")
    if receipt.get("stderr_summary"):
        print(f"stderr_summary: {helpers._short(str(receipt.get('stderr_summary')))}")
    return 0


def _sweep_run_references(run: dict[str, Any]) -> dict[str, Any]:
    ingest = run.get("ingest_output") if isinstance(run.get("ingest_output"), dict) else {}
    created_import_ids = [
        str(item) for item in ingest.get("created_import_ids", []) if isinstance(item, str) and item.strip()
    ]
    for item in run.get("stamped_import_ids", []):
        if isinstance(item, str) and item.strip() and item not in created_import_ids:
            created_import_ids.append(item)
    skipped_source_fingerprints = [
        str(item) for item in ingest.get("skipped_source_fingerprints", []) if isinstance(item, str) and item.strip()
    ]
    dismissed_source_fingerprints = [
        str(item) for item in ingest.get("dismissed_source_fingerprints", []) if isinstance(item, str) and item.strip()
    ]
    return {
        "scanner_id": run.get("scanner_id"),
        "scanner_source": run.get("source"),
        "scanner_run_id": run.get("run_id"),
        "receipt_path": _scanner_run_receipt_path(run),
        "import_path": ingest.get("path"),
        "created_import_ids": created_import_ids,
        "skipped_source_fingerprints": skipped_source_fingerprints,
        "dismissed_source_fingerprints": dismissed_source_fingerprints,
    }


def _sweep_import_references(report: dict[str, Any]) -> dict[str, Any]:
    existing = report.get("import_references")
    if isinstance(existing, dict):
        return existing
    runs = []
    run_result = report.get("run_result") if isinstance(report.get("run_result"), dict) else {}
    for run in run_result.get("runs", []):
        if isinstance(run, dict):
            runs.append(_sweep_run_references(run))
    return _sweep_references_from_runs(runs)


def _sweep_references_from_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    created_import_ids: list[str] = []
    skipped_source_fingerprints: list[str] = []
    dismissed_source_fingerprints: list[str] = []
    for run in runs:
        created_import_ids.extend(
            str(item) for item in run.get("created_import_ids", []) if isinstance(item, str) and item.strip()
        )
        skipped_source_fingerprints.extend(
            str(item) for item in run.get("skipped_source_fingerprints", []) if isinstance(item, str) and item.strip()
        )
        dismissed_source_fingerprints.extend(
            str(item) for item in run.get("dismissed_source_fingerprints", []) if isinstance(item, str) and item.strip()
        )
    return {
        "created_import_ids": sorted(set(created_import_ids)),
        "skipped_source_fingerprints": sorted(set(skipped_source_fingerprints)),
        "dismissed_source_fingerprints": sorted(set(dismissed_source_fingerprints)),
        "runs": runs,
    }


def _sweep_import_counts(run_payload: dict[str, Any]) -> dict[str, int]:
    runs = run_payload.get("runs") if isinstance(run_payload.get("runs"), list) else []
    created = 0
    skipped = 0
    dismissed = 0
    for run in runs:
        if not isinstance(run, dict):
            continue
        ingest = run.get("ingest_output") if isinstance(run.get("ingest_output"), dict) else {}
        created += int(ingest.get("created", 0) or 0)
        skipped += int(ingest.get("skipped", 0) or 0)
        dismissed += int(ingest.get("dismissed", 0) or 0)
    before = run_payload.get("imports_before") if isinstance(run_payload.get("imports_before"), dict) else {}
    after = run_payload.get("imports_after") if isinstance(run_payload.get("imports_after"), dict) else {}
    delta = int(after.get("total", 0) or 0) - int(before.get("total", 0) or 0)
    if delta > created:
        created = delta
    return {"created": created, "skipped": skipped, "dismissed": dismissed}


def _write_sweep_report(target: Path, report: dict[str, Any]) -> None:
    sweep_id = str(report.get("sweep_id") or "sweep")
    helpers._write_json(helpers._scanner_sweeps_root(target) / sweep_id / "sweep.json", report)


def _sweep_closeout_status(report: dict[str, Any]) -> str | None:
    closeout = report.get("review_closeout")
    if not isinstance(closeout, dict):
        return None
    status = closeout.get("status")
    return str(status) if isinstance(status, str) else None


def _sweep_is_closed(report: dict[str, Any]) -> bool:
    return _sweep_closeout_status(report) in {"reviewed", "reviewed_with_deferrals"}


def sweep(
    *,
    target: Path,
    scanner_id: str | None = None,
    all_matching: bool = False,
    include_disabled: bool = False,
    force: bool = False,
    ingest: bool = True,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if scanner_id and all_matching:
        print("error: pass --scanner or --all, not both", file=sys.stderr)
        return 2
    started = helpers._now()
    sweep_id = f"{started.strftime('%Y%m%d-%H%M%S')}-scanner-sweep-{uuid4().hex[:6]}"
    run_payload, run_rc = _scanners_run_payload(
        target=target,
        scanner_id=scanner_id,
        all_matching=all_matching,
        due=not scanner_id and not all_matching,
        include_disabled=include_disabled,
        force=force,
        ingest_output=ingest,
    )
    completed = helpers._now()
    runs = run_payload.get("runs") if isinstance(run_payload.get("runs"), list) else []
    errors = run_payload.get("errors") if isinstance(run_payload.get("errors"), list) else []
    status_text = "failed" if run_rc != 0 else "completed"
    inbox_hygiene = _inbox_hygiene_payload(target)
    run_references = [_sweep_run_references(run) for run in runs if isinstance(run, dict)]
    report = {
        "sweep_id": sweep_id,
        "status": status_text,
        "target": str(target),
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "duration_seconds": (completed - started).total_seconds(),
        "mode": "all" if all_matching else ("scanner" if scanner_id else "due"),
        "scanner": scanner_id,
        "include_disabled": include_disabled,
        "force": force,
        "ingest": ingest,
        "run_result": run_payload,
        "run_rc": run_rc,
        "errors": errors,
        "scanner_run_ids": [run.get("run_id") for run in runs if isinstance(run, dict)],
        "receipt_paths": [_scanner_run_receipt_path(run) for run in runs if isinstance(run, dict)],
        "import_counts": _sweep_import_counts(run_payload),
        "import_references": _sweep_references_from_runs(run_references),
        "inbox_hygiene": {
            "issue_count": inbox_hygiene["issue_count"],
            "top_issue": inbox_hygiene["top_issue"],
        },
        "suggested_commands": [
            "brigade work inbox",
            "brigade work inbox doctor",
            "brigade work import plan <import-id>",
        ],
    }
    _write_sweep_report(target, report)
    if json_output:
        print(json.dumps(report, indent=2, sort_keys=True))
        return run_rc
    print(f"work sweep: {target}")
    print(f"sweep: {sweep_id}")
    print(f"status: {status_text}")
    print(f"runs: {len(runs)}")
    print(f"created: {report['import_counts']['created']}")
    print(f"skipped: {report['import_counts']['skipped']}")
    print(f"dismissed: {report['import_counts']['dismissed']}")
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    print(f"report: {helpers._scanner_sweeps_root(target) / sweep_id / 'sweep.json'}")
    print("next: brigade work inbox")
    return run_rc


def sweeps(*, target: Path, json_output: bool = False, limit: int = 20) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    reports = _scanner_sweeps(target)[:limit]
    payload = {"target": str(target), "sweeps_root": str(helpers._scanner_sweeps_root(target)), "sweeps": reports}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"work sweeps: {target}")
    print(f"sweeps_root: {payload['sweeps_root']}")
    if not reports:
        print("sweeps: none")
        return 0
    for report in reports:
        print(
            f"- {report.get('sweep_id')} [{report.get('status')}] runs={len(report.get('scanner_run_ids') or [])} {report.get('started_at')}"
        )
    return 0


def plans(*, target: Path, json_output: bool = False, limit: int = 20) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    plans_dir = helpers._plans_dir(target)
    entries: list[dict[str, Any]] = []
    if plans_dir.is_dir():
        for json_path in plans_dir.glob("*.json"):
            name = json_path.name
            if name.endswith(".meta.json"):
                kind = "meta"
                task_id = name[: -len(".meta.json")]
            else:
                kind = "plan"
                task_id = name[: -len(".json")]
            _, md_path = helpers._plan_paths(target, task_id, kind)
            try:
                data = json.loads(json_path.read_text())
            except (json.JSONDecodeError, OSError):
                data = None
            if not isinstance(data, dict):
                entries.append(
                    {
                        "task_id": task_id,
                        "kind": kind,
                        "status": "unreadable",
                        "updated_at": "",
                        "path": ledger_mod._plan_rel_path(target, md_path),
                    }
                )
                continue
            entries.append(
                {
                    "task_id": str(data.get("task_id") or task_id),
                    "kind": str(data.get("kind") or kind),
                    "status": str(data.get("status") or ""),
                    "updated_at": str(data.get("updated_at") or ""),
                    "path": ledger_mod._plan_rel_path(target, md_path),
                }
            )
    entries.sort(key=lambda item: (item.get("updated_at") or "", item.get("task_id") or ""), reverse=True)
    entries = entries[:limit]
    if json_output:
        print(json.dumps(entries, indent=2, sort_keys=True))
        return 0
    if not entries:
        print("no plan artifacts")
        return 0
    for entry in entries:
        print(f"- {entry['task_id']} [{entry['kind']}] [{entry['status']}] {entry['updated_at']} {entry['path']}")
    return 0


def _plan_proposals_dir(target: Path) -> Path:
    return helpers._work_root(target) / "plan-proposals"


def _proposal_path(target: Path, task_id: str, as_kind: str) -> Path:
    return _plan_proposals_dir(target) / f"{task_id}-{as_kind}.md"


def _render_proposal_md(receipt: dict[str, Any], as_kind: str) -> str:
    def _bullets(items: Any) -> list[str]:
        values = [str(item) for item in items] if isinstance(items, list) else []
        if not values:
            return ["_none recorded_"]
        return [f"- {item}" for item in values]

    def _checklist(items: Any) -> list[str]:
        values = [str(item) for item in items] if isinstance(items, list) else []
        if not values:
            return ["_none recorded_"]
        return [f"- [ ] {item}" for item in values]

    title = str(receipt.get("title") or "")
    acceptance = receipt.get("acceptance")
    if title:
        intent = title
    elif isinstance(acceptance, list) and acceptance:
        intent = str(acceptance[0])
    else:
        intent = "_none recorded_"

    lines: list[str] = []
    lines.append(f"# Draft {as_kind}: {title}")
    lines.append("")
    lines.append(
        "> DRAFT proposal generated from an accepted plan. Review and move it into "
        "place yourself; Brigade does not install it."
    )
    lines.append("")
    lines.append(f"- **Source task:** {receipt.get('task_id', '')}")
    lines.append(f"- **Generated at:** {helpers._now().isoformat()}")
    lines.append("")
    lines.append("## Intent")
    lines.append(intent)
    lines.append("")
    lines.append("## Acceptance checklist")
    lines.extend(_checklist(acceptance))
    lines.append("")
    lines.append("## Steps")
    lines.extend(_bullets(receipt.get("steps")))
    lines.append("")
    lines.append("## Assumptions")
    lines.extend(_bullets(receipt.get("assumptions")))
    lines.append("")
    lines.append("## Risks")
    lines.extend(_bullets(receipt.get("risks")))
    lines.append("")
    lines.append("## Next safe command")
    lines.append(f"`{receipt.get('next_command', '')}`")
    lines.append("")
    return "\n".join(lines)


def plan_promote(*, target: Path, task_id: str, as_kind: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if as_kind not in constants._PROPOSAL_KINDS:
        print(
            f"error: --as must be one of {', '.join(constants._PROPOSAL_KINDS)}: {as_kind}",
            file=sys.stderr,
        )
        return 2
    task, _ = ledger_mod._find_task(target, task_id)
    lookup_id = str(task.get("id") or task_id) if task is not None else task_id
    receipt = ledger_mod._read_plan_receipt(target, lookup_id, kind="plan")
    if receipt is None:
        print(f"error: no plan artifact for task: {task_id}", file=sys.stderr)
        return 1
    if receipt.get("status") != "accepted":
        print(
            "error: plan not accepted (run: brigade work task plan {id} --write --accept)".format(id=task_id),
            file=sys.stderr,
        )
        return 1
    resolved_id = str(receipt.get("task_id") or task_id)
    proposal_path = _proposal_path(target, resolved_id, as_kind)
    _plan_proposals_dir(target).mkdir(parents=True, exist_ok=True)
    proposal_path.write_text(_render_proposal_md(receipt, as_kind))
    rel = ledger_mod._plan_rel_path(target, proposal_path)
    if json_output:
        print(json.dumps({"task_id": resolved_id, "as": as_kind, "path": rel}, indent=2, sort_keys=True))
        return 0
    print(f"wrote draft proposal: {rel}")
    print("review then move it into place yourself (not installed)")
    return 0


def plan_proposals(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    proposals_dir = _plan_proposals_dir(target)
    entries: list[dict[str, Any]] = []
    if proposals_dir.is_dir():
        for md_path in proposals_dir.glob("*.md"):
            stem = md_path.name[: -len(".md")]
            task_id, _, as_kind = stem.rpartition("-")
            if not task_id:
                task_id, as_kind = stem, ""
            try:
                mtime = md_path.stat().st_mtime
            except OSError:
                mtime = 0.0
            entries.append(
                {
                    "task_id": task_id,
                    "as": as_kind,
                    "path": ledger_mod._plan_rel_path(target, md_path),
                    "_mtime": mtime,
                }
            )
    entries.sort(key=lambda item: item.get("_mtime", 0.0), reverse=True)
    for entry in entries:
        entry.pop("_mtime", None)
    if json_output:
        print(json.dumps(entries, indent=2, sort_keys=True))
        return 0
    if not entries:
        print("no plan proposals")
        return 0
    for entry in entries:
        print(f"- {entry['task_id']} [{entry['as']}] {entry['path']}")
    return 0


def sweep_show(*, target: Path, sweep_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    matches = [report for report in _scanner_sweeps(target) if str(report.get("sweep_id") or "").startswith(sweep_id)]
    if not matches:
        print(f"error: sweep not found: {sweep_id}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"error: sweep id is ambiguous: {sweep_id}", file=sys.stderr)
        return 2
    report = matches[0]
    if json_output:
        print(json.dumps({"target": str(target), "sweep": report}, indent=2, sort_keys=True))
        return 0
    print(f"sweep: {report.get('sweep_id')}")
    print(f"status: {report.get('status')}")
    print(f"started_at: {report.get('started_at')}")
    print(f"completed_at: {report.get('completed_at')}")
    print(f"runs: {len(report.get('scanner_run_ids') or [])}")
    counts = report.get("import_counts") if isinstance(report.get("import_counts"), dict) else {}
    print(f"created: {counts.get('created', 0)}")
    print(f"skipped: {counts.get('skipped', 0)}")
    print(f"dismissed: {counts.get('dismissed', 0)}")
    hygiene = report.get("inbox_hygiene") if isinstance(report.get("inbox_hygiene"), dict) else {}
    print(f"inbox_hygiene: {hygiene.get('issue_count', 0)} issue(s)")
    return 0


def _find_sweep_report(target: Path, sweep_id: str) -> tuple[dict[str, Any] | None, str | None]:
    if sweep_id == "latest":
        latest = _scanner_latest_sweep(target)
        if latest is None:
            return None, "sweep not found: latest"
        return latest, None
    matches = [report for report in _scanner_sweeps(target) if str(report.get("sweep_id") or "").startswith(sweep_id)]
    if not matches:
        return None, f"sweep not found: {sweep_id}"
    if len(matches) > 1:
        return None, f"sweep id is ambiguous: {sweep_id}"
    return matches[0], None


def _sweep_import_suggested_commands(import_id: str, kind: str) -> list[str]:
    commands = [
        f"brigade work import plan {import_id}",
        f'brigade work import dismiss {import_id} --reason "..."',
    ]
    if kind == "task":
        commands.insert(1, f"brigade work import promote {import_id}")
        commands.append(f"brigade work import promote --run {import_id}")
    elif kind in constants.HANDOFF_READY_KINDS:
        commands.insert(1, f"brigade work import plan-handoff {import_id}")
        commands.insert(2, f"brigade work import promote-handoff {import_id}")
    return commands


def _sweep_import_review_summary(item: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    summary = ledger_mod._import_summary(item, now=now)
    metadata = summary.get("metadata") if isinstance(summary.get("metadata"), dict) else {}
    required = ("scanner_id", "scanner_source", "scanner_run_id", "source_fingerprint")
    provenance_complete = all(metadata.get(key) for key in required)
    acceptance_count = int(summary.get("acceptance_count", 0) or 0)
    if summary.get("kind") == "task":
        acceptance_coverage = "ready" if acceptance_count else "missing"
        priority = str(summary.get("priority") or "normal")
    else:
        acceptance_coverage = "n/a"
        priority = "n/a"
    import_id = str(summary.get("id") or "")
    summary.update(
        {
            "priority": priority,
            "acceptance_coverage": acceptance_coverage,
            "provenance_complete": provenance_complete,
            "provenance_status": "complete" if provenance_complete else "missing",
            "suggested_commands": _sweep_import_suggested_commands(import_id, str(summary.get("kind") or "task"))
            if summary.get("status") == "pending" and import_id
            else [],
        }
    )
    if summary.get("kind") in constants.HANDOFF_READY_KINDS:
        summary["handoff_ready"] = True
        summary["target_document"] = ledger_mod._handoff_target_document(item)
    return summary


def _sweep_group_key(item: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        str(item.get("source") or "manual"),
        str(item.get("kind") or "task"),
        str(item.get("priority") or "n/a"),
        str(item.get("acceptance_coverage") or "n/a"),
        str(item.get("provenance_status") or "missing"),
        str(item.get("status") or "pending"),
    )


def _sweep_review_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str, str, str], list[str]] = {}
    for item in items:
        import_id = item.get("id")
        if isinstance(import_id, str):
            grouped.setdefault(_sweep_group_key(item), []).append(import_id)
    result: list[dict[str, Any]] = []
    for key, import_ids in sorted(grouped.items()):
        source, kind, priority, acceptance_coverage, provenance_status, status = key
        result.append(
            {
                "source": source,
                "kind": kind,
                "priority": priority,
                "acceptance_coverage": acceptance_coverage,
                "provenance_status": provenance_status,
                "status": status,
                "count": len(import_ids),
                "import_ids": sorted(import_ids),
            }
        )
    return result


def _sweep_review_checks(
    *,
    report: dict[str, Any],
    references: dict[str, Any],
    items: list[dict[str, Any]],
    missing_import_ids: list[str],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    pending_ids = [
        str(item.get("id")) for item in items if item.get("status") == "pending" and isinstance(item.get("id"), str)
    ]
    completed = helpers._parse_iso_datetime(report.get("completed_at") or report.get("started_at"))
    stale_pending: list[str] = []
    if pending_ids and completed is not None:
        age_hours = (helpers._now() - completed).total_seconds() / 3600
        if age_hours > constants.SCANNER_SWEEP_REVIEW_STALE_HOURS:
            stale_pending = pending_ids
    checks.append(
        _import_hygiene_issue(
            constants.WARN if stale_pending else constants.OK,
            "scanner_sweep_unreviewed",
            f"{len(stale_pending)} pending sweep import(s) older than {constants.SCANNER_SWEEP_REVIEW_STALE_HOURS}h"
            if stale_pending
            else "none",
            stale_pending[:10],
        )
    )
    checks.append(
        _import_hygiene_issue(
            constants.WARN if missing_import_ids else constants.OK,
            "scanner_sweep_missing_imports",
            f"{len(missing_import_ids)} sweep import reference(s) missing from inbox" if missing_import_ids else "none",
            missing_import_ids[:10],
        )
    )
    missing_provenance = [
        str(item.get("id")) for item in items if not item.get("provenance_complete") and isinstance(item.get("id"), str)
    ]
    checks.append(
        _import_hygiene_issue(
            constants.WARN if missing_provenance else constants.OK,
            "scanner_sweep_missing_provenance",
            f"{len(missing_provenance)} sweep import(s) missing scanner provenance" if missing_provenance else "none",
            missing_provenance[:10],
        )
    )
    created = len(
        references.get("created_import_ids", []) if isinstance(references.get("created_import_ids"), list) else []
    )
    skipped = len(
        references.get("skipped_source_fingerprints", [])
        if isinstance(references.get("skipped_source_fingerprints"), list)
        else []
    )
    dismissed = len(
        references.get("dismissed_source_fingerprints", [])
        if isinstance(references.get("dismissed_source_fingerprints"), list)
        else []
    )
    noisy = created == 0 and (skipped + dismissed) > 0
    checks.append(
        _import_hygiene_issue(
            constants.WARN if noisy else constants.OK,
            "scanner_sweep_noisy_noop",
            f"created=0 skipped={skipped} dismissed={dismissed}" if noisy else "none",
        )
    )
    return checks


def _sweep_review_payload(target: Path, sweep_id: str) -> tuple[dict[str, Any] | None, str | None]:
    report, error = _find_sweep_report(target, sweep_id)
    if report is None:
        return None, error
    references = _sweep_import_references(report)
    imports_by_id = {
        str(item.get("id")): item
        for item in ledger_mod._read_imports(target)
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    now = helpers._now()
    import_ids = [
        str(item) for item in references.get("created_import_ids", []) if isinstance(item, str) and item.strip()
    ]
    missing_import_ids = sorted(import_id for import_id in import_ids if import_id not in imports_by_id)
    items = [
        _sweep_import_review_summary(imports_by_id[import_id], now=now)
        for import_id in import_ids
        if import_id in imports_by_id
    ]
    actionable = [item for item in items if item.get("status") == "pending"]
    checks = _sweep_review_checks(
        report=report,
        references=references,
        items=items,
        missing_import_ids=missing_import_ids,
    )
    closeout = report.get("review_closeout") if isinstance(report.get("review_closeout"), dict) else None
    if _sweep_is_closed(report):
        checks = [
            check
            for check in checks
            if check.get("name") not in {"scanner_sweep_unreviewed", "scanner_sweep_noisy_noop"}
        ]
        checks.append(
            _import_hygiene_issue(
                constants.OK,
                "scanner_sweep_closeout",
                f"{closeout.get('status')} at {closeout.get('closed_at')}" if closeout else "reviewed",
            )
        )
    return (
        {
            "target": str(target),
            "sweep": report,
            "references": references,
            "imports": items,
            "groups": _sweep_review_groups(items),
            "actionable_imports": actionable,
            "top_pending_import": actionable[0] if actionable else None,
            "missing_import_ids": missing_import_ids,
            "closeout": closeout,
            "checks": checks,
            "issues": [check for check in checks if check.get("status") != constants.OK],
        },
        None,
    )


def sweep_review(*, target: Path, sweep_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, error = _sweep_review_payload(target, sweep_id)
    if payload is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    sweep_data = payload["sweep"]
    print(f"sweep_review: {sweep_data.get('sweep_id')}")
    print(f"status: {sweep_data.get('status')}")
    print(f"created_imports: {len(payload['references'].get('created_import_ids') or [])}")
    print(f"missing_imports: {len(payload['missing_import_ids'])}")
    if payload["groups"]:
        print("groups:")
        for group in payload["groups"]:
            print(
                f"- {group['source']} {group['kind']} priority={group['priority']} "
                f"acceptance={group['acceptance_coverage']} provenance={group['provenance_status']} "
                f"status={group['status']} count={group['count']}"
            )
    if payload["actionable_imports"]:
        print("actionable:")
        for item in payload["actionable_imports"]:
            print(
                f"- {item.get('id')} [{item.get('kind')}] {item.get('source')}: {helpers._short(str(item.get('text', '')))}"
            )
            for command in item.get("suggested_commands", []):
                print(f"  next: {command}")
    for check in payload["checks"]:
        if check.get("status") != constants.OK:
            helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    return 0


def sweep_closeout(
    *,
    target: Path,
    sweep_id: str,
    reason: str | None = None,
    deferred_imports: list[str] | None = None,
    defer_all: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, error = _sweep_review_payload(target, sweep_id)
    if payload is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    report = payload["sweep"]
    pending_ids = sorted(
        str(item.get("id"))
        for item in payload.get("actionable_imports", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    )
    missing_import_ids = list(payload.get("missing_import_ids") or [])
    deferred = sorted(set(deferred_imports or []))
    unknown_deferred = sorted(import_id for import_id in deferred if import_id not in pending_ids)
    blocked: list[str] = []
    if missing_import_ids:
        blocked.append("missing sweep import references")
    if unknown_deferred:
        blocked.append("deferred imports are not pending sweep imports")
    if pending_ids and not defer_all:
        unresolved = sorted(import_id for import_id in pending_ids if import_id not in deferred)
        if unresolved:
            blocked.append("pending imports remain unreviewed")
    else:
        unresolved = []
    closeout = {
        "sweep_id": report.get("sweep_id"),
        "closed_at": helpers._now().isoformat(),
        "status": "blocked" if blocked else ("reviewed_with_deferrals" if pending_ids else "reviewed"),
        "pending_import_ids": pending_ids,
        "deferred_import_ids": pending_ids if defer_all and pending_ids else deferred,
        "missing_import_ids": missing_import_ids,
        "unresolved_import_ids": unresolved,
        "blocked_reasons": blocked,
        "reason": reason or "",
    }
    if not blocked:
        report["review_closeout"] = closeout
        _write_sweep_report(target, report)
    output = {"target": str(target), "sweep_id": report.get("sweep_id"), "closeout": closeout}
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0 if not blocked else 2
    print(f"sweep_closeout: {report.get('sweep_id')}")
    print(f"status: {closeout['status']}")
    print(f"pending_imports: {len(pending_ids)}")
    print(f"deferred_imports: {len(closeout['deferred_import_ids'])}")
    if blocked:
        for item in blocked:
            print(f"blocked: {item}", file=sys.stderr)
        return 2
    print(f"closed_at: {closeout['closed_at']}")
    return 0
