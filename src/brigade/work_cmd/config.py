"""Backup, scanner, and review configuration loading, validation, and schedule math."""

from __future__ import annotations

import json
import re
import shlex
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from .. import dogfood_cmd
from .. import toml_compat as tomllib
from . import constants, helpers, ledger as ledger_mod


def _format_backup_toml(destinations: tuple[dict[str, Any], ...] = constants.BACKUP_DEFAULTS) -> str:
    lines = [
        "# Local backup health registry. Store only safe labels and local summary paths here.",
        "",
    ]
    for destination in destinations:
        lines.append("[[destination]]")
        for key in (
            "id",
            "kind",
            "command_label",
            "summary_path",
            "snapshot_stale_hours",
            "check_stale_hours",
            "prune_stale_hours",
            "restore_rehearsal_stale_days",
            "enabled",
        ):
            lines.append(f"{key} = {dogfood_cmd._format_toml_value(destination[key])}")
        lines.append("")
    return "\n".join(lines)


def _load_backup_config(target: Path) -> tuple[list[dict[str, Any]], list[str]]:
    path = helpers._backup_config_path(target)
    if not path.is_file():
        return [], [f"backup config missing: {path}"]
    if tomllib is None:
        return [], ["backup config requires Python tomllib support"]
    try:
        payload = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:  # type: ignore[union-attr]
        return [], [f"invalid backup config: {exc}"]
    values = payload.get("destination")
    if not isinstance(values, list):
        return [], ["backup config must contain [[destination]] entries"]
    destinations: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(values, start=1):
        label = f"backup destination {index}"
        if not isinstance(item, dict):
            errors.append(f"{label} must be a table")
            continue
        destination: dict[str, Any] = {}
        for field in ("id", "kind", "command_label", "summary_path"):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{label}: {field} must be a non-empty string")
            else:
                destination[field] = value.strip()
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            errors.append(f"{label}: enabled must be true or false")
        else:
            destination["enabled"] = enabled
        for field in ("snapshot_stale_hours", "check_stale_hours", "prune_stale_hours", "restore_rehearsal_stale_days"):
            value = item.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                errors.append(f"{label}: {field} must be a positive number")
            else:
                destination[field] = float(value)
        destination_id = destination.get("id")
        if isinstance(destination_id, str):
            if destination_id in seen_ids:
                errors.append(f"{label}: duplicate id {destination_id}")
            seen_ids.add(destination_id)
        if destination:
            destinations.append(destination)
    return destinations, errors


def _backup_summary_path(target: Path, destination: dict[str, Any]) -> Path:
    path = Path(str(destination.get("summary_path") or "")).expanduser()
    return path if path.is_absolute() else target / path


def _backup_summary_unsafe_fields(payload: object, prefix: str = "") -> list[str]:
    unsafe: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            rendered = str(key)
            normalized = rendered.strip().casefold()
            path = f"{prefix}.{rendered}" if prefix else rendered
            if normalized in constants.BACKUP_UNSAFE_FIELDS or any(
                token in normalized for token in ("password", "secret", "token", "webhook")
            ):
                unsafe.append(path)
                continue
            unsafe.extend(_backup_summary_unsafe_fields(value, path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload, start=1):
            unsafe.extend(_backup_summary_unsafe_fields(value, f"{prefix}[{index}]"))
    return unsafe


def _backup_result_ok(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return value.strip().casefold() in constants.BACKUP_SUMMARY_ACCEPTED_SUCCESS_RESULTS


def _backup_summary_example(destination: dict[str, Any]) -> dict[str, str]:
    destination_id = str(destination.get("id") or "backup").strip() or "backup"
    label = f"{destination_id.upper()} backup" if len(destination_id) <= 4 else f"{destination_id.title()} backup"
    evidence_name = f"{destination_id}-evidence.json"
    return {
        "destination_label": label,
        "latest_snapshot_at": "2026-05-30T06:00:00+00:00",
        "latest_check_at": "2026-05-30T07:00:00+00:00",
        "latest_check_result": "ok",
        "latest_prune_at": "2026-05-30T07:30:00+00:00",
        "latest_prune_result": "ok",
        "latest_restore_rehearsal_at": "2026-05-01T12:00:00+00:00",
        "latest_restore_rehearsal_result": "ok",
        "summary": f"{label} is current.",
        "evidence_path": f".brigade/backups/{evidence_name}",
    }


def _backup_contract_destination(target: Path, destination: dict[str, Any]) -> dict[str, Any]:
    summary_path = _backup_summary_path(target, destination)
    return {
        "id": destination.get("id"),
        "kind": destination.get("kind"),
        "enabled": destination.get("enabled", True),
        "command_label": destination.get("command_label"),
        "summary_path": str(summary_path),
        "summary_path_config": destination.get("summary_path"),
        "required_fields": list(constants.BACKUP_SUMMARY_REQUIRED_FIELDS),
        "timestamp_fields": [
            "latest_snapshot_at",
            "latest_check_at",
            "latest_prune_at",
            "latest_restore_rehearsal_at",
        ],
        "result_fields": [
            "latest_check_result",
            "latest_prune_result",
            "latest_restore_rehearsal_result",
        ],
        "accepted_success_results": list(constants.BACKUP_SUMMARY_ACCEPTED_SUCCESS_RESULTS),
        "staleness_thresholds": {
            "snapshot_stale_hours": destination.get("snapshot_stale_hours"),
            "check_stale_hours": destination.get("check_stale_hours"),
            "prune_stale_hours": destination.get("prune_stale_hours"),
            "restore_rehearsal_stale_days": destination.get("restore_rehearsal_stale_days"),
        },
        "example_summary": _backup_summary_example(destination),
        "producer_contract": [
            "Write one JSON object to summary_path after each backup run or scheduled health check.",
            "Use ISO-8601 timestamps with timezone offsets for all timestamp fields.",
            "Use one accepted success result for passing check, prune, and restore rehearsal fields.",
            "Keep destination_label, summary, evidence_path, and command_label safe for logs and public docs.",
            "Do not include hostnames, mount paths, repository paths, webhook URLs, tokens, passwords, or raw command output.",
        ],
    }


def _backup_contract_payload(target: Path, destination_id: str | None = None) -> tuple[dict[str, Any], int]:
    target = target.expanduser().resolve()
    destinations, errors = _load_backup_config(target)
    config_loaded = not errors
    if not destinations and errors and not helpers._backup_config_path(target).is_file():
        destinations = [dict(item) for item in constants.BACKUP_DEFAULTS]
    selected = destinations
    if destination_id:
        wanted = destination_id.strip()
        selected = [destination for destination in destinations if destination.get("id") == wanted]
        if not selected:
            payload = {
                "schema_version": 1,
                "schema": {"name": "backup-summary-producer-contract", "version": 1},
                "target": str(target),
                "config_path": str(helpers._backup_config_path(target)),
                "config_loaded": config_loaded,
                "config_errors": errors,
                "destination": wanted,
                "destination_count": 0,
                "destinations": [],
                "would_write": False,
                "manual_only": True,
                "errors": [f"backup destination not found: {wanted}"],
            }
            return payload, 1
    payload = {
        "schema_version": 1,
        "schema": {"name": "backup-summary-producer-contract", "version": 1},
        "target": str(target),
        "config_path": str(helpers._backup_config_path(target)),
        "config_loaded": config_loaded,
        "config_errors": errors,
        "destination_count": len(selected),
        "destinations": [_backup_contract_destination(target, destination) for destination in selected],
        "required_fields": list(constants.BACKUP_SUMMARY_REQUIRED_FIELDS),
        "accepted_success_results": list(constants.BACKUP_SUMMARY_ACCEPTED_SUCCESS_RESULTS),
        "would_write": False,
        "manual_only": True,
        "privacy": {
            "safe_for_public_docs": True,
            "forbidden_field_names": sorted(constants.BACKUP_UNSAFE_FIELDS),
            "forbidden_field_name_tokens": ["password", "secret", "token", "webhook"],
            "notes": [
                "The summary JSON is local evidence for Brigade health checks, not a backup log archive.",
                "Store raw backup logs outside tracked docs and keep only safe evidence paths in summaries.",
            ],
        },
    }
    return payload, 0 if config_loaded or not helpers._backup_config_path(target).is_file() else 1


def _backup_age_hours(value: object, now: datetime) -> float | None:
    parsed = helpers._parse_iso_datetime(value)
    if parsed is None:
        return None
    return (now - parsed).total_seconds() / 3600


def _backup_issue(
    destination: dict[str, Any],
    issue_type: str,
    detail: str,
    *,
    severity: str = constants.WARN,
    summary: str | None = None,
    evidence_path: str | None = None,
    unsafe_fields: list[str] | None = None,
) -> dict[str, Any]:
    destination_id = str(destination.get("id") or "unknown")
    payload: dict[str, Any] = {
        "status": severity,
        "name": f"backup_{issue_type}",
        "destination": destination_id,
        "kind": destination.get("kind"),
        "issue_type": issue_type,
        "detail": detail,
    }
    if summary:
        payload["summary"] = summary
    if evidence_path:
        payload["evidence_path"] = evidence_path
    if unsafe_fields:
        payload["unsafe_fields"] = unsafe_fields
    return payload


def _backup_destination_checks(target: Path, destination: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    path = _backup_summary_path(target, destination)
    if not path.is_file():
        return [_backup_issue(destination, "missing_summary", f"missing summary: {path}")]
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [_backup_issue(destination, "invalid_summary", f"invalid summary JSON: {exc}")]
    if not isinstance(payload, dict):
        return [_backup_issue(destination, "invalid_summary", "summary must be a JSON object")]
    unsafe_fields = _backup_summary_unsafe_fields(payload)
    safe_summary = ledger_mod._string_field(payload.get("summary")) or ledger_mod._string_field(
        payload.get("safe_summary")
    )
    evidence_path = ledger_mod._string_field(payload.get("evidence_path"))
    destination_label = ledger_mod._string_field(payload.get("destination_label")) or str(destination.get("id"))
    if unsafe_fields:
        checks.append(
            _backup_issue(
                destination,
                "unsafe_summary_fields",
                f"{destination_label} contains unsafe private field names: {', '.join(unsafe_fields[:8])}",
                summary=safe_summary,
                evidence_path=evidence_path,
                unsafe_fields=unsafe_fields,
            )
        )
    snapshot_age = _backup_age_hours(payload.get("latest_snapshot_at"), now)
    if snapshot_age is None:
        checks.append(
            _backup_issue(
                destination,
                "snapshot_missing",
                f"{destination_label} latest snapshot time is missing",
                summary=safe_summary,
                evidence_path=evidence_path,
            )
        )
    elif snapshot_age > float(destination.get("snapshot_stale_hours", 36)):
        checks.append(
            _backup_issue(
                destination,
                "snapshot_stale",
                f"{destination_label} latest snapshot is {snapshot_age:.1f}h old",
                summary=safe_summary,
                evidence_path=evidence_path,
            )
        )
    check_result = payload.get("latest_check_result")
    check_age = _backup_age_hours(payload.get("latest_check_at"), now)
    if not _backup_result_ok(check_result):
        checks.append(
            _backup_issue(
                destination,
                "check_failed",
                f"{destination_label} latest check result is {check_result or 'missing'}",
                summary=safe_summary,
                evidence_path=evidence_path,
            )
        )
    elif check_age is None:
        checks.append(
            _backup_issue(
                destination,
                "check_missing",
                f"{destination_label} latest check time is missing",
                summary=safe_summary,
                evidence_path=evidence_path,
            )
        )
    elif check_age > float(destination.get("check_stale_hours", 168)):
        checks.append(
            _backup_issue(
                destination,
                "check_stale",
                f"{destination_label} latest check is {check_age:.1f}h old",
                summary=safe_summary,
                evidence_path=evidence_path,
            )
        )
    prune_result = payload.get("latest_prune_result")
    prune_age = _backup_age_hours(payload.get("latest_prune_at"), now)
    if not _backup_result_ok(prune_result):
        checks.append(
            _backup_issue(
                destination,
                "prune_failed",
                f"{destination_label} latest prune result is {prune_result or 'missing'}",
                summary=safe_summary,
                evidence_path=evidence_path,
            )
        )
    elif prune_age is None:
        checks.append(
            _backup_issue(
                destination,
                "prune_missing",
                f"{destination_label} latest prune time is missing",
                summary=safe_summary,
                evidence_path=evidence_path,
            )
        )
    elif prune_age > float(destination.get("prune_stale_hours", 168)):
        checks.append(
            _backup_issue(
                destination,
                "prune_stale",
                f"{destination_label} latest prune is {prune_age:.1f}h old",
                summary=safe_summary,
                evidence_path=evidence_path,
            )
        )
    restore_result = payload.get("latest_restore_rehearsal_result")
    restore_age = _backup_age_hours(payload.get("latest_restore_rehearsal_at"), now)
    if not _backup_result_ok(restore_result):
        checks.append(
            _backup_issue(
                destination,
                "restore_rehearsal_failed",
                f"{destination_label} latest restore rehearsal result is {restore_result or 'missing'}",
                summary=safe_summary,
                evidence_path=evidence_path,
            )
        )
    elif restore_age is None:
        checks.append(
            _backup_issue(
                destination,
                "restore_rehearsal_missing",
                f"{destination_label} latest restore rehearsal time is missing",
                summary=safe_summary,
                evidence_path=evidence_path,
            )
        )
    elif restore_age > float(destination.get("restore_rehearsal_stale_days", 90)) * 24:
        checks.append(
            _backup_issue(
                destination,
                "restore_rehearsal_overdue",
                f"{destination_label} latest restore rehearsal is {restore_age / 24:.1f}d old",
                summary=safe_summary,
                evidence_path=evidence_path,
            )
        )
    return checks


def _backup_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    destinations, errors = _load_backup_config(target)
    checks: list[dict[str, Any]] = []
    if errors:
        status = constants.WARN if not helpers._backup_config_path(target).is_file() else constants.FAIL
        checks.append({"status": status, "name": "backup_config", "detail": "; ".join(errors)})
    else:
        checks.append(
            {"status": constants.OK, "name": "backup_config", "detail": str(helpers._backup_config_path(target))}
        )
    now = helpers._now() if destinations else None
    for destination in destinations:
        if not destination.get("enabled", True):
            continue
        if now is not None:
            checks.extend(_backup_destination_checks(target, destination, now))
    closeout = _backup_latest_closeout(target)
    closed_fingerprints = set(closeout.get("source_fingerprints", [])) if isinstance(closeout, dict) else set()
    raw_issues = [check for check in checks if check.get("status") != constants.OK]
    quieted_issues = [issue for issue in raw_issues if _backup_issue_fingerprint(issue) in closed_fingerprints]
    issues = [issue for issue in raw_issues if _backup_issue_fingerprint(issue) not in closed_fingerprints]
    changed_fingerprints = [_backup_issue_fingerprint(issue) for issue in issues if closed_fingerprints]
    restore_rehearsal_issues = [
        issue
        for issue in raw_issues
        if str(issue.get("issue_type") or issue.get("name") or "").startswith("restore_rehearsal")
    ]
    operator_summary = (
        f"{len(issues)} active backup issue(s), "
        f"{len(quieted_issues)} reviewed/deferred issue(s), "
        f"{len(restore_rehearsal_issues)} restore rehearsal issue(s)"
    )
    return {
        "target": str(target),
        "config_path": str(helpers._backup_config_path(target)),
        "valid": not errors,
        "destinations": destinations,
        "checks": checks,
        "active_checks": [check for check in checks if check.get("status") == constants.OK] + issues,
        "raw_issues": raw_issues,
        "raw_issue_count": len(raw_issues),
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "quieted_issues": quieted_issues,
        "quieted_issue_count": len(quieted_issues),
        "changed_fingerprints": changed_fingerprints,
        "changed_fingerprint_count": len(changed_fingerprints),
        "restore_rehearsal_issues": restore_rehearsal_issues,
        "restore_rehearsal_issue_count": len(restore_rehearsal_issues),
        "operator_summary": operator_summary,
        "latest_closeout": closeout,
    }


def _backup_closeouts_root(target: Path) -> Path:
    return target / ".brigade" / "backups" / "closeouts"


def _backup_latest_closeout(target: Path) -> dict[str, Any] | None:
    root = _backup_closeouts_root(target)
    if not root.is_dir():
        return None
    closeouts: list[dict[str, Any]] = []
    for path in root.glob("*/closeout.json"):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload.setdefault("path", str(path))
            closeouts.append(payload)
    closeouts.sort(key=lambda item: str(item.get("created_at") or item.get("closeout_id") or ""), reverse=True)
    return closeouts[0] if closeouts else None


def _backup_issue_fingerprint(issue: dict[str, Any]) -> str:
    return helpers._stable_hash(
        {
            "destination": issue.get("destination") or "config",
            "issue_type": issue.get("issue_type") or issue.get("name"),
            "detail": issue.get("detail"),
            "summary": issue.get("summary"),
            "evidence_path": issue.get("evidence_path"),
            "unsafe_fields": issue.get("unsafe_fields"),
        }
    )


def _backup_issue_records(target: Path) -> list[dict[str, Any]]:
    health = _backup_health(target)
    records: list[dict[str, Any]] = []
    for issue in health["issues"]:
        name = str(issue.get("name") or "backup_issue")
        destination = str(issue.get("destination") or "config")
        issue_type = str(issue.get("issue_type") or name)
        detail = str(issue.get("detail") or "")
        metadata = {
            "backup_destination": destination,
            "backup_issue_type": issue_type,
            "backup_issue_detail": detail,
            "source_item_key": f"backup-health:{destination}:{issue_type}",
            "source_fingerprint": _backup_issue_fingerprint(issue),
        }
        if issue.get("summary"):
            metadata["safe_summary"] = issue["summary"]
        if issue.get("evidence_path"):
            metadata["evidence_path"] = issue["evidence_path"]
        if issue.get("unsafe_fields"):
            metadata["unsafe_fields"] = issue["unsafe_fields"]
        records.append(
            {
                "text": f"Repair backup health issue {destination}/{issue_type}: {detail}",
                "kind": "task" if issue_type in {"missing_summary", "unsafe_summary_fields"} else "incident",
                "source": "backup-health",
                "type": "workflow",
                "priority": "high"
                if issue_type in {"snapshot_stale", "check_failed", "restore_rehearsal_failed"}
                else "normal",
                "template": "bugfix",
                "acceptance": [f"`brigade work backup doctor` no longer reports {destination}/{issue_type}."],
                "metadata": metadata,
            }
        )
    return records


def _format_scanner_toml(scanners: tuple[dict[str, Any], ...] = constants.SCANNER_DEFAULTS) -> str:
    lines = [
        "# Local scanner registry. Brigade plans and inspects these commands but does not run them automatically.",
        "",
    ]
    for scanner in scanners:
        lines.append("[[scanner]]")
        for key in ("id", "source", "command", "cadence", "enabled", "timeout", "output_path", "conflict_window"):
            value = scanner[key]
            lines.append(f"{key} = {dogfood_cmd._format_toml_value(value)}")
        lines.append("")
    return "\n".join(lines)


def _format_toml_array(values: object) -> str:
    if not isinstance(values, list):
        return "[]"
    return "[" + ", ".join(dogfood_cmd._format_toml_value(item) for item in values) + "]"


def _format_review_toml(reviewers: tuple[dict[str, Any], ...] = constants.REVIEW_DEFAULTS) -> str:
    lines = [
        "# Local code review producers. Brigade runs these only when explicitly requested.",
        "",
    ]
    for reviewer in reviewers:
        lines.append("[[reviewer]]")
        for key in (
            "id",
            "name",
            "command",
            "cwd",
            "enabled",
            "timeout",
            "base_ref",
            "output_path",
            "findings_path",
            "privacy_mode",
        ):
            lines.append(f"{key} = {dogfood_cmd._format_toml_value(reviewer[key])}")
        lines.append(f"target_paths = {_format_toml_array(reviewer.get('target_paths'))}")
        lines.append(f"supported_modes = {_format_toml_array(reviewer.get('supported_modes'))}")
        lines.append("")
    return "\n".join(lines)


def _load_scanner_config(target: Path) -> tuple[list[dict[str, Any]], list[str]]:
    path = helpers._scanner_config_path(target)
    if not path.is_file():
        return [], [f"scanner config missing: {path}"]
    if tomllib is None:
        return [], ["scanner config requires Python tomllib support"]
    try:
        payload = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:  # type: ignore[union-attr]
        return [], [f"invalid scanner config: {exc}"]
    values = payload.get("scanner")
    if not isinstance(values, list):
        return [], ["scanner config must contain [[scanner]] entries"]
    scanners: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(values, start=1):
        label = f"scanner {index}"
        if not isinstance(item, dict):
            errors.append(f"{label} must be a table")
            continue
        scanner: dict[str, Any] = {}
        for field in ("id", "command", "source", "cadence", "output_path", "conflict_window"):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{label}: {field} must be a non-empty string")
            else:
                scanner[field] = value.strip()
        import_path = item.get("import_path")
        if import_path is not None:
            if not isinstance(import_path, str) or not import_path.strip():
                errors.append(f"{label}: import_path must be a non-empty string when present")
            elif Path(import_path).is_absolute() or ".." in Path(import_path).parts:
                errors.append(f"{label}: import_path must be relative and must not contain '..'")
            else:
                scanner["import_path"] = import_path.strip()
        import_format = item.get("import_format")
        if import_format is not None:
            if not isinstance(import_format, str) or import_format.strip() != "jsonl":
                errors.append(f"{label}: import_format must be jsonl")
            else:
                scanner["import_format"] = "jsonl"
        cwd = item.get("cwd", item.get("target"))
        if cwd is not None:
            field = "cwd" if "cwd" in item else "target"
            if not isinstance(cwd, str) or not cwd.strip():
                errors.append(f"{label}: {field} must be a non-empty string when present")
            elif Path(cwd).is_absolute() or ".." in Path(cwd).parts:
                errors.append(f"{label}: {field} must be relative and must not contain '..'")
            else:
                scanner["cwd"] = cwd.strip()
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            errors.append(f"{label}: enabled must be true or false")
        else:
            scanner["enabled"] = enabled
        timeout = item.get("timeout", 300)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            errors.append(f"{label}: timeout must be a positive number")
        else:
            scanner["timeout"] = float(timeout)
        scanner_id = scanner.get("id")
        if isinstance(scanner_id, str):
            if scanner_id in seen_ids:
                errors.append(f"{label}: duplicate id {scanner_id}")
            seen_ids.add(scanner_id)
        if "cadence" in scanner and _scanner_start_minute(scanner["cadence"]) is None:
            errors.append(f"{label}: cadence must be daily@HH:MM or hourly@MM")
        if "conflict_window" in scanner and _scanner_window_minutes(scanner["conflict_window"]) is None:
            errors.append(f"{label}: conflict_window must be HH:MM-HH:MM")
        if scanner:
            scanners.append(scanner)
    return scanners, errors


def _string_list(value: object, *, label: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"{label} must be a list of strings")
        return []
    result: list[str] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{label} item {index} must be a non-empty string")
            continue
        result.append(item.strip())
    return result


def _safe_relative_path(value: str, *, field: str, label: str, errors: list[str]) -> str | None:
    raw = value.strip()
    if not raw:
        errors.append(f"{label}: {field} must be a non-empty string")
        return None
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        errors.append(f"{label}: {field} must be relative and must not contain '..'")
        return None
    return raw


def _load_review_config(target: Path) -> tuple[list[dict[str, Any]], list[str]]:
    path = helpers._review_config_path(target)
    if not path.is_file():
        return [], [f"review config missing: {path}"]
    if tomllib is None:
        return [], ["review config requires Python tomllib support"]
    try:
        payload = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:  # type: ignore[union-attr]
        return [], [f"invalid review config: {exc}"]
    values = payload.get("reviewer")
    if not isinstance(values, list):
        return [], ["review config must contain [[reviewer]] entries"]
    reviewers: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(values, start=1):
        label = f"reviewer {index}"
        if not isinstance(item, dict):
            errors.append(f"{label} must be a table")
            continue
        reviewer: dict[str, Any] = {}
        for field in constants.REVIEW_REQUIRED_FIELDS:
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{label}: {field} must be a non-empty string")
            else:
                reviewer[field] = value.strip()
        cwd = item.get("cwd", ".")
        if not isinstance(cwd, str):
            errors.append(f"{label}: cwd must be a string")
        else:
            safe = _safe_relative_path(cwd, field="cwd", label=label, errors=errors)
            if safe is not None:
                reviewer["cwd"] = safe
        for field in ("output_path", "findings_path"):
            value = reviewer.get(field)
            if isinstance(value, str):
                safe = _safe_relative_path(value, field=field, label=label, errors=errors)
                if safe is not None:
                    reviewer[field] = safe
        target_paths = _string_list(item.get("target_paths", []), label=f"{label}: target_paths", errors=errors)
        supported_modes = _string_list(
            item.get("supported_modes", []), label=f"{label}: supported_modes", errors=errors
        )
        reviewer["target_paths"] = target_paths
        reviewer["supported_modes"] = supported_modes
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            errors.append(f"{label}: enabled must be true or false")
        else:
            reviewer["enabled"] = enabled
        timeout = item.get("timeout", 600)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            errors.append(f"{label}: timeout must be a positive number")
        else:
            reviewer["timeout"] = float(timeout)
        base_ref = item.get("base_ref", "HEAD")
        if not isinstance(base_ref, str) or not base_ref.strip():
            errors.append(f"{label}: base_ref must be a non-empty string")
        else:
            reviewer["base_ref"] = base_ref.strip()
        privacy_mode = reviewer.get("privacy_mode")
        if isinstance(privacy_mode, str) and privacy_mode not in constants.REVIEW_PRIVACY_MODES:
            errors.append(f"{label}: privacy_mode must be one of: {', '.join(constants.REVIEW_PRIVACY_MODES)}")
        reviewer_id = reviewer.get("id")
        if isinstance(reviewer_id, str):
            if reviewer_id in seen_ids:
                errors.append(f"{label}: duplicate id {reviewer_id}")
            seen_ids.add(reviewer_id)
        if reviewer:
            reviewers.append(reviewer)
    return reviewers, errors


def _parse_clock_minutes(value: str) -> int | None:
    match = re.fullmatch(r"([0-2]?\d):([0-5]\d)", value.strip())
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23:
        return None
    return hour * 60 + minute


def _format_clock_minutes(value: int) -> str:
    minute = value % (24 * 60)
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _scanner_start_minute(cadence: str) -> int | None:
    daily = re.fullmatch(r"daily@(.+)", cadence.strip())
    if daily:
        return _parse_clock_minutes(daily.group(1))
    hourly = re.fullmatch(r"hourly@([0-5]?\d)", cadence.strip())
    if hourly:
        return int(hourly.group(1))
    return None


def _scanner_window_minutes(value: str) -> tuple[int, int] | None:
    if "-" not in value:
        return None
    start_raw, end_raw = value.split("-", 1)
    start = _parse_clock_minutes(start_raw)
    end = _parse_clock_minutes(end_raw)
    if start is None or end is None or start == end:
        return None
    if end < start:
        end += 24 * 60
    return start, end


def _scanner_duration_minutes(scanner: dict[str, Any]) -> int:
    timeout = scanner.get("timeout")
    seconds = float(timeout) if isinstance(timeout, (int, float)) else 300.0
    return max(5, int((seconds + 59) // 60))


def _scanner_command_ok(command: str) -> bool:
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts:
        return False
    executable = parts[0]
    if executable == "brigade":
        return True
    if "/" in executable:
        return Path(executable).expanduser().exists()
    return shutil.which(executable) is not None


def _scanner_argv(command: str) -> tuple[list[str] | None, str | None]:
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return None, f"invalid command: {exc}"
    if not parts:
        return None, "empty command"
    executable = Path(parts[0]).name
    if executable in constants.SCANNER_HIGH_RISK_COMMANDS:
        return None, f"high-risk scanner command: {executable}"
    if any(constants.SCANNER_SHELL_META_RE.search(part) for part in parts):
        return None, "high-risk scanner command contains shell metacharacters"
    if not _scanner_command_ok(command):
        return None, f"scanner command is not resolvable: {parts[0]}"
    return parts, None


def _scanner_output_path(target: Path, scanner: dict[str, Any]) -> Path | None:
    output = scanner.get("output_path")
    if not isinstance(output, str) or not output.strip():
        return None
    path = Path(output).expanduser()
    return path if path.is_absolute() else target / path


def _scanner_import_path(target: Path, scanner: dict[str, Any]) -> Path | None:
    value = scanner.get("import_path")
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else target / path


def _scanner_cwd(target: Path, scanner: dict[str, Any]) -> Path:
    raw = scanner.get("cwd")
    if isinstance(raw, str) and raw.strip():
        return (target / raw).resolve()
    return target


def _review_output_path(target: Path, reviewer: dict[str, Any]) -> Path | None:
    output = reviewer.get("output_path")
    if not isinstance(output, str) or not output.strip():
        return None
    path = Path(output).expanduser()
    return path if path.is_absolute() else target / path


def _review_findings_path(target: Path, reviewer: dict[str, Any]) -> Path | None:
    output = reviewer.get("findings_path")
    if not isinstance(output, str) or not output.strip():
        return None
    path = Path(output).expanduser()
    return path if path.is_absolute() else target / path


def _review_cwd(target: Path, reviewer: dict[str, Any]) -> Path:
    raw = reviewer.get("cwd")
    if isinstance(raw, str) and raw.strip():
        return (target / raw).resolve()
    return target


def _review_argv(command: str) -> tuple[list[str] | None, str | None]:
    return _scanner_argv(command)
