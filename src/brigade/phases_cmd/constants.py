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
    lines = []
    lines.append(f"phase ledger schema: {target}")
    lines.append("no_silent_compression: true")
    for item in payload["schemas"]:
        lines.append(f"- {item['name']}")
    return emit(payload, json_output, lines, 0)


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
    lines = []
    lines.append(f"phase ledger: {_root(target)}")
    lines.append(f"written: {str(written).lower()}")
    return emit(payload, json_output, lines, 0)


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
    lines = []
    lines.append(f"planned: {len(created)}")
    lines.append(f"skipped: {len(skipped)}")
    for item in created:
        lines.append(f"- {item['phase_id']} [{item['status']}] {item['title']}")
    return emit(payload, json_output, lines, 0)


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
    lines = []
    lines.append(f"phase ledger: {target}")
    for record in records:
        lines.append(f"- {record.get('phase_id')} [{record.get('status')}] {record.get('title')}")
    return emit(payload, json_output, lines, 0)


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
        lines = []
        lines.append("next phase: none")
        return emit({**payload, "found": False}, json_output, lines, 1)
    out = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-next"),
        "target": payload["target"],
        "found": True,
        "phase": next_record,
        "suggested_next_command": payload["suggested_next_command"],
    }
    lines = []
    lines.append(f"next phase: {next_record.get('phase_id')}")
    lines.append(f"status: {next_record.get('status')}")
    lines.append(f"next: {out['suggested_next_command']}")
    return emit(out, json_output, lines, 0)


def show(*, target: Path, phase_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    path, record = _find_record(target, phase_id)
    if record is None:
        print(f"error: phase record not found: {phase_id}", file=sys.stderr)
        return 1
    record["path"] = str(path)
    lines = []
    lines.append(f"phase: {record.get('phase_id')}")
    lines.append(f"status: {record.get('status')}")
    lines.append(f"title: {record.get('title')}")
    lines.append(f"summary: {record.get('implementation_summary') or 'none'}")
    lines.append(f"next: {record.get('next_phase_recommendation') or 'none'}")
    return emit(record, json_output, lines, 0)


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
    lines = []
    lines.append(f"phase: {record.get('phase_id')}")
    lines.append("status: in-progress")
    return emit(record, json_output, lines, 0)


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
    lines = []
    lines.append(f"phase: {record.get('phase_id')}")
    lines.append(f"status: {status}")
    lines.append(f"tests: {len(record.get('tests_run') or [])}")
    return emit(record, json_output, lines, 0)


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
    lines = []
    lines.append(f"phase: {record.get('phase_id')}")
    lines.append("status: deferred")
    lines.append(f"reason: {reason}")
    return emit(record, json_output, lines, 0)


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
    from . import checks_actions

    if not isinstance(report, dict):
        return None
    phase_range = report.get("phase_range") if isinstance(report.get("phase_range"), str) else None
    current_status = status_payload(target, phase_range=phase_range)
    current_doctor = checks_actions.doctor_payload(target, phase_range=phase_range)
    checks: list[dict[str, Any]] = []
    report_status = report.get("status") if isinstance(report.get("status"), dict) else {}
    report_doctor = report.get("doctor") if isinstance(report.get("doctor"), dict) else {}
    if current_status.get("status_counts") != report_status.get("status_counts"):
        checks.append(
            checks_actions._check(
                "warn",
                "phase_report_status_counts_changed",
                "current phase status counts differ from report",
                suggested="brigade work phases report build",
            )
        )
    if int(current_doctor.get("issue_count") or 0) != int(report_doctor.get("issue_count") or 0):
        checks.append(
            checks_actions._check(
                "warn",
                "phase_report_doctor_issue_count_changed",
                f"{report_doctor.get('issue_count')} -> {current_doctor.get('issue_count')}",
                suggested="brigade work phases doctor",
            )
        )
    current_head = checks_actions._git_head(target)
    report_head = str(report.get("git_head") or "")
    if report_head and current_head and not checks_actions._same_commit(report_head, current_head):
        checks.append(
            checks_actions._check(
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
            checks_actions._check(
                "warn",
                "phase_report_missing_closeout",
                "phase report has no CLOSEOUT.json",
                suggested=f"brigade work phases report closeout {report.get('report_id')}",
            )
        )
    elif closeout.get("status") in {"deferred", "superseded", "archived"}:
        checks.append(
            checks_actions._check(
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
            checks_actions._check(
                "warn",
                "phase_report_newer_phase_record",
                "a phase record changed after this report was built",
                suggested="brigade work phases report build",
            )
        )
    if not checks:
        checks.append(checks_actions._check("ok", "phase_report_current", "phase report matches current ledger checks"))
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


__all__ = (
    "DONE_STATUSES",
    "PHASE_ACTION_STATUSES",
    "PHASE_CLOSEOUT_STATUSES",
    "PHASE_REPORT_CLOSEOUT_STATUSES",
    "PHASE_SESSION_CLOSEOUT_STATUSES",
    "PHASE_STATUSES",
    "PHASE_VERIFY_STATUSES",
    "PRIVACY_PATTERNS",
    "REPORT_STALE_HOURS",
    "SCHEMA_VERSION",
    "STALE_IN_PROGRESS_HOURS",
    "STALE_UNREVIEWED_COMPLETED_HOURS",
    "_actions_root",
    "_append_jsonl",
    "_append_unique",
    "_checkpoint_summary",
    "_closeouts_root",
    "_contract_schema",
    "_default_record",
    "_find_record",
    "_goals_root",
    "_index_path",
    "_latest_checkpoint_for_session",
    "_latest_record",
    "_latest_report",
    "_latest_report_for_range",
    "_latest_session",
    "_latest_session_for_range",
    "_parse_range",
    "_parse_time",
    "_phase_id_for",
    "_read_actions",
    "_read_closeouts",
    "_read_reports",
    "_read_session_checkpoints",
    "_read_session_recovery_notes",
    "_read_session_reports",
    "_read_sessions",
    "_record_path",
    "_record_summary",
    "_records",
    "_records_root",
    "_recovery_note_summary",
    "_report_compare_summary",
    "_reports_root",
    "_resolve_report",
    "_resolve_session",
    "_resolve_session_checkpoint",
    "_resolve_session_recovery_note",
    "_root",
    "_safe_phase_number",
    "_schema",
    "_selected_records",
    "_session_checkpoints_archive_path",
    "_session_checkpoints_root",
    "_session_phase_records",
    "_session_recovery_notes_root",
    "_session_reports_root",
    "_session_summary",
    "_sessions_root",
    "_slug",
    "_source_fingerprint",
    "_status_counts",
    "complete",
    "defer",
    "init",
    "list_phases",
    "next_phase",
    "plan",
    "schema",
    "show",
    "start",
    "status",
    "status_payload",
)
