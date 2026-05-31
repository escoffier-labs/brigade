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

SCHEMA_VERSION = 1
PHASE_STATUSES = {"pending", "in-progress", "implemented", "verified", "committed", "pushed", "deferred", "blocked"}
PHASE_CLOSEOUT_STATUSES = {"reviewed", "deferred", "blocked", "archived"}
PHASE_ACTION_STATUSES = {"pending", "active", "done", "deferred", "archived"}
PHASE_REPORT_CLOSEOUT_STATUSES = {"reviewed", "deferred", "superseded", "archived"}
PHASE_SESSION_CLOSEOUT_STATUSES = {"reviewed", "deferred", "blocked", "archived"}
DONE_STATUSES = {"implemented", "verified", "committed", "pushed"}
STALE_IN_PROGRESS_HOURS = 12
REPORT_STALE_HOURS = 24
STALE_UNREVIEWED_COMPLETED_HOURS = 24


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def _index_path(target: Path) -> Path:
    return _root(target) / "index.json"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


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
            _schema("phase-ledger-doctor"),
        ],
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
    payload = {"schema_version": SCHEMA_VERSION, "schema": _schema("phase-ledger-init"), "target": str(target), "path": str(_root(target)), "written": written}
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
            record = _default_record(group_id, title=title or f"Grouped phases {start}-{end}", source_goal=source_goal, kind="group")
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
            record = _default_record(item_id, title=(title or f"Phase {number}") if start == end else f"{title or 'Planned phase'} {number}", source_goal=source_goal)
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
    payload = {"schema_version": SCHEMA_VERSION, "schema": _schema("phase-ledger-list"), "target": str(target), "records": records, "record_count": len(records)}
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
    done_records = [record for record in range_records if record.get("status") in DONE_STATUSES or record.get("status") == "deferred"]
    next_record = next(
        (
            record
            for record in sorted(range_records, key=lambda item: (_safe_phase_number(item.get("phase_id")) or 999999, str(item.get("phase_id"))))
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
        "suggested_next_command": f"brigade work phases start {next_record.get('phase_id')}" if isinstance(next_record, dict) else "brigade work phases doctor",
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


def defer(*, target: Path, phase_id: str, reason: str, next_phase_recommendation: str | None = None, json_output: bool = False) -> int:
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
    return sorted(valid_records, key=lambda item: (_safe_phase_number(item.get("phase_id")) or -1, str(item.get("created_at") or "")))[-1]


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
        return [by_id[phase_id] for phase_id in expected if phase_id in by_id], [phase_id for phase_id in expected if phase_id not in by_id], f"{start}-{end}"
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
        checks.append(_check("warn", "phase_report_status_counts_changed", "current phase status counts differ from report", suggested="brigade work phases report build"))
    if int(current_doctor.get("issue_count") or 0) != int(report_doctor.get("issue_count") or 0):
        checks.append(_check("warn", "phase_report_doctor_issue_count_changed", f"{report_doctor.get('issue_count')} -> {current_doctor.get('issue_count')}", suggested="brigade work phases doctor"))
    current_head = _git_head(target)
    report_head = str(report.get("git_head") or "")
    if report_head and current_head and not _same_commit(report_head, current_head):
        checks.append(_check("warn", "phase_report_head_changed", f"current HEAD {current_head} differs from report HEAD {report_head}", suggested="brigade work phases report build"))
    report_path = Path(str(report.get("path") or ""))
    closeout = _read_json(report_path / "CLOSEOUT.json")
    if closeout is None:
        checks.append(_check("warn", "phase_report_missing_closeout", "phase report has no CLOSEOUT.json", suggested=f"brigade work phases report closeout {report.get('report_id')}"))
    elif closeout.get("status") in {"deferred", "superseded", "archived"}:
        checks.append(_check("warn", "phase_report_not_reviewed", f"phase report closeout status is {closeout.get('status')}", suggested=f"brigade work phases report closeout {report.get('report_id')} --status reviewed"))
    created = _parse_time(report.get("created_at"))
    latest_record_time = max(
        [
            parsed
            for parsed in (_parse_time(record.get("updated_at") or record.get("completed_at") or record.get("created_at")) for record in _records(target))
            if parsed is not None
        ],
        default=None,
    )
    if created and latest_record_time and latest_record_time > created:
        checks.append(_check("warn", "phase_report_newer_phase_record", "a phase record changed after this report was built", suggested="brigade work phases report build"))
    if not checks:
        checks.append(_check("ok", "phase_report_current", "phase report matches current ledger checks"))
    issues = [check for check in checks if check["status"] != "ok"]
    return {
        "report_id": report.get("report_id"),
        "phase_range": phase_range,
        "checks": checks,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "suggested_next_command": issues[0]["suggested_next_command"] if issues else "brigade work phases report show latest",
    }


def _resolve_report(target: Path, report_id: str) -> tuple[dict[str, Any] | None, str | None]:
    target = target.expanduser().resolve()
    if report_id == "latest":
        latest = _latest_report(target)
        return (latest, None) if latest else (None, "phase report not found: latest")
    candidates = sorted(_reports_root(target).glob(f"{report_id}*/PHASE_EVIDENCE.json"))
    if len(candidates) != 1:
        return None, f"phase report not found: {report_id}" if not candidates else f"phase report id is ambiguous: {report_id}"
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
        "closeout_status": (session.get("closeout") or {}).get("status") if isinstance(session.get("closeout"), dict) else None,
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


def _resolve_session(target: Path, session_id: str) -> tuple[Path | None, dict[str, Any] | None, str | None]:
    target = target.expanduser().resolve()
    if session_id == "latest":
        latest = _latest_session(target)
        return (Path(str(latest.get("path"))), latest, None) if latest else (None, None, "phase session not found: latest")
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
    latest_report_summary = {
        "report_id": latest_report.get("report_id"),
        "phase_range": latest_report.get("phase_range"),
        "path": latest_report.get("path"),
    } if latest_report else None
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


def session_closeout(*, target: Path, session_id: str, status: str = "reviewed", reason: str | None = None, json_output: bool = False) -> int:
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
        "source_fingerprint": _source_fingerprint(session.get("phase_records") if isinstance(session.get("phase_records"), list) else [], {"session_id": session.get("session_id"), "status": session.get("status")}),
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
                "suggested_next_command": record.get("next_phase_recommendation") or f"brigade work phases show {phase_id}",
            }
            break
        if status in DONE_STATUSES:
            if not record.get("tests_run"):
                step = {
                    "step_type": "unverified_phase",
                    "phase_id": phase_id,
                    "detail": f"{phase_id} has no recorded tests",
                    "suggested_next_command": f"brigade work phases complete {phase_id} --test \"<command>\"",
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
    if not missing and expected and all(by_id.get(phase_id, {}).get("status") in DONE_STATUSES | {"deferred"} for phase_id in expected):
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
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("phase-ledger-session-next"),
        "target": str(target),
        "session_id": session.get("session_id"),
        "phase_range": phase_range,
        "missing_phase_ids": missing,
        "next_step": step,
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
            }
        )
    return summaries


def _session_report_payload(target: Path, session: dict[str, Any]) -> dict[str, Any]:
    phase_range = str(session.get("phase_range") or "")
    records, missing = _session_phase_records(target, phase_range)
    doctor_data = doctor_payload(target, phase_range=phase_range)
    next_data = _session_next_payload(target, session)
    actions = [_action_summary(action) for action in _read_actions(target) if action.get("status") in {"pending", "active"}]
    latest_report = _latest_report(target)
    report_compare = _report_compare_summary(target, latest_report)
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
        "## Records",
        "",
    ]
    for record in payload.get("phase_records", []):
        lines.append(f"- `{record.get('phase_id')}` `{record.get('status')}` commit=`{record.get('commit_hash') or 'none'}` push=`{record.get('push_ref') or 'none'}`")
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
        return None, f"phase session report not found: {report_id}" if not candidates else f"phase session report id is ambiguous: {report_id}"
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
            "session_id": (report.get("session") or {}).get("session_id") if isinstance(report.get("session"), dict) else None,
            "created_at": report.get("created_at"),
            "phase_range": report.get("phase_range"),
            "issue_count": (report.get("doctor") or {}).get("issue_count") if isinstance(report.get("doctor"), dict) else None,
            "path": report.get("path"),
        }
        for report in reversed(_read_session_reports(target))
    ][:limit]
    payload = {"schema_version": SCHEMA_VERSION, "schema": _schema("phase-ledger-session-report-list"), "target": str(target), "reports": reports, "report_count": len(reports)}
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
        print(f"session: {(report.get('session') or {}).get('session_id') if isinstance(report.get('session'), dict) else 'none'}")
        print(f"issues: {(report.get('doctor') or {}).get('issue_count') if isinstance(report.get('doctor'), dict) else 0}")
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
        result = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=target, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _same_commit(expected: str, current: str) -> bool:
    if not expected or not current:
        return True
    return expected.startswith(current) or current.startswith(expected)


def _phase_has_current_closeout(target: Path, phase_id: str, record: dict[str, Any]) -> bool:
    wanted = _source_fingerprint([record])
    for item in _read_closeouts(target):
        if phase_id in (item.get("phase_ids") or []) and item.get("source_fingerprint") == wanted and item.get("status") in PHASE_CLOSEOUT_STATUSES:
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
    for check in doctor_data["checks"]:
        if check.get("status") == "ok":
            continue
        phase_id = str(check.get("phase_id") or "ledger")
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
        if closeout_record.get("status") not in {"blocked", "deferred"} and int(closeout_record.get("unresolved_issue_count") or 0) == 0:
            continue
        for issue in closeout_record.get("unresolved_issues") or []:
            if not isinstance(issue, dict):
                continue
            phase_id = str(issue.get("phase_id") or "ledger")
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
                    "suggested_next_command": issue.get("suggested_next_command") or "brigade work phases closeout latest",
                }
            )
    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for candidate in candidates:
        key = (str(candidate.get("phase_id")), str(candidate.get("issue_type")), str(candidate.get("source_fingerprint")))
        deduped.setdefault(key, candidate)
    return list(deduped.values())


def _check(status: str, name: str, detail: str, *, phase_id: str | None = None, suggested: str = "brigade work phases doctor") -> dict[str, Any]:
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
            checks.append(_check("warn", "phase_range_missing_records", f"{len(missing)} missing phase record(s): {', '.join(missing[:10])}", suggested=f"brigade work phases plan --range {start}-{end}"))
        else:
            checks.append(_check("ok", "phase_range_records", f"{start}-{end} present"))
    now = _now()
    for record in records:
        phase_id = str(record.get("phase_id") or "unknown")
        status = str(record.get("status") or "unknown")
        kind = str(record.get("kind") or "phase")
        if record.get("parse_error"):
            checks.append(_check("fail", "phase_record_parse_error", str(record.get("path") or phase_id), phase_id=phase_id))
            continue
        if status in DONE_STATUSES:
            if not record.get("tests_run"):
                checks.append(_check("warn", "phase_complete_without_tests", "phase is marked complete without tests run", phase_id=phase_id, suggested=f"brigade work phases show {phase_id}"))
            if not record.get("files_changed") and not record.get("deferred_items"):
                checks.append(_check("warn", "phase_complete_without_changes_or_deferral", "phase is complete without changed files or deferral evidence", phase_id=phase_id, suggested=f"brigade work phases show {phase_id}"))
            completed = _parse_time(record.get("completed_at"))
            if completed and now - completed > timedelta(hours=STALE_UNREVIEWED_COMPLETED_HOURS) and not _phase_has_current_closeout(target, phase_id, record):
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
            checks.append(_check("warn", "phase_committed_without_hash", "phase is committed without a commit hash", phase_id=phase_id, suggested=f"brigade work phases complete {phase_id} --commit <hash>"))
        if status == "pushed" and not str(record.get("push_ref") or "").strip():
            checks.append(_check("warn", "phase_pushed_without_ref", "phase is pushed without a push ref", phase_id=phase_id, suggested=f"brigade work phases complete {phase_id} --push-ref <ref>"))
        if status == "in-progress":
            started = _parse_time(record.get("started_at"))
            if started and now - started > timedelta(hours=STALE_IN_PROGRESS_HOURS):
                checks.append(_check("warn", "phase_stale_in_progress", f"phase has been in progress for more than {STALE_IN_PROGRESS_HOURS}h", phase_id=phase_id, suggested=f"brigade work phases show {phase_id}"))
        if status == "blocked" and not str(record.get("next_phase_recommendation") or "").strip():
            checks.append(_check("warn", "phase_blocked_without_next_step", "blocked phase is missing a next phase recommendation", phase_id=phase_id, suggested=f"brigade work phases show {phase_id}"))
        phase_range_value = str(record.get("phase_range") or "")
        if kind != "group" and re.fullmatch(r"\d+-\d+", phase_range_value) and not record.get("explicit_grouping"):
            checks.append(_check("warn", "phase_range_compressed_without_group", "phase range record lacks explicit grouping", phase_id=phase_id, suggested="brigade work phases plan --grouped"))
        if kind == "group" and not record.get("explicit_grouping"):
            checks.append(_check("warn", "phase_group_without_explicit_grouping", "group record is missing explicit grouping marker", phase_id=phase_id, suggested=f"brigade work phases show {phase_id}"))
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
        "suggested_next_command": issue_checks[0]["suggested_next_command"] if issue_checks else "brigade work phases list",
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
            "issue_count": (latest_report.get("doctor") or {}).get("issue_count") if isinstance(latest_report.get("doctor"), dict) else None,
        }
        if latest_report
        else None,
        "latest_report_compare": latest_report_compare,
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


def closeout(*, target: Path, selector: str, status: str = "reviewed", reason: str | None = None, json_output: bool = False) -> int:
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


def compare(*, target: Path, selector: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    records, missing, parsed_range = _selected_records(target, selector)
    checks: list[dict[str, Any]] = []
    if missing:
        checks.append(_check("warn", "phase_compare_missing_records", f"missing phase record(s): {', '.join(missing)}", suggested=f"brigade work phases plan --range {parsed_range or selector}"))
    current_head = _git_head(target)
    latest_report = _latest_report(target)
    doctor_data = doctor_payload(target, phase_range=parsed_range)
    current_issue_count = doctor_data["issue_count"]
    for record in records:
        phase_id = str(record.get("phase_id") or "unknown")
        commit_hash = str(record.get("commit_hash") or "")
        push_ref = str(record.get("push_ref") or "")
        if not commit_hash:
            checks.append(_check("warn", "phase_compare_missing_commit_hash", "phase record has no commit hash", phase_id=phase_id, suggested=f"brigade work phases complete {phase_id} --commit <hash>"))
        elif current_head and not _same_commit(commit_hash, current_head):
            checks.append(_check("warn", "phase_compare_changed_head", f"current HEAD {current_head} differs from phase commit {commit_hash}", phase_id=phase_id, suggested=f"brigade work phases show {phase_id}"))
        if record.get("status") == "pushed" and not push_ref:
            checks.append(_check("warn", "phase_compare_missing_push_ref", "pushed phase record has no push ref", phase_id=phase_id, suggested=f"brigade work phases complete {phase_id} --push-ref <ref>"))
        missing_files = [path for path in record.get("files_changed") or [] if path and not (target / str(path)).exists()]
        if missing_files:
            checks.append(_check("warn", "phase_compare_missing_referenced_files", f"missing referenced file(s): {', '.join(missing_files[:5])}", phase_id=phase_id, suggested=f"brigade work phases show {phase_id}"))
        completed = _parse_time(record.get("completed_at"))
        report_created = _parse_time(latest_report.get("created_at")) if latest_report else None
        if completed and report_created and report_created > completed:
            checks.append(_check("warn", "phase_compare_newer_phase_report", f"newer phase report exists: {latest_report.get('report_id')}", phase_id=phase_id, suggested="brigade work phases report show latest"))
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
            checks.append(_check("warn", "phase_compare_newer_test_evidence", "phase report is newer than stored test evidence", phase_id=phase_id, suggested="brigade work phases report show latest"))
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
    existing = {(item.get("phase_id"), item.get("issue_type"), item.get("source_fingerprint")): item for item in _read_actions(target) if item.get("status") != "archived"}
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
    existing = {(item.get("phase_id"), item.get("issue_type"), item.get("source_fingerprint")): item for item in _read_actions(target) if item.get("status") != "archived"}
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
    payload = {"schema_version": SCHEMA_VERSION, "schema": _schema("phase-ledger-action-list"), "target": str(target), "actions": actions, "action_count": len(actions)}
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


def _set_action_status(target: Path, action_id: str, status: str, reason: str | None = None) -> tuple[int, dict[str, Any] | None]:
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


def _actions_update_status(*, target: Path, action_id: str, status: str, reason: str | None = None, json_output: bool = False) -> int:
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
    return _actions_update_status(target=target, action_id=action_id, status="deferred", reason=reason, json_output=json_output)


def actions_archive(*, target: Path, action_id: str | None = None, completed: bool = False, json_output: bool = False) -> int:
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
    payload = {"schema_version": SCHEMA_VERSION, "schema": _schema("phase-ledger-action-archive"), "target": str(target), "archived": archived, "archived_count": len(archived)}
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
        source_fingerprint = str(action.get("source_fingerprint") or hashlib.sha256(json.dumps(action, sort_keys=True).encode("utf-8")).hexdigest()[:16])
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
                "issue_count": (payload.get("doctor") or {}).get("issue_count") if isinstance(payload.get("doctor"), dict) else None,
                "path": str(path.parent),
            }
        )
    reports = reports[:limit]
    out = {"schema_version": SCHEMA_VERSION, "schema": _schema("phase-ledger-report-list"), "target": str(target), "reports": reports, "report_count": len(reports)}
    if json_output:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print(f"phase reports: {target}")
        for item in reports:
            print(f"- {item.get('report_id')} issues={item.get('issue_count')} range={item.get('phase_range') or 'all'}")
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


def report_closeout(*, target: Path, report_id: str, status: str = "reviewed", reason: str | None = None, json_output: bool = False) -> int:
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
        "issue_count": (report.get("doctor") or {}).get("issue_count") if isinstance(report.get("doctor"), dict) else None,
        "record_count": report.get("record_count"),
        "source_fingerprint": _source_fingerprint(report.get("records") if isinstance(report.get("records"), list) else [], {"report_id": report.get("report_id"), "issue_count": (report.get("doctor") or {}).get("issue_count") if isinstance(report.get("doctor"), dict) else None}),
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
    summary = _report_compare_summary(target, report) or {"checks": [], "issue_count": 0, "top_issue": None, "phase_range": None}
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
        "suggested_next_command": issues[0]["suggested_next_command"] if issues else "brigade work phases report show latest",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"phase report compare: {report.get('report_id')}")
        print(f"issues: {len(issues)}")
        for check in summary["checks"]:
            print(f"[{check['status']}] {check['name']}: {check['detail']}")
    return 0 if not issues else 1


def import_issues(*, target: Path, phase_range: str | None = None, dry_run: bool = False, json_output: bool = False) -> int:
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
