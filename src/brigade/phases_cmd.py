"""Auditable local phase execution ledger."""

from __future__ import annotations

import json
import re
import sys
import hashlib
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from .localio import read_json_dict as _read_json, utc_now as _now, write_json as _write_json

SCHEMA_VERSION = 1
PHASE_STATUSES = {"pending", "in-progress", "implemented", "verified", "committed", "pushed", "deferred", "blocked"}
PHASE_CLOSEOUT_STATUSES = {"reviewed", "deferred", "blocked", "archived"}
PHASE_ACTION_STATUSES = {"pending", "active", "done", "deferred", "archived"}
PHASE_REPORT_CLOSEOUT_STATUSES = {"reviewed", "deferred", "superseded", "archived"}
PHASE_SESSION_CLOSEOUT_STATUSES = {"reviewed", "deferred", "blocked", "archived"}
PHASE_VERIFY_STATUSES = {"expected", "passed", "failed", "skipped", "deferred"}
DONE_STATUSES = {"implemented", "verified", "committed", "pushed"}
STALE_IN_PROGRESS_HOURS = 12
REPORT_STALE_HOURS = 24
STALE_UNREVIEWED_COMPLETED_HOURS = 24
PRIVACY_PATTERNS = {
    "private_path": re.compile(r"/(?:home|Users|private|mnt|Volumes)/[^\s`\"'<>]+"),
    "token_like": re.compile(r"(?i)(token|secret|password|api[_-]?key)\s*[=:]\s*[^\s`\"'<>]+"),
    "private_url": re.compile(
        r"(?i)https?://(?:local"
        r"host|127\.0\.0\.1|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])|[^/\s`\"'<>]*(?:internal|private|corp|lan|local|nas)[^/\s`\"'<>]*)[^\s`\"'<>]*"
    ),
}


def _root(target: Path) -> Path:
    return target / ".brigade" / "work" / "phases"


def _records_root(target: Path) -> Path:
    return _root(target) / "records"


def _reports_root(target: Path) -> Path:
    return _root(target) / "reports"


def _closeouts_root(target: Path) -> Path:
    return _root(target) / "closeouts"


def _actions_root(target: Path) -> Path:
    return _root(target) / "actions"


def _sessions_root(target: Path) -> Path:
    return _root(target) / "sessions"


def _session_reports_root(target: Path) -> Path:
    return _root(target) / "session-reports"


def _session_checkpoints_root(target: Path) -> Path:
    return _root(target) / "session-checkpoints"


def _session_checkpoints_archive_path(target: Path) -> Path:
    return _session_checkpoints_root(target) / "archive.jsonl"


def _session_recovery_notes_root(target: Path) -> Path:
    return _root(target) / "session-recovery-notes"


def _goals_root(target: Path) -> Path:
    return _root(target) / "goals"


def _index_path(target: Path) -> Path:
    return _root(target) / "index.json"


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _schema(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "version": SCHEMA_VERSION,
        "record_fields": [
            "phase_id",
            "title",
            "source_goal",
            "status",
            "started_at",
            "completed_at",
            "implementation_summary",
            "files_changed",
            "tests_run",
            "test_result_summary",
            "commit_hash",
            "push_ref",
            "deferred_items",
            "blocker_reason",
            "next_phase_recommendation",
        ],
    }


def _contract_schema(name: str, fields: list[str], *, description: str) -> dict[str, Any]:
    return {
        "name": name,
        "version": SCHEMA_VERSION,
        "description": description,
        "record_fields": fields,
    }


def _slug(value: str) -> str:
    rendered = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-").lower()
    return rendered or f"phase-{uuid4().hex[:8]}"


def _parse_range(value: str | None) -> tuple[int, int] | None:
    if value is None:
        return None
    match = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", value)
    if not match:
        raise ValueError("--range must be N or N-M")
    start = int(match.group(1))
    end = int(match.group(2) or match.group(1))
    if end < start:
        raise ValueError("--range end must be greater than or equal to start")
    return start, end


def _phase_id_for(number: int) -> str:
    return f"phase-{number}"


def _record_path(target: Path, phase_id: str) -> Path:
    return _records_root(target) / f"{_slug(phase_id)}.json"


def _default_record(phase_id: str, *, title: str, source_goal: str, kind: str = "phase") -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-record"),
        "kind": kind,
        "phase_id": phase_id,
        "title": title,
        "source_goal": source_goal,
        "status": "pending",
        "created_at": _now().isoformat(),
        "started_at": None,
        "completed_at": None,
        "implementation_summary": "",
        "files_changed": [],
        "tests_run": [],
        "test_result_summary": "",
        "commit_hash": "",
        "push_ref": "",
        "deferred_items": [],
        "blocker_reason": "",
        "next_phase_recommendation": "",
        "group_id": None,
        "phase_range": None,
        "grouped_phase_ids": [],
        "explicit_grouping": False,
    }


def _records(target: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(_records_root(target).glob("*.json")):
        payload = _read_json(path)
        if payload is None:
            records.append({"phase_id": path.stem, "status": "invalid", "path": str(path), "parse_error": True})
            continue
        payload.setdefault("path", str(path))
        records.append(payload)
    return records


def _find_record(target: Path, phase_id: str) -> tuple[Path, dict[str, Any] | None]:
    wanted = _slug(phase_id)
    exact = _record_path(target, wanted)
    if exact.is_file():
        return exact, _read_json(exact)
    matches = [path for path in _records_root(target).glob("*.json") if path.stem.startswith(wanted)]
    if len(matches) == 1:
        return matches[0], _read_json(matches[0])
    return exact, None


def _record_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "phase_id": record.get("phase_id"),
        "title": record.get("title"),
        "kind": record.get("kind", "phase"),
        "status": record.get("status"),
        "started_at": record.get("started_at"),
        "completed_at": record.get("completed_at"),
        "commit_hash": record.get("commit_hash"),
        "push_ref": record.get("push_ref"),
        "path": record.get("path"),
        "phase_range": record.get("phase_range"),
        "explicit_grouping": record.get("explicit_grouping"),
    }


def _status_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts = {status: 0 for status in PHASE_STATUSES}
    counts["invalid"] = 0
    for record in records:
        status = str(record.get("status") or "invalid")
        counts[status] = counts.get(status, 0) + 1
    return {key: value for key, value in counts.items() if value}


def _safe_phase_number(phase_id: object) -> int | None:
    match = re.fullmatch(r"phase-(\d+)", str(phase_id or ""))
    return int(match.group(1)) if match else None


def _append_unique(values: list[Any], additions: list[str]) -> list[str]:
    rendered = [str(item) for item in values if str(item)]
    for item in additions:
        if item and item not in rendered:
            rendered.append(item)
    return rendered


def schema(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    session_health_schemas = [
        _contract_schema(
            "phase-ledger-session-next",
            ["session", "next_step", "checks", "checkpoint", "suggested_next_command"],
            description="Read-only next-step decision for an AFK phase session.",
        ),
        _contract_schema(
            "phase-ledger-session-resume",
            ["session", "resume", "metadata_path", "suggested_next_command"],
            description="Metadata-only resume receipt for an AFK phase session.",
        ),
        _contract_schema(
            "phase-ledger-session-protocol",
            [
                "session",
                "next_step",
                "safe_resume",
                "resume_blockers",
                "wrapper_steps",
                "allowed_command_prefixes",
                "forbidden_actions",
            ],
            description="Read-only wrapper protocol for safe AFK phase session resume decisions.",
        ),
        _contract_schema(
            "phase-ledger-session-audit",
            [
                "session",
                "ready_for_resume",
                "ready_for_completion_claim",
                "checks",
                "issue_count",
                "suggested_next_command",
            ],
            description="Read-only self-audit across AFK session health, evidence, and completion gate outputs.",
        ),
        _contract_schema(
            "phase-ledger-session-checkpoint",
            [
                "checkpoint_id",
                "session_id",
                "phase_id",
                "status",
                "summary",
                "next_step",
                "suggested_next_command",
                "source_fingerprint",
            ],
            description="Local AFK recovery checkpoint without command execution.",
        ),
        _contract_schema(
            "phase-ledger-session-checkpoint-compare",
            ["checkpoint_id", "session_id", "issue_count", "checks", "top_issue", "suggested_next_command"],
            description="Read-only drift check between a checkpoint and current session state.",
        ),
        _contract_schema(
            "phase-ledger-session-recovery-note",
            ["note_id", "session_id", "status", "summary", "evidence_labels", "next_step", "source_fingerprint"],
            description="Safe local resume context attached to an AFK phase session.",
        ),
        _contract_schema(
            "phase-ledger-session-risk",
            [
                "session",
                "risk_level",
                "risk_count",
                "risks",
                "checkpoint",
                "recovery_note_count",
                "suggested_next_command",
            ],
            description="Read-only session risk summary across next step, checkpoint, notes, and doctor issues.",
        ),
        _contract_schema(
            "phase-ledger-session-verification",
            [
                "session",
                "status_counts",
                "missing_phases",
                "failed_phases",
                "deferred_phases",
                "suggested_next_command",
            ],
            description="Read-only verification coverage rollup across an AFK phase session.",
        ),
        _contract_schema(
            "phase-ledger-session-privacy",
            ["session", "status_counts", "missing_phases", "blocked_phases", "suggested_next_command"],
            description="Read-only privacy-check coverage rollup across an AFK phase session.",
        ),
        _contract_schema(
            "phase-ledger-session-handoffs",
            [
                "session",
                "status_counts",
                "missing_phases",
                "failed_phases",
                "deferred_phases",
                "suggested_next_command",
            ],
            description="Read-only Memory Handoff coverage rollup across an AFK phase session.",
        ),
        _contract_schema(
            "phase-ledger-session-report",
            [
                "session",
                "records",
                "doctor",
                "recovery",
                "actions",
                "imports",
                "commits",
                "tests",
                "blockers",
                "suggested_next_command",
            ],
            description="Local session report evidence bundle with recovery context.",
        ),
        _contract_schema(
            "phase-ledger-session-progress",
            [
                "session",
                "percent_complete",
                "status_counts",
                "blockers",
                "current_phase",
                "test_summary",
                "commit_summary",
                "push_summary",
                "suggested_next_command",
            ],
            description="Read-only progress summary for an AFK phase session.",
        ),
        _contract_schema(
            "phase-ledger-session-gate",
            ["session", "safe_to_claim_complete", "checks", "blocker_count", "warning_count", "suggested_next_command"],
            description="Final local claim gate for an AFK phase session.",
        ),
    ]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-schema"),
        "target": str(target),
        "schemas": [
            _schema("phase-record"),
            _schema("phase-ledger-index"),
            _schema("phase-ledger-plan"),
            _schema("phase-ledger-status"),
            _schema("phase-ledger-report"),
            _schema("phase-ledger-closeout"),
            _schema("phase-ledger-action"),
            _schema("phase-ledger-session"),
            _schema("phase-ledger-handoff"),
            _schema("phase-ledger-doctor"),
            *session_health_schemas,
        ],
        "session_health_schemas": session_health_schemas,
        "status_values": sorted(PHASE_STATUSES),
        "completion_rule": "A phase is complete only with evidence or an explicit deferral.",
        "no_silent_compression": True,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase ledger schema: {target}")
        print("no_silent_compression: true")
        for item in payload["schemas"]:
            print(f"- {item['name']}")
    return 0


def init(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _records_root(target).mkdir(parents=True, exist_ok=True)
    index = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-index"),
        "created_at": _now().isoformat(),
        "records_path": str(_records_root(target)),
        "no_silent_compression": True,
        "completion_rule": "A phase is complete only with evidence or an explicit deferral.",
    }
    if not _index_path(target).is_file():
        _write_json(_index_path(target), index)
        written = True
    else:
        written = False
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-init"),
        "target": str(target),
        "path": str(_root(target)),
        "written": written,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase ledger: {_root(target)}")
        print(f"written: {str(written).lower()}")
    return 0


def plan(
    *,
    target: Path,
    phase_id: str | None = None,
    phase_range: str | None = None,
    title: str | None = None,
    source_goal: str | None = None,
    grouped: bool = False,
    force: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    _records_root(target).mkdir(parents=True, exist_ok=True)
    source_goal = source_goal or "unspecified"
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    try:
        parsed_range = _parse_range(phase_range)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if parsed_range is None and phase_id is None:
        print("error: pass --phase-id or --range", file=sys.stderr)
        return 2
    if parsed_range is not None:
        start, end = parsed_range
        if grouped:
            group_id = phase_id or f"phase-{start}-{end}-group"
            record = _default_record(
                group_id, title=title or f"Grouped phases {start}-{end}", source_goal=source_goal, kind="group"
            )
            record["phase_range"] = f"{start}-{end}"
            record["grouped_phase_ids"] = [_phase_id_for(number) for number in range(start, end + 1)]
            record["explicit_grouping"] = True
            path = _record_path(target, group_id)
            if path.exists() and not force:
                existing = _read_json(path) or {"phase_id": group_id, "path": str(path)}
                skipped.append(_record_summary(existing))
            else:
                record["path"] = str(path)
                _write_json(path, record)
                created.append(_record_summary(record))
        for number in range(start, end + 1):
            item_id = _phase_id_for(number)
            record = _default_record(
                item_id,
                title=(title or f"Phase {number}") if start == end else f"{title or 'Planned phase'} {number}",
                source_goal=source_goal,
            )
            if grouped:
                record["group_id"] = phase_id or f"phase-{start}-{end}-group"
                record["explicit_grouping"] = True
            path = _record_path(target, item_id)
            if path.exists() and not force:
                existing = _read_json(path) or {"phase_id": item_id, "path": str(path)}
                skipped.append(_record_summary(existing))
            else:
                record["path"] = str(path)
                _write_json(path, record)
                created.append(_record_summary(record))
    else:
        assert phase_id is not None
        record = _default_record(phase_id, title=title or phase_id, source_goal=source_goal)
        path = _record_path(target, phase_id)
        if path.exists() and not force:
            existing = _read_json(path) or {"phase_id": phase_id, "path": str(path)}
            skipped.append(_record_summary(existing))
        else:
            record["path"] = str(path)
            _write_json(path, record)
            created.append(_record_summary(record))
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-plan"),
        "target": str(target),
        "created": created,
        "skipped": skipped,
        "created_count": len(created),
        "skipped_count": len(skipped),
        "suggested_next_command": "brigade work phases list",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"planned: {len(created)}")
        print(f"skipped: {len(skipped)}")
        for item in created:
            print(f"- {item['phase_id']} [{item['status']}] {item['title']}")
    return 0


def list_phases(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    records = [_record_summary(record) for record in _records(target)]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-list"),
        "target": str(target),
        "records": records,
        "record_count": len(records),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase ledger: {target}")
        for record in records:
            print(f"- {record.get('phase_id')} [{record.get('status')}] {record.get('title')}")
    return 0


def status_payload(target: Path, *, phase_range: str | None = None) -> dict[str, Any]:
    target = target.expanduser().resolve()
    records = _records(target)
    range_records = records
    missing: list[str] = []
    try:
        parsed_range = _parse_range(phase_range)
    except ValueError as exc:
        parsed_range = None
        missing = [str(exc)]
    if parsed_range is not None:
        start, end = parsed_range
        by_id = {str(record.get("phase_id")): record for record in records}
        expected = [_phase_id_for(number) for number in range(start, end + 1)]
        range_records = [by_id[phase_id] for phase_id in expected if phase_id in by_id]
        missing = [phase_id for phase_id in expected if phase_id not in by_id]
    open_records = [record for record in range_records if record.get("status") in {"pending", "in-progress", "blocked"}]
    done_records = [
        record
        for record in range_records
        if record.get("status") in DONE_STATUSES or record.get("status") == "deferred"
    ]
    next_record = next(
        (
            record
            for record in sorted(
                range_records,
                key=lambda item: (_safe_phase_number(item.get("phase_id")) or 999999, str(item.get("phase_id"))),
            )
            if record.get("status") in {"pending", "blocked", "in-progress"}
        ),
        None,
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-status"),
        "target": str(target),
        "phase_range": phase_range,
        "record_count": len(range_records),
        "total_record_count": len(records),
        "status_counts": _status_counts(range_records),
        "missing_phase_ids": missing,
        "missing_count": len(missing),
        "open_count": len(open_records),
        "done_count": len(done_records),
        "complete": not missing and bool(range_records) and len(done_records) == len(range_records),
        "next_phase": _record_summary(next_record) if isinstance(next_record, dict) else None,
        "suggested_next_command": f"brigade work phases start {next_record.get('phase_id')}"
        if isinstance(next_record, dict)
        else "brigade work phases doctor",
    }
    return payload


def status(*, target: Path, phase_range: str | None = None, json_output: bool = False) -> int:
    try:
        payload = status_payload(target, phase_range=phase_range)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase ledger status: {payload['target']}")
        print(f"records: {payload['record_count']}")
        print(f"missing: {payload['missing_count']}")
        print(f"open: {payload['open_count']}")
        print(f"complete: {str(payload['complete']).lower()}")
        next_phase = payload.get("next_phase")
        if isinstance(next_phase, dict):
            print(f"next: {next_phase.get('phase_id')} [{next_phase.get('status')}]")
    return 0


def next_phase(*, target: Path, phase_range: str | None = None, json_output: bool = False) -> int:
    payload = status_payload(target, phase_range=phase_range)
    next_record = payload.get("next_phase")
    if not isinstance(next_record, dict):
        if json_output:
            print(json.dumps({**payload, "found": False}, indent=2, sort_keys=True))
        else:
            print("next phase: none")
        return 1
    out = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-next"),
        "target": payload["target"],
        "found": True,
        "phase": next_record,
        "suggested_next_command": payload["suggested_next_command"],
    }
    if json_output:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print(f"next phase: {next_record.get('phase_id')}")
        print(f"status: {next_record.get('status')}")
        print(f"next: {out['suggested_next_command']}")
    return 0


def show(*, target: Path, phase_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    path, record = _find_record(target, phase_id)
    if record is None:
        print(f"error: phase record not found: {phase_id}", file=sys.stderr)
        return 1
    record["path"] = str(path)
    if json_output:
        print(json.dumps(record, indent=2, sort_keys=True))
    else:
        print(f"phase: {record.get('phase_id')}")
        print(f"status: {record.get('status')}")
        print(f"title: {record.get('title')}")
        print(f"summary: {record.get('implementation_summary') or 'none'}")
        print(f"next: {record.get('next_phase_recommendation') or 'none'}")
    return 0


def start(*, target: Path, phase_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    path, record = _find_record(target, phase_id)
    if record is None:
        print(f"error: phase record not found: {phase_id}", file=sys.stderr)
        return 1
    record["status"] = "in-progress"
    record["started_at"] = record.get("started_at") or _now().isoformat()
    record["updated_at"] = _now().isoformat()
    record["path"] = str(path)
    _write_json(path, record)
    if json_output:
        print(json.dumps(record, indent=2, sort_keys=True))
    else:
        print(f"phase: {record.get('phase_id')}")
        print("status: in-progress")
    return 0


def complete(
    *,
    target: Path,
    phase_id: str,
    status: str = "implemented",
    summary: str | None = None,
    files_changed: list[str] | None = None,
    tests_run: list[str] | None = None,
    test_result_summary: str | None = None,
    commit_hash: str | None = None,
    push_ref: str | None = None,
    deferred_items: list[str] | None = None,
    next_phase_recommendation: str | None = None,
    json_output: bool = False,
) -> int:
    if status not in DONE_STATUSES:
        print(f"error: --status must be one of {sorted(DONE_STATUSES)}", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    path, record = _find_record(target, phase_id)
    if record is None:
        print(f"error: phase record not found: {phase_id}", file=sys.stderr)
        return 1
    record["status"] = status
    record["completed_at"] = record.get("completed_at") or _now().isoformat()
    record["updated_at"] = _now().isoformat()
    if summary is not None:
        record["implementation_summary"] = summary
    if files_changed:
        record["files_changed"] = _append_unique(record.get("files_changed", []), files_changed)
    if tests_run:
        record["tests_run"] = _append_unique(record.get("tests_run", []), tests_run)
    if test_result_summary is not None:
        record["test_result_summary"] = test_result_summary
    if commit_hash is not None:
        record["commit_hash"] = commit_hash
    if push_ref is not None:
        record["push_ref"] = push_ref
    if deferred_items:
        record["deferred_items"] = _append_unique(record.get("deferred_items", []), deferred_items)
    if next_phase_recommendation is not None:
        record["next_phase_recommendation"] = next_phase_recommendation
    record["path"] = str(path)
    _write_json(path, record)
    if json_output:
        print(json.dumps(record, indent=2, sort_keys=True))
    else:
        print(f"phase: {record.get('phase_id')}")
        print(f"status: {status}")
        print(f"tests: {len(record.get('tests_run') or [])}")
    return 0


def defer(
    *, target: Path, phase_id: str, reason: str, next_phase_recommendation: str | None = None, json_output: bool = False
) -> int:
    if not reason.strip():
        print("error: --reason is required", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    path, record = _find_record(target, phase_id)
    if record is None:
        print(f"error: phase record not found: {phase_id}", file=sys.stderr)
        return 1
    record["status"] = "deferred"
    record["completed_at"] = record.get("completed_at") or _now().isoformat()
    record["updated_at"] = _now().isoformat()
    record["deferred_items"] = _append_unique(record.get("deferred_items", []), [reason])
    if next_phase_recommendation is not None:
        record["next_phase_recommendation"] = next_phase_recommendation
    record["path"] = str(path)
    _write_json(path, record)
    if json_output:
        print(json.dumps(record, indent=2, sort_keys=True))
    else:
        print(f"phase: {record.get('phase_id')}")
        print("status: deferred")
        print(f"reason: {reason}")
    return 0


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _latest_record(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    valid_records = [record for record in records if record.get("phase_id")]
    if not valid_records:
        return None
    return sorted(
        valid_records,
        key=lambda item: (_safe_phase_number(item.get("phase_id")) or -1, str(item.get("created_at") or "")),
    )[-1]


def _selected_records(target: Path, selector: str) -> tuple[list[dict[str, Any]], list[str], str | None]:
    target = target.expanduser().resolve()
    records = _records(target)
    if selector == "latest":
        latest = _latest_record(records)
        return ([latest] if latest else []), ([] if latest else ["latest"]), None
    parsed_range: tuple[int, int] | None = None
    try:
        parsed_range = _parse_range(selector)
    except ValueError:
        parsed_range = None
    if parsed_range is not None:
        start, end = parsed_range
        by_id = {str(record.get("phase_id")): record for record in records}
        expected = [_phase_id_for(number) for number in range(start, end + 1)]
        return (
            [by_id[phase_id] for phase_id in expected if phase_id in by_id],
            [phase_id for phase_id in expected if phase_id not in by_id],
            f"{start}-{end}",
        )
    path, record = _find_record(target, selector)
    if record is None:
        return [], [selector], None
    record["path"] = str(path)
    return [record], [], None


def _source_fingerprint(records: list[dict[str, Any]], extra: dict[str, Any] | None = None) -> str:
    safe_records = [
        {
            "phase_id": record.get("phase_id"),
            "status": record.get("status"),
            "updated_at": record.get("updated_at"),
            "completed_at": record.get("completed_at"),
            "commit_hash": record.get("commit_hash"),
            "push_ref": record.get("push_ref"),
            "files_changed": record.get("files_changed") or [],
            "tests_run": record.get("tests_run") or [],
            "deferred_items": record.get("deferred_items") or [],
        }
        for record in records
    ]
    payload = {"records": safe_records, "extra": extra or {}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _read_closeouts(target: Path) -> list[dict[str, Any]]:
    closeouts: list[dict[str, Any]] = []
    for path in sorted(_closeouts_root(target).glob("*.json")):
        payload = _read_json(path)
        if payload is None:
            continue
        payload.setdefault("path", str(path))
        closeouts.append(payload)
    closeouts.sort(key=lambda item: str(item.get("reviewed_at") or item.get("closeout_id") or ""))
    return closeouts


def _read_reports(target: Path) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for path in sorted(_reports_root(target).glob("*/PHASE_EVIDENCE.json")):
        payload = _read_json(path)
        if payload is None:
            continue
        payload.setdefault("path", str(path.parent))
        reports.append(payload)
    return reports


def _latest_report(target: Path) -> dict[str, Any] | None:
    reports = _read_reports(target)
    if not reports:
        return None
    return sorted(reports, key=lambda item: str(item.get("created_at") or ""))[-1]


def _latest_report_for_range(target: Path, phase_range: str) -> dict[str, Any] | None:
    reports = [report for report in _read_reports(target) if report.get("phase_range") == phase_range]
    if not reports:
        return None
    return sorted(reports, key=lambda item: str(item.get("created_at") or ""))[-1]


def _report_compare_summary(target: Path, report: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(report, dict):
        return None
    phase_range = report.get("phase_range") if isinstance(report.get("phase_range"), str) else None
    current_status = status_payload(target, phase_range=phase_range)
    current_doctor = doctor_payload(target, phase_range=phase_range)
    checks: list[dict[str, Any]] = []
    report_status = report.get("status") if isinstance(report.get("status"), dict) else {}
    report_doctor = report.get("doctor") if isinstance(report.get("doctor"), dict) else {}
    if current_status.get("status_counts") != report_status.get("status_counts"):
        checks.append(
            _check(
                "warn",
                "phase_report_status_counts_changed",
                "current phase status counts differ from report",
                suggested="brigade work phases report build",
            )
        )
    if int(current_doctor.get("issue_count") or 0) != int(report_doctor.get("issue_count") or 0):
        checks.append(
            _check(
                "warn",
                "phase_report_doctor_issue_count_changed",
                f"{report_doctor.get('issue_count')} -> {current_doctor.get('issue_count')}",
                suggested="brigade work phases doctor",
            )
        )
    current_head = _git_head(target)
    report_head = str(report.get("git_head") or "")
    if report_head and current_head and not _same_commit(report_head, current_head):
        checks.append(
            _check(
                "warn",
                "phase_report_head_changed",
                f"current HEAD {current_head} differs from report HEAD {report_head}",
                suggested="brigade work phases report build",
            )
        )
    report_path = Path(str(report.get("path") or ""))
    closeout = _read_json(report_path / "CLOSEOUT.json")
    if closeout is None:
        checks.append(
            _check(
                "warn",
                "phase_report_missing_closeout",
                "phase report has no CLOSEOUT.json",
                suggested=f"brigade work phases report closeout {report.get('report_id')}",
            )
        )
    elif closeout.get("status") in {"deferred", "superseded", "archived"}:
        checks.append(
            _check(
                "warn",
                "phase_report_not_reviewed",
                f"phase report closeout status is {closeout.get('status')}",
                suggested=f"brigade work phases report closeout {report.get('report_id')} --status reviewed",
            )
        )
    created = _parse_time(report.get("created_at"))
    latest_record_time = max(
        [
            parsed
            for parsed in (
                _parse_time(record.get("updated_at") or record.get("completed_at") or record.get("created_at"))
                for record in _records(target)
            )
            if parsed is not None
        ],
        default=None,
    )
    if created and latest_record_time and latest_record_time > created:
        checks.append(
            _check(
                "warn",
                "phase_report_newer_phase_record",
                "a phase record changed after this report was built",
                suggested="brigade work phases report build",
            )
        )
    if not checks:
        checks.append(_check("ok", "phase_report_current", "phase report matches current ledger checks"))
    issues = [check for check in checks if check["status"] != "ok"]
    return {
        "report_id": report.get("report_id"),
        "phase_range": phase_range,
        "checks": checks,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "suggested_next_command": issues[0]["suggested_next_command"]
        if issues
        else "brigade work phases report show latest",
    }


def _resolve_report(target: Path, report_id: str) -> tuple[dict[str, Any] | None, str | None]:
    target = target.expanduser().resolve()
    if report_id == "latest":
        latest = _latest_report(target)
        return (latest, None) if latest else (None, "phase report not found: latest")
    candidates = sorted(_reports_root(target).glob(f"{report_id}*/PHASE_EVIDENCE.json"))
    if len(candidates) != 1:
        return (
            None,
            f"phase report not found: {report_id}" if not candidates else f"phase report id is ambiguous: {report_id}",
        )
    payload = _read_json(candidates[0])
    if payload is None:
        return None, f"invalid phase report: {candidates[0]}"
    payload.setdefault("path", str(candidates[0].parent))
    return payload, None


def _read_actions(target: Path) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for path in sorted(_actions_root(target).glob("*.json")):
        payload = _read_json(path)
        if payload is None:
            continue
        payload.setdefault("path", str(path))
        actions.append(payload)
    return actions


def _session_summary(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session.get("session_id"),
        "phase_range": session.get("phase_range"),
        "status": session.get("status"),
        "current_phase_id": session.get("current_phase_id"),
        "started_at": session.get("started_at"),
        "completed_at": session.get("completed_at"),
        "closeout_status": (session.get("closeout") or {}).get("status")
        if isinstance(session.get("closeout"), dict)
        else None,
        "path": session.get("path"),
        "next_recommended_command": session.get("next_recommended_command"),
    }


def _read_sessions(target: Path) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for path in sorted(_sessions_root(target).glob("*.json")):
        payload = _read_json(path)
        if payload is None:
            continue
        payload.setdefault("path", str(path))
        sessions.append(payload)
    sessions.sort(key=lambda item: str(item.get("started_at") or item.get("session_id") or ""))
    return sessions


def _latest_session(target: Path) -> dict[str, Any] | None:
    sessions = _read_sessions(target)
    return sessions[-1] if sessions else None


def _latest_session_for_range(target: Path, phase_range: str) -> dict[str, Any] | None:
    for session in reversed(_read_sessions(target)):
        if str(session.get("phase_range") or "") == phase_range:
            return session
    return None


def _read_session_reports(target: Path) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for path in sorted(_session_reports_root(target).glob("*/SESSION_EVIDENCE.json")):
        payload = _read_json(path)
        if payload is None:
            continue
        payload.setdefault("path", str(path.parent))
        reports.append(payload)
    reports.sort(key=lambda item: str(item.get("created_at") or item.get("report_id") or ""))
    return reports


def _read_session_checkpoints(target: Path) -> list[dict[str, Any]]:
    checkpoints: list[dict[str, Any]] = []
    for path in sorted(_session_checkpoints_root(target).glob("*.json")):
        payload = _read_json(path)
        if payload is None:
            continue
        payload.setdefault("path", str(path))
        checkpoints.append(payload)
    checkpoints.sort(key=lambda item: str(item.get("created_at") or item.get("checkpoint_id") or ""))
    return checkpoints


def _read_session_recovery_notes(target: Path) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    for path in sorted(_session_recovery_notes_root(target).glob("*.json")):
        payload = _read_json(path)
        if payload is None:
            continue
        payload.setdefault("path", str(path))
        notes.append(payload)
    notes.sort(key=lambda item: str(item.get("created_at") or item.get("note_id") or ""))
    return notes


def _checkpoint_summary(checkpoint: dict[str, Any]) -> dict[str, Any]:
    return {
        "checkpoint_id": checkpoint.get("checkpoint_id"),
        "session_id": checkpoint.get("session_id"),
        "phase_id": checkpoint.get("phase_id"),
        "status": checkpoint.get("status"),
        "summary": checkpoint.get("summary"),
        "created_at": checkpoint.get("created_at"),
        "path": checkpoint.get("path"),
        "suggested_next_command": checkpoint.get("suggested_next_command"),
    }


def _recovery_note_summary(note: dict[str, Any]) -> dict[str, Any]:
    return {
        "note_id": note.get("note_id"),
        "session_id": note.get("session_id"),
        "phase_id": note.get("phase_id"),
        "status": note.get("status"),
        "summary": note.get("summary"),
        "created_at": note.get("created_at"),
        "path": note.get("path"),
        "suggested_next_command": note.get("suggested_next_command"),
    }


def _resolve_session_checkpoint(target: Path, checkpoint_id: str) -> tuple[dict[str, Any] | None, str | None]:
    target = target.expanduser().resolve()
    checkpoints = _read_session_checkpoints(target)
    if checkpoint_id == "latest":
        return (checkpoints[-1], None) if checkpoints else (None, "phase session checkpoint not found: latest")
    wanted = _slug(checkpoint_id)
    exact = _session_checkpoints_root(target) / f"{wanted}.json"
    if exact.is_file():
        checkpoint = _read_json(exact)
        if checkpoint is not None:
            checkpoint.setdefault("path", str(exact))
        return checkpoint, None
    matches = [path for path in _session_checkpoints_root(target).glob("*.json") if path.stem.startswith(wanted)]
    if len(matches) == 1:
        checkpoint = _read_json(matches[0])
        if checkpoint is not None:
            checkpoint.setdefault("path", str(matches[0]))
        return checkpoint, None
    if len(matches) > 1:
        return None, f"phase session checkpoint id is ambiguous: {checkpoint_id}"
    return None, f"phase session checkpoint not found: {checkpoint_id}"


def _resolve_session_recovery_note(target: Path, note_id: str) -> tuple[dict[str, Any] | None, str | None]:
    target = target.expanduser().resolve()
    notes = _read_session_recovery_notes(target)
    if note_id == "latest":
        return (notes[-1], None) if notes else (None, "phase session recovery note not found: latest")
    wanted = _slug(note_id)
    exact = _session_recovery_notes_root(target) / f"{wanted}.json"
    if exact.is_file():
        note = _read_json(exact)
        if note is not None:
            note.setdefault("path", str(exact))
        return note, None
    matches = [path for path in _session_recovery_notes_root(target).glob("*.json") if path.stem.startswith(wanted)]
    if len(matches) == 1:
        note = _read_json(matches[0])
        if note is not None:
            note.setdefault("path", str(matches[0]))
        return note, None
    if len(matches) > 1:
        return None, f"phase session recovery note id is ambiguous: {note_id}"
    return None, f"phase session recovery note not found: {note_id}"


def _latest_checkpoint_for_session(target: Path, session_id: object) -> dict[str, Any] | None:
    wanted = str(session_id or "")
    if not wanted:
        return None
    matches = [checkpoint for checkpoint in _read_session_checkpoints(target) if checkpoint.get("session_id") == wanted]
    return matches[-1] if matches else None


def _resolve_session(target: Path, session_id: str) -> tuple[Path | None, dict[str, Any] | None, str | None]:
    target = target.expanduser().resolve()
    if session_id == "latest":
        latest = _latest_session(target)
        return (
            (Path(str(latest.get("path"))), latest, None) if latest else (None, None, "phase session not found: latest")
        )
    wanted = _slug(session_id)
    exact = _sessions_root(target) / f"{wanted}.json"
    if exact.is_file():
        return exact, _read_json(exact), None
    matches = [path for path in _sessions_root(target).glob("*.json") if path.stem.startswith(wanted)]
    if len(matches) == 1:
        return matches[0], _read_json(matches[0]), None
    if len(matches) > 1:
        return None, None, f"phase session id is ambiguous: {session_id}"
    return None, None, f"phase session not found: {session_id}"


def _session_phase_records(target: Path, phase_range: str) -> tuple[list[dict[str, Any]], list[str]]:
    parsed = _parse_range(phase_range)
    if parsed is None:
        return [], []
    start, end = parsed
    records = {str(record.get("phase_id")): record for record in _records(target)}
    expected = [_phase_id_for(number) for number in range(start, end + 1)]
    return [records[item] for item in expected if item in records], [item for item in expected if item not in records]


def _session_payload(target: Path, *, phase_range: str, source_goal: str | None = None) -> dict[str, Any]:
    records, missing = _session_phase_records(target, phase_range)
    status_data = status_payload(target, phase_range=phase_range)
    doctor_data = doctor_payload(target, phase_range=phase_range)
    next_phase_record = status_data.get("next_phase")
    current_phase_id = next_phase_record.get("phase_id") if isinstance(next_phase_record, dict) else None
    latest_report = _latest_report(target)
    latest_report_summary = (
        {
            "report_id": latest_report.get("report_id"),
            "phase_range": latest_report.get("phase_range"),
            "path": latest_report.get("path"),
        }
        if latest_report
        else None
    )
    session_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-phase-session-{uuid4().hex[:6]}"
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session"),
        "target": str(target),
        "session_id": session_id,
        "source_goal": source_goal or "unspecified",
        "phase_range": phase_range,
        "status": "active",
        "started_at": _now().isoformat(),
        "completed_at": None,
        "current_phase_id": current_phase_id,
        "missing_phase_ids": missing,
        "phase_status": status_data,
        "doctor": {"issue_count": doctor_data["issue_count"], "top_issue": doctor_data["top_issue"]},
        "phase_records": [_record_summary(record) for record in records],
        "commit_summary": {
            "committed": len([record for record in records if record.get("commit_hash")]),
            "pushed": len([record for record in records if record.get("push_ref")]),
        },
        "test_summary": {
            "with_tests": len([record for record in records if record.get("tests_run")]),
            "without_tests": len([record for record in records if not record.get("tests_run")]),
        },
        "report_references": [latest_report_summary] if latest_report_summary else [],
        "closeout": None,
        "next_recommended_command": status_data.get("suggested_next_command") or "brigade work phases doctor",
    }


def session_start(*, target: Path, phase_range: str, source_goal: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    try:
        _parse_range(phase_range)
        payload = _session_payload(target, phase_range=phase_range, source_goal=source_goal)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    path = _sessions_root(target) / f"{payload['session_id']}.json"
    payload["path"] = str(path)
    _write_json(path, payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase session: {payload['session_id']}")
        print(f"range: {payload['phase_range']}")
        print(f"current: {payload.get('current_phase_id') or 'none'}")
        print(f"next: {payload['next_recommended_command']}")
    return 0


def session_list(*, target: Path, limit: int = 20, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    sessions = [_session_summary(session) for session in reversed(_read_sessions(target))][:limit]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-list"),
        "target": str(target),
        "sessions": sessions,
        "session_count": len(sessions),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase sessions: {len(sessions)}")
        for session in sessions:
            print(f"- {session.get('session_id')} [{session.get('status')}] range={session.get('phase_range')}")
    return 0


def session_show(*, target: Path, session_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _path, session, error = _resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if json_output:
        print(json.dumps(session, indent=2, sort_keys=True))
    else:
        print(f"phase session: {session.get('session_id')}")
        print(f"status: {session.get('status')}")
        print(f"range: {session.get('phase_range')}")
        print(f"current: {session.get('current_phase_id') or 'none'}")
        print(f"next: {session.get('next_recommended_command') or 'none'}")
    return 0


def session_checkpoint(
    *,
    target: Path,
    session_id: str,
    phase_id: str | None = None,
    status: str = "noted",
    summary: str | None = None,
    notes: list[str] | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    path, session, error = _resolve_session(target, session_id)
    if session is None or path is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if status not in {"noted", "blocked", "recovered"}:
        print("error: --status must be one of ['blocked', 'noted', 'recovered']", file=sys.stderr)
        return 2
    try:
        next_payload = _session_next_payload(target, session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    selected_phase_id = phase_id or next_payload["next_step"].get("phase_id") or session.get("current_phase_id")
    created_at = _now().isoformat()
    checkpoint_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-session-checkpoint-{uuid4().hex[:6]}"
    checkpoint_path = _session_checkpoints_root(target) / f"{checkpoint_id}.json"
    record = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-checkpoint"),
        "target": str(target),
        "checkpoint_id": checkpoint_id,
        "session_id": session.get("session_id"),
        "phase_range": session.get("phase_range"),
        "phase_id": selected_phase_id,
        "status": status,
        "summary": summary or str(next_payload["next_step"].get("detail") or "phase session checkpoint recorded"),
        "notes": [str(item) for item in (notes or []) if str(item)],
        "created_at": created_at,
        "next_step": next_payload["next_step"],
        "suggested_next_command": next_payload["suggested_next_command"],
        "source_fingerprint": _source_fingerprint(
            [],
            {
                "session_id": session.get("session_id"),
                "phase_id": selected_phase_id,
                "status": status,
                "next_step": next_payload["next_step"],
            },
        ),
        "path": str(checkpoint_path),
    }
    _write_json(checkpoint_path, record)
    references = session.get("checkpoint_references") if isinstance(session.get("checkpoint_references"), list) else []
    references.append(_checkpoint_summary(record))
    session["checkpoint_references"] = references[-50:]
    session["latest_checkpoint"] = _checkpoint_summary(record)
    session["current_phase_id"] = selected_phase_id
    session["next_recommended_command"] = next_payload["suggested_next_command"]
    session["updated_at"] = created_at
    session["path"] = str(path)
    _write_json(path, session)
    if json_output:
        print(json.dumps(record, indent=2, sort_keys=True))
    else:
        print(f"phase session checkpoint: {checkpoint_id}")
        print(f"session: {session.get('session_id')}")
        print(f"phase: {selected_phase_id or 'none'}")
        print(f"status: {status}")
        print(f"next: {record['suggested_next_command']}")
    return 0


def session_checkpoint_list(
    *, target: Path, session_id: str | None = None, limit: int = 20, json_output: bool = False
) -> int:
    target = target.expanduser().resolve()
    checkpoints = list(reversed(_read_session_checkpoints(target)))
    if session_id:
        _path, session, error = _resolve_session(target, session_id)
        if session is None:
            print(f"error: {error}", file=sys.stderr)
            return 1
        resolved_session_id = str(session.get("session_id") or "")
        checkpoints = [checkpoint for checkpoint in checkpoints if checkpoint.get("session_id") == resolved_session_id]
    else:
        resolved_session_id = None
    summaries = [_checkpoint_summary(checkpoint) for checkpoint in checkpoints[:limit]]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-checkpoint-list"),
        "target": str(target),
        "session_id": resolved_session_id,
        "checkpoints": summaries,
        "checkpoint_count": len(summaries),
        "suggested_next_command": "brigade work phases session checkpoints show latest"
        if summaries
        else "brigade work phases session checkpoint latest",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase session checkpoints: {len(summaries)}")
        for checkpoint in summaries:
            print(
                f"- {checkpoint.get('checkpoint_id')} [{checkpoint.get('status')}] phase={checkpoint.get('phase_id') or 'none'}"
            )
    return 0


def session_checkpoint_show(*, target: Path, checkpoint_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    checkpoint, error = _resolve_session_checkpoint(target, checkpoint_id)
    if checkpoint is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if json_output:
        print(json.dumps(checkpoint, indent=2, sort_keys=True))
    else:
        print(f"phase session checkpoint: {checkpoint.get('checkpoint_id')}")
        print(f"session: {checkpoint.get('session_id')}")
        print(f"phase: {checkpoint.get('phase_id') or 'none'}")
        print(f"status: {checkpoint.get('status')}")
        print(f"summary: {checkpoint.get('summary')}")
        print(f"next: {checkpoint.get('suggested_next_command') or 'none'}")
    return 0


def _session_checkpoint_compare_payload(target: Path, checkpoint: dict[str, Any]) -> dict[str, Any]:
    session_id = str(checkpoint.get("session_id") or "")
    _path, session, error = _resolve_session(target, session_id)
    checks: list[dict[str, Any]] = []
    current_next = None
    if session is None:
        checks.append(
            _check(
                "block",
                "phase_session_checkpoint_missing_session",
                str(error or "checkpoint session is missing"),
                suggested="brigade work phases session list",
            )
        )
    else:
        current_next = _session_next_payload(target, session)
        saved_step = checkpoint.get("next_step") if isinstance(checkpoint.get("next_step"), dict) else {}
        current_step = current_next.get("next_step") if isinstance(current_next.get("next_step"), dict) else {}
        if saved_step.get("step_type") != current_step.get("step_type"):
            checks.append(
                _check(
                    "warn",
                    "phase_session_checkpoint_step_changed",
                    f"{saved_step.get('step_type')} -> {current_step.get('step_type')}",
                    phase_id=current_step.get("phase_id"),
                    suggested="brigade work phases session checkpoint latest",
                )
            )
        if saved_step.get("phase_id") != current_step.get("phase_id"):
            checks.append(
                _check(
                    "warn",
                    "phase_session_checkpoint_phase_changed",
                    f"{saved_step.get('phase_id')} -> {current_step.get('phase_id')}",
                    phase_id=current_step.get("phase_id"),
                    suggested="brigade work phases session checkpoint latest",
                )
            )
        if checkpoint.get("suggested_next_command") != current_next.get("suggested_next_command"):
            checks.append(
                _check(
                    "warn",
                    "phase_session_checkpoint_command_changed",
                    "saved suggested command differs from current session next command",
                    phase_id=current_step.get("phase_id"),
                    suggested="brigade work phases session next latest",
                )
            )
        current_fingerprint = _source_fingerprint(
            [],
            {
                "session_id": session.get("session_id"),
                "phase_id": checkpoint.get("phase_id"),
                "status": checkpoint.get("status"),
                "next_step": current_step,
            },
        )
        if checkpoint.get("source_fingerprint") != current_fingerprint:
            checks.append(
                _check(
                    "warn",
                    "phase_session_checkpoint_fingerprint_changed",
                    "checkpoint source fingerprint differs from current session state",
                    phase_id=current_step.get("phase_id"),
                    suggested="brigade work phases session checkpoint latest",
                )
            )
    if not checks:
        checks.append(
            _check(
                "ok",
                "phase_session_checkpoint_current",
                "checkpoint matches current session next state",
                phase_id=checkpoint.get("phase_id"),
                suggested="brigade work phases session checkpoints show latest",
            )
        )
    issues = [check for check in checks if check["status"] != "ok"]
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-checkpoint-compare"),
        "target": str(target),
        "checkpoint_id": checkpoint.get("checkpoint_id"),
        "session_id": checkpoint.get("session_id"),
        "phase_id": checkpoint.get("phase_id"),
        "checkpoint": _checkpoint_summary(checkpoint),
        "current_next": current_next,
        "checks": checks,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "suggested_next_command": issues[0]["suggested_next_command"]
        if issues
        else "brigade work phases session checkpoints show latest",
    }


def session_checkpoint_compare(*, target: Path, checkpoint_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    checkpoint, error = _resolve_session_checkpoint(target, checkpoint_id)
    if checkpoint is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        payload = _session_checkpoint_compare_payload(target, checkpoint)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase session checkpoint compare: {payload['checkpoint_id']}")
        print(f"issues: {payload['issue_count']}")
        if payload.get("top_issue"):
            print(f"top: {payload['top_issue']['name']}")
        print(f"next: {payload['suggested_next_command']}")
    return 0


def _checkpoint_issue_import_records(
    target: Path, checkpoint: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    compare = _session_checkpoint_compare_payload(target, checkpoint)
    checkpoint_id = str(checkpoint.get("checkpoint_id") or "")
    session_id = str(checkpoint.get("session_id") or "")
    phase_id = str(checkpoint.get("phase_id") or "")
    records: list[dict[str, Any]] = []

    def append_record(*, issue_type: str, detail: str, suggested_command: object) -> None:
        fingerprint = _source_fingerprint(
            [],
            {
                "checkpoint_id": checkpoint_id,
                "session_id": session_id,
                "phase_id": phase_id,
                "issue_type": issue_type,
                "detail": detail,
                "checkpoint_fingerprint": checkpoint.get("source_fingerprint"),
            },
        )
        records.append(
            {
                "kind": "task",
                "source": "phase-session-checkpoint",
                "text": f"Resolve phase session checkpoint issue {issue_type} for {phase_id or checkpoint_id}: {detail}",
                "type": "workflow",
                "priority": "high"
                if issue_type.endswith("_missing_session") or checkpoint.get("status") == "blocked"
                else "normal",
                "template": "bugfix",
                "acceptance": [
                    f"Checkpoint `{checkpoint_id}` no longer reports `{issue_type}`.",
                    "The phase session has current checkpoint, closeout, report, or deferral evidence.",
                    "`brigade work phases session checkpoints compare` reflects the reviewed state.",
                ],
                "metadata": {
                    "checkpoint_id": checkpoint_id,
                    "session_id": session_id,
                    "phase_id": phase_id,
                    "issue_type": issue_type,
                    "safe_summary": detail,
                    "suggested_command": suggested_command or compare.get("suggested_next_command"),
                    "source_item_key": f"phase-session-checkpoint:{checkpoint_id}:{issue_type}",
                    "source_fingerprint": fingerprint,
                },
            }
        )

    if checkpoint.get("status") == "blocked":
        append_record(
            issue_type="phase_session_checkpoint_blocked",
            detail=str(checkpoint.get("summary") or "checkpoint is marked blocked"),
            suggested_command=checkpoint.get("suggested_next_command"),
        )
    for check in compare.get("checks") or []:
        if not isinstance(check, dict) or check.get("status") == "ok":
            continue
        append_record(
            issue_type=str(check.get("name") or "phase_session_checkpoint_issue"),
            detail=str(check.get("detail") or check.get("name") or "checkpoint issue"),
            suggested_command=check.get("suggested_next_command"),
        )
    return records, compare


def session_checkpoint_import_issues(
    *, target: Path, checkpoint_id: str, dry_run: bool = False, json_output: bool = False
) -> int:
    from . import work_cmd

    target = target.expanduser().resolve()
    checkpoint, error = _resolve_session_checkpoint(target, checkpoint_id)
    if checkpoint is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        records, compare = _checkpoint_issue_import_records(target, checkpoint)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    dismissed: list[dict[str, Any]] = []
    if dry_run:
        created = records
    elif records:
        created, skipped, dismissed = work_cmd._append_import_records(target, records)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-checkpoint-import-issues"),
        "target": str(target),
        "checkpoint_id": checkpoint.get("checkpoint_id"),
        "session_id": checkpoint.get("session_id"),
        "phase_id": checkpoint.get("phase_id"),
        "dry_run": dry_run,
        "compare_issue_count": compare.get("issue_count"),
        "candidate_count": len(records),
        "created": created,
        "skipped": skipped,
        "dismissed": dismissed,
        "created_count": len(created),
        "skipped_count": len(skipped),
        "dismissed_count": len(dismissed),
        "suggested_next_command": "brigade work inbox"
        if records
        else "brigade work phases session checkpoints show latest",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase session checkpoint imports: {payload['checkpoint_id']}")
        print(f"created: {payload['created_count']}")
        print(f"skipped: {payload['skipped_count']}")
        print(f"dismissed: {payload['dismissed_count']}")
    return 0


def session_recovery_note(
    *,
    target: Path,
    session_id: str,
    phase_id: str | None = None,
    summary: str | None = None,
    notes: list[str] | None = None,
    evidence: list[str] | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    path, session, error = _resolve_session(target, session_id)
    if session is None or path is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        next_payload = _session_next_payload(target, session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    selected_phase_id = phase_id or next_payload["next_step"].get("phase_id") or session.get("current_phase_id")
    created_at = _now().isoformat()
    note_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-session-recovery-note-{uuid4().hex[:6]}"
    note_path = _session_recovery_notes_root(target) / f"{note_id}.json"
    safe_notes = [str(item) for item in (notes or []) if str(item)]
    safe_evidence = [str(item) for item in (evidence or []) if str(item)]
    record = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-recovery-note"),
        "target": str(target),
        "note_id": note_id,
        "session_id": session.get("session_id"),
        "phase_range": session.get("phase_range"),
        "phase_id": selected_phase_id,
        "status": "open",
        "summary": summary or str(next_payload["next_step"].get("detail") or "phase session recovery note recorded"),
        "notes": safe_notes,
        "evidence": safe_evidence,
        "created_at": created_at,
        "next_step": next_payload["next_step"],
        "suggested_next_command": next_payload["suggested_next_command"],
        "source_fingerprint": _source_fingerprint(
            [],
            {
                "session_id": session.get("session_id"),
                "phase_id": selected_phase_id,
                "next_step": next_payload["next_step"],
                "summary": summary,
                "notes": safe_notes,
                "evidence": safe_evidence,
            },
        ),
        "path": str(note_path),
    }
    _write_json(note_path, record)
    references = (
        session.get("recovery_note_references") if isinstance(session.get("recovery_note_references"), list) else []
    )
    references.append(_recovery_note_summary(record))
    session["recovery_note_references"] = references[-50:]
    session["latest_recovery_note"] = _recovery_note_summary(record)
    session["updated_at"] = created_at
    session["path"] = str(path)
    _write_json(path, session)
    if json_output:
        print(json.dumps(record, indent=2, sort_keys=True))
    else:
        print(f"phase session recovery note: {note_id}")
        print(f"session: {session.get('session_id')}")
        print(f"phase: {selected_phase_id or 'none'}")
        print(f"summary: {record['summary']}")
    return 0


def session_recovery_note_list(
    *, target: Path, session_id: str | None = None, limit: int = 20, json_output: bool = False
) -> int:
    target = target.expanduser().resolve()
    notes = list(reversed(_read_session_recovery_notes(target)))
    if session_id:
        _path, session, error = _resolve_session(target, session_id)
        if session is None:
            print(f"error: {error}", file=sys.stderr)
            return 1
        resolved_session_id = str(session.get("session_id") or "")
        notes = [note for note in notes if note.get("session_id") == resolved_session_id]
    else:
        resolved_session_id = None
    summaries = [_recovery_note_summary(note) for note in notes[:limit]]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-recovery-note-list"),
        "target": str(target),
        "session_id": resolved_session_id,
        "notes": summaries,
        "note_count": len(summaries),
        "suggested_next_command": "brigade work phases session recovery-notes show latest"
        if summaries
        else "brigade work phases session recovery-note latest",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase session recovery notes: {len(summaries)}")
        for note in summaries:
            print(f"- {note.get('note_id')} [{note.get('status')}] phase={note.get('phase_id') or 'none'}")
    return 0


def session_recovery_note_show(*, target: Path, note_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    note, error = _resolve_session_recovery_note(target, note_id)
    if note is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if json_output:
        print(json.dumps(note, indent=2, sort_keys=True))
    else:
        print(f"phase session recovery note: {note.get('note_id')}")
        print(f"session: {note.get('session_id')}")
        print(f"phase: {note.get('phase_id') or 'none'}")
        print(f"status: {note.get('status')}")
        print(f"summary: {note.get('summary')}")
    return 0


def session_recovery_note_closeout(
    *, target: Path, note_id: str, status: str = "reviewed", reason: str | None = None, json_output: bool = False
) -> int:
    if status not in {"reviewed", "deferred", "blocked", "archived"}:
        print("error: --status must be one of ['archived', 'blocked', 'deferred', 'reviewed']", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    note, error = _resolve_session_recovery_note(target, note_id)
    if note is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    path = Path(str(note.get("path") or (_session_recovery_notes_root(target) / f"{note.get('note_id')}.json")))
    closed_at = _now().isoformat()
    closeout = {
        "status": status,
        "reason": reason or f"phase session recovery note marked {status}",
        "reviewed_at": closed_at,
        "source_fingerprint": note.get("source_fingerprint"),
    }
    note["status"] = status
    note["closeout"] = closeout
    note["updated_at"] = closed_at
    _write_json(path, note)
    session_id = str(note.get("session_id") or "")
    session_path, session, _error = _resolve_session(target, session_id) if session_id else (None, None, None)
    if session is not None and session_path is not None:
        references = (
            session.get("recovery_note_references") if isinstance(session.get("recovery_note_references"), list) else []
        )
        updated_summary = _recovery_note_summary(note)
        session["recovery_note_references"] = [
            updated_summary if item.get("note_id") == note.get("note_id") else item
            for item in references
            if isinstance(item, dict)
        ]
        session["latest_recovery_note"] = updated_summary
        session["updated_at"] = closed_at
        _write_json(session_path, session)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-recovery-note-closeout"),
        "target": str(target),
        "note": note,
        "closeout": closeout,
        "suggested_next_command": "brigade work phases session recovery-notes list",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase session recovery note closeout: {note.get('note_id')}")
        print(f"status: {status}")
        print(f"reason: {closeout['reason']}")
    return 0


def _session_risk_payload(target: Path, session: dict[str, Any]) -> dict[str, Any]:
    next_payload = _session_next_payload(target, session)
    doctor = doctor_payload(target, phase_range=str(session.get("phase_range") or ""))
    risks: list[dict[str, Any]] = []
    step = next_payload.get("next_step") if isinstance(next_payload.get("next_step"), dict) else {}
    step_type = str(step.get("step_type") or "")
    if step_type in {
        "missing_record",
        "blocked_phase",
        "stale_in_progress_phase",
        "unverified_phase",
        "missing_commit_hash",
        "missing_push_ref",
        "unreviewed_pushed_phase",
    }:
        risks.append(
            _check(
                "block" if step_type in {"missing_record", "blocked_phase"} else "warn",
                f"phase_session_{step_type}",
                str(step.get("detail") or step_type),
                phase_id=step.get("phase_id"),
                suggested=step.get("suggested_next_command"),
            )
        )
    checkpoint = next_payload.get("checkpoint") if isinstance(next_payload.get("checkpoint"), dict) else None
    if checkpoint and int(checkpoint.get("issue_count") or 0) > 0:
        top_issue = checkpoint.get("top_issue") if isinstance(checkpoint.get("top_issue"), dict) else {}
        status = "block" if top_issue.get("status") == "block" else "warn"
        risks.append(
            _check(
                status,
                "phase_session_checkpoint_risk",
                str(top_issue.get("detail") or "checkpoint issues are open"),
                phase_id=top_issue.get("phase_id"),
                suggested=checkpoint.get("suggested_next_command"),
            )
        )
    open_notes = [
        note
        for note in _read_session_recovery_notes(target)
        if note.get("session_id") == session.get("session_id") and note.get("status") in {"open", "blocked"}
    ]
    if open_notes:
        top_note = open_notes[-1]
        risks.append(
            _check(
                "warn",
                "phase_session_open_recovery_notes",
                f"{len(open_notes)} open recovery note(s)",
                phase_id=top_note.get("phase_id"),
                suggested=f"brigade work phases session recovery-notes show {top_note.get('note_id')}",
            )
        )
    if int(doctor.get("issue_count") or 0) > 0:
        top = doctor.get("top_issue") if isinstance(doctor.get("top_issue"), dict) else {}
        risks.append(
            _check(
                "warn",
                "phase_session_doctor_issues",
                str(top.get("detail") or "phase ledger doctor has issue(s)"),
                phase_id=top.get("phase_id"),
                suggested=top.get("suggested_next_command") or "brigade work phases doctor",
            )
        )
    if not risks:
        risks.append(
            _check(
                "ok",
                "phase_session_risk",
                "no phase session risks detected",
                suggested="brigade work phases session next latest",
            )
        )
    issues = [risk for risk in risks if risk["status"] != "ok"]
    risk_level = "high" if any(risk["status"] == "block" for risk in issues) else "medium" if issues else "low"
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-risk"),
        "target": str(target),
        "session_id": session.get("session_id"),
        "phase_range": session.get("phase_range"),
        "risk_level": risk_level,
        "risks": risks,
        "risk_count": len(issues),
        "top_risk": issues[0] if issues else None,
        "next_step": step,
        "checkpoint": checkpoint,
        "open_recovery_note_count": len(open_notes),
        "doctor_issue_count": doctor.get("issue_count"),
        "suggested_next_command": issues[0]["suggested_next_command"]
        if issues
        else next_payload.get("suggested_next_command"),
    }


def session_risk(*, target: Path, session_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _path, session, error = _resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        payload = _session_risk_payload(target, session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase session risk: {payload['session_id']}")
        print(f"risk: {payload['risk_level']}")
        print(f"issues: {payload['risk_count']}")
        if payload.get("top_risk"):
            print(f"top: {payload['top_risk']['name']}")
        print(f"next: {payload['suggested_next_command']}")
    return 0


def _session_verification_payload(target: Path, session: dict[str, Any]) -> dict[str, Any]:
    records, missing = _session_phase_records(target, str(session.get("phase_range") or ""))
    status_counts: dict[str, int] = {status: 0 for status in sorted(PHASE_VERIFY_STATUSES)}
    phase_rows: list[dict[str, Any]] = []
    missing_verification: list[str] = []
    failed_verification: list[str] = []
    for record in records:
        entries = _verification_entries(record)
        for entry in entries:
            status = str(entry.get("status") or "expected")
            status_counts[status] = status_counts.get(status, 0) + 1
        if any(entry.get("command") == "focused verification not declared" for entry in entries):
            missing_verification.append(str(record.get("phase_id")))
        if any(entry.get("status") == "failed" for entry in entries):
            failed_verification.append(str(record.get("phase_id")))
        phase_rows.append(
            {
                "phase_id": record.get("phase_id"),
                "status": record.get("status"),
                "verification": entries,
                "verification_count": len(entries),
                "has_tests": bool(record.get("tests_run")),
            }
        )
    issue_count = len(missing) + len(missing_verification) + len(failed_verification)
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-verification"),
        "target": str(target),
        "session_id": session.get("session_id"),
        "phase_range": session.get("phase_range"),
        "record_count": len(records),
        "missing_phase_ids": missing,
        "status_counts": status_counts,
        "phases": phase_rows,
        "missing_verification_phase_ids": missing_verification,
        "failed_verification_phase_ids": failed_verification,
        "issue_count": issue_count,
        "suggested_next_command": f"brigade work phases verify plan {missing_verification[0]}"
        if missing_verification
        else "brigade work phases session progress latest",
    }


def session_verification(*, target: Path, session_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _path, session, error = _resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    payload = _session_verification_payload(target, session)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase session verification: {payload['session_id']}")
        print(f"records: {payload['record_count']}")
        print(f"issues: {payload['issue_count']}")
        print(f"passed: {payload['status_counts'].get('passed', 0)}")
        print(f"next: {payload['suggested_next_command']}")
    return 0


def _latest_privacy_check(record: dict[str, Any]) -> dict[str, Any] | None:
    checks = record.get("privacy_checks") if isinstance(record.get("privacy_checks"), list) else []
    for check in reversed(checks):
        if isinstance(check, dict):
            return check
    return None


def _session_privacy_payload(target: Path, session: dict[str, Any]) -> dict[str, Any]:
    records, missing = _session_phase_records(target, str(session.get("phase_range") or ""))
    phase_rows: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {"clean": 0, "blocked": 0, "missing": 0}
    blocked_phase_ids: list[str] = []
    missing_privacy_phase_ids: list[str] = []
    for record in records:
        latest = _latest_privacy_check(record)
        status = str((latest or {}).get("status") or "missing")
        if status not in status_counts:
            status_counts[status] = 0
        status_counts[status] += 1
        phase_id = str(record.get("phase_id") or "")
        if status == "blocked":
            blocked_phase_ids.append(phase_id)
        if status == "missing":
            missing_privacy_phase_ids.append(phase_id)
        phase_rows.append(
            {
                "phase_id": phase_id,
                "status": record.get("status"),
                "privacy_status": status,
                "latest_privacy_check": latest,
            }
        )
    issue_count = len(missing) + len(blocked_phase_ids) + len(missing_privacy_phase_ids)
    next_phase = (
        blocked_phase_ids[0]
        if blocked_phase_ids
        else missing_privacy_phase_ids[0]
        if missing_privacy_phase_ids
        else None
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-privacy"),
        "target": str(target),
        "session_id": session.get("session_id"),
        "phase_range": session.get("phase_range"),
        "record_count": len(records),
        "missing_phase_ids": missing,
        "status_counts": status_counts,
        "phases": phase_rows,
        "blocked_phase_ids": blocked_phase_ids,
        "missing_privacy_phase_ids": missing_privacy_phase_ids,
        "issue_count": issue_count,
        "suggested_next_command": f"brigade work phases privacy {next_phase}"
        if next_phase
        else "brigade work phases session progress latest",
    }


def session_privacy(*, target: Path, session_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _path, session, error = _resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    payload = _session_privacy_payload(target, session)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase session privacy: {payload['session_id']}")
        print(f"records: {payload['record_count']}")
        print(f"issues: {payload['issue_count']}")
        print(f"clean: {payload['status_counts'].get('clean', 0)}")
        print(f"next: {payload['suggested_next_command']}")
    return 0


def _latest_phase_handoff(record: dict[str, Any]) -> dict[str, Any] | None:
    handoffs = record.get("phase_handoffs") if isinstance(record.get("phase_handoffs"), list) else []
    for handoff_item in reversed(handoffs):
        if isinstance(handoff_item, dict):
            return handoff_item
    return None


def _record_has_handoff_deferral(record: dict[str, Any]) -> bool:
    deferred = " ".join(str(item).casefold() for item in record.get("deferred_items") or [])
    return "handoff" in deferred


def _session_handoffs_payload(target: Path, session: dict[str, Any]) -> dict[str, Any]:
    records, missing = _session_phase_records(target, str(session.get("phase_range") or ""))
    status_counts: dict[str, int] = {"linted": 0, "drafted": 0, "failed": 0, "deferred": 0, "missing": 0}
    missing_handoff_phase_ids: list[str] = []
    failed_handoff_phase_ids: list[str] = []
    phase_rows: list[dict[str, Any]] = []
    for record in records:
        phase_id = str(record.get("phase_id") or "")
        latest = _latest_phase_handoff(record)
        lint = latest.get("lint") if isinstance(latest, dict) and isinstance(latest.get("lint"), dict) else {}
        lint_status = str(lint.get("status") or "not-run")
        if lint_status == "passed":
            handoff_status = "linted"
        elif lint_status == "failed":
            handoff_status = "failed"
            failed_handoff_phase_ids.append(phase_id)
        elif latest is not None:
            handoff_status = "drafted"
        elif _record_has_handoff_deferral(record):
            handoff_status = "deferred"
        else:
            handoff_status = "missing"
            missing_handoff_phase_ids.append(phase_id)
        status_counts[handoff_status] = status_counts.get(handoff_status, 0) + 1
        phase_rows.append(
            {
                "phase_id": phase_id,
                "status": record.get("status"),
                "handoff_status": handoff_status,
                "latest_handoff": latest,
            }
        )
    issue_count = len(missing) + len(failed_handoff_phase_ids) + len(missing_handoff_phase_ids)
    next_phase = (
        failed_handoff_phase_ids[0]
        if failed_handoff_phase_ids
        else missing_handoff_phase_ids[0]
        if missing_handoff_phase_ids
        else None
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-handoffs"),
        "target": str(target),
        "session_id": session.get("session_id"),
        "phase_range": session.get("phase_range"),
        "record_count": len(records),
        "missing_phase_ids": missing,
        "status_counts": status_counts,
        "phases": phase_rows,
        "missing_handoff_phase_ids": missing_handoff_phase_ids,
        "failed_handoff_phase_ids": failed_handoff_phase_ids,
        "issue_count": issue_count,
        "suggested_next_command": f"brigade work phases handoff {next_phase} --lint"
        if next_phase
        else "brigade work phases session progress latest",
    }


def session_handoffs(*, target: Path, session_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _path, session, error = _resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    payload = _session_handoffs_payload(target, session)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase session handoffs: {payload['session_id']}")
        print(f"records: {payload['record_count']}")
        print(f"issues: {payload['issue_count']}")
        print(f"linted: {payload['status_counts'].get('linted', 0)}")
        print(f"next: {payload['suggested_next_command']}")
    return 0


def _checkpoint_state_for_session_next(
    target: Path, session: dict[str, Any], step: dict[str, Any]
) -> dict[str, Any] | None:
    checkpoint = _latest_checkpoint_for_session(target, session.get("session_id"))
    if checkpoint is None:
        return None
    checks: list[dict[str, Any]] = []
    saved_step = checkpoint.get("next_step") if isinstance(checkpoint.get("next_step"), dict) else {}
    if checkpoint.get("status") == "blocked":
        checks.append(
            _check(
                "block",
                "phase_session_checkpoint_blocked",
                str(checkpoint.get("summary") or "checkpoint is marked blocked"),
                phase_id=checkpoint.get("phase_id"),
                suggested=checkpoint.get("suggested_next_command"),
            )
        )
    if saved_step.get("step_type") != step.get("step_type"):
        checks.append(
            _check(
                "warn",
                "phase_session_checkpoint_step_changed",
                f"{saved_step.get('step_type')} -> {step.get('step_type')}",
                phase_id=step.get("phase_id"),
                suggested="brigade work phases session checkpoint latest",
            )
        )
    if saved_step.get("phase_id") != step.get("phase_id"):
        checks.append(
            _check(
                "warn",
                "phase_session_checkpoint_phase_changed",
                f"{saved_step.get('phase_id')} -> {step.get('phase_id')}",
                phase_id=step.get("phase_id"),
                suggested="brigade work phases session checkpoint latest",
            )
        )
    if checkpoint.get("suggested_next_command") != step.get("suggested_next_command"):
        checks.append(
            _check(
                "warn",
                "phase_session_checkpoint_command_changed",
                "saved suggested command differs from current session next command",
                phase_id=step.get("phase_id"),
                suggested="brigade work phases session next latest",
            )
        )
    current_fingerprint = _source_fingerprint(
        [],
        {
            "session_id": session.get("session_id"),
            "phase_id": checkpoint.get("phase_id"),
            "status": checkpoint.get("status"),
            "next_step": step,
        },
    )
    if checkpoint.get("source_fingerprint") != current_fingerprint:
        checks.append(
            _check(
                "warn",
                "phase_session_checkpoint_fingerprint_changed",
                "checkpoint source fingerprint differs from current session state",
                phase_id=step.get("phase_id"),
                suggested="brigade work phases session checkpoint latest",
            )
        )
    issues = [check for check in checks if check["status"] != "ok"]
    return {
        "latest_checkpoint": _checkpoint_summary(checkpoint),
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "suggested_next_command": "brigade work phases session checkpoints import-issues latest"
        if issues
        else "brigade work phases session checkpoints show latest",
    }


def session_closeout(
    *, target: Path, session_id: str, status: str = "reviewed", reason: str | None = None, json_output: bool = False
) -> int:
    if status not in PHASE_SESSION_CLOSEOUT_STATUSES:
        print(f"error: --status must be one of {sorted(PHASE_SESSION_CLOSEOUT_STATUSES)}", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    path, session, error = _resolve_session(target, session_id)
    if session is None or path is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    phase_range = str(session.get("phase_range") or "")
    doctor_data = doctor_payload(target, phase_range=phase_range)
    closeout_payload = {
        "status": status,
        "reason": reason or f"phase session marked {status}",
        "reviewed_at": _now().isoformat(),
        "unresolved_issue_count": doctor_data["issue_count"],
        "source_fingerprint": _source_fingerprint(
            session.get("phase_records") if isinstance(session.get("phase_records"), list) else [],
            {"session_id": session.get("session_id"), "status": session.get("status")},
        ),
    }
    session["status"] = "closed" if status in {"reviewed", "archived"} else status
    session["completed_at"] = session.get("completed_at") or _now().isoformat()
    session["updated_at"] = _now().isoformat()
    session["closeout"] = closeout_payload
    session["next_recommended_command"] = "brigade work phases session list"
    session["path"] = str(path)
    _write_json(path, session)
    if json_output:
        print(json.dumps(session, indent=2, sort_keys=True))
    else:
        print(f"phase session closeout: {session.get('session_id')}")
        print(f"status: {status}")
        print(f"unresolved: {doctor_data['issue_count']}")
    return 0


def _session_next_payload(target: Path, session: dict[str, Any]) -> dict[str, Any]:
    phase_range = str(session.get("phase_range") or "")
    records, missing = _session_phase_records(target, phase_range)
    by_id = {str(record.get("phase_id")): record for record in records}
    parsed = _parse_range(phase_range)
    expected = [_phase_id_for(number) for number in range(parsed[0], parsed[1] + 1)] if parsed else []
    step = {
        "step_type": "session_complete",
        "phase_id": None,
        "detail": "phase session range is complete",
        "suggested_next_command": f"brigade work phases session closeout {session.get('session_id')}",
    }
    for phase_id in expected:
        if phase_id not in by_id:
            step = {
                "step_type": "missing_record",
                "phase_id": phase_id,
                "detail": f"{phase_id} is missing from the phase ledger",
                "suggested_next_command": f"brigade work phases plan --phase-id {phase_id}",
            }
            break
        record = by_id[phase_id]
        status = str(record.get("status") or "pending")
        if status == "pending":
            step = {
                "step_type": "pending_phase",
                "phase_id": phase_id,
                "detail": f"{phase_id} is pending",
                "suggested_next_command": f"brigade work phases start {phase_id}",
            }
            break
        if status == "in-progress":
            started = _parse_time(record.get("started_at"))
            stale = bool(started and _now() - started > timedelta(hours=STALE_IN_PROGRESS_HOURS))
            step = {
                "step_type": "stale_in_progress_phase" if stale else "in_progress_phase",
                "phase_id": phase_id,
                "detail": f"{phase_id} is in progress" + (" and stale" if stale else ""),
                "suggested_next_command": f"brigade work phases show {phase_id}",
            }
            break
        if status == "blocked":
            step = {
                "step_type": "blocked_phase",
                "phase_id": phase_id,
                "detail": str(record.get("blocker_reason") or f"{phase_id} is blocked"),
                "suggested_next_command": record.get("next_phase_recommendation")
                or f"brigade work phases show {phase_id}",
            }
            break
        if status in DONE_STATUSES:
            if not record.get("tests_run"):
                step = {
                    "step_type": "unverified_phase",
                    "phase_id": phase_id,
                    "detail": f"{phase_id} has no recorded tests",
                    "suggested_next_command": f'brigade work phases complete {phase_id} --test "<command>"',
                }
                break
            if status in {"committed", "pushed"} and not record.get("commit_hash"):
                step = {
                    "step_type": "missing_commit_hash",
                    "phase_id": phase_id,
                    "detail": f"{phase_id} is missing commit evidence",
                    "suggested_next_command": f"brigade work phases complete {phase_id} --commit <hash>",
                }
                break
            if status == "pushed" and not record.get("push_ref"):
                step = {
                    "step_type": "missing_push_ref",
                    "phase_id": phase_id,
                    "detail": f"{phase_id} is missing push evidence",
                    "suggested_next_command": f"brigade work phases complete {phase_id} --push-ref <ref>",
                }
                break
            if status == "pushed" and not _phase_has_current_closeout(target, phase_id, record):
                step = {
                    "step_type": "unreviewed_pushed_phase",
                    "phase_id": phase_id,
                    "detail": f"{phase_id} is pushed but lacks a current closeout",
                    "suggested_next_command": f"brigade work phases closeout {phase_id}",
                }
                break
    if (
        not missing
        and expected
        and all(by_id.get(phase_id, {}).get("status") in DONE_STATUSES | {"deferred"} for phase_id in expected)
    ):
        closeout = session.get("closeout") if isinstance(session.get("closeout"), dict) else None
        if closeout and closeout.get("status") == "reviewed":
            step = {
                "step_type": "session_reviewed",
                "phase_id": None,
                "detail": "phase session is reviewed",
                "suggested_next_command": "brigade work phases session list",
            }
        elif step["step_type"] == "session_complete":
            step = {
                "step_type": "session_closeout_needed",
                "phase_id": None,
                "detail": "all phases are done or deferred, but the session is not reviewed",
                "suggested_next_command": f"brigade work phases session closeout {session.get('session_id')}",
            }
    checkpoint_state = _checkpoint_state_for_session_next(target, session, step)
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-next"),
        "target": str(target),
        "session_id": session.get("session_id"),
        "phase_range": phase_range,
        "missing_phase_ids": missing,
        "next_step": step,
        "checkpoint": checkpoint_state,
        "suggested_next_command": step["suggested_next_command"],
    }


def session_next(*, target: Path, session_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _path, session, error = _resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        payload = _session_next_payload(target, session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        step = payload["next_step"]
        print(f"phase session next: {payload['session_id']}")
        print(f"step: {step['step_type']}")
        print(f"detail: {step['detail']}")
        checkpoint = payload.get("checkpoint") if isinstance(payload.get("checkpoint"), dict) else None
        if checkpoint:
            latest = (
                checkpoint.get("latest_checkpoint") if isinstance(checkpoint.get("latest_checkpoint"), dict) else {}
            )
            print(f"checkpoint: {latest.get('checkpoint_id')} issues={checkpoint.get('issue_count')}")
        print(f"next: {payload['suggested_next_command']}")
    return 0


def _session_protocol_payload(target: Path, session: dict[str, Any]) -> dict[str, Any]:
    next_payload = _session_next_payload(target, session)
    risk_payload = _session_risk_payload(target, session)
    progress_payload = _session_progress_payload(target, session)
    verification_payload = _session_verification_payload(target, session)
    privacy_payload = _session_privacy_payload(target, session)
    handoffs_payload = _session_handoffs_payload(target, session)
    gate_payload = _session_gate_payload(target, session)
    checkpoint = next_payload.get("checkpoint") if isinstance(next_payload.get("checkpoint"), dict) else None
    resume_blockers: list[dict[str, Any]] = []
    next_command = str(next_payload.get("suggested_next_command") or "")
    if checkpoint and int(checkpoint.get("issue_count") or 0) > 0:
        top_issue = checkpoint.get("top_issue") if isinstance(checkpoint.get("top_issue"), dict) else {}
        resume_blockers.append(
            _check(
                "block",
                "phase_session_protocol_checkpoint_issue",
                str(top_issue.get("detail") or "checkpoint needs review before resume"),
                phase_id=top_issue.get("phase_id"),
                suggested=str(
                    checkpoint.get("suggested_next_command") or "brigade work phases session checkpoints compare latest"
                ),
            )
        )
    if risk_payload.get("risk_level") == "high":
        resume_blockers.append(
            _check(
                "block",
                "phase_session_protocol_high_risk",
                "session risk is high",
                suggested=str(risk_payload.get("suggested_next_command") or "brigade work phases session risk latest"),
            )
        )
    if next_command and not next_command.startswith("brigade work phases "):
        resume_blockers.append(
            _check(
                "block",
                "phase_session_protocol_command_not_allowed",
                "suggested next command is outside the phase ledger command family",
                suggested="brigade work phases session next latest",
            )
        )
    wrapper_steps = [
        {
            "step": "inspect-next",
            "command": f"brigade work phases session next {session.get('session_id')} --json",
            "writes": False,
        },
        {
            "step": "inspect-risk",
            "command": f"brigade work phases session risk {session.get('session_id')} --json",
            "writes": False,
        },
        {
            "step": "inspect-progress",
            "command": f"brigade work phases session progress {session.get('session_id')} --json",
            "writes": False,
        },
    ]
    if checkpoint:
        latest = checkpoint.get("latest_checkpoint") if isinstance(checkpoint.get("latest_checkpoint"), dict) else {}
        wrapper_steps.append(
            {
                "step": "compare-checkpoint",
                "command": f"brigade work phases session checkpoints compare {latest.get('checkpoint_id') or 'latest'} --json",
                "writes": False,
            }
        )
    if resume_blockers:
        wrapper_steps.append(
            {
                "step": "route-blockers",
                "command": "brigade work phases session checkpoints import-issues latest --json"
                if checkpoint
                else f"brigade work phases session import-issues {session.get('session_id')} --json",
                "writes": True,
            }
        )
    else:
        wrapper_steps.append(
            {
                "step": "record-resume",
                "command": f"brigade work phases session resume {session.get('session_id')} --json",
                "writes": True,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-protocol"),
        "target": str(target),
        "session_id": session.get("session_id"),
        "phase_range": session.get("phase_range"),
        "safe_resume": not resume_blockers,
        "metadata_only": True,
        "executes_suggested_command": False,
        "next_step": next_payload.get("next_step"),
        "checkpoint": checkpoint,
        "risk": {
            "risk_level": risk_payload.get("risk_level"),
            "risk_count": risk_payload.get("risk_count"),
            "suggested_next_command": risk_payload.get("suggested_next_command"),
        },
        "progress": {
            "percent_complete": progress_payload.get("percent_complete"),
            "blocker_count": progress_payload.get("blocker_count"),
            "current_phase_id": progress_payload.get("current_phase_id"),
        },
        "verification": {
            "issue_count": verification_payload.get("issue_count"),
            "suggested_next_command": verification_payload.get("suggested_next_command"),
        },
        "privacy": {
            "issue_count": privacy_payload.get("issue_count"),
            "suggested_next_command": privacy_payload.get("suggested_next_command"),
        },
        "handoffs": {
            "issue_count": handoffs_payload.get("issue_count"),
            "suggested_next_command": handoffs_payload.get("suggested_next_command"),
        },
        "completion_gate": {
            "safe_to_claim_complete": gate_payload.get("safe_to_claim_complete"),
            "blocker_count": gate_payload.get("blocker_count"),
            "suggested_next_command": gate_payload.get("suggested_next_command"),
        },
        "resume_blockers": resume_blockers,
        "resume_blocker_count": len(resume_blockers),
        "allowed_command_prefixes": ["brigade work phases "],
        "forbidden_actions": [
            "arbitrary command execution",
            "git push",
            "remote mutation",
            "scanner execution",
            "reviewer execution",
            "tool execution",
        ],
        "wrapper_steps": wrapper_steps,
        "suggested_next_command": wrapper_steps[-1]["command"],
    }


def session_protocol(*, target: Path, session_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _path, session, error = _resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        payload = _session_protocol_payload(target, session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase session protocol: {payload['session_id']}")
        print(f"safe_resume: {str(payload['safe_resume']).lower()}")
        print(f"blockers: {payload['resume_blocker_count']}")
        for step in payload["wrapper_steps"]:
            print(f"- {step['step']}: {step['command']}")
    return 0


def _session_audit_payload(target: Path, session: dict[str, Any]) -> dict[str, Any]:
    protocol = _session_protocol_payload(target, session)
    progress = _session_progress_payload(target, session)
    risk = _session_risk_payload(target, session)
    verification = _session_verification_payload(target, session)
    privacy = _session_privacy_payload(target, session)
    handoffs = _session_handoffs_payload(target, session)
    gate = _session_gate_payload(target, session)
    checks: list[dict[str, Any]] = []
    checks.append(
        _gate_check(
            "ok" if protocol.get("safe_resume") else "block",
            "phase_session_audit_resume_protocol",
            "resume protocol is safe" if protocol.get("safe_resume") else "resume protocol has blockers",
            suggested=protocol.get("suggested_next_command"),
        )
    )
    checks.append(
        _gate_check(
            "ok" if risk.get("risk_level") == "low" else "warn" if risk.get("risk_level") == "medium" else "block",
            "phase_session_audit_risk",
            f"risk level is {risk.get('risk_level')}",
            suggested=risk.get("suggested_next_command"),
        )
    )
    checks.append(
        _gate_check(
            "ok" if int(progress.get("blocker_count") or 0) == 0 else "warn",
            "phase_session_audit_progress",
            f"{progress.get('percent_complete')}% complete with {progress.get('blocker_count')} blocker(s)",
            phase_id=progress.get("current_phase_id"),
            suggested=progress.get("suggested_next_command"),
        )
    )
    for name, payload in (
        ("verification", verification),
        ("privacy", privacy),
        ("handoffs", handoffs),
    ):
        issue_count = int(payload.get("issue_count") or 0)
        checks.append(
            _gate_check(
                "ok" if issue_count == 0 else "block",
                f"phase_session_audit_{name}",
                f"{issue_count} {name} issue(s)",
                suggested=payload.get("suggested_next_command"),
            )
        )
    checks.append(
        _gate_check(
            "ok" if gate.get("safe_to_claim_complete") else "block",
            "phase_session_audit_completion_gate",
            "completion gate is clean"
            if gate.get("safe_to_claim_complete")
            else f"completion gate has {gate.get('blocker_count')} blocker(s)",
            suggested=gate.get("suggested_next_command"),
        )
    )
    issues = [check for check in checks if check.get("status") != "ok"]
    blockers = [check for check in checks if check.get("status") == "block"]
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-audit"),
        "target": str(target),
        "session_id": session.get("session_id"),
        "phase_range": session.get("phase_range"),
        "ready_for_resume": bool(protocol.get("safe_resume")),
        "ready_for_completion_claim": bool(gate.get("safe_to_claim_complete")) and not blockers,
        "checks": checks,
        "issue_count": len(issues),
        "blocker_count": len(blockers),
        "top_issue": issues[0] if issues else None,
        "protocol": {
            "safe_resume": protocol.get("safe_resume"),
            "resume_blocker_count": protocol.get("resume_blocker_count"),
            "suggested_next_command": protocol.get("suggested_next_command"),
        },
        "progress": {
            "percent_complete": progress.get("percent_complete"),
            "blocker_count": progress.get("blocker_count"),
            "current_phase_id": progress.get("current_phase_id"),
        },
        "risk": {
            "risk_level": risk.get("risk_level"),
            "risk_count": risk.get("risk_count"),
        },
        "verification": {"issue_count": verification.get("issue_count")},
        "privacy": {"issue_count": privacy.get("issue_count")},
        "handoffs": {"issue_count": handoffs.get("issue_count")},
        "completion_gate": {
            "safe_to_claim_complete": gate.get("safe_to_claim_complete"),
            "blocker_count": gate.get("blocker_count"),
            "top_blocker": gate.get("top_blocker"),
        },
        "suggested_next_command": issues[0]["suggested_next_command"]
        if issues
        else "brigade work phases session show latest",
    }


def session_audit(*, target: Path, session_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _path, session, error = _resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        payload = _session_audit_payload(target, session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase session audit: {payload['session_id']}")
        print(f"ready_for_resume: {str(payload['ready_for_resume']).lower()}")
        print(f"ready_for_completion_claim: {str(payload['ready_for_completion_claim']).lower()}")
        print(f"issues: {payload['issue_count']}")
        print(f"next: {payload['suggested_next_command']}")
    return 0


def session_resume(*, target: Path, session_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    path, session, error = _resolve_session(target, session_id)
    if session is None or path is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        next_payload = _session_next_payload(target, session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    resume_event = {
        "resumed_at": _now().isoformat(),
        "next_step": next_payload["next_step"],
        "checkpoint": next_payload.get("checkpoint"),
        "suggested_next_command": next_payload["suggested_next_command"],
    }
    history = session.get("resume_history") if isinstance(session.get("resume_history"), list) else []
    history.append(resume_event)
    session["resume_history"] = history[-20:]
    session["current_phase_id"] = next_payload["next_step"].get("phase_id")
    session["next_recommended_command"] = next_payload["suggested_next_command"]
    session["updated_at"] = _now().isoformat()
    _write_json(path, session)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-resume"),
        "target": str(target),
        "session_id": session.get("session_id"),
        "resume": resume_event,
        "writes": ["session resume metadata"],
        "executed": False,
        "suggested_next_command": next_payload["suggested_next_command"],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase session resume: {session.get('session_id')}")
        print("executed: false")
        checkpoint = next_payload.get("checkpoint") if isinstance(next_payload.get("checkpoint"), dict) else None
        if checkpoint:
            latest = (
                checkpoint.get("latest_checkpoint") if isinstance(checkpoint.get("latest_checkpoint"), dict) else {}
            )
            print(f"checkpoint: {latest.get('checkpoint_id')} issues={checkpoint.get('issue_count')}")
        print(f"next: {payload['suggested_next_command']}")
    return 0


def _session_import_summaries(target: Path) -> list[dict[str, Any]]:
    try:
        from . import work_cmd

        imports = work_cmd._pending_imports(target)
    except Exception:
        imports = []
    summaries = []
    for item in imports:
        if item.get("source") not in {"phase-ledger", "phase-ledger-action", "phase-session"}:
            continue
        summaries.append(
            {
                "import_id": item.get("id"),
                "source": item.get("source"),
                "kind": item.get("kind"),
                "status": item.get("status"),
                "text": item.get("text"),
                "created_at": item.get("created_at"),
            }
        )
    return summaries


def _session_report_payload(target: Path, session: dict[str, Any]) -> dict[str, Any]:
    phase_range = str(session.get("phase_range") or "")
    records, missing = _session_phase_records(target, phase_range)
    doctor_data = doctor_payload(target, phase_range=phase_range)
    next_data = _session_next_payload(target, session)
    actions = [
        _action_summary(action) for action in _read_actions(target) if action.get("status") in {"pending", "active"}
    ]
    latest_report = _latest_report(target)
    report_compare = _report_compare_summary(target, latest_report)
    latest_checkpoint = _latest_checkpoint_for_session(target, session.get("session_id"))
    checkpoint_compare = None
    if isinstance(latest_checkpoint, dict):
        try:
            checkpoint_compare = _session_checkpoint_compare_payload(target, latest_checkpoint)
        except ValueError:
            checkpoint_compare = None
    recovery_notes = [
        _recovery_note_summary(note)
        for note in _read_session_recovery_notes(target)
        if note.get("session_id") == session.get("session_id")
    ]
    open_recovery_notes = [note for note in recovery_notes if note.get("status") not in {"reviewed", "archived"}]
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-report"),
        "target": str(target),
        "report_id": f"{_now().strftime('%Y%m%d-%H%M%S')}-phase-session-report-{uuid4().hex[:6]}",
        "created_at": _now().isoformat(),
        "git_head": _git_head(target),
        "session": _session_summary(session),
        "phase_range": phase_range,
        "missing_phase_ids": missing,
        "phase_records": [_record_summary(record) for record in records],
        "doctor": {
            "issue_count": doctor_data["issue_count"],
            "top_issue": doctor_data["top_issue"],
            "checks": doctor_data["checks"],
        },
        "next": next_data,
        "recovery": {
            "latest_checkpoint": _checkpoint_summary(latest_checkpoint)
            if isinstance(latest_checkpoint, dict)
            else None,
            "checkpoint_compare": {
                "issue_count": checkpoint_compare.get("issue_count"),
                "top_issue": checkpoint_compare.get("top_issue"),
                "suggested_next_command": checkpoint_compare.get("suggested_next_command"),
            }
            if isinstance(checkpoint_compare, dict)
            else None,
            "recovery_notes": recovery_notes,
            "recovery_note_count": len(recovery_notes),
            "open_recovery_note_count": len(open_recovery_notes),
            "suggested_next_command": "brigade work phases session recovery-notes list"
            if recovery_notes
            else "brigade work phases session recovery-note latest",
        },
        "actions": actions,
        "action_count": len(actions),
        "imports": _session_import_summaries(target),
        "phase_report_compare": report_compare,
        "commit_summary": {
            "committed": len([record for record in records if record.get("commit_hash")]),
            "pushed": len([record for record in records if record.get("push_ref")]),
        },
        "test_summary": {
            "with_tests": len([record for record in records if record.get("tests_run")]),
            "without_tests": len([record for record in records if not record.get("tests_run")]),
        },
        "blockers": [check for check in doctor_data["checks"] if check.get("status") != "ok"],
        "suggested_next_commands": [
            next_data["suggested_next_command"],
            "brigade work phases session closeout latest",
        ],
    }


def _write_session_report_markdown(path: Path, payload: dict[str, Any]) -> None:
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    lines = [
        "# Brigade Phase Session Report",
        "",
        f"- Report id: `{payload['report_id']}`",
        f"- Session id: `{session.get('session_id')}`",
        f"- Created: `{payload['created_at']}`",
        f"- Phase range: `{payload.get('phase_range') or 'all'}`",
        f"- Doctor issues: `{payload['doctor']['issue_count']}`",
        f"- Open actions: `{payload['action_count']}`",
        "",
        "## Next Step",
        "",
        f"- `{payload['next']['next_step']['step_type']}`: {payload['next']['next_step']['detail']}",
        f"- Command: `{payload['next']['suggested_next_command']}`",
        "",
        "## Recovery",
        "",
    ]
    recovery = payload.get("recovery") if isinstance(payload.get("recovery"), dict) else {}
    checkpoint = recovery.get("latest_checkpoint") if isinstance(recovery.get("latest_checkpoint"), dict) else None
    checkpoint_compare = (
        recovery.get("checkpoint_compare") if isinstance(recovery.get("checkpoint_compare"), dict) else {}
    )
    if checkpoint:
        lines.append(f"- Checkpoint: `{checkpoint.get('checkpoint_id')}` `{checkpoint.get('status')}`")
        lines.append(f"- Checkpoint issues: `{checkpoint_compare.get('issue_count', 0)}`")
    else:
        lines.append("- Checkpoint: none")
    notes = recovery.get("recovery_notes") if isinstance(recovery.get("recovery_notes"), list) else []
    lines.append(f"- Recovery notes: `{len(notes)}`")
    for note in notes[:10]:
        if isinstance(note, dict):
            lines.append(f"- `{note.get('note_id')}` `{note.get('status')}`: {note.get('summary')}")
    lines.extend(
        [
            "",
            "## Records",
            "",
        ]
    )
    for record in payload.get("phase_records", []):
        lines.append(
            f"- `{record.get('phase_id')}` `{record.get('status')}` commit=`{record.get('commit_hash') or 'none'}` push=`{record.get('push_ref') or 'none'}`"
        )
    lines.extend(["", "## Blockers", ""])
    blockers = payload.get("blockers") or []
    if not blockers:
        lines.append("- none")
    for blocker in blockers:
        lines.append(f"- `{blocker.get('status')}` `{blocker.get('name')}`: {blocker.get('detail')}")
    path.write_text("\n".join(lines).rstrip() + "\n")


def _resolve_session_report(target: Path, report_id: str) -> tuple[dict[str, Any] | None, str | None]:
    target = target.expanduser().resolve()
    if report_id == "latest":
        reports = _read_session_reports(target)
        return (reports[-1], None) if reports else (None, "phase session report not found: latest")
    candidates = sorted(_session_reports_root(target).glob(f"{report_id}*/SESSION_EVIDENCE.json"))
    if len(candidates) != 1:
        return (
            None,
            f"phase session report not found: {report_id}"
            if not candidates
            else f"phase session report id is ambiguous: {report_id}",
        )
    payload = _read_json(candidates[0])
    if payload is None:
        return None, f"invalid phase session report: {candidates[0]}"
    payload.setdefault("path", str(candidates[0].parent))
    return payload, None


def session_report_build(*, target: Path, session_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _path, session, error = _resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    payload = _session_report_payload(target, session)
    report_dir = _session_reports_root(target) / str(payload["report_id"])
    payload["path"] = str(report_dir)
    payload["bundle_files"] = ["SESSION_REPORT.md", "SESSION_EVIDENCE.json"]
    _write_json(report_dir / "SESSION_EVIDENCE.json", payload)
    _write_session_report_markdown(report_dir / "SESSION_REPORT.md", payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase session report: {payload['report_id']}")
        print(f"session: {payload['session'].get('session_id')}")
        print(f"issues: {payload['doctor']['issue_count']}")
    return 0


def session_report_list(*, target: Path, limit: int = 20, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    reports = [
        {
            "report_id": report.get("report_id"),
            "session_id": (report.get("session") or {}).get("session_id")
            if isinstance(report.get("session"), dict)
            else None,
            "created_at": report.get("created_at"),
            "phase_range": report.get("phase_range"),
            "issue_count": (report.get("doctor") or {}).get("issue_count")
            if isinstance(report.get("doctor"), dict)
            else None,
            "path": report.get("path"),
        }
        for report in reversed(_read_session_reports(target))
    ][:limit]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-report-list"),
        "target": str(target),
        "reports": reports,
        "report_count": len(reports),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase session reports: {len(reports)}")
        for report in reports:
            print(f"- {report.get('report_id')} session={report.get('session_id')} issues={report.get('issue_count')}")
    return 0


def session_report_show(*, target: Path, report_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    report, error = _resolve_session_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if json_output:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"phase session report: {report.get('report_id')}")
        print(
            f"session: {(report.get('session') or {}).get('session_id') if isinstance(report.get('session'), dict) else 'none'}"
        )
        print(
            f"issues: {(report.get('doctor') or {}).get('issue_count') if isinstance(report.get('doctor'), dict) else 0}"
        )
    return 0


def _activity_event(
    *,
    timestamp: object,
    event_type: str,
    summary: str,
    phase_id: object = None,
    local_id: object = None,
    status: object = None,
    path: object = None,
    suggested: str | None = None,
) -> dict[str, Any]:
    rendered_timestamp = str(timestamp or "")
    seed = json.dumps(
        {
            "timestamp": rendered_timestamp,
            "event_type": event_type,
            "phase_id": phase_id,
            "local_id": local_id,
            "summary": summary,
        },
        sort_keys=True,
    )
    return {
        "event_id": hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16],
        "timestamp": rendered_timestamp,
        "event_type": event_type,
        "phase_id": phase_id,
        "local_id": local_id,
        "status": status,
        "safe_summary": summary,
        "path": path,
        "suggested_next_command": suggested,
    }


def _session_activity_payload(target: Path, session: dict[str, Any]) -> dict[str, Any]:
    phase_range = str(session.get("phase_range") or "")
    records, missing = _session_phase_records(target, phase_range)
    phase_ids = {str(record.get("phase_id")) for record in records if record.get("phase_id")}
    events: list[dict[str, Any]] = []
    events.append(
        _activity_event(
            timestamp=session.get("started_at"),
            event_type="session-started",
            local_id=session.get("session_id"),
            status=session.get("status"),
            summary=f"phase session started for range {phase_range or 'all'}",
            path=session.get("path"),
            suggested=session.get("next_recommended_command"),
        )
    )
    for item in session.get("resume_history") or []:
        if not isinstance(item, dict):
            continue
        next_step = item.get("next_step") if isinstance(item.get("next_step"), dict) else {}
        events.append(
            _activity_event(
                timestamp=item.get("resumed_at"),
                event_type="session-resume",
                local_id=session.get("session_id"),
                phase_id=next_step.get("phase_id"),
                status=next_step.get("step_type"),
                summary=str(next_step.get("detail") or "session resume recommendation recorded"),
                suggested=item.get("suggested_next_command"),
            )
        )
    for checkpoint in _read_session_checkpoints(target):
        if checkpoint.get("session_id") != session.get("session_id"):
            continue
        events.append(
            _activity_event(
                timestamp=checkpoint.get("created_at"),
                event_type="session-checkpoint",
                local_id=checkpoint.get("checkpoint_id"),
                phase_id=checkpoint.get("phase_id"),
                status=checkpoint.get("status"),
                summary=str(checkpoint.get("summary") or "phase session checkpoint recorded"),
                path=checkpoint.get("path"),
                suggested=checkpoint.get("suggested_next_command"),
            )
        )
    for note in _read_session_recovery_notes(target):
        if note.get("session_id") != session.get("session_id"):
            continue
        events.append(
            _activity_event(
                timestamp=note.get("created_at"),
                event_type="session-recovery-note",
                local_id=note.get("note_id"),
                phase_id=note.get("phase_id"),
                status=note.get("status"),
                summary=str(note.get("summary") or "phase session recovery note recorded"),
                path=note.get("path"),
                suggested=note.get("suggested_next_command"),
            )
        )
    if isinstance(session.get("closeout"), dict):
        closeout = session["closeout"]
        events.append(
            _activity_event(
                timestamp=closeout.get("reviewed_at"),
                event_type="session-closeout",
                local_id=session.get("session_id"),
                status=closeout.get("status"),
                summary=str(closeout.get("reason") or "phase session closeout recorded"),
                path=session.get("path"),
            )
        )
    for record in records:
        phase_id = record.get("phase_id")
        if record.get("created_at"):
            events.append(
                _activity_event(
                    timestamp=record.get("created_at"),
                    event_type="phase-record-created",
                    phase_id=phase_id,
                    status=record.get("status"),
                    summary="phase record created",
                    path=record.get("path"),
                )
            )
        if record.get("started_at"):
            events.append(
                _activity_event(
                    timestamp=record.get("started_at"),
                    event_type="phase-started",
                    phase_id=phase_id,
                    status=record.get("status"),
                    summary="phase marked in progress",
                    path=record.get("path"),
                )
            )
        for command in record.get("tests_run") or []:
            events.append(
                _activity_event(
                    timestamp=record.get("completed_at") or record.get("updated_at"),
                    event_type="phase-test-recorded",
                    phase_id=phase_id,
                    status=record.get("test_result_summary") or "recorded",
                    summary=f"test recorded: {command}",
                    path=record.get("path"),
                )
            )
        if record.get("commit_hash"):
            events.append(
                _activity_event(
                    timestamp=record.get("completed_at") or record.get("updated_at"),
                    event_type="phase-commit-recorded",
                    phase_id=phase_id,
                    status=record.get("status"),
                    summary=f"commit recorded: {record.get('commit_hash')}",
                    path=record.get("path"),
                )
            )
        if record.get("push_ref"):
            events.append(
                _activity_event(
                    timestamp=record.get("completed_at") or record.get("updated_at"),
                    event_type="phase-push-recorded",
                    phase_id=phase_id,
                    status=record.get("status"),
                    summary=f"push ref recorded: {record.get('push_ref')}",
                    path=record.get("path"),
                )
            )
        if record.get("completed_at"):
            events.append(
                _activity_event(
                    timestamp=record.get("completed_at"),
                    event_type="phase-completed",
                    phase_id=phase_id,
                    status=record.get("status"),
                    summary=str(record.get("implementation_summary") or "phase completion evidence recorded"),
                    path=record.get("path"),
                )
            )
        for handoff_item in record.get("phase_handoffs") or []:
            if isinstance(handoff_item, dict):
                events.append(
                    _activity_event(
                        timestamp=handoff_item.get("created_at"),
                        event_type="phase-handoff-drafted",
                        phase_id=phase_id,
                        local_id=handoff_item.get("handoff_id"),
                        status=(handoff_item.get("lint") or {}).get("status")
                        if isinstance(handoff_item.get("lint"), dict)
                        else None,
                        summary="phase handoff draft recorded",
                        path=handoff_item.get("path"),
                        suggested="brigade handoff lint",
                    )
                )
    for closeout in _read_closeouts(target):
        closeout_phase_ids = {str(item) for item in closeout.get("phase_ids") or []}
        if phase_ids and not closeout_phase_ids.intersection(phase_ids):
            continue
        events.append(
            _activity_event(
                timestamp=closeout.get("reviewed_at"),
                event_type="phase-closeout",
                local_id=closeout.get("closeout_id"),
                status=closeout.get("status"),
                summary=str(closeout.get("reason") or "phase closeout recorded"),
                path=closeout.get("path"),
            )
        )
    for action in _read_actions(target):
        if phase_ids and str(action.get("phase_id")) not in phase_ids:
            continue
        events.append(
            _activity_event(
                timestamp=action.get("updated_at") or action.get("created_at"),
                event_type="phase-action",
                phase_id=action.get("phase_id"),
                local_id=action.get("action_id"),
                status=action.get("status"),
                summary=str(action.get("safe_summary") or action.get("issue_type") or "phase action"),
                path=action.get("path"),
                suggested=action.get("suggested_next_command"),
            )
        )
    for report in _read_reports(target):
        if report.get("phase_range") != phase_range:
            continue
        events.append(
            _activity_event(
                timestamp=report.get("created_at"),
                event_type="phase-report",
                local_id=report.get("report_id"),
                status=(report.get("doctor") or {}).get("issue_count")
                if isinstance(report.get("doctor"), dict)
                else None,
                summary="phase report built",
                path=report.get("path"),
                suggested="brigade work phases report show latest",
            )
        )
        compare_summary = _report_compare_summary(target, report)
        if compare_summary:
            events.append(
                _activity_event(
                    timestamp=report.get("created_at"),
                    event_type="phase-report-compare",
                    local_id=report.get("report_id"),
                    status=compare_summary.get("issue_count"),
                    summary="phase report compare state available",
                    path=report.get("path"),
                    suggested=compare_summary.get("suggested_next_command"),
                )
            )
    for report in _read_session_reports(target):
        session_summary = report.get("session") if isinstance(report.get("session"), dict) else {}
        if session_summary.get("session_id") != session.get("session_id"):
            continue
        events.append(
            _activity_event(
                timestamp=report.get("created_at"),
                event_type="session-report",
                local_id=report.get("report_id"),
                status=(report.get("doctor") or {}).get("issue_count")
                if isinstance(report.get("doctor"), dict)
                else None,
                summary="phase session report built",
                path=report.get("path"),
                suggested="brigade work phases session report show latest",
            )
        )
    for item in _session_import_summaries(target):
        events.append(
            _activity_event(
                timestamp=item.get("created_at"),
                event_type="phase-import",
                local_id=item.get("import_id"),
                status=item.get("status"),
                summary=str(item.get("text") or "phase import"),
                suggested=f"brigade work import show {item.get('import_id')}",
            )
        )
    events = [event for event in events if event.get("timestamp")]
    events.sort(
        key=lambda item: (
            str(item.get("timestamp") or ""),
            str(item.get("event_type") or ""),
            str(item.get("event_id") or ""),
        )
    )
    next_payload = _session_next_payload(target, session)
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-activity"),
        "target": str(target),
        "session_id": session.get("session_id"),
        "phase_range": phase_range,
        "missing_phase_ids": missing,
        "events": events,
        "event_count": len(events),
        "suggested_next_command": next_payload["suggested_next_command"],
    }


def session_activity(*, target: Path, session_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _path, session, error = _resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        payload = _session_activity_payload(target, session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase session activity: {payload['session_id']}")
        print(f"events: {payload['event_count']}")
        for event in payload["events"][-20:]:
            print(
                f"- {event.get('timestamp')} {event.get('event_type')} {event.get('phase_id') or event.get('local_id')}: {event.get('safe_summary')}"
            )
    return 0


def _session_progress_payload(target: Path, session: dict[str, Any]) -> dict[str, Any]:
    phase_range = str(session.get("phase_range") or "")
    records, missing = _session_phase_records(target, phase_range)
    parsed = _parse_range(phase_range) if phase_range else None
    expected_total = (parsed[1] - parsed[0] + 1) if parsed else len(records)
    complete_records = [record for record in records if record.get("status") in DONE_STATUSES | {"deferred"}]
    percent_complete = round((len(complete_records) / expected_total) * 100, 1) if expected_total else 0.0
    status_counts = _status_counts(records)
    doctor_data = doctor_payload(target, phase_range=phase_range)
    blockers = [check for check in doctor_data["checks"] if check.get("status") != "ok"]
    next_payload = _session_next_payload(target, session)
    test_with = len([record for record in records if record.get("tests_run")])
    test_without = len(records) - test_with
    commit_count = len([record for record in records if record.get("commit_hash")])
    push_count = len([record for record in records if record.get("push_ref")])
    remaining_phase_count = max(expected_total - len(complete_records), 0)
    estimated_remaining_local_steps = remaining_phase_count + len(missing) + len(blockers)
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-progress"),
        "target": str(target),
        "session_id": session.get("session_id"),
        "phase_range": phase_range,
        "expected_phase_count": expected_total,
        "record_count": len(records),
        "missing_phase_ids": missing,
        "percent_complete": percent_complete,
        "status_counts": status_counts,
        "current_phase_id": next_payload["next_step"].get("phase_id"),
        "next_step": next_payload["next_step"],
        "suggested_next_command": next_payload["suggested_next_command"],
        "blockers": blockers,
        "blocker_count": len(blockers),
        "test_coverage": {
            "with_tests": test_with,
            "without_tests": test_without,
            "coverage_percent": round((test_with / len(records)) * 100, 1) if records else 0.0,
        },
        "commit_summary": {
            "with_commit": commit_count,
            "without_commit": len(records) - commit_count,
        },
        "push_summary": {
            "with_push_ref": push_count,
            "without_push_ref": len(records) - push_count,
        },
        "estimated_remaining_local_steps": estimated_remaining_local_steps,
    }


def session_progress(*, target: Path, session_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _path, session, error = _resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        payload = _session_progress_payload(target, session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase session progress: {payload['session_id']}")
        print(f"complete: {payload['percent_complete']}%")
        print(f"current: {payload['current_phase_id'] or 'none'}")
        print(f"blockers: {payload['blocker_count']}")
        print(f"next: {payload['suggested_next_command']}")
    return 0


def _session_blocker_fingerprint(*, session_id: object, phase_id: object, issue_type: object, detail: object) -> str:
    payload = {
        "session_id": session_id,
        "phase_id": phase_id,
        "issue_type": issue_type,
        "detail": detail,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _session_blocker_import_candidates(target: Path, session: dict[str, Any]) -> list[dict[str, Any]]:
    progress = _session_progress_payload(target, session)
    candidates: list[dict[str, Any]] = []
    session_id = session.get("session_id")
    for phase_id in progress.get("missing_phase_ids") or []:
        issue_type = "phase_session_missing_record"
        detail = f"{phase_id} is missing from the phase session range"
        fingerprint = _session_blocker_fingerprint(
            session_id=session_id, phase_id=phase_id, issue_type=issue_type, detail=detail
        )
        candidates.append(
            {
                "text": f"Resolve phase session blocker {issue_type} for {phase_id}: {detail}",
                "kind": "task",
                "source": "phase-session",
                "metadata": {
                    "session_id": session_id,
                    "phase_id": phase_id,
                    "issue_type": issue_type,
                    "safe_summary": detail,
                    "source_fingerprint": fingerprint,
                    "suggested_next_command": f"brigade work phases plan --phase-id {phase_id}",
                },
                "acceptance": [
                    f"Phase session `{session_id}` no longer reports `{issue_type}` for `{phase_id}`.",
                    "The phase ledger remains local and auditable.",
                ],
            }
        )
    for blocker in progress.get("blockers") or []:
        if not isinstance(blocker, dict):
            continue
        issue_type = str(blocker.get("name") or "phase_session_blocker")
        phase_id = blocker.get("phase_id") or progress.get("current_phase_id") or "session"
        detail = str(blocker.get("detail") or issue_type)
        fingerprint = _session_blocker_fingerprint(
            session_id=session_id, phase_id=phase_id, issue_type=issue_type, detail=detail
        )
        candidates.append(
            {
                "text": f"Resolve phase session blocker {issue_type} for {phase_id}: {detail}",
                "kind": "task",
                "source": "phase-session",
                "metadata": {
                    "session_id": session_id,
                    "phase_id": phase_id,
                    "issue_type": issue_type,
                    "safe_summary": detail,
                    "source_fingerprint": fingerprint,
                    "suggested_next_command": blocker.get("suggested_next_command")
                    or progress.get("suggested_next_command"),
                },
                "acceptance": [
                    f"Phase session `{session_id}` no longer reports `{issue_type}` for `{phase_id}`.",
                    "The fix is represented by local phase evidence, deferral, or reviewed closeout metadata.",
                ],
            }
        )
    return candidates


def session_import_issues(*, target: Path, session_id: str, dry_run: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _path, session, error = _resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        candidates = _session_blocker_import_candidates(target, session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    from . import work_cmd

    existing = work_cmd._read_imports(target)
    existing_keys = {
        (
            item.get("source"),
            (item.get("metadata") or {}).get("session_id") if isinstance(item.get("metadata"), dict) else None,
            (item.get("metadata") or {}).get("phase_id") if isinstance(item.get("metadata"), dict) else None,
            (item.get("metadata") or {}).get("issue_type") if isinstance(item.get("metadata"), dict) else None,
            (item.get("metadata") or {}).get("source_fingerprint") if isinstance(item.get("metadata"), dict) else None,
        ): item
        for item in existing
        if item.get("source") == "phase-session" and isinstance(item.get("metadata"), dict)
    }
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for candidate in candidates:
        metadata = candidate["metadata"]
        key = (
            "phase-session",
            metadata.get("session_id"),
            metadata.get("phase_id"),
            metadata.get("issue_type"),
            metadata.get("source_fingerprint"),
        )
        if key in existing_keys:
            skipped.append(
                {
                    "import_id": existing_keys[key].get("id"),
                    "status": existing_keys[key].get("status"),
                    "metadata": metadata,
                }
            )
            continue
        item = work_cmd._make_import(
            candidate["text"],
            kind=candidate["kind"],
            source=candidate["source"],
            metadata=metadata,
            task_type="task",
            priority="high",
            acceptance=candidate["acceptance"],
            template="bugfix",
        )
        created.append(item)
        if not dry_run:
            existing.append(item)
    if created and not dry_run:
        work_cmd._write_imports(target, existing)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-import-issues"),
        "target": str(target),
        "session_id": session.get("session_id"),
        "dry_run": dry_run,
        "created": created,
        "created_count": len(created),
        "skipped": skipped,
        "skipped_count": len(skipped),
        "candidate_count": len(candidates),
        "suggested_next_command": "brigade work inbox",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase session imports: {session.get('session_id')}")
        print(f"created: {len(created)}")
        print(f"skipped: {len(skipped)}")
    return 0


def session_checkpoint_archive(*, target: Path, checkpoint_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    checkpoint, error = _resolve_session_checkpoint(target, checkpoint_id)
    if checkpoint is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    checkpoint_path = Path(str(checkpoint.get("path") or ""))
    archive_record = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-checkpoint-archive"),
        "archived_at": _now().isoformat(),
        "checkpoint": checkpoint,
        "checkpoint_id": checkpoint.get("checkpoint_id"),
        "session_id": checkpoint.get("session_id"),
        "phase_id": checkpoint.get("phase_id"),
        "status": "archived",
        "source_fingerprint": checkpoint.get("source_fingerprint"),
    }
    _append_jsonl(_session_checkpoints_archive_path(target), archive_record)
    try:
        if checkpoint_path.is_file() and checkpoint_path.parent == _session_checkpoints_root(target):
            checkpoint_path.unlink()
    except OSError:
        pass
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-checkpoint-archive"),
        "target": str(target),
        "checkpoint_id": checkpoint.get("checkpoint_id"),
        "session_id": checkpoint.get("session_id"),
        "archived": True,
        "archive_path": str(_session_checkpoints_archive_path(target)),
        "suggested_next_command": "brigade work phases session checkpoints list",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase session checkpoint archived: {checkpoint.get('checkpoint_id')}")
        print(f"archive: {_session_checkpoints_archive_path(target)}")
    return 0


def _record_has_clean_privacy(record: dict[str, Any]) -> bool:
    checks = record.get("privacy_checks") if isinstance(record.get("privacy_checks"), list) else []
    return bool(checks and isinstance(checks[-1], dict) and checks[-1].get("status") == "clean")


def _record_has_linted_handoff(record: dict[str, Any]) -> bool:
    handoffs = record.get("phase_handoffs") if isinstance(record.get("phase_handoffs"), list) else []
    for item in reversed(handoffs):
        if not isinstance(item, dict):
            continue
        lint = item.get("lint") if isinstance(item.get("lint"), dict) else {}
        if lint.get("status") == "passed":
            return True
    deferred = " ".join(str(item).casefold() for item in record.get("deferred_items") or [])
    return "handoff" in deferred


def _latest_session_report_for_session(target: Path, session_id: object) -> dict[str, Any] | None:
    reports = []
    for report in _read_session_reports(target):
        session_summary = report.get("session") if isinstance(report.get("session"), dict) else {}
        if session_summary.get("session_id") == session_id:
            reports.append(report)
    return reports[-1] if reports else None


def _gate_check(
    status: str, name: str, detail: str, *, phase_id: object = None, suggested: str | None = None
) -> dict[str, Any]:
    return {
        "status": status,
        "name": name,
        "detail": detail,
        "phase_id": phase_id,
        "suggested_next_command": suggested or "brigade work phases session next latest",
    }


def _session_gate_payload(target: Path, session: dict[str, Any]) -> dict[str, Any]:
    phase_range = str(session.get("phase_range") or "")
    records, missing = _session_phase_records(target, phase_range)
    checks: list[dict[str, Any]] = []
    if missing:
        checks.append(
            _gate_check(
                "block",
                "phase_session_gate_missing_records",
                f"missing phase record(s): {', '.join(missing)}",
                suggested=f"brigade work phases plan --range {phase_range}",
            )
        )
    for record in records:
        phase_id = record.get("phase_id")
        status = str(record.get("status") or "pending")
        if status not in DONE_STATUSES | {"deferred"}:
            checks.append(
                _gate_check(
                    "block",
                    "phase_session_gate_phase_open",
                    f"{phase_id} status is {status}",
                    phase_id=phase_id,
                    suggested=f"brigade work phases show {phase_id}",
                )
            )
            continue
        if status == "deferred":
            continue
        if not record.get("tests_run"):
            checks.append(
                _gate_check(
                    "block",
                    "phase_session_gate_missing_tests",
                    f"{phase_id} has no recorded tests",
                    phase_id=phase_id,
                    suggested=f"brigade work phases verify plan {phase_id}",
                )
            )
        if not record.get("commit_hash"):
            checks.append(
                _gate_check(
                    "block",
                    "phase_session_gate_missing_commit",
                    f"{phase_id} has no commit hash",
                    phase_id=phase_id,
                    suggested=f"brigade work phases complete {phase_id} --commit <hash>",
                )
            )
        if not record.get("push_ref"):
            checks.append(
                _gate_check(
                    "block",
                    "phase_session_gate_missing_push_ref",
                    f"{phase_id} has no push ref",
                    phase_id=phase_id,
                    suggested=f"brigade work phases complete {phase_id} --push-ref <ref>",
                )
            )
        if not _record_has_clean_privacy(record):
            checks.append(
                _gate_check(
                    "block",
                    "phase_session_gate_missing_privacy_check",
                    f"{phase_id} has no clean privacy check",
                    phase_id=phase_id,
                    suggested=f"brigade work phases privacy {phase_id}",
                )
            )
        if not _record_has_linted_handoff(record):
            checks.append(
                _gate_check(
                    "block",
                    "phase_session_gate_missing_handoff",
                    f"{phase_id} has no linted handoff or handoff deferral",
                    phase_id=phase_id,
                    suggested=f"brigade work phases handoff {phase_id} --lint",
                )
            )
    phase_report = _latest_report_for_range(target, phase_range)
    if phase_report is None:
        checks.append(
            _gate_check(
                "block",
                "phase_session_gate_missing_phase_report",
                "no phase report exists for the session range",
                suggested=f"brigade work phases report build --range {phase_range}",
            )
        )
    else:
        compare = _report_compare_summary(target, phase_report)
        if compare and int(compare.get("issue_count") or 0) > 0:
            top = compare.get("top_issue") if isinstance(compare.get("top_issue"), dict) else {}
            checks.append(
                _gate_check(
                    "block",
                    "phase_session_gate_compare_not_clean",
                    str(top.get("name") or "phase report compare has issues"),
                    suggested=compare.get("suggested_next_command"),
                )
            )
    session_report = _latest_session_report_for_session(target, session.get("session_id"))
    if session_report is None:
        checks.append(
            _gate_check(
                "block",
                "phase_session_gate_missing_session_report",
                "no session report exists",
                suggested=f"brigade work phases session report build {session.get('session_id')}",
            )
        )
    closeout = session.get("closeout") if isinstance(session.get("closeout"), dict) else {}
    if closeout.get("status") != "reviewed":
        checks.append(
            _gate_check(
                "block",
                "phase_session_gate_missing_reviewed_closeout",
                "session closeout is not reviewed",
                suggested=f"brigade work phases session closeout {session.get('session_id')} --status reviewed",
            )
        )
    if not checks:
        checks.append(
            _gate_check(
                "ok",
                "phase_session_gate_ready",
                "phase session satisfies completion gate",
                suggested="brigade work phases session show latest",
            )
        )
    blockers = [check for check in checks if check.get("status") != "ok"]
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-gate"),
        "target": str(target),
        "session_id": session.get("session_id"),
        "phase_range": phase_range,
        "safe_to_claim_complete": not blockers,
        "checks": checks,
        "blocker_count": len(blockers),
        "top_blocker": blockers[0] if blockers else None,
        "phase_report": {"report_id": phase_report.get("report_id"), "path": phase_report.get("path")}
        if isinstance(phase_report, dict)
        else None,
        "session_report": {"report_id": session_report.get("report_id"), "path": session_report.get("path")}
        if isinstance(session_report, dict)
        else None,
        "suggested_next_command": blockers[0]["suggested_next_command"]
        if blockers
        else "brigade work phases session show latest",
    }


def session_gate(*, target: Path, session_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _path, session, error = _resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        payload = _session_gate_payload(target, session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase session gate: {payload['session_id']}")
        print(f"safe: {str(payload['safe_to_claim_complete']).lower()}")
        print(f"blockers: {payload['blocker_count']}")
        print(f"next: {payload['suggested_next_command']}")
        for check in payload["checks"]:
            print(f"[{check['status']}] {check['name']}: {check['detail']}")
    return 0


def _goal_scaffold_markdown(payload: dict[str, Any]) -> str:
    blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
    records = payload.get("records") if isinstance(payload.get("records"), list) else []
    lines = [
        f"/goal Brigade phases {payload['phase_range']}: continue AFK phase execution from ledger state",
        "",
        "Use docs/phase-execution-ledger.md and docs/roadmap-completion-plan.md as the source of truth.",
        "",
        "Primary objective:",
        "Continue the declared phase range without compression. Each phase needs its own ledger evidence, focused verification, commit evidence, and explicit deferral if it cannot be completed.",
        "",
        "Current ledger state:",
        f"- Phase range: {payload['phase_range']}",
        f"- Existing records: {payload['record_count']}",
        f"- Missing records: {', '.join(payload['missing_phase_ids']) if payload['missing_phase_ids'] else 'none'}",
        f"- Latest session: {payload.get('session_id') or 'none'}",
        f"- Suggested next command: `{payload['suggested_next_command']}`",
        "",
        "Phase status:",
    ]
    for record in records:
        lines.append(f"- `{record.get('phase_id')}` `{record.get('status')}`")
    lines.extend(["", "Unresolved blockers:"])
    if blockers:
        for blocker in blockers[:20]:
            lines.append(
                f"- `{blocker.get('name')}` `{blocker.get('phase_id') or 'session'}`: {_safe_handoff_text(blocker.get('detail'))}"
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "Execution rules:",
            "- Start each phase before editing for it.",
            "- Complete each phase only after implementation, focused tests, and commit evidence.",
            "- Do not mark pushed until push evidence exists.",
            "- Do not compress phases unless a grouped record already exists.",
            "- If a phase is too large, defer it with a concrete reason and move on.",
            "",
            "Safety boundaries:",
            "- No remote mutation except a final normal push when explicitly required by the phase goal.",
            "- No daemon, scheduler, automatic promotion, automatic memory edits, or arbitrary command execution.",
            "- Do not copy raw logs, private paths, raw scanner output, private evidence, tokens, hostnames, private repo names, owner names, or org names into public files.",
            "",
            "Acceptance:",
            "- Every phase in the range is implemented or explicitly deferred.",
            "- Tests and git diff checks pass.",
            "- Privacy scan passes.",
            "- Memory Handoff is written and linted or explicitly deferred.",
            "- Ledger records contain implementation, verification, commit, and push evidence where applicable.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def goal_scaffold(*, target: Path, phase_range: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    try:
        parsed = _parse_range(phase_range)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if parsed is None:
        print("error: --range is required", file=sys.stderr)
        return 2
    rendered_range = f"{parsed[0]}-{parsed[1]}"
    records, missing, _ = _selected_records(target, rendered_range)
    doctor_data = doctor_payload(target, phase_range=rendered_range)
    session = _latest_session_for_range(target, rendered_range)
    next_command = (
        f"brigade work phases session next {session.get('session_id')}"
        if session
        else f"brigade work phases next --range {rendered_range}"
    )
    goal_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-phase-goal-{uuid4().hex[:6]}"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-goal-scaffold"),
        "target": str(target),
        "goal_id": goal_id,
        "phase_range": rendered_range,
        "session_id": session.get("session_id") if isinstance(session, dict) else None,
        "records": [_record_summary(record) for record in records],
        "record_count": len(records),
        "missing_phase_ids": missing,
        "blockers": [check for check in doctor_data["checks"] if check.get("status") != "ok"],
        "blocker_count": doctor_data["issue_count"],
        "suggested_next_command": next_command,
        "source_fingerprint": _source_fingerprint(
            records, {"phase_range": rendered_range, "missing": missing, "issue_count": doctor_data["issue_count"]}
        ),
    }
    path = _goals_root(target) / f"{goal_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_goal_scaffold_markdown(payload))
    payload["path"] = str(path)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase goal scaffold: {goal_id}")
        print(f"range: {rendered_range}")
        print(f"path: {path}")
        print(f"blockers: {payload['blocker_count']}")
    return 0


def _find_action(target: Path, action_id: str) -> tuple[Path, dict[str, Any] | None]:
    wanted = _slug(action_id)
    exact = _actions_root(target) / f"{wanted}.json"
    if exact.is_file():
        return exact, _read_json(exact)
    matches = [path for path in _actions_root(target).glob("*.json") if path.stem.startswith(wanted)]
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
    wanted = _source_fingerprint([record])
    records_by_id = {str(item.get("phase_id")): item for item in _records(target)}
    for item in _read_closeouts(target):
        if phase_id not in (item.get("phase_ids") or []) or item.get("status") not in PHASE_CLOSEOUT_STATUSES:
            continue
        if item.get("source_fingerprint") == wanted:
            return True
        phase_fingerprints = item.get("phase_fingerprints") if isinstance(item.get("phase_fingerprints"), dict) else {}
        if phase_fingerprints.get(phase_id) == wanted:
            return True
        closeout_phase_ids = [str(value) for value in item.get("phase_ids") or []]
        closeout_records = [records_by_id[value] for value in closeout_phase_ids if value in records_by_id]
        if closeout_records and item.get("source_fingerprint") == _source_fingerprint(closeout_records):
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
        parsed_range = _parse_range(phase_range)
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
                "schema_version": SCHEMA_VERSION,
                "schema": _schema("phase-ledger-action"),
                "action_id": f"phase-action-{_slug(phase_id)}-{_slug(issue_type)}-{fingerprint[:8]}",
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
    for closeout_record in _read_closeouts(target):
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
                    "schema_version": SCHEMA_VERSION,
                    "schema": _schema("phase-ledger-action"),
                    "action_id": f"phase-action-{_slug(phase_id)}-{_slug(issue_type)}-{fingerprint[:8]}",
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
    latest_checkpoint, _checkpoint_error = _resolve_session_checkpoint(target, "latest")
    if isinstance(latest_checkpoint, dict):
        checkpoint_id = str(latest_checkpoint.get("checkpoint_id") or "latest")
        phase_id = str(latest_checkpoint.get("phase_id") or "ledger")
        if phase_in_scope(phase_id) and latest_checkpoint.get("status") == "blocked":
            issue_type = "phase_session_checkpoint_blocked"
            detail = str(latest_checkpoint.get("summary") or "phase session checkpoint is blocked")
            fingerprint = _action_source_fingerprint(phase_id, issue_type, detail)
            candidates.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "schema": _schema("phase-ledger-action"),
                    "action_id": f"phase-action-{_slug(phase_id)}-{_slug(issue_type)}-{fingerprint[:8]}",
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
            checkpoint_compare = _session_checkpoint_compare_payload(target, latest_checkpoint)
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
                        "schema_version": SCHEMA_VERSION,
                        "schema": _schema("phase-ledger-action"),
                        "action_id": f"phase-action-{_slug(phase_id)}-{_slug(issue_type)}-{fingerprint[:8]}",
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
    records = _records(target)
    checks: list[dict[str, Any]] = []
    try:
        parsed_range = _parse_range(phase_range)
    except ValueError as exc:
        parsed_range = None
        checks.append(_check("fail", "phase_range_invalid", str(exc)))
    if parsed_range is not None:
        start, end = parsed_range
        present = {str(record.get("phase_id")) for record in records}
        missing = [_phase_id_for(number) for number in range(start, end + 1) if _phase_id_for(number) not in present]
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
        if status in DONE_STATUSES:
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
            completed = _parse_time(record.get("completed_at"))
            if (
                completed
                and now - completed > timedelta(hours=STALE_UNREVIEWED_COMPLETED_HOURS)
                and not _phase_has_current_closeout(target, phase_id, record)
            ):
                checks.append(
                    _check(
                        "warn",
                        "phase_stale_unreviewed_completed",
                        f"completed phase has not been reviewed for more than {STALE_UNREVIEWED_COMPLETED_HOURS}h",
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
            started = _parse_time(record.get("started_at"))
            if started and now - started > timedelta(hours=STALE_IN_PROGRESS_HOURS):
                checks.append(
                    _check(
                        "warn",
                        "phase_stale_in_progress",
                        f"phase has been in progress for more than {STALE_IN_PROGRESS_HOURS}h",
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
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-doctor"),
        "target": str(target),
        "records_path": str(_records_root(target)),
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
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase ledger doctor: {payload['target']}")
        for check in payload["checks"]:
            print(f"[{check['status']}] {check['name']}: {check['detail']}")
    return 1 if any(check.get("status") == "fail" for check in payload["checks"]) else 0


def health(target: Path) -> dict[str, Any]:
    payload = doctor_payload(target)
    records = _records(target)
    open_records = [record for record in records if record.get("status") in {"pending", "in-progress", "blocked"}]
    target = target.expanduser().resolve()
    closeouts = _read_closeouts(target)
    latest_report = _latest_report(target)
    latest_report_compare = _report_compare_summary(target, latest_report)
    latest_session = _latest_session(target)
    latest_session_checkpoint = (
        _latest_checkpoint_for_session(target, latest_session.get("session_id"))
        if isinstance(latest_session, dict)
        else None
    )
    latest_session_checkpoint_compare = None
    if isinstance(latest_session_checkpoint, dict):
        try:
            latest_session_checkpoint_compare = _session_checkpoint_compare_payload(target, latest_session_checkpoint)
        except ValueError:
            latest_session_checkpoint_compare = None
    latest_session_report = _read_session_reports(target)[-1] if _read_session_reports(target) else None
    latest_session_gate = _session_gate_payload(target, latest_session) if isinstance(latest_session, dict) else None
    actions = _read_actions(target)
    open_actions = [action for action in actions if action.get("status") in {"pending", "active"}]
    action_counts: dict[str, int] = {}
    for action in actions:
        status = str(action.get("status") or "unknown")
        action_counts[status] = action_counts.get(status, 0) + 1
    return {
        "records_path": str(_records_root(target)),
        "record_count": len(records),
        "open_count": len(open_records),
        "latest": _record_summary(records[-1]) if records else None,
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
        "latest_session": _session_summary(latest_session) if isinstance(latest_session, dict) else None,
        "latest_session_checkpoint": _checkpoint_summary(latest_session_checkpoint)
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
        "actions_path": str(_actions_root(target)),
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
    if status not in PHASE_CLOSEOUT_STATUSES:
        print(f"error: --status must be one of {sorted(PHASE_CLOSEOUT_STATUSES)}", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    records, missing, parsed_range = _selected_records(target, selector)
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
    fingerprint = _source_fingerprint(records)
    closeout_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-phase-closeout-{uuid4().hex[:6]}"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-closeout"),
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
            str(record.get("phase_id")): _source_fingerprint([record]) for record in records if record.get("phase_id")
        },
        "source_fingerprint": fingerprint,
        "suggested_next_command": "brigade work phases doctor",
    }
    path = _closeouts_root(target) / f"{closeout_id}.json"
    payload["path"] = str(path)
    _write_json(path, payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase closeout: {closeout_id}")
        print(f"status: {status}")
        print(f"phases: {', '.join(phase_ids)}")
        print(f"unresolved: {len(selected_issues)}")
    return 0


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
    path, record = _find_record(target, phase_id)
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
        record["files_changed"] = _append_unique(record.get("files_changed", []), attachment["files_changed"])
    if attachment["tests_run"]:
        record["tests_run"] = _append_unique(record.get("tests_run", []), attachment["tests_run"])
    if test_result_summary:
        record["test_result_summary"] = test_result_summary
    record["updated_at"] = _now().isoformat()
    record["path"] = str(path)
    _write_json(path, record)
    if json_output:
        print(json.dumps(record, indent=2, sort_keys=True))
    else:
        print(f"phase evidence: {record.get('phase_id')}")
        print(f"attachments: {len(attachments)}")
    return 0


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
    records, missing, parsed_range = _selected_records(target, selector)
    if missing:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "schema": _schema("phase-ledger-verify-plan"),
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
            "schema_version": SCHEMA_VERSION,
            "schema": _schema("phase-ledger-verify-plan"),
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
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase verification plan: {selector}")
        print(f"records: {payload['record_count']}")
        for record in payload["records"]:
            print(f"- {record.get('phase_id')} verification={len(record.get('verification') or [])}")
    return 0


def verify_record(
    *, target: Path, phase_id: str, command: str, status: str, summary: str | None = None, json_output: bool = False
) -> int:
    if status not in PHASE_VERIFY_STATUSES - {"expected"}:
        print(f"error: --status must be one of {sorted(PHASE_VERIFY_STATUSES - {'expected'})}", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    path, record = _find_record(target, phase_id)
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
        record["tests_run"] = _append_unique(record.get("tests_run", []), [command])
    if summary:
        record["test_result_summary"] = summary
    record["updated_at"] = _now().isoformat()
    record["path"] = str(path)
    _write_json(path, record)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-verify-record"),
        "target": str(target),
        "phase_id": record.get("phase_id"),
        "recorded": entry,
        "verification": entries,
        "suggested_next_command": f"brigade work phases verify plan {record.get('phase_id')}",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase verification: {record.get('phase_id')}")
        print(f"status: {status}")
    return 0


def reconcile(*, target: Path, selector: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    records, missing, parsed_range = _selected_records(target, selector)
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
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-reconcile"),
        "target": str(target),
        "selector": selector,
        "phase_range": parsed_range,
        "records": [_record_summary(record) for record in records],
        "record_count": len(records),
        "dirty_paths": dirty_paths[:20],
        "checks": checks,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "suggested_next_command": issues[0]["suggested_next_command"] if issues else "brigade work phases status",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase reconcile: {selector}")
        print(f"records: {len(records)}")
        for check in checks:
            print(f"[{check['status']}] {check['name']}: {check['detail']}")
    return 0


def _privacy_findings_for_text(text: str, *, source: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for name, pattern in PRIVACY_PATTERNS.items():
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
    records, missing, parsed_range = _selected_records(target, selector)
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
        path, current = _find_record(target, str(record.get("phase_id")))
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
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-privacy"),
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
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase privacy: {selector}")
        print(f"status: {payload['status']}")
        print(f"findings: {len(findings)}")
        for finding in findings:
            print(f"[{finding['status']}] {finding['name']}: {finding['source']}")
    return 1 if findings else 0


def _handoff_root(target: Path) -> Path:
    return target / ".claude" / "memory-handoffs"


def _safe_handoff_text(value: object) -> str:
    rendered = str(value or "").strip()
    for pattern in PRIVACY_PATTERNS.values():
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
    records, missing, parsed_range = _selected_records(target, selector)
    if missing or not records:
        print(f"error: phase selector has missing records: {', '.join(missing or [selector])}", file=sys.stderr)
        return 1
    handoff_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-phase-handoff-{uuid4().hex[:6]}"
    path = _handoff_root(target) / f"{handoff_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_phase_handoff_content(records, selector=selector, handoff_id=handoff_id))
    lint_payload: dict[str, Any] = {"requested": lint, "status": "not-run", "errors": [], "warnings": []}
    if lint:
        from . import handoff_cmd

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
        "source_fingerprint": _source_fingerprint(records, {"handoff_id": handoff_id}),
    }
    for record in records:
        record_path, current = _find_record(target, str(record.get("phase_id")))
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
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-handoff"),
        "target": str(target),
        "selector": selector,
        "phase_range": parsed_range,
        "phase_ids": [record.get("phase_id") for record in records],
        "handoff_id": handoff_id,
        "path": str(path),
        "lint": lint_payload,
        "suggested_next_command": f"brigade handoff lint --target . {path}",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase handoff: {handoff_id}")
        print(f"path: {path}")
        print(f"lint: {lint_payload['status']}")
    return 1 if lint_payload["status"] == "failed" else 0


def compare(*, target: Path, selector: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    records, missing, parsed_range = _selected_records(target, selector)
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
    latest_report = _latest_report(target)
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
        completed = _parse_time(record.get("completed_at"))
        report_created = _parse_time(latest_report.get("created_at")) if latest_report else None
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
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-compare"),
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
        "records": [_record_summary(record) for record in records],
        "record_count": len(records),
        "checks": checks,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "suggested_next_command": issues[0]["suggested_next_command"] if issues else "brigade work phases doctor",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase compare: {selector}")
        print(f"records: {len(records)}")
        for check in checks:
            print(f"[{check['status']}] {check['name']}: {check['detail']}")
    return 0


def actions_plan(*, target: Path, phase_range: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    existing = {
        (item.get("phase_id"), item.get("issue_type"), item.get("source_fingerprint")): item
        for item in _read_actions(target)
        if item.get("status") != "archived"
    }
    planned: list[dict[str, Any]] = []
    for candidate in _phase_action_candidates(target, phase_range=phase_range):
        key = (candidate.get("phase_id"), candidate.get("issue_type"), candidate.get("source_fingerprint"))
        planned.append(_action_summary(existing.get(key, candidate)))
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-action-plan"),
        "target": str(target),
        "phase_range": phase_range,
        "actions": planned,
        "action_count": len(planned),
        "suggested_next_command": "brigade work phases actions build",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase actions planned: {len(planned)}")
        for action in planned:
            print(f"- {action.get('action_id')} [{action.get('status')}] {action.get('issue_type')}")
    return 0


def actions_build(*, target: Path, phase_range: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _actions_root(target).mkdir(parents=True, exist_ok=True)
    existing = {
        (item.get("phase_id"), item.get("issue_type"), item.get("source_fingerprint")): item
        for item in _read_actions(target)
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
        path = _actions_root(target) / f"{candidate['action_id']}.json"
        candidate["path"] = str(path)
        _write_json(path, candidate)
        created.append(_action_summary(candidate))
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-action-build"),
        "target": str(target),
        "phase_range": phase_range,
        "created": created,
        "skipped": skipped,
        "created_count": len(created),
        "skipped_count": len(skipped),
        "suggested_next_command": "brigade work phases actions list",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase actions created: {len(created)}")
        print(f"phase actions skipped: {len(skipped)}")
    return 0


def actions_list(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    actions = [_action_summary(action) for action in _read_actions(target) if action.get("status") != "archived"]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-action-list"),
        "target": str(target),
        "actions": actions,
        "action_count": len(actions),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase actions: {len(actions)}")
        for action in actions:
            print(f"- {action.get('action_id')} [{action.get('status')}] {action.get('issue_type')}")
    return 0


def actions_show(*, target: Path, action_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    path, action = _find_action(target, action_id)
    if action is None:
        print(f"error: phase action not found: {action_id}", file=sys.stderr)
        return 1
    action["path"] = str(path)
    if json_output:
        print(json.dumps(action, indent=2, sort_keys=True))
    else:
        print(f"phase action: {action.get('action_id')}")
        print(f"status: {action.get('status')}")
        print(f"issue: {action.get('issue_type')}")
        print(f"next: {action.get('suggested_next_command')}")
    return 0


def _set_action_status(
    target: Path, action_id: str, status: str, reason: str | None = None
) -> tuple[int, dict[str, Any] | None]:
    if status not in PHASE_ACTION_STATUSES:
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
    if json_output:
        print(json.dumps(action, indent=2, sort_keys=True))
    else:
        print(f"phase action: {action.get('action_id')}")
        print(f"status: {status}")
    return 0


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
        candidates = [action for action in _read_actions(target) if action.get("status") in {"done", "deferred"}]
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
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-action-archive"),
        "target": str(target),
        "archived": archived,
        "archived_count": len(archived),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase actions archived: {len(archived)}")
    return 0


def actions_import_issues(*, target: Path, dry_run: bool = False, json_output: bool = False) -> int:
    from . import work_cmd

    target = target.expanduser().resolve()
    records: list[dict[str, Any]] = []
    for action in _read_actions(target):
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
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-action-import-issues"),
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
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase action imports: {target}")
        print(f"created: {payload['created_count']}")
        print(f"skipped: {payload['skipped_count']}")
        print(f"dismissed: {payload['dismissed_count']}")
    return 0


def _report_payload(target: Path, *, phase_range: str | None = None) -> dict[str, Any]:
    target = target.expanduser().resolve()
    status_data = status_payload(target, phase_range=phase_range)
    doctor_data = doctor_payload(target, phase_range=phase_range)
    records = _records(target)
    if phase_range:
        parsed = _parse_range(phase_range)
        if parsed is not None:
            start, end = parsed
            wanted = {_phase_id_for(number) for number in range(start, end + 1)}
            records = [record for record in records if record.get("phase_id") in wanted]
    report_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-phase-report-{uuid4().hex[:6]}"
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-report"),
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
        "records": [_record_summary(record) for record in records],
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
    report_dir = _reports_root(target) / str(payload["report_id"])
    payload["path"] = str(report_dir)
    payload["bundle_files"] = ["PHASE_REPORT.md", "PHASE_EVIDENCE.json"]
    _write_json(report_dir / "PHASE_EVIDENCE.json", payload)
    _write_report_markdown(report_dir / "PHASE_REPORT.md", payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase report: {payload['report_id']}")
        print(f"path: {report_dir}")
        print(f"issues: {payload['doctor']['issue_count']}")
    return 0


def report_list(*, target: Path, limit: int = 20, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    reports: list[dict[str, Any]] = []
    for path in sorted(_reports_root(target).glob("*/PHASE_EVIDENCE.json"), reverse=True):
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
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-report-list"),
        "target": str(target),
        "reports": reports,
        "report_count": len(reports),
    }
    if json_output:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print(f"phase reports: {target}")
        for item in reports:
            print(
                f"- {item.get('report_id')} issues={item.get('issue_count')} range={item.get('phase_range') or 'all'}"
            )
    return 0


def report_show(*, target: Path, report_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload, error = _resolve_report(target, report_id)
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
    if status not in PHASE_REPORT_CLOSEOUT_STATUSES:
        print(f"error: --status must be one of {sorted(PHASE_REPORT_CLOSEOUT_STATUSES)}", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    report, error = _resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    report_path = Path(str(report.get("path") or ""))
    if not report_path.is_dir():
        print(f"error: phase report path is missing: {report.get('path')}", file=sys.stderr)
        return 1
    closeout = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-report-closeout"),
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
        "source_fingerprint": _source_fingerprint(
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
    if json_output:
        print(json.dumps(closeout, indent=2, sort_keys=True))
    else:
        print(f"phase report closeout: {report.get('report_id')}")
        print(f"status: {status}")
        print(f"reason: {closeout['reason']}")
    return 0


def report_compare(*, target: Path, report_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    report, error = _resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    report_path = Path(str(report.get("path") or ""))
    summary = _report_compare_summary(target, report) or {
        "checks": [],
        "issue_count": 0,
        "top_issue": None,
        "phase_range": None,
    }
    issues = [check for check in summary["checks"] if check["status"] != "ok"]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-report-compare"),
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
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase report compare: {report.get('report_id')}")
        print(f"issues: {len(issues)}")
        for check in summary["checks"]:
            print(f"[{check['status']}] {check['name']}: {check['detail']}")
    return 0 if not issues else 1


def import_issues(
    *, target: Path, phase_range: str | None = None, dry_run: bool = False, json_output: bool = False
) -> int:
    from . import work_cmd

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
                    "source_fingerprint": _slug(fingerprint),
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
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-import-issues"),
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
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase ledger imports: {target}")
        print(f"created: {payload['created_count']}")
        print(f"skipped: {payload['skipped_count']}")
        print(f"dismissed: {payload['dismissed_count']}")
    return 0
