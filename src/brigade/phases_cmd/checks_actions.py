"""Auditable local phase execution ledger."""

# ruff: noqa: F401
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..localio import read_json_dict as _read_json, utc_now as _now, write_json as _write_json
from ..render import emit

from . import constants
from . import session_lifecycle
from . import session_ops


def _find_action(target: Path, action_id: str) -> tuple[Path, dict[str, Any] | None]:
    wanted = constants._slug(action_id)
    exact = constants._actions_root(target) / f"{wanted}.json"
    if exact.is_file():
        return exact, _read_json(exact)
    matches = [path for path in constants._actions_root(target).glob("*.json") if path.stem.startswith(wanted)]
    if len(matches) == 1:
        return matches[0], _read_json(matches[0])
    return exact, None


def _git_head(target: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=target,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _git_commit_exists(target: Path, commit_hash: str) -> bool:
    if not commit_hash:
        return False
    try:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{commit_hash}^{{commit}}"],
            cwd=target,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return result.returncode == 0


def _git_commit_on_branch(target: Path, commit_hash: str) -> bool:
    if not commit_hash:
        return False
    try:
        result = subprocess.run(
            ["git", "branch", "--contains", commit_hash],
            cwd=target,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def _git_dirty_paths(target: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=target,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    paths = []
    for line in result.stdout.splitlines():
        if line.strip():
            paths.append(line[3:] if len(line) > 3 else line.strip())
    return paths


def _same_commit(expected: str, current: str) -> bool:
    if not expected or not current:
        return True
    return expected.startswith(current) or current.startswith(expected)


def _phase_has_current_closeout(target: Path, phase_id: str, record: dict[str, Any]) -> bool:
    wanted = constants._source_fingerprint([record])
    records_by_id = {str(item.get("phase_id")): item for item in constants._records(target)}
    for item in constants._read_closeouts(target):
        if phase_id not in (item.get("phase_ids") or []) or item.get("status") not in constants.PHASE_CLOSEOUT_STATUSES:
            continue
        if item.get("source_fingerprint") == wanted:
            return True
        phase_fingerprints = item.get("phase_fingerprints") if isinstance(item.get("phase_fingerprints"), dict) else {}
        if phase_fingerprints.get(phase_id) == wanted:
            return True
        closeout_phase_ids = [str(value) for value in item.get("phase_ids") or []]
        closeout_records = [records_by_id[value] for value in closeout_phase_ids if value in records_by_id]
        if closeout_records and item.get("source_fingerprint") == constants._source_fingerprint(closeout_records):
            return True
    return False


def _action_source_fingerprint(phase_id: str, issue_type: str, detail: str) -> str:
    return hashlib.sha256(f"{phase_id}:{issue_type}:{detail}".encode("utf-8")).hexdigest()[:16]


def _action_summary(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_id": action.get("action_id"),
        "phase_id": action.get("phase_id"),
        "issue_type": action.get("issue_type"),
        "status": action.get("status"),
        "safe_summary": action.get("safe_summary"),
        "source_fingerprint": action.get("source_fingerprint"),
        "suggested_next_command": action.get("suggested_next_command"),
        "path": action.get("path"),
    }


def _phase_action_candidates(target: Path, *, phase_range: str | None = None) -> list[dict[str, Any]]:
    doctor_data = doctor_payload(target, phase_range=phase_range)
    candidates: list[dict[str, Any]] = []
    try:
        parsed_range = constants._parse_range(phase_range)
    except ValueError:
        parsed_range = None

    def phase_in_scope(phase_id: str) -> bool:
        if parsed_range is None or phase_id == "ledger" or not phase_id.startswith("phase-"):
            return True
        try:
            number = int(phase_id.split("-", 1)[1])
        except ValueError:
            return True
        return number in parsed_range

    for check in doctor_data["checks"]:
        if check.get("status") == "ok":
            continue
        phase_id = str(check.get("phase_id") or "ledger")
        if not phase_in_scope(phase_id):
            continue
        issue_type = str(check.get("name") or "phase_issue")
        detail = str(check.get("detail") or "")
        fingerprint = _action_source_fingerprint(phase_id, issue_type, detail)
        candidates.append(
            {
                "schema_version": constants.SCHEMA_VERSION,
                "schema": constants._schema("phase-ledger-action"),
                "action_id": f"phase-action-{constants._slug(phase_id)}-{constants._slug(issue_type)}-{fingerprint[:8]}",
                "phase_id": phase_id,
                "issue_type": issue_type,
                "status": "pending",
                "safe_summary": detail,
                "source": "doctor",
                "source_fingerprint": fingerprint,
                "source_status": check.get("status"),
                "created_at": None,
                "updated_at": None,
                "reviewed_at": None,
                "review_reason": "",
                "suggested_next_command": check.get("suggested_next_command") or "brigade work phases doctor",
            }
        )
    for closeout_record in constants._read_closeouts(target):
        if (
            closeout_record.get("status") not in {"blocked", "deferred"}
            and int(closeout_record.get("unresolved_issue_count") or 0) == 0
        ):
            continue
        for issue in closeout_record.get("unresolved_issues") or []:
            if not isinstance(issue, dict):
                continue
            phase_id = str(issue.get("phase_id") or "ledger")
            if not phase_in_scope(phase_id):
                continue
            issue_type = f"closeout_{issue.get('name') or 'blocker'}"
            detail = str(issue.get("detail") or closeout_record.get("reason") or "closeout blocker")
            fingerprint = _action_source_fingerprint(phase_id, issue_type, detail)
            candidates.append(
                {
                    "schema_version": constants.SCHEMA_VERSION,
                    "schema": constants._schema("phase-ledger-action"),
                    "action_id": f"phase-action-{constants._slug(phase_id)}-{constants._slug(issue_type)}-{fingerprint[:8]}",
                    "phase_id": phase_id,
                    "issue_type": issue_type,
                    "status": "pending",
                    "safe_summary": detail,
                    "source": "closeout",
                    "source_closeout_id": closeout_record.get("closeout_id"),
                    "source_fingerprint": fingerprint,
                    "source_status": issue.get("status"),
                    "created_at": None,
                    "updated_at": None,
                    "reviewed_at": None,
                    "review_reason": "",
                    "suggested_next_command": issue.get("suggested_next_command")
                    or "brigade work phases closeout latest",
                }
            )
    latest_checkpoint, _checkpoint_error = constants._resolve_session_checkpoint(target, "latest")
    if isinstance(latest_checkpoint, dict):
        checkpoint_id = str(latest_checkpoint.get("checkpoint_id") or "latest")
        phase_id = str(latest_checkpoint.get("phase_id") or "ledger")
        if phase_in_scope(phase_id) and latest_checkpoint.get("status") == "blocked":
            issue_type = "phase_session_checkpoint_blocked"
            detail = str(latest_checkpoint.get("summary") or "phase session checkpoint is blocked")
            fingerprint = _action_source_fingerprint(phase_id, issue_type, detail)
            candidates.append(
                {
                    "schema_version": constants.SCHEMA_VERSION,
                    "schema": constants._schema("phase-ledger-action"),
                    "action_id": f"phase-action-{constants._slug(phase_id)}-{constants._slug(issue_type)}-{fingerprint[:8]}",
                    "phase_id": phase_id,
                    "issue_type": issue_type,
                    "status": "pending",
                    "safe_summary": detail,
                    "source": "checkpoint",
                    "source_checkpoint_id": checkpoint_id,
                    "source_fingerprint": fingerprint,
                    "source_status": latest_checkpoint.get("status"),
                    "created_at": None,
                    "updated_at": None,
                    "reviewed_at": None,
                    "review_reason": "",
                    "suggested_next_command": f"brigade work phases session checkpoints show {checkpoint_id}",
                }
            )
        try:
            checkpoint_compare = session_lifecycle._session_checkpoint_compare_payload(target, latest_checkpoint)
        except ValueError:
            checkpoint_compare = None
        if isinstance(checkpoint_compare, dict):
            for check in checkpoint_compare.get("checks") or []:
                if not isinstance(check, dict) or check.get("status") == "ok":
                    continue
                phase_id = str(check.get("phase_id") or latest_checkpoint.get("phase_id") or "ledger")
                if not phase_in_scope(phase_id):
                    continue
                issue_type = str(check.get("name") or "phase_session_checkpoint_issue")
                detail = str(check.get("detail") or "phase session checkpoint compare issue")
                fingerprint = _action_source_fingerprint(phase_id, issue_type, detail)
                candidates.append(
                    {
                        "schema_version": constants.SCHEMA_VERSION,
                        "schema": constants._schema("phase-ledger-action"),
                        "action_id": f"phase-action-{constants._slug(phase_id)}-{constants._slug(issue_type)}-{fingerprint[:8]}",
                        "phase_id": phase_id,
                        "issue_type": issue_type,
                        "status": "pending",
                        "safe_summary": detail,
                        "source": "checkpoint",
                        "source_checkpoint_id": checkpoint_id,
                        "source_fingerprint": fingerprint,
                        "source_status": check.get("status"),
                        "created_at": None,
                        "updated_at": None,
                        "reviewed_at": None,
                        "review_reason": "",
                        "suggested_next_command": check.get("suggested_next_command")
                        or "brigade work phases session checkpoints compare latest",
                    }
                )
    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for candidate in candidates:
        key = (
            str(candidate.get("phase_id")),
            str(candidate.get("issue_type")),
            str(candidate.get("source_fingerprint")),
        )
        deduped.setdefault(key, candidate)
    return list(deduped.values())


def _check(
    status: str, name: str, detail: str, *, phase_id: str | None = None, suggested: str = "brigade work phases doctor"
) -> dict[str, Any]:
    return {
        "status": status,
        "name": name,
        "detail": detail,
        "phase_id": phase_id,
        "suggested_next_command": suggested,
    }


def doctor_payload(target: Path, *, phase_range: str | None = None) -> dict[str, Any]:
    target = target.expanduser().resolve()
    records = constants._records(target)
    checks: list[dict[str, Any]] = []
    try:
        parsed_range = constants._parse_range(phase_range)
    except ValueError as exc:
        parsed_range = None
        checks.append(_check("fail", "phase_range_invalid", str(exc)))
    if parsed_range is not None:
        start, end = parsed_range
        present = {str(record.get("phase_id")) for record in records}
        missing = [
            constants._phase_id_for(number)
            for number in range(start, end + 1)
            if constants._phase_id_for(number) not in present
        ]
        if missing:
            checks.append(
                _check(
                    "warn",
                    "phase_range_missing_records",
                    f"{len(missing)} missing phase record(s): {', '.join(missing[:10])}",
                    suggested=f"brigade work phases plan --range {start}-{end}",
                )
            )
        else:
            checks.append(_check("ok", "phase_range_records", f"{start}-{end} present"))
    now = _now()
    for record in records:
        phase_id = str(record.get("phase_id") or "unknown")
        status = str(record.get("status") or "unknown")
        kind = str(record.get("kind") or "phase")
        if record.get("parse_error"):
            checks.append(
                _check("fail", "phase_record_parse_error", str(record.get("path") or phase_id), phase_id=phase_id)
            )
            continue
        if status in constants.DONE_STATUSES:
            if not record.get("tests_run"):
                checks.append(
                    _check(
                        "warn",
                        "phase_complete_without_tests",
                        "phase is marked complete without tests run",
                        phase_id=phase_id,
                        suggested=f"brigade work phases show {phase_id}",
                    )
                )
            if not record.get("files_changed") and not record.get("deferred_items"):
                checks.append(
                    _check(
                        "warn",
                        "phase_complete_without_changes_or_deferral",
                        "phase is complete without changed files or deferral evidence",
                        phase_id=phase_id,
                        suggested=f"brigade work phases show {phase_id}",
                    )
                )
            for attachment in record.get("evidence_attachments") or []:
                if not isinstance(attachment, dict):
                    continue
                for key in ("files_changed", "handoff_paths"):
                    for rel_path in attachment.get(key) or []:
                        if rel_path and not (target / str(rel_path)).exists():
                            checks.append(
                                _check(
                                    "warn",
                                    "phase_evidence_missing_reference",
                                    f"missing {key[:-1]} evidence: {rel_path}",
                                    phase_id=phase_id,
                                    suggested=f"brigade work phases evidence add {phase_id}",
                                )
                            )
            completed = constants._parse_time(record.get("completed_at"))
            if (
                completed
                and now - completed > timedelta(hours=constants.STALE_UNREVIEWED_COMPLETED_HOURS)
                and not _phase_has_current_closeout(target, phase_id, record)
            ):
                checks.append(
                    _check(
                        "warn",
                        "phase_stale_unreviewed_completed",
                        f"completed phase has not been reviewed for more than {constants.STALE_UNREVIEWED_COMPLETED_HOURS}h",
                        phase_id=phase_id,
                        suggested=f"brigade work phases closeout {phase_id}",
                    )
                )
        if status in {"committed", "pushed"} and not str(record.get("commit_hash") or "").strip():
            checks.append(
                _check(
                    "warn",
                    "phase_committed_without_hash",
                    "phase is committed without a commit hash",
                    phase_id=phase_id,
                    suggested=f"brigade work phases complete {phase_id} --commit <hash>",
                )
            )
        if status == "pushed" and not str(record.get("push_ref") or "").strip():
            checks.append(
                _check(
                    "warn",
                    "phase_pushed_without_ref",
                    "phase is pushed without a push ref",
                    phase_id=phase_id,
                    suggested=f"brigade work phases complete {phase_id} --push-ref <ref>",
                )
            )
        if status == "in-progress":
            started = constants._parse_time(record.get("started_at"))
            if started and now - started > timedelta(hours=constants.STALE_IN_PROGRESS_HOURS):
                checks.append(
                    _check(
                        "warn",
                        "phase_stale_in_progress",
                        f"phase has been in progress for more than {constants.STALE_IN_PROGRESS_HOURS}h",
                        phase_id=phase_id,
                        suggested=f"brigade work phases show {phase_id}",
                    )
                )
        if status == "blocked" and not str(record.get("next_phase_recommendation") or "").strip():
            checks.append(
                _check(
                    "warn",
                    "phase_blocked_without_next_step",
                    "blocked phase is missing a next phase recommendation",
                    phase_id=phase_id,
                    suggested=f"brigade work phases show {phase_id}",
                )
            )
        phase_range_value = str(record.get("phase_range") or "")
        if kind != "group" and re.fullmatch(r"\d+-\d+", phase_range_value) and not record.get("explicit_grouping"):
            checks.append(
                _check(
                    "warn",
                    "phase_range_compressed_without_group",
                    "phase range record lacks explicit grouping",
                    phase_id=phase_id,
                    suggested="brigade work phases plan --grouped",
                )
            )
        if kind == "group" and not record.get("explicit_grouping"):
            checks.append(
                _check(
                    "warn",
                    "phase_group_without_explicit_grouping",
                    "group record is missing explicit grouping marker",
                    phase_id=phase_id,
                    suggested=f"brigade work phases show {phase_id}",
                )
            )
    issue_checks = [check for check in checks if check["status"] != "ok"]
    if not issue_checks:
        checks.append(_check("ok", "phase_ledger", f"{len(records)} phase record(s) checked"))
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-doctor"),
        "target": str(target),
        "records_path": str(constants._records_root(target)),
        "record_count": len(records),
        "checks": checks,
        "issue_count": len(issue_checks),
        "top_issue": issue_checks[0] if issue_checks else None,
        "suggested_next_command": issue_checks[0]["suggested_next_command"]
        if issue_checks
        else "brigade work phases list",
    }
    return payload


def doctor(*, target: Path, phase_range: str | None = None, json_output: bool = False) -> int:
    payload = doctor_payload(target, phase_range=phase_range)
    lines = []
    lines.append(f"phase ledger doctor: {payload['target']}")
    for check in payload["checks"]:
        lines.append(f"[{check['status']}] {check['name']}: {check['detail']}")
    return emit(
        payload, json_output, lines, 1 if any(check.get("status") == "fail" for check in payload["checks"]) else 0
    )


def health(target: Path) -> dict[str, Any]:
    payload = doctor_payload(target)
    records = constants._records(target)
    open_records = [record for record in records if record.get("status") in {"pending", "in-progress", "blocked"}]
    target = target.expanduser().resolve()
    closeouts = constants._read_closeouts(target)
    latest_report = constants._latest_report(target)
    latest_report_compare = constants._report_compare_summary(target, latest_report)
    latest_session = constants._latest_session(target)
    latest_session_checkpoint = (
        constants._latest_checkpoint_for_session(target, latest_session.get("session_id"))
        if isinstance(latest_session, dict)
        else None
    )
    latest_session_checkpoint_compare = None
    if isinstance(latest_session_checkpoint, dict):
        try:
            latest_session_checkpoint_compare = session_lifecycle._session_checkpoint_compare_payload(
                target, latest_session_checkpoint
            )
        except ValueError:
            latest_session_checkpoint_compare = None
    latest_session_report = (
        constants._read_session_reports(target)[-1] if constants._read_session_reports(target) else None
    )
    latest_session_gate = (
        session_ops._session_gate_payload(target, latest_session) if isinstance(latest_session, dict) else None
    )
    actions = constants._read_actions(target)
    open_actions = [action for action in actions if action.get("status") in {"pending", "active"}]
    action_counts: dict[str, int] = {}
    for action in actions:
        status = str(action.get("status") or "unknown")
        action_counts[status] = action_counts.get(status, 0) + 1
    return {
        "records_path": str(constants._records_root(target)),
        "record_count": len(records),
        "open_count": len(open_records),
        "latest": constants._record_summary(records[-1]) if records else None,
        "latest_closeout": closeouts[-1] if closeouts else None,
        "latest_report": {
            "report_id": latest_report.get("report_id"),
            "created_at": latest_report.get("created_at"),
            "path": latest_report.get("path"),
            "issue_count": (latest_report.get("doctor") or {}).get("issue_count")
            if isinstance(latest_report.get("doctor"), dict)
            else None,
        }
        if latest_report
        else None,
        "latest_report_compare": latest_report_compare,
        "latest_session": constants._session_summary(latest_session) if isinstance(latest_session, dict) else None,
        "latest_session_checkpoint": constants._checkpoint_summary(latest_session_checkpoint)
        if isinstance(latest_session_checkpoint, dict)
        else None,
        "latest_session_checkpoint_compare": {
            "checkpoint_id": latest_session_checkpoint_compare.get("checkpoint_id"),
            "session_id": latest_session_checkpoint_compare.get("session_id"),
            "issue_count": latest_session_checkpoint_compare.get("issue_count"),
            "top_issue": latest_session_checkpoint_compare.get("top_issue"),
            "suggested_next_command": latest_session_checkpoint_compare.get("suggested_next_command"),
        }
        if isinstance(latest_session_checkpoint_compare, dict)
        else None,
        "latest_session_gate": {
            "session_id": latest_session_gate.get("session_id"),
            "safe_to_claim_complete": latest_session_gate.get("safe_to_claim_complete"),
            "blocker_count": latest_session_gate.get("blocker_count"),
            "top_blocker": latest_session_gate.get("top_blocker"),
            "suggested_next_command": latest_session_gate.get("suggested_next_command"),
        }
        if isinstance(latest_session_gate, dict)
        else None,
        "latest_session_report": {
            "report_id": latest_session_report.get("report_id"),
            "session_id": (latest_session_report.get("session") or {}).get("session_id")
            if isinstance(latest_session_report.get("session"), dict)
            else None,
            "created_at": latest_session_report.get("created_at"),
            "path": latest_session_report.get("path"),
            "issue_count": (latest_session_report.get("doctor") or {}).get("issue_count")
            if isinstance(latest_session_report.get("doctor"), dict)
            else None,
        }
        if isinstance(latest_session_report, dict)
        else None,
        "closeout_count": len(closeouts),
        "actions_path": str(constants._actions_root(target)),
        "action_count": len(actions),
        "open_action_count": len(open_actions),
        "action_counts": action_counts,
        "top_action": _action_summary(open_actions[0]) if open_actions else None,
        "checks": payload["checks"],
        "issue_count": payload["issue_count"],
        "top_issue": payload["top_issue"],
    }


def closeout(
    *, target: Path, selector: str, status: str = "reviewed", reason: str | None = None, json_output: bool = False
) -> int:
    if status not in constants.PHASE_CLOSEOUT_STATUSES:
        print(f"error: --status must be one of {sorted(constants.PHASE_CLOSEOUT_STATUSES)}", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    records, missing, parsed_range = constants._selected_records(target, selector)
    if not records or missing:
        print(f"error: phase selector has missing records: {', '.join(missing or [selector])}", file=sys.stderr)
        return 1
    phase_ids = [str(record.get("phase_id")) for record in records if record.get("phase_id")]
    doctor_data = doctor_payload(target, phase_range=parsed_range)
    selected_issues = [
        check
        for check in doctor_data["checks"]
        if check.get("status") != "ok" and (not check.get("phase_id") or check.get("phase_id") in phase_ids)
    ]
    deferred_phase_ids = [str(record.get("phase_id")) for record in records if record.get("status") == "deferred"]
    if status == "deferred":
        deferred_phase_ids = phase_ids
    fingerprint = constants._source_fingerprint(records)
    closeout_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-phase-closeout-{uuid4().hex[:6]}"
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-closeout"),
        "target": str(target),
        "closeout_id": closeout_id,
        "selector": selector,
        "phase_range": parsed_range,
        "phase_ids": phase_ids,
        "status": status,
        "reason": reason or "",
        "reviewed_at": _now().isoformat(),
        "unresolved_issue_count": len(selected_issues),
        "unresolved_issues": selected_issues,
        "deferred_phase_ids": deferred_phase_ids,
        "phase_fingerprints": {
            str(record.get("phase_id")): constants._source_fingerprint([record])
            for record in records
            if record.get("phase_id")
        },
        "source_fingerprint": fingerprint,
        "suggested_next_command": "brigade work phases doctor",
    }
    path = constants._closeouts_root(target) / f"{closeout_id}.json"
    payload["path"] = str(path)
    _write_json(path, payload)
    lines = []
    lines.append(f"phase closeout: {closeout_id}")
    lines.append(f"status: {status}")
    lines.append(f"phases: {', '.join(phase_ids)}")
    lines.append(f"unresolved: {len(selected_issues)}")
    return emit(payload, json_output, lines, 0)


def evidence_add(
    *,
    target: Path,
    phase_id: str,
    files_changed: list[str] | None = None,
    tests_run: list[str] | None = None,
    test_result_summary: str | None = None,
    report_ids: list[str] | None = None,
    handoff_paths: list[str] | None = None,
    notes: list[str] | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    path, record = constants._find_record(target, phase_id)
    if record is None:
        print(f"error: phase record not found: {phase_id}", file=sys.stderr)
        return 1
    attachment = {
        "attached_at": _now().isoformat(),
        "files_changed": [str(item) for item in (files_changed or []) if str(item)],
        "tests_run": [str(item) for item in (tests_run or []) if str(item)],
        "test_result_summary": test_result_summary or "",
        "report_ids": [str(item) for item in (report_ids or []) if str(item)],
        "handoff_paths": [str(item) for item in (handoff_paths or []) if str(item)],
        "notes": [str(item) for item in (notes or []) if str(item)],
    }
    attachments = record.get("evidence_attachments") if isinstance(record.get("evidence_attachments"), list) else []
    attachments.append(attachment)
    record["evidence_attachments"] = attachments
    if attachment["files_changed"]:
        record["files_changed"] = constants._append_unique(record.get("files_changed", []), attachment["files_changed"])
    if attachment["tests_run"]:
        record["tests_run"] = constants._append_unique(record.get("tests_run", []), attachment["tests_run"])
    if test_result_summary:
        record["test_result_summary"] = test_result_summary
    record["updated_at"] = _now().isoformat()
    record["path"] = str(path)
    _write_json(path, record)
    lines = []
    lines.append(f"phase evidence: {record.get('phase_id')}")
    lines.append(f"attachments: {len(attachments)}")
    return emit(record, json_output, lines, 0)


def _verification_entries(record: dict[str, Any]) -> list[dict[str, Any]]:
    existing = record.get("verification_matrix") if isinstance(record.get("verification_matrix"), list) else []
    by_command = {
        str(item.get("command")): dict(item) for item in existing if isinstance(item, dict) and item.get("command")
    }
    for command in record.get("tests_run") or []:
        rendered = str(command)
        by_command.setdefault(
            rendered,
            {
                "command": rendered,
                "status": "expected",
                "summary": "",
                "recorded_at": None,
            },
        )
    if not by_command:
        by_command["focused verification not declared"] = {
            "command": "focused verification not declared",
            "status": "deferred",
            "summary": "No phase-specific verification command has been recorded.",
            "recorded_at": None,
        }
    return list(by_command.values())


def verify_plan(*, target: Path, selector: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    records, missing, parsed_range = constants._selected_records(target, selector)
    if missing:
        payload = {
            "schema_version": constants.SCHEMA_VERSION,
            "schema": constants._schema("phase-ledger-verify-plan"),
            "target": str(target),
            "selector": selector,
            "phase_range": parsed_range,
            "missing_phase_ids": missing,
            "records": [],
            "record_count": 0,
            "suggested_next_command": f"brigade work phases plan --range {parsed_range or selector}",
        }
    else:
        record_payloads = []
        for record in records:
            record_payloads.append(
                {
                    "phase_id": record.get("phase_id"),
                    "status": record.get("status"),
                    "verification": _verification_entries(record),
                }
            )
        payload = {
            "schema_version": constants.SCHEMA_VERSION,
            "schema": constants._schema("phase-ledger-verify-plan"),
            "target": str(target),
            "selector": selector,
            "phase_range": parsed_range,
            "missing_phase_ids": [],
            "records": record_payloads,
            "record_count": len(record_payloads),
            "suggested_next_command": f"brigade work phases verify record {records[0].get('phase_id')}"
            if records
            else "brigade work phases status",
        }
    lines = []
    lines.append(f"phase verification plan: {selector}")
    lines.append(f"records: {payload['record_count']}")
    for record in payload["records"]:
        lines.append(f"- {record.get('phase_id')} verification={len(record.get('verification') or [])}")
    return emit(payload, json_output, lines, 0)


def verify_record(
    *, target: Path, phase_id: str, command: str, status: str, summary: str | None = None, json_output: bool = False
) -> int:
    if status not in constants.PHASE_VERIFY_STATUSES - {"expected"}:
        print(
            f"error: --status must be one of {sorted(constants.PHASE_VERIFY_STATUSES - {'expected'})}", file=sys.stderr
        )
        return 2
    target = target.expanduser().resolve()
    path, record = constants._find_record(target, phase_id)
    if record is None:
        print(f"error: phase record not found: {phase_id}", file=sys.stderr)
        return 1
    entries = [entry for entry in _verification_entries(record) if entry.get("command") != command]
    entry = {
        "command": command,
        "status": status,
        "summary": summary or "",
        "recorded_at": _now().isoformat(),
    }
    entries.append(entry)
    record["verification_matrix"] = entries
    if command != "focused verification not declared":
        record["tests_run"] = constants._append_unique(record.get("tests_run", []), [command])
    if summary:
        record["test_result_summary"] = summary
    record["updated_at"] = _now().isoformat()
    record["path"] = str(path)
    _write_json(path, record)
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-verify-record"),
        "target": str(target),
        "phase_id": record.get("phase_id"),
        "recorded": entry,
        "verification": entries,
        "suggested_next_command": f"brigade work phases verify plan {record.get('phase_id')}",
    }
    lines = []
    lines.append(f"phase verification: {record.get('phase_id')}")
    lines.append(f"status: {status}")
    return emit(payload, json_output, lines, 0)


def reconcile(*, target: Path, selector: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    records, missing, parsed_range = constants._selected_records(target, selector)
    checks: list[dict[str, Any]] = []
    if missing:
        checks.append(
            _check(
                "warn",
                "phase_reconcile_missing_records",
                f"missing phase record(s): {', '.join(missing)}",
                suggested=f"brigade work phases plan --range {parsed_range or selector}",
            )
        )
    dirty_paths = _git_dirty_paths(target)
    if dirty_paths:
        checks.append(
            _check(
                "warn",
                "phase_reconcile_dirty_worktree",
                f"{len(dirty_paths)} dirty path(s)",
                suggested="git status --short",
            )
        )
    for record in records:
        phase_id = str(record.get("phase_id") or "unknown")
        status = str(record.get("status") or "")
        commit_hash = str(record.get("commit_hash") or "")
        push_ref = str(record.get("push_ref") or "")
        if status in {"committed", "pushed"} and not commit_hash:
            checks.append(
                _check(
                    "warn",
                    "phase_reconcile_missing_commit_hash",
                    "phase status requires commit hash",
                    phase_id=phase_id,
                    suggested=f"brigade work phases complete {phase_id} --commit <hash>",
                )
            )
            continue
        if commit_hash and not _git_commit_exists(target, commit_hash):
            checks.append(
                _check(
                    "warn",
                    "phase_reconcile_commit_missing",
                    f"commit not found locally: {commit_hash}",
                    phase_id=phase_id,
                    suggested="git log --oneline",
                )
            )
        elif commit_hash and not _git_commit_on_branch(target, commit_hash):
            checks.append(
                _check(
                    "warn",
                    "phase_reconcile_commit_not_on_branch",
                    f"commit is not on a local branch: {commit_hash}",
                    phase_id=phase_id,
                    suggested="git branch --contains <hash>",
                )
            )
        if status == "pushed" and not push_ref:
            checks.append(
                _check(
                    "warn",
                    "phase_reconcile_pushed_without_ref",
                    "pushed phase lacks push ref",
                    phase_id=phase_id,
                    suggested=f"brigade work phases complete {phase_id} --push-ref <ref>",
                )
            )
    if not checks:
        checks.append(_check("ok", "phase_reconcile_clean", "selected phase records match local git evidence"))
    issues = [check for check in checks if check.get("status") != "ok"]
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-reconcile"),
        "target": str(target),
        "selector": selector,
        "phase_range": parsed_range,
        "records": [constants._record_summary(record) for record in records],
        "record_count": len(records),
        "dirty_paths": dirty_paths[:20],
        "checks": checks,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "suggested_next_command": issues[0]["suggested_next_command"] if issues else "brigade work phases status",
    }
    lines = []
    lines.append(f"phase reconcile: {selector}")
    lines.append(f"records: {len(records)}")
    for check in checks:
        lines.append(f"[{check['status']}] {check['name']}: {check['detail']}")
    return emit(payload, json_output, lines, 0)


def _privacy_findings_for_text(text: str, *, source: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for name, pattern in constants.PRIVACY_PATTERNS.items():
        for match in pattern.finditer(text):
            line_number = text.count("\n", 0, match.start()) + 1
            findings.append(
                {
                    "status": "warn",
                    "name": f"phase_privacy_{name}",
                    "source": source,
                    "line": line_number,
                    "detail": f"{name} pattern found in phase evidence",
                }
            )
            break
    return findings


def _git_added_text_for_file(target: Path, commit_hash: str, rel_path: str) -> str | None:
    if not commit_hash:
        return None
    try:
        result = subprocess.run(
            ["git", "show", "--format=", "--unified=0", commit_hash, "--", rel_path],
            cwd=target,
            check=False,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    added = []
    for line in result.stdout.splitlines():
        if line.startswith("+++") or not line.startswith("+"):
            continue
        added.append(line[1:])
    return "\n".join(added)


def privacy(*, target: Path, selector: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    records, missing, parsed_range = constants._selected_records(target, selector)
    findings: list[dict[str, Any]] = []
    if missing:
        findings.append(
            {
                "status": "warn",
                "name": "phase_privacy_missing_records",
                "source": selector,
                "line": None,
                "detail": f"missing phase record(s): {', '.join(missing)}",
            }
        )
    scan_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-phase-privacy-{uuid4().hex[:6]}"
    for record in records:
        phase_findings: list[dict[str, Any]] = []
        commit_hash = str(record.get("commit_hash") or "")
        for rel_path in record.get("files_changed") or []:
            rendered_path = str(rel_path)
            text = _git_added_text_for_file(target, commit_hash, rendered_path)
            if text is None:
                path = target / rendered_path
                if not path.is_file():
                    continue
                try:
                    text = path.read_text(errors="replace")
                except OSError:
                    continue
            phase_findings.extend(_privacy_findings_for_text(text, source=rendered_path))
        if record.get("implementation_summary"):
            phase_findings.extend(
                _privacy_findings_for_text(
                    str(record.get("implementation_summary")), source=f"{record.get('phase_id')}:summary"
                )
            )
        findings.extend([{**finding, "phase_id": record.get("phase_id")} for finding in phase_findings])
        path, current = constants._find_record(target, str(record.get("phase_id")))
        if current is not None:
            checks = current.get("privacy_checks") if isinstance(current.get("privacy_checks"), list) else []
            checks.append(
                {
                    "scan_id": scan_id,
                    "scanned_at": _now().isoformat(),
                    "selector": selector,
                    "finding_count": len(phase_findings),
                    "status": "blocked" if phase_findings else "clean",
                }
            )
            current["privacy_checks"] = checks[-20:]
            current["updated_at"] = _now().isoformat()
            current["path"] = str(path)
            _write_json(path, current)
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-privacy"),
        "target": str(target),
        "selector": selector,
        "phase_range": parsed_range,
        "scan_id": scan_id,
        "record_count": len(records),
        "findings": findings,
        "finding_count": len(findings),
        "status": "blocked" if findings else "clean",
        "suggested_next_command": "brigade work phases privacy " + selector,
    }
    lines = []
    lines.append(f"phase privacy: {selector}")
    lines.append(f"status: {payload['status']}")
    lines.append(f"findings: {len(findings)}")
    for finding in findings:
        lines.append(f"[{finding['status']}] {finding['name']}: {finding['source']}")
    return emit(payload, json_output, lines, 1 if findings else 0)


def _handoff_root(target: Path) -> Path:
    return target / ".claude" / "memory-handoffs"


def _safe_handoff_text(value: object) -> str:
    rendered = str(value or "").strip()
    for pattern in constants.PRIVACY_PATTERNS.values():
        rendered = pattern.sub("[redacted]", rendered)
    rendered = rendered.replace("##", "section")
    return rendered[:500] if rendered else "not recorded"


def _phase_handoff_content(records: list[dict[str, Any]], *, selector: str, handoff_id: str) -> str:
    phase_ids = [str(record.get("phase_id")) for record in records if record.get("phase_id")]
    lines = [
        "# Memory Handoff",
        "",
        "## Type",
        "workflow",
        "",
        "## Title",
        "Brigade phase execution ledger closeout",
        "",
        "## Summary",
        f"Brigade drafted a phase handoff for `{selector}` so durable AFK execution lessons can be reviewed without editing canonical memory directly.",
        "",
        "## Durable facts",
        f"- Handoff id: `{handoff_id}`",
        f"- Phase selector: `{selector}`",
        f"- Phase ids: `{', '.join(phase_ids) if phase_ids else 'none'}`",
        "- Source: local phase execution ledger",
        "",
        "## Evidence",
        "- Phase records are stored in the local phase execution ledger.",
        "- This draft omits raw logs, private paths, scanner output, and private evidence.",
        "",
        "## Recommended memory action",
        "no-card",
        "",
        "## Target document",
        ".learnings/LEARNINGS.md",
        "",
        "## Suggested document content",
        "### Brigade phase execution ledger closeout",
        "",
        f"Phase selector `{selector}` produced a reviewed handoff draft `{handoff_id}`. Preserve the useful AFK execution lesson after checking the local phase ledger evidence.",
        "",
        "Phase summaries:",
    ]
    for record in records:
        summary = _safe_handoff_text(
            record.get("implementation_summary") or record.get("title") or record.get("status")
        )
        lines.append(f"- `{record.get('phase_id')}` `{record.get('status')}`: {summary}")
    return "\n".join(lines).rstrip() + "\n"


def handoff(*, target: Path, selector: str, lint: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    records, missing, parsed_range = constants._selected_records(target, selector)
    if missing or not records:
        print(f"error: phase selector has missing records: {', '.join(missing or [selector])}", file=sys.stderr)
        return 1
    handoff_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-phase-handoff-{uuid4().hex[:6]}"
    path = _handoff_root(target) / f"{handoff_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_phase_handoff_content(records, selector=selector, handoff_id=handoff_id))
    lint_payload: dict[str, Any] = {"requested": lint, "status": "not-run", "errors": [], "warnings": []}
    if lint:
        from .. import handoff_cmd

        result = handoff_cmd.lint_file(path)
        lint_payload = {
            "requested": True,
            "status": "passed" if result.valid else "failed",
            "errors": list(result.errors),
            "warnings": list(result.warnings),
        }
    attachment = {
        "handoff_id": handoff_id,
        "path": str(path),
        "selector": selector,
        "phase_range": parsed_range,
        "created_at": _now().isoformat(),
        "lint": lint_payload,
        "target_document": ".learnings/LEARNINGS.md",
        "source_fingerprint": constants._source_fingerprint(records, {"handoff_id": handoff_id}),
    }
    for record in records:
        record_path, current = constants._find_record(target, str(record.get("phase_id")))
        if current is None:
            continue
        handoffs = current.get("phase_handoffs") if isinstance(current.get("phase_handoffs"), list) else []
        handoffs.append(attachment)
        current["phase_handoffs"] = handoffs[-20:]
        attachments = (
            current.get("evidence_attachments") if isinstance(current.get("evidence_attachments"), list) else []
        )
        attachments.append(
            {
                "attached_at": _now().isoformat(),
                "files_changed": [],
                "tests_run": [],
                "test_result_summary": "",
                "report_ids": [],
                "handoff_paths": [str(path)],
                "notes": [f"phase handoff draft {handoff_id}"],
            }
        )
        current["evidence_attachments"] = attachments[-50:]
        current["updated_at"] = _now().isoformat()
        current["path"] = str(record_path)
        _write_json(record_path, current)
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-handoff"),
        "target": str(target),
        "selector": selector,
        "phase_range": parsed_range,
        "phase_ids": [record.get("phase_id") for record in records],
        "handoff_id": handoff_id,
        "path": str(path),
        "lint": lint_payload,
        "suggested_next_command": f"brigade handoff lint --target . {path}",
    }
    lines = []
    lines.append(f"phase handoff: {handoff_id}")
    lines.append(f"path: {path}")
    lines.append(f"lint: {lint_payload['status']}")
    return emit(payload, json_output, lines, 1 if lint_payload["status"] == "failed" else 0)


def compare(*, target: Path, selector: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    records, missing, parsed_range = constants._selected_records(target, selector)
    checks: list[dict[str, Any]] = []
    if missing:
        checks.append(
            _check(
                "warn",
                "phase_compare_missing_records",
                f"missing phase record(s): {', '.join(missing)}",
                suggested=f"brigade work phases plan --range {parsed_range or selector}",
            )
        )
    current_head = _git_head(target)
    latest_report = constants._latest_report(target)
    doctor_data = doctor_payload(target, phase_range=parsed_range)
    current_issue_count = doctor_data["issue_count"]
    for record in records:
        phase_id = str(record.get("phase_id") or "unknown")
        commit_hash = str(record.get("commit_hash") or "")
        push_ref = str(record.get("push_ref") or "")
        if not commit_hash:
            checks.append(
                _check(
                    "warn",
                    "phase_compare_missing_commit_hash",
                    "phase record has no commit hash",
                    phase_id=phase_id,
                    suggested=f"brigade work phases complete {phase_id} --commit <hash>",
                )
            )
        elif current_head and not _same_commit(commit_hash, current_head):
            checks.append(
                _check(
                    "warn",
                    "phase_compare_changed_head",
                    f"current HEAD {current_head} differs from phase commit {commit_hash}",
                    phase_id=phase_id,
                    suggested=f"brigade work phases show {phase_id}",
                )
            )
        if record.get("status") == "pushed" and not push_ref:
            checks.append(
                _check(
                    "warn",
                    "phase_compare_missing_push_ref",
                    "pushed phase record has no push ref",
                    phase_id=phase_id,
                    suggested=f"brigade work phases complete {phase_id} --push-ref <ref>",
                )
            )
        missing_files = [
            path for path in record.get("files_changed") or [] if path and not (target / str(path)).exists()
        ]
        if missing_files:
            checks.append(
                _check(
                    "warn",
                    "phase_compare_missing_referenced_files",
                    f"missing referenced file(s): {', '.join(missing_files[:5])}",
                    phase_id=phase_id,
                    suggested=f"brigade work phases show {phase_id}",
                )
            )
        completed = constants._parse_time(record.get("completed_at"))
        report_created = constants._parse_time(latest_report.get("created_at")) if latest_report else None
        if completed and report_created and report_created > completed:
            checks.append(
                _check(
                    "warn",
                    "phase_compare_newer_phase_report",
                    f"newer phase report exists: {latest_report.get('report_id')}",
                    phase_id=phase_id,
                    suggested="brigade work phases report show latest",
                )
            )
        stored_issue_count = record.get("doctor_issue_count")
        if isinstance(stored_issue_count, int) and stored_issue_count != current_issue_count:
            checks.append(
                _check(
                    "warn",
                    "phase_compare_changed_doctor_issue_count",
                    f"doctor issue count changed from {stored_issue_count} to {current_issue_count}",
                    phase_id=phase_id,
                    suggested="brigade work phases doctor",
                )
            )
        if record.get("tests_run") and completed and report_created and report_created > completed:
            checks.append(
                _check(
                    "warn",
                    "phase_compare_newer_test_evidence",
                    "phase report is newer than stored test evidence",
                    phase_id=phase_id,
                    suggested="brigade work phases report show latest",
                )
            )
    if not checks:
        checks.append(_check("ok", "phase_compare_current", "selected phase evidence matches current local checks"))
    issues = [check for check in checks if check["status"] != "ok"]
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-compare"),
        "target": str(target),
        "selector": selector,
        "phase_range": parsed_range,
        "current_head": current_head,
        "latest_report": {
            "report_id": latest_report.get("report_id"),
            "created_at": latest_report.get("created_at"),
            "path": latest_report.get("path"),
        }
        if latest_report
        else None,
        "records": [constants._record_summary(record) for record in records],
        "record_count": len(records),
        "checks": checks,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "suggested_next_command": issues[0]["suggested_next_command"] if issues else "brigade work phases doctor",
    }
    lines = []
    lines.append(f"phase compare: {selector}")
    lines.append(f"records: {len(records)}")
    for check in checks:
        lines.append(f"[{check['status']}] {check['name']}: {check['detail']}")
    return emit(payload, json_output, lines, 0)


def actions_plan(*, target: Path, phase_range: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    existing = {
        (item.get("phase_id"), item.get("issue_type"), item.get("source_fingerprint")): item
        for item in constants._read_actions(target)
        if item.get("status") != "archived"
    }
    planned: list[dict[str, Any]] = []
    for candidate in _phase_action_candidates(target, phase_range=phase_range):
        key = (candidate.get("phase_id"), candidate.get("issue_type"), candidate.get("source_fingerprint"))
        planned.append(_action_summary(existing.get(key, candidate)))
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-action-plan"),
        "target": str(target),
        "phase_range": phase_range,
        "actions": planned,
        "action_count": len(planned),
        "suggested_next_command": "brigade work phases actions build",
    }
    lines = []
    lines.append(f"phase actions planned: {len(planned)}")
    for action in planned:
        lines.append(f"- {action.get('action_id')} [{action.get('status')}] {action.get('issue_type')}")
    return emit(payload, json_output, lines, 0)


def actions_build(*, target: Path, phase_range: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    constants._actions_root(target).mkdir(parents=True, exist_ok=True)
    existing = {
        (item.get("phase_id"), item.get("issue_type"), item.get("source_fingerprint")): item
        for item in constants._read_actions(target)
        if item.get("status") != "archived"
    }
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for candidate in _phase_action_candidates(target, phase_range=phase_range):
        key = (candidate.get("phase_id"), candidate.get("issue_type"), candidate.get("source_fingerprint"))
        if key in existing:
            skipped.append(_action_summary(existing[key]))
            continue
        now = _now().isoformat()
        candidate["created_at"] = now
        candidate["updated_at"] = now
        path = constants._actions_root(target) / f"{candidate['action_id']}.json"
        candidate["path"] = str(path)
        _write_json(path, candidate)
        created.append(_action_summary(candidate))
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-action-build"),
        "target": str(target),
        "phase_range": phase_range,
        "created": created,
        "skipped": skipped,
        "created_count": len(created),
        "skipped_count": len(skipped),
        "suggested_next_command": "brigade work phases actions list",
    }
    lines = []
    lines.append(f"phase actions created: {len(created)}")
    lines.append(f"phase actions skipped: {len(skipped)}")
    return emit(payload, json_output, lines, 0)


def actions_list(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    actions = [
        _action_summary(action) for action in constants._read_actions(target) if action.get("status") != "archived"
    ]
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-action-list"),
        "target": str(target),
        "actions": actions,
        "action_count": len(actions),
    }
    lines = []
    lines.append(f"phase actions: {len(actions)}")
    for action in actions:
        lines.append(f"- {action.get('action_id')} [{action.get('status')}] {action.get('issue_type')}")
    return emit(payload, json_output, lines, 0)


def actions_show(*, target: Path, action_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    path, action = _find_action(target, action_id)
    if action is None:
        print(f"error: phase action not found: {action_id}", file=sys.stderr)
        return 1
    action["path"] = str(path)
    lines = []
    lines.append(f"phase action: {action.get('action_id')}")
    lines.append(f"status: {action.get('status')}")
    lines.append(f"issue: {action.get('issue_type')}")
    lines.append(f"next: {action.get('suggested_next_command')}")
    return emit(action, json_output, lines, 0)


def _set_action_status(
    target: Path, action_id: str, status: str, reason: str | None = None
) -> tuple[int, dict[str, Any] | None]:
    if status not in constants.PHASE_ACTION_STATUSES:
        print(f"error: invalid phase action status: {status}", file=sys.stderr)
        return 2, None
    target = target.expanduser().resolve()
    path, action = _find_action(target, action_id)
    if action is None:
        print(f"error: phase action not found: {action_id}", file=sys.stderr)
        return 1, None
    action["status"] = status
    action["updated_at"] = _now().isoformat()
    if status in {"done", "deferred", "archived"}:
        action["reviewed_at"] = action["updated_at"]
    if reason is not None:
        action["review_reason"] = reason
    action["path"] = str(path)
    _write_json(path, action)
    return 0, action


def _actions_update_status(
    *, target: Path, action_id: str, status: str, reason: str | None = None, json_output: bool = False
) -> int:
    result, action = _set_action_status(target.expanduser().resolve(), action_id, status, reason)
    if result != 0 or action is None:
        return result
    lines = []
    lines.append(f"phase action: {action.get('action_id')}")
    lines.append(f"status: {status}")
    return emit(action, json_output, lines, 0)


def actions_start(*, target: Path, action_id: str, json_output: bool = False) -> int:
    return _actions_update_status(target=target, action_id=action_id, status="active", json_output=json_output)


def actions_done(*, target: Path, action_id: str, json_output: bool = False) -> int:
    return _actions_update_status(target=target, action_id=action_id, status="done", json_output=json_output)


def actions_defer(*, target: Path, action_id: str, reason: str, json_output: bool = False) -> int:
    if not reason.strip():
        print("error: --reason is required", file=sys.stderr)
        return 2
    return _actions_update_status(
        target=target, action_id=action_id, status="deferred", reason=reason, json_output=json_output
    )


def actions_archive(
    *, target: Path, action_id: str | None = None, completed: bool = False, json_output: bool = False
) -> int:
    target = target.expanduser().resolve()
    archived: list[dict[str, Any]] = []
    if completed:
        candidates = [
            action for action in constants._read_actions(target) if action.get("status") in {"done", "deferred"}
        ]
    elif action_id:
        path, action = _find_action(target, action_id)
        if action is None:
            print(f"error: phase action not found: {action_id}", file=sys.stderr)
            return 1
        action["path"] = str(path)
        candidates = [action]
    else:
        print("error: pass an action id or --completed", file=sys.stderr)
        return 2
    for action in candidates:
        result, updated = _set_action_status(target, str(action.get("action_id")), "archived")
        if result == 0 and updated is not None:
            archived.append(_action_summary(updated))
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-action-archive"),
        "target": str(target),
        "archived": archived,
        "archived_count": len(archived),
    }
    lines = []
    lines.append(f"phase actions archived: {len(archived)}")
    return emit(payload, json_output, lines, 0)


def actions_import_issues(*, target: Path, dry_run: bool = False, json_output: bool = False) -> int:
    from .. import work_cmd

    target = target.expanduser().resolve()
    records: list[dict[str, Any]] = []
    for action in constants._read_actions(target):
        if action.get("status") not in {"pending", "active"}:
            continue
        action_id = str(action.get("action_id") or "")
        issue_type = str(action.get("issue_type") or "phase_action")
        source_fingerprint = str(
            action.get("source_fingerprint")
            or hashlib.sha256(json.dumps(action, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        )
        records.append(
            {
                "kind": "task",
                "source": "phase-ledger-action",
                "text": f"Resolve phase ledger action: {issue_type}",
                "type": "workflow",
                "priority": "high" if "missing" in issue_type or "blocked" in issue_type else "normal",
                "acceptance": [
                    "The phase ledger action is resolved, deferred, or archived with a reason.",
                    "The affected phase ledger evidence has current tests, commit, push, closeout, or report metadata as appropriate.",
                    "`brigade work phases doctor` and `brigade work phases actions list` reflect the updated state.",
                ],
                "metadata": {
                    "phase_action_id": action_id,
                    "phase_id": action.get("phase_id"),
                    "issue_type": issue_type,
                    "safe_summary": action.get("safe_summary"),
                    "suggested_command": action.get("suggested_next_command"),
                    "source_item_key": f"phase-ledger-action:{action_id}",
                    "source_fingerprint": source_fingerprint,
                },
            }
        )
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    dismissed: list[dict[str, Any]] = []
    if dry_run:
        created = records
    elif records:
        created, skipped, dismissed = work_cmd._append_import_records(target, records)
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-action-import-issues"),
        "target": str(target),
        "dry_run": dry_run,
        "created": created,
        "skipped": skipped,
        "dismissed": dismissed,
        "invalid": [],
        "created_count": len(created),
        "skipped_count": len(skipped),
        "dismissed_count": len(dismissed),
        "invalid_count": 0,
    }
    lines = []
    lines.append(f"phase action imports: {target}")
    lines.append(f"created: {payload['created_count']}")
    lines.append(f"skipped: {payload['skipped_count']}")
    lines.append(f"dismissed: {payload['dismissed_count']}")
    return emit(payload, json_output, lines, 0)


def _report_payload(target: Path, *, phase_range: str | None = None) -> dict[str, Any]:
    target = target.expanduser().resolve()
    status_data = constants.status_payload(target, phase_range=phase_range)
    doctor_data = doctor_payload(target, phase_range=phase_range)
    records = constants._records(target)
    if phase_range:
        parsed = constants._parse_range(phase_range)
        if parsed is not None:
            start, end = parsed
            wanted = {constants._phase_id_for(number) for number in range(start, end + 1)}
            records = [record for record in records if record.get("phase_id") in wanted]
    report_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-phase-report-{uuid4().hex[:6]}"
    return {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-report"),
        "target": str(target),
        "report_id": report_id,
        "created_at": _now().isoformat(),
        "git_head": _git_head(target),
        "phase_range": phase_range,
        "status": status_data,
        "doctor": {
            "issue_count": doctor_data["issue_count"],
            "top_issue": doctor_data["top_issue"],
            "checks": doctor_data["checks"],
        },
        "records": [constants._record_summary(record) for record in records],
        "record_count": len(records),
        "suggested_next_commands": [
            "brigade work phases doctor",
            status_data.get("suggested_next_command") or "brigade work phases list",
        ],
    }


def _write_report_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# Brigade Phase Ledger Report",
        "",
        f"- Report id: `{payload['report_id']}`",
        f"- Created: `{payload['created_at']}`",
        f"- Phase range: `{payload.get('phase_range') or 'all'}`",
        f"- Records: `{payload['record_count']}`",
        f"- Issues: `{payload['doctor']['issue_count']}`",
        "",
        "## Status Counts",
        "",
    ]
    for status_name, count in sorted(payload["status"].get("status_counts", {}).items()):
        lines.append(f"- `{status_name}`: {count}")
    lines.extend(["", "## Checks", ""])
    for check in payload["doctor"].get("checks", []):
        lines.append(f"- `{check.get('status')}` `{check.get('name')}`: {check.get('detail')}")
    lines.extend(["", "## Records", ""])
    for record in payload.get("records", []):
        lines.append(f"- `{record.get('phase_id')}` `{record.get('status')}`: {record.get('title')}")
    path.write_text("\n".join(lines).rstrip() + "\n")


def report_build(*, target: Path, phase_range: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = _report_payload(target, phase_range=phase_range)
    report_dir = constants._reports_root(target) / str(payload["report_id"])
    payload["path"] = str(report_dir)
    payload["bundle_files"] = ["PHASE_REPORT.md", "PHASE_EVIDENCE.json"]
    _write_json(report_dir / "PHASE_EVIDENCE.json", payload)
    _write_report_markdown(report_dir / "PHASE_REPORT.md", payload)
    lines = []
    lines.append(f"phase report: {payload['report_id']}")
    lines.append(f"path: {report_dir}")
    lines.append(f"issues: {payload['doctor']['issue_count']}")
    return emit(payload, json_output, lines, 0)


def report_list(*, target: Path, limit: int = 20, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    reports: list[dict[str, Any]] = []
    for path in sorted(constants._reports_root(target).glob("*/PHASE_EVIDENCE.json"), reverse=True):
        payload = _read_json(path)
        if payload is None:
            continue
        reports.append(
            {
                "report_id": payload.get("report_id"),
                "created_at": payload.get("created_at"),
                "phase_range": payload.get("phase_range"),
                "record_count": payload.get("record_count"),
                "issue_count": (payload.get("doctor") or {}).get("issue_count")
                if isinstance(payload.get("doctor"), dict)
                else None,
                "path": str(path.parent),
            }
        )
    reports = reports[:limit]
    out = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-report-list"),
        "target": str(target),
        "reports": reports,
        "report_count": len(reports),
    }
    lines = []
    lines.append(f"phase reports: {target}")
    for item in reports:
        lines.append(
            f"- {item.get('report_id')} issues={item.get('issue_count')} range={item.get('phase_range') or 'all'}"
        )
    return emit(out, json_output, lines, 0)


def report_show(*, target: Path, report_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload, error = constants._resolve_report(target, report_id)
    if payload is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase report: {payload.get('report_id')}")
        print(f"records: {payload.get('record_count')}")
        doctor_data = payload.get("doctor") if isinstance(payload.get("doctor"), dict) else {}
        print(f"issues: {doctor_data.get('issue_count', 0)}")
    return 0


def report_closeout(
    *, target: Path, report_id: str, status: str = "reviewed", reason: str | None = None, json_output: bool = False
) -> int:
    if status not in constants.PHASE_REPORT_CLOSEOUT_STATUSES:
        print(f"error: --status must be one of {sorted(constants.PHASE_REPORT_CLOSEOUT_STATUSES)}", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    report, error = constants._resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    report_path = Path(str(report.get("path") or ""))
    if not report_path.is_dir():
        print(f"error: phase report path is missing: {report.get('path')}", file=sys.stderr)
        return 1
    closeout = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-report-closeout"),
        "target": str(target),
        "report_id": report.get("report_id"),
        "report_path": str(report_path),
        "status": status,
        "reason": reason or f"phase report marked {status}",
        "reviewed_at": _now().isoformat(),
        "issue_count": (report.get("doctor") or {}).get("issue_count")
        if isinstance(report.get("doctor"), dict)
        else None,
        "record_count": report.get("record_count"),
        "source_fingerprint": constants._source_fingerprint(
            report.get("records") if isinstance(report.get("records"), list) else [],
            {
                "report_id": report.get("report_id"),
                "issue_count": (report.get("doctor") or {}).get("issue_count")
                if isinstance(report.get("doctor"), dict)
                else None,
            },
        ),
        "suggested_next_command": "brigade work phases report list",
    }
    _write_json(report_path / "CLOSEOUT.json", closeout)
    lines = []
    lines.append(f"phase report closeout: {report.get('report_id')}")
    lines.append(f"status: {status}")
    lines.append(f"reason: {closeout['reason']}")
    return emit(closeout, json_output, lines, 0)


def report_compare(*, target: Path, report_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    report, error = constants._resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    report_path = Path(str(report.get("path") or ""))
    summary = constants._report_compare_summary(target, report) or {
        "checks": [],
        "issue_count": 0,
        "top_issue": None,
        "phase_range": None,
    }
    issues = [check for check in summary["checks"] if check["status"] != "ok"]
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-report-compare"),
        "target": str(target),
        "report_id": report.get("report_id"),
        "report_path": str(report_path),
        "phase_range": summary.get("phase_range"),
        "current_head": _git_head(target),
        "checks": summary["checks"],
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "suggested_next_command": issues[0]["suggested_next_command"]
        if issues
        else "brigade work phases report show latest",
    }
    lines = []
    lines.append(f"phase report compare: {report.get('report_id')}")
    lines.append(f"issues: {len(issues)}")
    for check in summary["checks"]:
        lines.append(f"[{check['status']}] {check['name']}: {check['detail']}")
    return emit(payload, json_output, lines, 0 if not issues else 1)


def import_issues(
    *, target: Path, phase_range: str | None = None, dry_run: bool = False, json_output: bool = False
) -> int:
    from .. import work_cmd

    target = target.expanduser().resolve()
    doctor_data = doctor_payload(target, phase_range=phase_range)
    records: list[dict[str, Any]] = []
    for check in doctor_data["checks"]:
        if check.get("status") == "ok":
            continue
        fingerprint = f"phase-ledger:{check.get('phase_id') or 'ledger'}:{check.get('name')}:{check.get('detail')}"
        records.append(
            {
                "kind": "task",
                "source": "phase-ledger",
                "text": f"Resolve phase ledger issue: {check.get('name')}",
                "type": "workflow",
                "priority": "high" if check.get("status") == "fail" else "normal",
                "acceptance": [
                    "The phase ledger issue is fixed or explicitly deferred.",
                    "The affected phase record has current evidence or a clear next recommendation.",
                    "`brigade work phases doctor` no longer reports this issue.",
                ],
                "metadata": {
                    "phase_id": check.get("phase_id"),
                    "issue_type": check.get("name"),
                    "safe_summary": check.get("detail"),
                    "suggested_command": check.get("suggested_next_command"),
                    "source_fingerprint": constants._slug(fingerprint),
                },
            }
        )
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    dismissed: list[dict[str, Any]] = []
    if not dry_run and records:
        created, skipped, dismissed = work_cmd._append_import_records(target, records)
    elif dry_run:
        created = records
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-import-issues"),
        "target": str(target),
        "dry_run": dry_run,
        "created": created,
        "skipped": skipped,
        "dismissed": dismissed,
        "invalid": [],
        "created_count": len(created),
        "skipped_count": len(skipped),
        "dismissed_count": len(dismissed),
        "invalid_count": 0,
    }
    lines = []
    lines.append(f"phase ledger imports: {target}")
    lines.append(f"created: {payload['created_count']}")
    lines.append(f"skipped: {payload['skipped_count']}")
    lines.append(f"dismissed: {payload['dismissed_count']}")
    return emit(payload, json_output, lines, 0)


__all__ = (
    "_action_source_fingerprint",
    "_action_summary",
    "_actions_update_status",
    "_check",
    "_find_action",
    "_git_added_text_for_file",
    "_git_commit_exists",
    "_git_commit_on_branch",
    "_git_dirty_paths",
    "_git_head",
    "_handoff_root",
    "_phase_action_candidates",
    "_phase_handoff_content",
    "_phase_has_current_closeout",
    "_privacy_findings_for_text",
    "_report_payload",
    "_safe_handoff_text",
    "_same_commit",
    "_set_action_status",
    "_verification_entries",
    "_write_report_markdown",
    "actions_archive",
    "actions_build",
    "actions_defer",
    "actions_done",
    "actions_import_issues",
    "actions_list",
    "actions_plan",
    "actions_show",
    "actions_start",
    "closeout",
    "compare",
    "doctor",
    "doctor_payload",
    "evidence_add",
    "handoff",
    "health",
    "import_issues",
    "privacy",
    "reconcile",
    "report_build",
    "report_closeout",
    "report_compare",
    "report_list",
    "report_show",
    "verify_plan",
    "verify_record",
)
