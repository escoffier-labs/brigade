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


def _session_next_payload(target: Path, session: dict[str, Any]) -> dict[str, Any]:
    from . import checks_actions

    phase_range = str(session.get("phase_range") or "")
    records, missing = constants._session_phase_records(target, phase_range)
    by_id = {str(record.get("phase_id")): record for record in records}
    parsed = constants._parse_range(phase_range)
    expected = [constants._phase_id_for(number) for number in range(parsed[0], parsed[1] + 1)] if parsed else []
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
            started = constants._parse_time(record.get("started_at"))
            stale = bool(started and _now() - started > timedelta(hours=constants.STALE_IN_PROGRESS_HOURS))
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
        if status in constants.DONE_STATUSES:
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
            if status == "pushed" and not checks_actions._phase_has_current_closeout(target, phase_id, record):
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
        and all(
            by_id.get(phase_id, {}).get("status") in constants.DONE_STATUSES | {"deferred"} for phase_id in expected
        )
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
    checkpoint_state = session_lifecycle._checkpoint_state_for_session_next(target, session, step)
    return {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-next"),
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
    _path, session, error = constants._resolve_session(target, session_id)
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
    from . import checks_actions

    next_payload = _session_next_payload(target, session)
    risk_payload = session_lifecycle._session_risk_payload(target, session)
    progress_payload = _session_progress_payload(target, session)
    verification_payload = session_lifecycle._session_verification_payload(target, session)
    privacy_payload = session_lifecycle._session_privacy_payload(target, session)
    handoffs_payload = session_lifecycle._session_handoffs_payload(target, session)
    gate_payload = _session_gate_payload(target, session)
    checkpoint = next_payload.get("checkpoint") if isinstance(next_payload.get("checkpoint"), dict) else None
    resume_blockers: list[dict[str, Any]] = []
    next_command = str(next_payload.get("suggested_next_command") or "")
    if checkpoint and int(checkpoint.get("issue_count") or 0) > 0:
        top_issue = checkpoint.get("top_issue") if isinstance(checkpoint.get("top_issue"), dict) else {}
        resume_blockers.append(
            checks_actions._check(
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
            checks_actions._check(
                "block",
                "phase_session_protocol_high_risk",
                "session risk is high",
                suggested=str(risk_payload.get("suggested_next_command") or "brigade work phases session risk latest"),
            )
        )
    if next_command and not next_command.startswith("brigade work phases "):
        resume_blockers.append(
            checks_actions._check(
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
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-protocol"),
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
    _path, session, error = constants._resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        payload = _session_protocol_payload(target, session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    lines = []
    lines.append(f"phase session protocol: {payload['session_id']}")
    lines.append(f"safe_resume: {str(payload['safe_resume']).lower()}")
    lines.append(f"blockers: {payload['resume_blocker_count']}")
    for step in payload["wrapper_steps"]:
        lines.append(f"- {step['step']}: {step['command']}")
    return emit(payload, json_output, lines, 0)


def _session_audit_payload(target: Path, session: dict[str, Any]) -> dict[str, Any]:
    protocol = _session_protocol_payload(target, session)
    progress = _session_progress_payload(target, session)
    risk = session_lifecycle._session_risk_payload(target, session)
    verification = session_lifecycle._session_verification_payload(target, session)
    privacy = session_lifecycle._session_privacy_payload(target, session)
    handoffs = session_lifecycle._session_handoffs_payload(target, session)
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
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-audit"),
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
    _path, session, error = constants._resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        payload = _session_audit_payload(target, session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    lines = []
    lines.append(f"phase session audit: {payload['session_id']}")
    lines.append(f"ready_for_resume: {str(payload['ready_for_resume']).lower()}")
    lines.append(f"ready_for_completion_claim: {str(payload['ready_for_completion_claim']).lower()}")
    lines.append(f"issues: {payload['issue_count']}")
    lines.append(f"next: {payload['suggested_next_command']}")
    return emit(payload, json_output, lines, 0)


def session_resume(*, target: Path, session_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    path, session, error = constants._resolve_session(target, session_id)
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
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-resume"),
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
        from .. import work_cmd

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
    from . import checks_actions

    phase_range = str(session.get("phase_range") or "")
    records, missing = constants._session_phase_records(target, phase_range)
    doctor_data = checks_actions.doctor_payload(target, phase_range=phase_range)
    next_data = _session_next_payload(target, session)
    actions = [
        checks_actions._action_summary(action)
        for action in constants._read_actions(target)
        if action.get("status") in {"pending", "active"}
    ]
    latest_report = constants._latest_report(target)
    report_compare = constants._report_compare_summary(target, latest_report)
    latest_checkpoint = constants._latest_checkpoint_for_session(target, session.get("session_id"))
    checkpoint_compare = None
    if isinstance(latest_checkpoint, dict):
        try:
            checkpoint_compare = session_lifecycle._session_checkpoint_compare_payload(target, latest_checkpoint)
        except ValueError:
            checkpoint_compare = None
    recovery_notes = [
        constants._recovery_note_summary(note)
        for note in constants._read_session_recovery_notes(target)
        if note.get("session_id") == session.get("session_id")
    ]
    open_recovery_notes = [note for note in recovery_notes if note.get("status") not in {"reviewed", "archived"}]
    return {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-report"),
        "target": str(target),
        "report_id": f"{_now().strftime('%Y%m%d-%H%M%S')}-phase-session-report-{uuid4().hex[:6]}",
        "created_at": _now().isoformat(),
        "git_head": checks_actions._git_head(target),
        "session": constants._session_summary(session),
        "phase_range": phase_range,
        "missing_phase_ids": missing,
        "phase_records": [constants._record_summary(record) for record in records],
        "doctor": {
            "issue_count": doctor_data["issue_count"],
            "top_issue": doctor_data["top_issue"],
            "checks": doctor_data["checks"],
        },
        "next": next_data,
        "recovery": {
            "latest_checkpoint": constants._checkpoint_summary(latest_checkpoint)
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
        reports = constants._read_session_reports(target)
        return (reports[-1], None) if reports else (None, "phase session report not found: latest")
    candidates = sorted(constants._session_reports_root(target).glob(f"{report_id}*/SESSION_EVIDENCE.json"))
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
    _path, session, error = constants._resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    payload = _session_report_payload(target, session)
    report_dir = constants._session_reports_root(target) / str(payload["report_id"])
    payload["path"] = str(report_dir)
    payload["bundle_files"] = ["SESSION_REPORT.md", "SESSION_EVIDENCE.json"]
    _write_json(report_dir / "SESSION_EVIDENCE.json", payload)
    _write_session_report_markdown(report_dir / "SESSION_REPORT.md", payload)
    lines = []
    lines.append(f"phase session report: {payload['report_id']}")
    lines.append(f"session: {payload['session'].get('session_id')}")
    lines.append(f"issues: {payload['doctor']['issue_count']}")
    return emit(payload, json_output, lines, 0)


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
        for report in reversed(constants._read_session_reports(target))
    ][:limit]
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-report-list"),
        "target": str(target),
        "reports": reports,
        "report_count": len(reports),
    }
    lines = []
    lines.append(f"phase session reports: {len(reports)}")
    for report in reports:
        lines.append(
            f"- {report.get('report_id')} session={report.get('session_id')} issues={report.get('issue_count')}"
        )
    return emit(payload, json_output, lines, 0)


def session_report_show(*, target: Path, report_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    report, error = _resolve_session_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    lines = []
    lines.append(f"phase session report: {report.get('report_id')}")
    lines.append(
        f"session: {(report.get('session') or {}).get('session_id') if isinstance(report.get('session'), dict) else 'none'}"
    )
    lines.append(
        f"issues: {(report.get('doctor') or {}).get('issue_count') if isinstance(report.get('doctor'), dict) else 0}"
    )
    return emit(report, json_output, lines, 0)


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
    records, missing = constants._session_phase_records(target, phase_range)
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
    for checkpoint in constants._read_session_checkpoints(target):
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
    for note in constants._read_session_recovery_notes(target):
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
    for closeout in constants._read_closeouts(target):
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
    for action in constants._read_actions(target):
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
    for report in constants._read_reports(target):
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
        compare_summary = constants._report_compare_summary(target, report)
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
    for report in constants._read_session_reports(target):
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
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-activity"),
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
    _path, session, error = constants._resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        payload = _session_activity_payload(target, session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    lines = []
    lines.append(f"phase session activity: {payload['session_id']}")
    lines.append(f"events: {payload['event_count']}")
    for event in payload["events"][-20:]:
        lines.append(
            f"- {event.get('timestamp')} {event.get('event_type')} {event.get('phase_id') or event.get('local_id')}: {event.get('safe_summary')}"
        )
    return emit(payload, json_output, lines, 0)


def _session_progress_payload(target: Path, session: dict[str, Any]) -> dict[str, Any]:
    from . import checks_actions

    phase_range = str(session.get("phase_range") or "")
    records, missing = constants._session_phase_records(target, phase_range)
    parsed = constants._parse_range(phase_range) if phase_range else None
    expected_total = (parsed[1] - parsed[0] + 1) if parsed else len(records)
    complete_records = [record for record in records if record.get("status") in constants.DONE_STATUSES | {"deferred"}]
    percent_complete = round((len(complete_records) / expected_total) * 100, 1) if expected_total else 0.0
    status_counts = constants._status_counts(records)
    doctor_data = checks_actions.doctor_payload(target, phase_range=phase_range)
    blockers = [check for check in doctor_data["checks"] if check.get("status") != "ok"]
    next_payload = _session_next_payload(target, session)
    test_with = len([record for record in records if record.get("tests_run")])
    test_without = len(records) - test_with
    commit_count = len([record for record in records if record.get("commit_hash")])
    push_count = len([record for record in records if record.get("push_ref")])
    remaining_phase_count = max(expected_total - len(complete_records), 0)
    estimated_remaining_local_steps = remaining_phase_count + len(missing) + len(blockers)
    return {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-progress"),
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
    _path, session, error = constants._resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        payload = _session_progress_payload(target, session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    lines = []
    lines.append(f"phase session progress: {payload['session_id']}")
    lines.append(f"complete: {payload['percent_complete']}%")
    lines.append(f"current: {payload['current_phase_id'] or 'none'}")
    lines.append(f"blockers: {payload['blocker_count']}")
    lines.append(f"next: {payload['suggested_next_command']}")
    return emit(payload, json_output, lines, 0)


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
    _path, session, error = constants._resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        candidates = _session_blocker_import_candidates(target, session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    from .. import work_cmd

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
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-import-issues"),
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
    lines = []
    lines.append(f"phase session imports: {session.get('session_id')}")
    lines.append(f"created: {len(created)}")
    lines.append(f"skipped: {len(skipped)}")
    return emit(payload, json_output, lines, 0)


def session_checkpoint_archive(*, target: Path, checkpoint_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    checkpoint, error = constants._resolve_session_checkpoint(target, checkpoint_id)
    if checkpoint is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    checkpoint_path = Path(str(checkpoint.get("path") or ""))
    archive_record = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-checkpoint-archive"),
        "archived_at": _now().isoformat(),
        "checkpoint": checkpoint,
        "checkpoint_id": checkpoint.get("checkpoint_id"),
        "session_id": checkpoint.get("session_id"),
        "phase_id": checkpoint.get("phase_id"),
        "status": "archived",
        "source_fingerprint": checkpoint.get("source_fingerprint"),
    }
    constants._append_jsonl(constants._session_checkpoints_archive_path(target), archive_record)
    try:
        if checkpoint_path.is_file() and checkpoint_path.parent == constants._session_checkpoints_root(target):
            checkpoint_path.unlink()
    except OSError:
        pass
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-checkpoint-archive"),
        "target": str(target),
        "checkpoint_id": checkpoint.get("checkpoint_id"),
        "session_id": checkpoint.get("session_id"),
        "archived": True,
        "archive_path": str(constants._session_checkpoints_archive_path(target)),
        "suggested_next_command": "brigade work phases session checkpoints list",
    }
    lines = []
    lines.append(f"phase session checkpoint archived: {checkpoint.get('checkpoint_id')}")
    lines.append(f"archive: {constants._session_checkpoints_archive_path(target)}")
    return emit(payload, json_output, lines, 0)


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
    for report in constants._read_session_reports(target):
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
    records, missing = constants._session_phase_records(target, phase_range)
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
        if status not in constants.DONE_STATUSES | {"deferred"}:
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
    phase_report = constants._latest_report_for_range(target, phase_range)
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
        compare = constants._report_compare_summary(target, phase_report)
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
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-session-gate"),
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
    _path, session, error = constants._resolve_session(target, session_id)
    if session is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        payload = _session_gate_payload(target, session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    lines = []
    lines.append(f"phase session gate: {payload['session_id']}")
    lines.append(f"safe: {str(payload['safe_to_claim_complete']).lower()}")
    lines.append(f"blockers: {payload['blocker_count']}")
    lines.append(f"next: {payload['suggested_next_command']}")
    for check in payload["checks"]:
        lines.append(f"[{check['status']}] {check['name']}: {check['detail']}")
    return emit(payload, json_output, lines, 0)


def _goal_scaffold_markdown(payload: dict[str, Any]) -> str:
    from . import checks_actions

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
                f"- `{blocker.get('name')}` `{blocker.get('phase_id') or 'session'}`: {checks_actions._safe_handoff_text(blocker.get('detail'))}"
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
    from . import checks_actions

    target = target.expanduser().resolve()
    try:
        parsed = constants._parse_range(phase_range)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if parsed is None:
        print("error: --range is required", file=sys.stderr)
        return 2
    rendered_range = f"{parsed[0]}-{parsed[1]}"
    records, missing, _ = constants._selected_records(target, rendered_range)
    doctor_data = checks_actions.doctor_payload(target, phase_range=rendered_range)
    session = constants._latest_session_for_range(target, rendered_range)
    next_command = (
        f"brigade work phases session next {session.get('session_id')}"
        if session
        else f"brigade work phases next --range {rendered_range}"
    )
    goal_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-phase-goal-{uuid4().hex[:6]}"
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "schema": constants._schema("phase-ledger-goal-scaffold"),
        "target": str(target),
        "goal_id": goal_id,
        "phase_range": rendered_range,
        "session_id": session.get("session_id") if isinstance(session, dict) else None,
        "records": [constants._record_summary(record) for record in records],
        "record_count": len(records),
        "missing_phase_ids": missing,
        "blockers": [check for check in doctor_data["checks"] if check.get("status") != "ok"],
        "blocker_count": doctor_data["issue_count"],
        "suggested_next_command": next_command,
        "source_fingerprint": constants._source_fingerprint(
            records, {"phase_range": rendered_range, "missing": missing, "issue_count": doctor_data["issue_count"]}
        ),
    }
    path = constants._goals_root(target) / f"{goal_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_goal_scaffold_markdown(payload))
    payload["path"] = str(path)
    lines = []
    lines.append(f"phase goal scaffold: {goal_id}")
    lines.append(f"range: {rendered_range}")
    lines.append(f"path: {path}")
    lines.append(f"blockers: {payload['blocker_count']}")
    return emit(payload, json_output, lines, 0)


__all__ = (
    "_activity_event",
    "_gate_check",
    "_goal_scaffold_markdown",
    "_latest_session_report_for_session",
    "_record_has_clean_privacy",
    "_record_has_linted_handoff",
    "_resolve_session_report",
    "_session_activity_payload",
    "_session_audit_payload",
    "_session_blocker_fingerprint",
    "_session_blocker_import_candidates",
    "_session_gate_payload",
    "_session_import_summaries",
    "_session_next_payload",
    "_session_progress_payload",
    "_session_protocol_payload",
    "_session_report_payload",
    "_write_session_report_markdown",
    "goal_scaffold",
    "session_activity",
    "session_audit",
    "session_checkpoint_archive",
    "session_gate",
    "session_import_issues",
    "session_next",
    "session_progress",
    "session_protocol",
    "session_report_build",
    "session_report_list",
    "session_report_show",
    "session_resume",
)
