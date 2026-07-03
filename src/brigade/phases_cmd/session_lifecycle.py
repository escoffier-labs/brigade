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


def _session_payload(target: Path, *, phase_range: str, source_goal: str | None = None) -> dict[str, Any]:
    from . import checks_actions

    records, missing = constants._session_phase_records(target, phase_range)
    status_data = constants.status_payload(target, phase_range=phase_range)
    doctor_data = checks_actions.doctor_payload(target, phase_range=phase_range)
    next_phase_record = status_data.get("next_phase")
    current_phase_id = next_phase_record.get("phase_id") if isinstance(next_phase_record, dict) else None
    latest_report = constants._latest_report(target)
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
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session"),
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
        "phase_records": [constants._record_summary(record) for record in records],
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
        constants._parse_range(phase_range)
        payload = _session_payload(target, phase_range=phase_range, source_goal=source_goal)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    path = constants._sessions_root(target) / f"{payload['session_id']}.json"
    payload["path"] = str(path)
    _write_json(path, payload)
    lines = []
    lines.append(f"phase session: {payload['session_id']}")
    lines.append(f"range: {payload['phase_range']}")
    lines.append(f"current: {payload.get('current_phase_id') or 'none'}")
    lines.append(f"next: {payload['next_recommended_command']}")
    return emit(payload, json_output, lines, 0)


def session_list(*, target: Path, limit: int = 20, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    sessions = [constants._session_summary(session) for session in reversed(constants._read_sessions(target))][:limit]
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-list"),
        "target": str(target),
        "sessions": sessions,
        "session_count": len(sessions),
    }
    lines = []
    lines.append(f"phase sessions: {len(sessions)}")
    for session in sessions:
        lines.append(f"- {session.get('session_id')} [{session.get('status')}] range={session.get('phase_range')}")
    return emit(payload, json_output, lines, 0)


def session_show(*, target: Path, session_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _path, session, error = constants._resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    lines = []
    lines.append(f"phase session: {session.get('session_id')}")
    lines.append(f"status: {session.get('status')}")
    lines.append(f"range: {session.get('phase_range')}")
    lines.append(f"current: {session.get('current_phase_id') or 'none'}")
    lines.append(f"next: {session.get('next_recommended_command') or 'none'}")
    return emit(session, json_output, lines, 0)


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
    from . import session_ops

    target = target.expanduser().resolve()
    path, session, error = constants._resolve_session(target, session_id)
    if session is None or path is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if status not in {"noted", "blocked", "recovered"}:
        print("error: --status must be one of ['blocked', 'noted', 'recovered']", file=sys.stderr)
        return 2
    try:
        next_payload = session_ops._session_next_payload(target, session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    selected_phase_id = phase_id or next_payload["next_step"].get("phase_id") or session.get("current_phase_id")
    created_at = _now().isoformat()
    checkpoint_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-session-checkpoint-{uuid4().hex[:6]}"
    checkpoint_path = constants._session_checkpoints_root(target) / f"{checkpoint_id}.json"
    record = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-checkpoint"),
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
        "source_fingerprint": constants._source_fingerprint(
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
    references.append(constants._checkpoint_summary(record))
    session["checkpoint_references"] = references[-50:]
    session["latest_checkpoint"] = constants._checkpoint_summary(record)
    session["current_phase_id"] = selected_phase_id
    session["next_recommended_command"] = next_payload["suggested_next_command"]
    session["updated_at"] = created_at
    session["path"] = str(path)
    _write_json(path, session)
    lines = []
    lines.append(f"phase session checkpoint: {checkpoint_id}")
    lines.append(f"session: {session.get('session_id')}")
    lines.append(f"phase: {selected_phase_id or 'none'}")
    lines.append(f"status: {status}")
    lines.append(f"next: {record['suggested_next_command']}")
    return emit(record, json_output, lines, 0)


def session_checkpoint_list(
    *, target: Path, session_id: str | None = None, limit: int = 20, json_output: bool = False
) -> int:
    target = target.expanduser().resolve()
    checkpoints = list(reversed(constants._read_session_checkpoints(target)))
    if session_id:
        _path, session, error = constants._resolve_session(target, session_id)
        if session is None:
            print(f"error: {error}", file=sys.stderr)
            return 1
        resolved_session_id = str(session.get("session_id") or "")
        checkpoints = [checkpoint for checkpoint in checkpoints if checkpoint.get("session_id") == resolved_session_id]
    else:
        resolved_session_id = None
    summaries = [constants._checkpoint_summary(checkpoint) for checkpoint in checkpoints[:limit]]
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-checkpoint-list"),
        "target": str(target),
        "session_id": resolved_session_id,
        "checkpoints": summaries,
        "checkpoint_count": len(summaries),
        "suggested_next_command": "brigade work phases session checkpoints show latest"
        if summaries
        else "brigade work phases session checkpoint latest",
    }
    lines = []
    lines.append(f"phase session checkpoints: {len(summaries)}")
    for checkpoint in summaries:
        lines.append(
            f"- {checkpoint.get('checkpoint_id')} [{checkpoint.get('status')}] phase={checkpoint.get('phase_id') or 'none'}"
        )
    return emit(payload, json_output, lines, 0)


def session_checkpoint_show(*, target: Path, checkpoint_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    checkpoint, error = constants._resolve_session_checkpoint(target, checkpoint_id)
    if checkpoint is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    lines = []
    lines.append(f"phase session checkpoint: {checkpoint.get('checkpoint_id')}")
    lines.append(f"session: {checkpoint.get('session_id')}")
    lines.append(f"phase: {checkpoint.get('phase_id') or 'none'}")
    lines.append(f"status: {checkpoint.get('status')}")
    lines.append(f"summary: {checkpoint.get('summary')}")
    lines.append(f"next: {checkpoint.get('suggested_next_command') or 'none'}")
    return emit(checkpoint, json_output, lines, 0)


def _session_checkpoint_compare_payload(target: Path, checkpoint: dict[str, Any]) -> dict[str, Any]:
    from . import checks_actions
    from . import session_ops

    session_id = str(checkpoint.get("session_id") or "")
    _path, session, error = constants._resolve_session(target, session_id)
    checks: list[dict[str, Any]] = []
    current_next = None
    if session is None:
        checks.append(
            checks_actions._check(
                "block",
                "phase_session_checkpoint_missing_session",
                str(error or "checkpoint session is missing"),
                suggested="brigade work phases session list",
            )
        )
    else:
        current_next = session_ops._session_next_payload(target, session)
        saved_step = checkpoint.get("next_step") if isinstance(checkpoint.get("next_step"), dict) else {}
        current_step = current_next.get("next_step") if isinstance(current_next.get("next_step"), dict) else {}
        if saved_step.get("step_type") != current_step.get("step_type"):
            checks.append(
                checks_actions._check(
                    "warn",
                    "phase_session_checkpoint_step_changed",
                    f"{saved_step.get('step_type')} -> {current_step.get('step_type')}",
                    phase_id=current_step.get("phase_id"),
                    suggested="brigade work phases session checkpoint latest",
                )
            )
        if saved_step.get("phase_id") != current_step.get("phase_id"):
            checks.append(
                checks_actions._check(
                    "warn",
                    "phase_session_checkpoint_phase_changed",
                    f"{saved_step.get('phase_id')} -> {current_step.get('phase_id')}",
                    phase_id=current_step.get("phase_id"),
                    suggested="brigade work phases session checkpoint latest",
                )
            )
        if checkpoint.get("suggested_next_command") != current_next.get("suggested_next_command"):
            checks.append(
                checks_actions._check(
                    "warn",
                    "phase_session_checkpoint_command_changed",
                    "saved suggested command differs from current session next command",
                    phase_id=current_step.get("phase_id"),
                    suggested="brigade work phases session next latest",
                )
            )
        current_fingerprint = constants._source_fingerprint(
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
                checks_actions._check(
                    "warn",
                    "phase_session_checkpoint_fingerprint_changed",
                    "checkpoint source fingerprint differs from current session state",
                    phase_id=current_step.get("phase_id"),
                    suggested="brigade work phases session checkpoint latest",
                )
            )
    if not checks:
        checks.append(
            checks_actions._check(
                "ok",
                "phase_session_checkpoint_current",
                "checkpoint matches current session next state",
                phase_id=checkpoint.get("phase_id"),
                suggested="brigade work phases session checkpoints show latest",
            )
        )
    issues = [check for check in checks if check["status"] != "ok"]
    return {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-checkpoint-compare"),
        "target": str(target),
        "checkpoint_id": checkpoint.get("checkpoint_id"),
        "session_id": checkpoint.get("session_id"),
        "phase_id": checkpoint.get("phase_id"),
        "checkpoint": constants._checkpoint_summary(checkpoint),
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
    checkpoint, error = constants._resolve_session_checkpoint(target, checkpoint_id)
    if checkpoint is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        payload = _session_checkpoint_compare_payload(target, checkpoint)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    lines = []
    lines.append(f"phase session checkpoint compare: {payload['checkpoint_id']}")
    lines.append(f"issues: {payload['issue_count']}")
    if payload.get("top_issue"):
        lines.append(f"top: {payload['top_issue']['name']}")
    lines.append(f"next: {payload['suggested_next_command']}")
    return emit(payload, json_output, lines, 0)


def _checkpoint_issue_import_records(
    target: Path, checkpoint: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    compare = _session_checkpoint_compare_payload(target, checkpoint)
    checkpoint_id = str(checkpoint.get("checkpoint_id") or "")
    session_id = str(checkpoint.get("session_id") or "")
    phase_id = str(checkpoint.get("phase_id") or "")
    records: list[dict[str, Any]] = []

    def append_record(*, issue_type: str, detail: str, suggested_command: object) -> None:
        fingerprint = constants._source_fingerprint(
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
    from .. import work_cmd

    target = target.expanduser().resolve()
    checkpoint, error = constants._resolve_session_checkpoint(target, checkpoint_id)
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
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-checkpoint-import-issues"),
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
    lines = []
    lines.append(f"phase session checkpoint imports: {payload['checkpoint_id']}")
    lines.append(f"created: {payload['created_count']}")
    lines.append(f"skipped: {payload['skipped_count']}")
    lines.append(f"dismissed: {payload['dismissed_count']}")
    return emit(payload, json_output, lines, 0)


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
    from . import session_ops

    target = target.expanduser().resolve()
    path, session, error = constants._resolve_session(target, session_id)
    if session is None or path is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        next_payload = session_ops._session_next_payload(target, session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    selected_phase_id = phase_id or next_payload["next_step"].get("phase_id") or session.get("current_phase_id")
    created_at = _now().isoformat()
    note_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-session-recovery-note-{uuid4().hex[:6]}"
    note_path = constants._session_recovery_notes_root(target) / f"{note_id}.json"
    safe_notes = [str(item) for item in (notes or []) if str(item)]
    safe_evidence = [str(item) for item in (evidence or []) if str(item)]
    record = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-recovery-note"),
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
        "source_fingerprint": constants._source_fingerprint(
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
    references.append(constants._recovery_note_summary(record))
    session["recovery_note_references"] = references[-50:]
    session["latest_recovery_note"] = constants._recovery_note_summary(record)
    session["updated_at"] = created_at
    session["path"] = str(path)
    _write_json(path, session)
    lines = []
    lines.append(f"phase session recovery note: {note_id}")
    lines.append(f"session: {session.get('session_id')}")
    lines.append(f"phase: {selected_phase_id or 'none'}")
    lines.append(f"summary: {record['summary']}")
    return emit(record, json_output, lines, 0)


def session_recovery_note_list(
    *, target: Path, session_id: str | None = None, limit: int = 20, json_output: bool = False
) -> int:
    target = target.expanduser().resolve()
    notes = list(reversed(constants._read_session_recovery_notes(target)))
    if session_id:
        _path, session, error = constants._resolve_session(target, session_id)
        if session is None:
            print(f"error: {error}", file=sys.stderr)
            return 1
        resolved_session_id = str(session.get("session_id") or "")
        notes = [note for note in notes if note.get("session_id") == resolved_session_id]
    else:
        resolved_session_id = None
    summaries = [constants._recovery_note_summary(note) for note in notes[:limit]]
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-recovery-note-list"),
        "target": str(target),
        "session_id": resolved_session_id,
        "notes": summaries,
        "note_count": len(summaries),
        "suggested_next_command": "brigade work phases session recovery-notes show latest"
        if summaries
        else "brigade work phases session recovery-note latest",
    }
    lines = []
    lines.append(f"phase session recovery notes: {len(summaries)}")
    for note in summaries:
        lines.append(f"- {note.get('note_id')} [{note.get('status')}] phase={note.get('phase_id') or 'none'}")
    return emit(payload, json_output, lines, 0)


def session_recovery_note_show(*, target: Path, note_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    note, error = constants._resolve_session_recovery_note(target, note_id)
    if note is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    lines = []
    lines.append(f"phase session recovery note: {note.get('note_id')}")
    lines.append(f"session: {note.get('session_id')}")
    lines.append(f"phase: {note.get('phase_id') or 'none'}")
    lines.append(f"status: {note.get('status')}")
    lines.append(f"summary: {note.get('summary')}")
    return emit(note, json_output, lines, 0)


def session_recovery_note_closeout(
    *, target: Path, note_id: str, status: str = "reviewed", reason: str | None = None, json_output: bool = False
) -> int:
    if status not in {"reviewed", "deferred", "blocked", "archived"}:
        print("error: --status must be one of ['archived', 'blocked', 'deferred', 'reviewed']", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    note, error = constants._resolve_session_recovery_note(target, note_id)
    if note is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    path = Path(
        str(note.get("path") or (constants._session_recovery_notes_root(target) / f"{note.get('note_id')}.json"))
    )
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
    session_path, session, _error = constants._resolve_session(target, session_id) if session_id else (None, None, None)
    if session is not None and session_path is not None:
        references = (
            session.get("recovery_note_references") if isinstance(session.get("recovery_note_references"), list) else []
        )
        updated_summary = constants._recovery_note_summary(note)
        session["recovery_note_references"] = [
            updated_summary if item.get("note_id") == note.get("note_id") else item
            for item in references
            if isinstance(item, dict)
        ]
        session["latest_recovery_note"] = updated_summary
        session["updated_at"] = closed_at
        _write_json(session_path, session)
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-recovery-note-closeout"),
        "target": str(target),
        "note": note,
        "closeout": closeout,
        "suggested_next_command": "brigade work phases session recovery-notes list",
    }
    lines = []
    lines.append(f"phase session recovery note closeout: {note.get('note_id')}")
    lines.append(f"status: {status}")
    lines.append(f"reason: {closeout['reason']}")
    return emit(payload, json_output, lines, 0)


def _session_risk_payload(target: Path, session: dict[str, Any]) -> dict[str, Any]:
    from . import checks_actions
    from . import session_ops

    next_payload = session_ops._session_next_payload(target, session)
    doctor = checks_actions.doctor_payload(target, phase_range=str(session.get("phase_range") or ""))
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
            checks_actions._check(
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
            checks_actions._check(
                status,
                "phase_session_checkpoint_risk",
                str(top_issue.get("detail") or "checkpoint issues are open"),
                phase_id=top_issue.get("phase_id"),
                suggested=checkpoint.get("suggested_next_command"),
            )
        )
    open_notes = [
        note
        for note in constants._read_session_recovery_notes(target)
        if note.get("session_id") == session.get("session_id") and note.get("status") in {"open", "blocked"}
    ]
    if open_notes:
        top_note = open_notes[-1]
        risks.append(
            checks_actions._check(
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
            checks_actions._check(
                "warn",
                "phase_session_doctor_issues",
                str(top.get("detail") or "phase ledger doctor has issue(s)"),
                phase_id=top.get("phase_id"),
                suggested=top.get("suggested_next_command") or "brigade work phases doctor",
            )
        )
    if not risks:
        risks.append(
            checks_actions._check(
                "ok",
                "phase_session_risk",
                "no phase session risks detected",
                suggested="brigade work phases session next latest",
            )
        )
    issues = [risk for risk in risks if risk["status"] != "ok"]
    risk_level = "high" if any(risk["status"] == "block" for risk in issues) else "medium" if issues else "low"
    return {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-risk"),
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
    _path, session, error = constants._resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        payload = _session_risk_payload(target, session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    lines = []
    lines.append(f"phase session risk: {payload['session_id']}")
    lines.append(f"risk: {payload['risk_level']}")
    lines.append(f"issues: {payload['risk_count']}")
    if payload.get("top_risk"):
        lines.append(f"top: {payload['top_risk']['name']}")
    lines.append(f"next: {payload['suggested_next_command']}")
    return emit(payload, json_output, lines, 0)


def _session_verification_payload(target: Path, session: dict[str, Any]) -> dict[str, Any]:
    from . import checks_actions

    records, missing = constants._session_phase_records(target, str(session.get("phase_range") or ""))
    status_counts: dict[str, int] = {status: 0 for status in sorted(constants.PHASE_VERIFY_STATUSES)}
    phase_rows: list[dict[str, Any]] = []
    missing_verification: list[str] = []
    failed_verification: list[str] = []
    for record in records:
        entries = checks_actions._verification_entries(record)
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
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-verification"),
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
    _path, session, error = constants._resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    payload = _session_verification_payload(target, session)
    lines = []
    lines.append(f"phase session verification: {payload['session_id']}")
    lines.append(f"records: {payload['record_count']}")
    lines.append(f"issues: {payload['issue_count']}")
    lines.append(f"passed: {payload['status_counts'].get('passed', 0)}")
    lines.append(f"next: {payload['suggested_next_command']}")
    return emit(payload, json_output, lines, 0)


def _latest_privacy_check(record: dict[str, Any]) -> dict[str, Any] | None:
    checks = record.get("privacy_checks") if isinstance(record.get("privacy_checks"), list) else []
    for check in reversed(checks):
        if isinstance(check, dict):
            return check
    return None


def _session_privacy_payload(target: Path, session: dict[str, Any]) -> dict[str, Any]:
    records, missing = constants._session_phase_records(target, str(session.get("phase_range") or ""))
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
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-privacy"),
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
    _path, session, error = constants._resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    payload = _session_privacy_payload(target, session)
    lines = []
    lines.append(f"phase session privacy: {payload['session_id']}")
    lines.append(f"records: {payload['record_count']}")
    lines.append(f"issues: {payload['issue_count']}")
    lines.append(f"clean: {payload['status_counts'].get('clean', 0)}")
    lines.append(f"next: {payload['suggested_next_command']}")
    return emit(payload, json_output, lines, 0)


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
    records, missing = constants._session_phase_records(target, str(session.get("phase_range") or ""))
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
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-handoffs"),
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
    _path, session, error = constants._resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    payload = _session_handoffs_payload(target, session)
    lines = []
    lines.append(f"phase session handoffs: {payload['session_id']}")
    lines.append(f"records: {payload['record_count']}")
    lines.append(f"issues: {payload['issue_count']}")
    lines.append(f"linted: {payload['status_counts'].get('linted', 0)}")
    lines.append(f"next: {payload['suggested_next_command']}")
    return emit(payload, json_output, lines, 0)


def _checkpoint_state_for_session_next(
    target: Path, session: dict[str, Any], step: dict[str, Any]
) -> dict[str, Any] | None:
    from . import checks_actions

    checkpoint = constants._latest_checkpoint_for_session(target, session.get("session_id"))
    if checkpoint is None:
        return None
    checks: list[dict[str, Any]] = []
    saved_step = checkpoint.get("next_step") if isinstance(checkpoint.get("next_step"), dict) else {}
    if checkpoint.get("status") == "blocked":
        checks.append(
            checks_actions._check(
                "block",
                "phase_session_checkpoint_blocked",
                str(checkpoint.get("summary") or "checkpoint is marked blocked"),
                phase_id=checkpoint.get("phase_id"),
                suggested=checkpoint.get("suggested_next_command"),
            )
        )
    if saved_step.get("step_type") != step.get("step_type"):
        checks.append(
            checks_actions._check(
                "warn",
                "phase_session_checkpoint_step_changed",
                f"{saved_step.get('step_type')} -> {step.get('step_type')}",
                phase_id=step.get("phase_id"),
                suggested="brigade work phases session checkpoint latest",
            )
        )
    if saved_step.get("phase_id") != step.get("phase_id"):
        checks.append(
            checks_actions._check(
                "warn",
                "phase_session_checkpoint_phase_changed",
                f"{saved_step.get('phase_id')} -> {step.get('phase_id')}",
                phase_id=step.get("phase_id"),
                suggested="brigade work phases session checkpoint latest",
            )
        )
    if checkpoint.get("suggested_next_command") != step.get("suggested_next_command"):
        checks.append(
            checks_actions._check(
                "warn",
                "phase_session_checkpoint_command_changed",
                "saved suggested command differs from current session next command",
                phase_id=step.get("phase_id"),
                suggested="brigade work phases session next latest",
            )
        )
    current_fingerprint = constants._source_fingerprint(
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
            checks_actions._check(
                "warn",
                "phase_session_checkpoint_fingerprint_changed",
                "checkpoint source fingerprint differs from current session state",
                phase_id=step.get("phase_id"),
                suggested="brigade work phases session checkpoint latest",
            )
        )
    issues = [check for check in checks if check["status"] != "ok"]
    return {
        "latest_checkpoint": constants._checkpoint_summary(checkpoint),
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "suggested_next_command": "brigade work phases session checkpoints import-issues latest"
        if issues
        else "brigade work phases session checkpoints show latest",
    }


def session_closeout(
    *, target: Path, session_id: str, status: str = "reviewed", reason: str | None = None, json_output: bool = False
) -> int:
    from . import checks_actions

    if status not in constants.PHASE_SESSION_CLOSEOUT_STATUSES:
        print(f"error: --status must be one of {sorted(constants.PHASE_SESSION_CLOSEOUT_STATUSES)}", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    path, session, error = constants._resolve_session(target, session_id)
    if session is None or path is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    phase_range = str(session.get("phase_range") or "")
    doctor_data = checks_actions.doctor_payload(target, phase_range=phase_range)
    closeout_payload = {
        "status": status,
        "reason": reason or f"phase session marked {status}",
        "reviewed_at": _now().isoformat(),
        "unresolved_issue_count": doctor_data["issue_count"],
        "source_fingerprint": constants._source_fingerprint(
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
    lines = []
    lines.append(f"phase session closeout: {session.get('session_id')}")
    lines.append(f"status: {status}")
    lines.append(f"unresolved: {doctor_data['issue_count']}")
    return emit(session, json_output, lines, 0)


__all__ = (
    "_checkpoint_issue_import_records",
    "_checkpoint_state_for_session_next",
    "_latest_phase_handoff",
    "_latest_privacy_check",
    "_record_has_handoff_deferral",
    "_session_checkpoint_compare_payload",
    "_session_handoffs_payload",
    "_session_payload",
    "_session_privacy_payload",
    "_session_risk_payload",
    "_session_verification_payload",
    "session_checkpoint",
    "session_checkpoint_compare",
    "session_checkpoint_import_issues",
    "session_checkpoint_list",
    "session_checkpoint_show",
    "session_closeout",
    "session_handoffs",
    "session_list",
    "session_privacy",
    "session_recovery_note",
    "session_recovery_note_closeout",
    "session_recovery_note_list",
    "session_recovery_note_show",
    "session_risk",
    "session_show",
    "session_start",
    "session_verification",
)
