"""Daily work session helpers."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import dogfood_cmd, scrub
from .. import toml_compat as tomllib
from ..install import apply_gitignore
from ..selection import Selection
from ..untrusted import scan_untrusted, wrap_untrusted
from . import services as services_mod
from . import services
from .services import (
    _scanner_read_receipt,
    _scanner_receipts,
    _review_read_receipt,
    _review_receipts,
    _review_latest_success,
    _review_receipt_path,
    _scanner_read_sweep,
    _scanner_sweeps,
    _scanner_latest_sweep,
    _scanner_latest_success,
    _scanner_is_due,
    _scanner_due_items,
    _scanner_running_receipts,
    _scanner_output_snapshot,
    _scanner_run_summary,
    _scanner_run_receipt_path,
    _scanner_import_fingerprint,
    _scanner_import_provenance,
    _scanner_enrich_import_records,
    _scanner_stamp_new_imports,
    _scanner_validate_import_output,
    _review_redact,
    _review_safe_text,
    _review_finding_fingerprint,
    _normalize_review_finding,
    _load_review_findings,
    _review_import_record,
    _scanner_run_one,
    _review_stamp_completed_tasks,
    _review_run_one,
    _review_plan_payload,
    _review_pending_finding,
    _review_imports,
    _review_tasks_by_id,
    _review_current_fingerprints,
    _review_finding_resolution,
    _review_finding_summary,
    _review_findings_payload,
    _find_review_finding,
    _review_malformed_findings,
    _review_health,
    _review_closeout_path,
    _resolve_review_run,
    _review_stamp_task_closeouts,
    _review_stamp_latest_session,
    _review_closeout_payload,
    _scanner_plan_payload,
    _scanner_health,
    _scanner_sweep_health,
    _default_verify_commands,
    _verify_parse_command,
    _latest_verify_receipt,
    _verify_read_receipt,
    _verify_receipts,
    _resolve_verify_receipt,
    _verification_task_from_session,
    _verification_evidence_payload,
    _verify_plan_payload,
    _write_verify_markdown,
    _run_verify_commands,
    _resolve_closeout_session,
    _work_closeout_path,
    _latest_work_closeout_payload,
    _write_work_closeout_markdown,
    _work_closeout_payload,
    _scanner_health_issue_records,
    import_add,
    import_context,
    import_list,
    import_validate,
    import_ingest,
    import_issue_repairs,
    import_memory_care,
    import_memory_refresh,
    _memory_refresh_cards,
    _import_memory_refresh_queue,
    _safe_chat_metadata,
    _chat_sweep_records,
    import_chat_sweep,
    _content_guard_import_records,
    import_content_guard,
    import_triage,
    _metadata_has_any,
    _provenance_audit_sources,
    _provenance_audit_item,
    _import_provenance_payload,
    import_provenance,
    _inbox_payload,
    _scanner_source_map,
    _import_hygiene_issue,
    _inbox_hygiene_payload,
    _inbox_quality_payload,
    inbox,
    inbox_doctor,
    _archive_import_cutoff,
    inbox_archive,
    import_show,
    _import_plan_payload,
    import_plan,
    import_plan_handoff,
    import_promote_handoff,
    import_promote,
    import_dismiss,
    backup_init,
    backup_contract,
    backup_status,
    backup_doctor,
    backup_import_issues,
    scanners_init,
    backup_closeout,
    review_init,
    review_plan,
    _select_reviewers_for_run,
    review_run,
    review_runs,
    review_show,
    review_import_findings,
    review_findings,
    review_finding_show,
    review_closeout,
    verify_plan,
    verify_run,
    verify_runs,
    verify_show,
    closeout,
    _acceptance_payload,
    acceptance,
    scanners_list,
    scanners_show,
    scanners_plan,
    scanners_doctor,
    _select_scanners_for_run,
    _scanners_run_payload,
    scanners_run,
    scanners_runs,
    scanners_run_show,
    _sweep_run_references,
    _sweep_import_references,
    _sweep_references_from_runs,
    _sweep_import_counts,
    _write_sweep_report,
    _sweep_closeout_status,
    _sweep_is_closed,
    sweep,
    sweeps,
    plans,
    _plan_proposals_dir,
    _proposal_path,
    _render_proposal_md,
    plan_promote,
    plan_proposals,
    sweep_show,
    _find_sweep_report,
    _sweep_import_suggested_commands,
    _sweep_import_review_summary,
    _sweep_group_key,
    _sweep_review_groups,
    _sweep_review_checks,
    _sweep_review_payload,
    sweep_review,
    sweep_closeout,
)  # noqa: F401
from . import config as config_mod
from . import config
from .config import (
    _format_backup_toml,
    _load_backup_config,
    _backup_summary_path,
    _backup_summary_unsafe_fields,
    _backup_result_ok,
    _backup_summary_example,
    _backup_contract_destination,
    _backup_contract_payload,
    _backup_age_hours,
    _backup_issue,
    _backup_destination_checks,
    _backup_health,
    _backup_closeouts_root,
    _backup_latest_closeout,
    _backup_issue_fingerprint,
    _backup_issue_records,
    _format_scanner_toml,
    _format_toml_array,
    _format_review_toml,
    _load_scanner_config,
    _string_list,
    _safe_relative_path,
    _load_review_config,
    _parse_clock_minutes,
    _format_clock_minutes,
    _scanner_start_minute,
    _scanner_window_minutes,
    _scanner_duration_minutes,
    _scanner_command_ok,
    _scanner_argv,
    _scanner_output_path,
    _scanner_import_path,
    _scanner_cwd,
    _review_output_path,
    _review_findings_path,
    _review_cwd,
    _review_argv,
)  # noqa: F401
from . import ledger as ledger_mod
from . import ledger
from .ledger import (
    _read_task_ledger,
    _write_task_ledger,
    _read_imports,
    _write_imports,
    _append_archived_imports,
    _task_sort_key,
    _import_sort_key,
    _task_text_key,
    _string_field,
    _confidence_rank,
    _normalize_task_type,
    _normalize_task_priority,
    _normalize_acceptance,
    _task_acceptance,
    _task_summary,
    _import_task_acceptance,
    _import_task_type,
    _import_task_priority,
    _import_task_template,
    _import_context,
    _import_summary,
    _task_preview_from_import,
    _scanner_candidate,
    _handoff_ready_imports,
    _handoff_candidate,
    _task_snapshot,
    _template_acceptance,
    _combined_acceptance,
    _normalize_issue_heading,
    _is_issue_acceptance_heading,
    _issue_heading,
    _issue_list_item,
    _extract_issue_acceptance,
    _task_issue_metadata,
    _github_issue_ref,
    _read_github_issue,
    _safe_issue_task_id,
    _issue_repair_record,
    _issue_repair_records,
    _import_record_key,
    _import_source_key,
    _import_fingerprint,
    _import_source_identity,
    _validate_import_record,
    _load_import_jsonl,
    _append_import_records,
    _pending_tasks,
    _pending_imports,
    _import_counts,
    _matching_pending_imports,
    _import_metadata_matches,
    _parse_metadata_filters,
    _parse_or_report_metadata_filters,
    _find_pending_task_by_text,
    _find_import,
    _mark_import_promoted,
    _handoff_is_document_target,
    _handoff_target_document,
    _handoff_type,
    _handoff_private_fields,
    _handoff_redact_value,
    _handoff_render_value,
    _handoff_provenance,
    _handoff_safe_text,
    _handoff_title,
    _handoff_suggested_document_content,
    _render_import_handoff,
    _import_handoff_plan_payload,
    _write_import_handoff,
    _mark_import_handoff_promoted,
    _find_task,
    _make_task,
    _parse_metadata,
    _make_import,
    _add_task,
    _plan_rel_path,
    _append_dedupe,
    _read_plan_receipt,
    _build_plan_receipt,
    _render_plan_md,
    _plan_artifact_summary,
    _significant_pending_without_plan,
    _plan_coverage_payload,
    _write_plan_artifact,
)  # noqa: F401
from . import helpers
from .helpers import (
    _git,
    _git_value,
    _short,
    _count_status,
    _slug,
    _work_root,
    _current_path,
    _tasks_path,
    _plans_dir,
    _plan_paths,
    _imports_path,
    _imports_archive_path,
    _backup_config_path,
    _scanner_config_path,
    _scanner_runs_root,
    _scanner_sweeps_root,
    _review_config_path,
    _review_runs_root,
    _verify_runs_root,
    _work_closeouts_root,
    _git_snapshot,
    _dogfood_snapshot,
    _session_snapshot,
    _read_session,
    _session_sort_key,
    _parse_iso_datetime,
    _parse_since,
    _collect_sessions,
    _resolve_session,
    _dirty_count,
    _snapshot,
    _branch,
    _next_step,
    _session_info,
    _handoff_inbox,
    _doctor_line,
    _active_session_info,
    _active_session_dir,
    _work_selection,
    _now,
    _read_json,
    _stable_hash,
    _write_json,
)  # noqa: F401
from . import constants
from .constants import *  # noqa: F401,F403
from .constants import _PROPOSAL_KINDS  # noqa: F401







































































































































































































def _latest_run_next_metadata(target: Path) -> tuple[str | None, dict[str, Any]]:
    dogfood = helpers._dogfood_snapshot(target)
    next_step = dogfood.get("next") if isinstance(dogfood.get("next"), str) else None
    latest = dogfood.get("latest_run") if isinstance(dogfood.get("latest_run"), dict) else None
    metadata: dict[str, Any] = {
        "dogfood_next_source": dogfood.get("next_source"),
    }
    if isinstance(latest, dict):
        metadata.update(
            {
                "run_path": latest.get("path"),
                "run_started_at": latest.get("started_at"),
                "run_status": latest.get("status"),
                "run_task": latest.get("task"),
            }
        )
    return next_step.strip() if next_step and next_step.strip() else None, metadata


def _queue_latest_next(
    target: Path,
    *,
    session_dir: Path | None = None,
    session_title: str | None = None,
) -> tuple[dict[str, Any] | None, bool, str | None]:
    next_step, metadata = _latest_run_next_metadata(target)
    if not next_step:
        return None, False, "no extracted next step is available"
    if session_dir is not None:
        metadata["session_path"] = str(session_dir)
    if session_title:
        metadata["session_title"] = session_title
    task, created = ledger_mod._add_task(
        target,
        next_step,
        source="latest_dogfood_run",
        metadata=metadata,
    )
    return task, created, None


def _latest_completed_run_path(target: Path, output_dir: Path | None) -> str | None:
    if output_dir is not None:
        candidate = output_dir.expanduser()
        if (candidate / "run.json").is_file():
            return str(candidate)
    dogfood = helpers._dogfood_snapshot(target)
    latest = dogfood.get("latest_run") if isinstance(dogfood.get("latest_run"), dict) else None
    path = latest.get("path") if isinstance(latest, dict) else None
    return path if isinstance(path, str) and path else None






































































































































































































































def _resolve_next_task(target: Path) -> dict[str, Any]:
    pending = ledger_mod._pending_tasks(target)
    if pending:
        task = pending[0]
        return {
            "task": str(task.get("text", "")).strip(),
            "source": "task_ledger",
            "task_id": task.get("id"),
            "ledger_task": task,
            "dogfood": helpers._dogfood_snapshot(target),
        }
    dogfood = helpers._dogfood_snapshot(target)
    next_step = dogfood.get("next") if isinstance(dogfood.get("next"), str) else None
    if next_step and next_step.strip():
        return {
            "task": next_step.strip(),
            "source": "latest_dogfood_run",
            "task_id": None,
            "dogfood": dogfood,
        }
    return {
        "task": dogfood_cmd.DEFAULT_TASK,
        "source": "default_review",
        "task_id": None,
        "dogfood": dogfood,
    }


def _render_task_run_prompt(task: dict[str, Any]) -> str:
    text = str(task.get("text") or "").strip()
    lines = [text]
    acceptance = ledger_mod._task_acceptance(task)
    if acceptance:
        lines.extend(["", "Acceptance criteria:"])
        lines.extend(f"- {item}" for item in acceptance)
    lines.extend(
        [
            "",
            "Task metadata:",
            f"- type: {ledger_mod._normalize_task_type(task.get('type'))}",
            f"- priority: {ledger_mod._normalize_task_priority(task.get('priority'))}",
            "",
            "Definition of done:",
            "- Treat the acceptance criteria above as the completion checklist.",
            "- Report the verification command you ran, or explain the blocker.",
        ]
    )
    return "\n".join(lines).strip()


def _task_plan_payload(target: Path, task_id: str) -> tuple[dict[str, Any] | None, int]:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return None, 2
    task, _ = ledger_mod._find_task(target, task_id)
    if task is None:
        print(f"error: task not found: {task_id}", file=sys.stderr)
        return None, 1
    summary = ledger_mod._task_summary(task)
    template = summary.get("template") if isinstance(summary.get("template"), str) else None
    if template:
        summary["guidance"] = list(TASK_TEMPLATES.get(template, {}).get("guidance", ()))
    summary["suggested_command"] = "brigade work run"
    summary["tasks_path"] = str(helpers._tasks_path(target))
    return summary, 0


def _display_session(path: Path, payload: dict[str, Any]) -> None:
    print(f"session: {path}")
    print(f"id: {payload.get('id', path.name)}")
    print(f"status: {payload.get('status', 'unknown')}")
    if payload.get("title"):
        print(f"title: {payload['title']}")
    print(f"target: {payload.get('target', '')}")
    print(f"started: {payload.get('started_at', '')}")
    if payload.get("ended_at"):
        print(f"ended: {payload['ended_at']}")
    if payload.get("note"):
        print(f"note: {payload['note']}")
    notes = payload.get("notes")
    if isinstance(notes, list):
        print(f"notes: {len(notes)}")
        if notes and isinstance(notes[-1], dict) and notes[-1].get("text"):
            print(f"latest_note: {helpers._short(str(notes[-1]['text']))}")
    if payload.get("handoff"):
        print(f"handoff: {payload['handoff']}")
    task = payload.get("task")
    if isinstance(task, dict):
        print("task:")
        print(f"  id: {task.get('id', '')}")
        print(f"  source: {task.get('source', '')}")
        print(f"  type: {task.get('type', '')}")
        print(f"  priority: {task.get('priority', '')}")
        if task.get("template"):
            print(f"  template: {task['template']}")
        acceptance = task.get("acceptance") if isinstance(task.get("acceptance"), list) else []
        print(f"  acceptance: {len(acceptance)}")
        issue = task.get("issue") if isinstance(task.get("issue"), dict) else None
        if issue:
            print(f"  issue: {issue.get('url') or issue.get('number')}")

    start_snapshot = payload.get("start") if isinstance(payload.get("start"), dict) else {}
    end_snapshot = payload.get("end") if isinstance(payload.get("end"), dict) else {}
    snapshot = end_snapshot or start_snapshot
    git = snapshot.get("git") if isinstance(snapshot, dict) else {}
    if isinstance(git, dict) and git.get("available"):
        print("git:")
        print(f"  branch: {git.get('branch')}")
        dirty = git.get("dirty_files") if isinstance(git.get("dirty_files"), list) else []
        print(f"  dirty_files: {len(dirty)}")
        for item in dirty[:20]:
            print(f"    {item}")
    dogfood = snapshot.get("dogfood") if isinstance(snapshot, dict) else {}
    if isinstance(dogfood, dict):
        print("dogfood:")
        print(f"  ready: {dogfood.get('ready')}")
        latest = dogfood.get("latest_run")
        if isinstance(latest, dict):
            print(f"  latest_run: {latest.get('started_at')} [{latest.get('status')}] {latest.get('path')}")
            if latest.get("task"):
                print(f"  latest_task: {helpers._short(str(latest['task']))}")
        if dogfood.get("next"):
            print(f"  next: {helpers._short(str(dogfood['next']))}")


def _session_task_markdown(task: object) -> list[str]:
    if not isinstance(task, dict):
        return []
    lines = ["", "## Task", ""]
    lines.append(f"- Task: `{task.get('id', '')}`")
    if task.get("text"):
        lines.append(f"- Text: {task['text']}")
    lines.append(f"- Source: {task.get('source', '')}")
    lines.append(f"- Type: {task.get('type', '')}")
    lines.append(f"- Priority: {task.get('priority', '')}")
    if task.get("template"):
        lines.append(f"- Template: {task['template']}")
    issue = task.get("issue") if isinstance(task.get("issue"), dict) else None
    if issue:
        lines.append(f"- Issue: {issue.get('url') or issue.get('number')}")
        if issue.get("title"):
            lines.append(f"- Issue title: {issue['title']}")
        if issue.get("state"):
            lines.append(f"- Issue state: {issue['state']}")
    acceptance = task.get("acceptance") if isinstance(task.get("acceptance"), list) else []
    lines.extend(["", "### Acceptance Criteria", ""])
    if acceptance:
        lines.extend(f"- {item}" for item in acceptance)
    else:
        lines.append("- none")
    return lines


def _write_session_markdown(path: Path, *, title: str, payload: dict[str, Any], key: str) -> None:
    snapshot = payload[key]
    git = snapshot.get("git", {})
    dogfood = snapshot.get("dogfood", {})
    lines = [
        f"# {title}",
        "",
        f"- Session: {payload['id']}",
        f"- Target: {payload['target']}",
        f"- Started: {payload['started_at']}",
    ]
    if payload.get("ended_at"):
        lines.append(f"- Ended: {payload['ended_at']}")
    if payload.get("title"):
        lines.append(f"- Title: {payload['title']}")
    if payload.get("note"):
        lines.append(f"- Note: {payload['note']}")
    lines.extend(_session_task_markdown(payload.get("task")))
    lines.extend(["", "## Git", ""])
    if git.get("available"):
        lines.append(f"- Branch: {git.get('branch')}")
        dirty = git.get("dirty_files") or []
        lines.append(f"- Dirty files: {len(dirty)}")
        for item in dirty[:20]:
            lines.append(f"  - `{item}`")
    else:
        lines.append("- unavailable")
    lines.extend(["", "## Dogfood", ""])
    lines.append(f"- Ready: {dogfood.get('ready')}")
    if dogfood.get("latest_run"):
        latest = dogfood["latest_run"]
        lines.append(f"- Latest run: {latest.get('started_at')} [{latest.get('status')}] {latest.get('path')}")
    if dogfood.get("next"):
        lines.append(f"- Next: {dogfood['next']}")
    path.write_text("\n".join(lines) + "\n")




def _write_work_handoff(target: Path, session_dir: Path, payload: dict[str, Any], inbox: Path) -> Path:
    ended = payload.get("ended_at") or helpers._now().isoformat()
    ended_slug = re.sub(r"[^0-9]", "", str(ended))[:12] or helpers._now().strftime("%Y%m%d%H%M")
    title = payload.get("title") or payload.get("id") or "work-session"
    path = inbox / f"{ended_slug}-brigade-work-{helpers._slug(str(title))}-{uuid4().hex[:6]}.md"
    end_snapshot = payload.get("end", {})
    git = end_snapshot.get("git", {})
    dogfood = end_snapshot.get("dogfood", {})
    dirty = git.get("dirty_files") if isinstance(git, dict) else []
    dirty_lines = "\n".join(f"  - `{item}`" for item in dirty[:20]) if isinstance(dirty, list) else "  - unavailable"
    latest = dogfood.get("latest_run") if isinstance(dogfood, dict) else None
    latest_line = "- latest run: none"
    if isinstance(latest, dict):
        latest_line = f"- latest run: `{latest.get('path')}` ({latest.get('status')})"
    next_step = dogfood.get("next") if isinstance(dogfood, dict) else None
    next_line = f"- next: {next_step}" if next_step else "- next: none extracted"
    note = payload.get("note") or ""
    document_content = f"""### Brigade work session: {payload.get('id')}
- target: `{target}`
- session artifacts: `{session_dir}`
- branch: {git.get('branch') if isinstance(git, dict) else 'unknown'}
- dirty files: {len(dirty) if isinstance(dirty, list) else 'unknown'}
{latest_line}
{next_line}
"""
    if note:
        document_content += f"- note: {note}\n"
    body = f"""# Memory Handoff

## Type

workflow

## Title

Brigade work session ended: {helpers._slug(str(title))}

## Summary

A Brigade work session was ended and local session artifacts were written. This handoff captures the session path, final git state, latest dogfood run, and extracted next step so the memory owner can route durable workflow context.

## Durable facts

- session: `{payload.get('id')}`
- target: `{target}`
- session artifacts: `{session_dir}`
- status: {payload.get('status')}
- started: {payload.get('started_at')}
- ended: {payload.get('ended_at')}
- note: {note or 'none'}
- branch: {git.get('branch') if isinstance(git, dict) else 'unknown'}
- dirty files:
{dirty_lines}
{latest_line}
{next_line}

## Evidence

- session.json: `{session_dir / 'session.json'}`
- start summary: `{session_dir / 'start.md'}`
- end summary: `{session_dir / 'end.md'}`

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

{document_content.strip()}
"""
    inbox.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _print_dirty(lines: list[str], *, limit: int) -> None:
    print(f"dirty_files: {len(lines)}")
    for line in lines[:limit]:
        print(f"  {line}")
    remaining = len(lines) - limit
    if remaining > 0:
        print(f"  ... {remaining} more")




def _doctor_ignore_level(value: str) -> str:
    if value in {"yes", "outside-target"}:
        return OK
    if value == "no":
        return WARN
    return WARN


def _workflow_rule_health(target: Path) -> dict[str, Any]:
    missing = [rel for rel in WORKFLOW_RULE_TEMPLATES if not (target / rel).is_file()]
    return {
        "status": OK if not missing else WARN,
        "name": "workflow_rules",
        "detail": (
            "repo-shareable workflow rules installed"
            if not missing
            else f"missing {', '.join(missing)}; run `brigade init --target {target} --depth repo --force` to refresh templates"
        ),
        "missing": missing,
    }






def _next_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    active = helpers._active_session_info(target)
    resolved = _resolve_next_task(target)
    dogfood = resolved["dogfood"]
    ledger_task = resolved.get("ledger_task") if isinstance(resolved.get("ledger_task"), dict) else None
    suggested = 'brigade work end --note "..." --handoff' if active is not None else "brigade work run"
    return {
        "target": str(target),
        "active_session": active,
        "dogfood": dogfood,
        "next_source": resolved["source"],
        "task_id": resolved.get("task_id"),
        "next_task": ledger_mod._task_summary(ledger_task) if ledger_task else None,
        "next_issue": ledger_mod._task_issue_metadata(ledger_task) if ledger_task else None,
        "next": str(resolved["task"]),
        "suggested_command": suggested,
    }


def _suggested_command(active: dict[str, Any] | None, next_text: object, source: object) -> str:
    if active is not None:
        return 'brigade work end --note "..." --handoff'
    if source == "task_ledger":
        return "brigade work run"
    if isinstance(next_text, str) and next_text.strip() and source != "default_review":
        return f"brigade work run {shlex.quote(next_text.strip())}"
    return "brigade work run"


def _pick_fields(payload: object, fields: list[str]) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    compact: dict[str, Any] = {}
    for field in fields:
        if field not in payload:
            continue
        value = payload.get(field)
        if isinstance(value, str | int | float | bool | type(None)):
            compact[field] = value
    return compact


def _compact_top(payload: object) -> dict[str, Any] | None:
    return _pick_fields(
        payload,
        [
            "status",
            "name",
            "detail",
            "issue_type",
            "id",
            "local_id",
            "priority",
            "severity",
            "safe_summary",
            "suggested_next_command",
            "suggested_command",
        ],
    )


def _compact_operator_report_latest(payload: object) -> dict[str, Any] | None:
    compact = _pick_fields(
        payload,
        [
            "report_id",
            "id",
            "created_at",
            "status",
            "review_status",
            "blocker_count",
            "warning_count",
            "fingerprint",
        ],
    )
    if compact is None:
        return None
    activity = payload.get("activity") if isinstance(payload, dict) else None
    if isinstance(activity, list):
        compact["activity_count"] = len(activity)
    reviews = payload.get("reviews") if isinstance(payload, dict) else None
    if isinstance(reviews, list):
        compact["review_count"] = len(reviews)
    return compact


def _compact_repo_fleet_latest(payload: object) -> dict[str, Any] | None:
    compact = _pick_fields(
        payload,
        [
            "sweep_id",
            "train_id",
            "report_id",
            "path_label",
            "status",
            "created_at",
            "started_at",
            "completed_at",
            "repo_count",
            "failed_count",
            "warning_count",
            "blocker_count",
            "open_count",
            "action_count",
            "classification_counts",
            "suggested_next_commands",
        ],
    )
    if compact is None:
        return None
    repos = payload.get("repos") if isinstance(payload, dict) else None
    if isinstance(repos, list):
        compact["repo_count"] = compact.get("repo_count", len(repos))
    commands = payload.get("commands") if isinstance(payload, dict) else None
    if isinstance(commands, list):
        compact["command_count"] = len(commands)
    closeout = payload.get("closeout") if isinstance(payload, dict) else None
    if isinstance(closeout, dict):
        compact["closeout"] = _pick_fields(closeout, ["status", "reviewed_at", "blocker_count", "warning_count"])
    return compact


def _compact_health_section(payload: object) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    compact: dict[str, Any] = {}
    for key in (
        "config_path",
        "repo_count",
        "report_count",
        "action_count",
        "open_count",
        "issue_count",
        "due_count",
        "suggested_command",
        "suggested_next_command",
    ):
        if key in payload:
            compact[key] = payload[key]
    if "top_issue" in payload:
        compact["top_issue"] = _compact_top(payload.get("top_issue"))
    if "top_action" in payload:
        compact["top_action"] = _compact_top(payload.get("top_action"))
    if "counts" in payload:
        compact["counts"] = payload.get("counts")
    if "checks" in payload and isinstance(payload.get("checks"), list):
        compact["checks"] = payload["checks"][:5]
        compact["check_count"] = len(payload["checks"])
    if "latest" in payload:
        compact["latest"] = _compact_repo_fleet_latest(payload.get("latest"))
    if "review" in payload and isinstance(payload.get("review"), dict):
        review = payload["review"]
        compact["review"] = {
            "issue_count": review.get("issue_count", 0),
            "top_issue": _compact_top(review.get("top_issue")),
            "top_pending_import": ledger_mod._import_summary(review.get("top_pending_import")) if review.get("top_pending_import") else None,
        }
    return compact


def _compact_repo_fleet_health(payload: dict[str, Any]) -> dict[str, Any]:
    release_train = payload.get("release_train") if isinstance(payload.get("release_train"), dict) else {}
    compact_release = _compact_health_section(release_train) or {}
    if isinstance(release_train.get("actions"), dict):
        compact_release["actions"] = _compact_health_section(release_train.get("actions"))
    if isinstance(release_train.get("evidence"), dict):
        compact_release["evidence"] = _compact_health_section(release_train.get("evidence"))
    return {
        "config_path": payload["config_path"],
        "repo_count": payload["repo_count"],
        "issue_count": payload["issue_count"],
        "top_issue": payload["top_issue"],
        "report": _compact_health_section(payload.get("report")),
        "actions": _compact_health_section(payload.get("actions")),
        "sweep": _compact_health_section(payload.get("sweep")),
        "release_train": compact_release,
    }


def _brief_payload(target: Path, *, limit: int = 3) -> dict[str, Any]:
    from .. import center_cmd, chat_cmd, context_cmd, daily_cmd, handoff_cmd, learn_cmd, memory_cmd, notifications_cmd, pantry_cmd, phases_cmd, projects_cmd, repos_cmd, research_cmd, roadmap_cmd, security_cmd, tools_cmd

    target = target.expanduser().resolve()
    active = helpers._active_session_info(target)
    sessions, skipped = helpers._collect_sessions(helpers._work_root(target))
    latest_session = helpers._session_info(sessions[0][0], sessions[0][1]) if sessions else None
    recent_sessions = [helpers._session_info(path, payload) for path, payload in sessions[:limit]]
    resolved = _resolve_next_task(target)
    ledger_task = resolved.get("ledger_task") if isinstance(resolved.get("ledger_task"), dict) else None
    git = helpers._git_snapshot(target)
    suggested = _suggested_command(active, resolved["task"], resolved["source"])
    pending = ledger_mod._pending_tasks(target)
    pending_imports = ledger_mod._pending_imports(target)
    pending_import_counts = ledger_mod._import_counts(pending_imports)
    scanner_candidate = ledger_mod._scanner_candidate(pending_imports)
    handoff_candidate = ledger_mod._handoff_candidate(pending_imports)
    inbox_hygiene = services_mod._inbox_hygiene_payload(target)
    scanner_health = services_mod._scanner_health(target)
    sweep_health = services_mod._scanner_sweep_health(target)
    review_health = services_mod._review_health(target)
    chat_health = chat_cmd.health(target)
    memory_health = memory_cmd.health(target)
    security_health = security_cmd.health(target)
    backup_health = config_mod._backup_health(target)
    tool_health = tools_cmd.health(target)
    roadmap_health = roadmap_cmd.health(target)
    repo_health = repos_cmd.health(target)
    pantry_health = pantry_cmd.status_payload(target)
    notification_health = notifications_cmd.health(target)
    context_health = context_cmd.health(target)
    projects_health = projects_cmd.health(target)
    learning_health = learn_cmd.health(target)
    research_health = research_cmd.health(target)
    center_report_health = center_cmd.report_health(target)
    center_actions_health = center_cmd.actions_health(target)
    daily_health = daily_cmd.health(target)
    phase_health = phases_cmd.health(target)
    handoff_issues = handoff_cmd.collect_issues(target)
    known_handoff_issue_ids = handoff_cmd._known_local_issue_ids(target)
    new_handoff_issues = [issue for issue in handoff_issues if issue.id not in known_handoff_issue_ids]
    handoff_drafts = handoff_cmd.draft_queue_payload(target)
    return {
        "target": str(target),
        "git": git,
        "active_session": active,
        "latest_session": latest_session,
        "recent_sessions": recent_sessions,
        "skipped_sessions": skipped,
        "tasks_path": str(helpers._tasks_path(target)),
        "pending_tasks": pending,
        "plan_coverage": ledger_mod._plan_coverage_payload(target),
        "imports_path": str(helpers._imports_path(target)),
        "pending_imports": pending_imports,
        "pending_import_counts": pending_import_counts,
        "scanner_candidate": ledger_mod._import_summary(scanner_candidate) if scanner_candidate else None,
        "handoff_candidate": ledger_mod._import_summary(handoff_candidate) if handoff_candidate else None,
        "inbox_hygiene": {
            "issue_count": inbox_hygiene["issue_count"],
            "top_issue": inbox_hygiene["top_issue"],
        },
        "scanner_health": {
            "config_path": scanner_health["config_path"],
            "checks": scanner_health["checks"],
            "next_run": scanner_health["next_run"],
            "latest_run": scanner_health.get("latest_run"),
            "due": scanner_health.get("due"),
        },
        "scanner_sweeps": {
            "sweeps_root": sweep_health["sweeps_root"],
            "latest": sweep_health["latest"],
            "checks": sweep_health["checks"],
            "due_count": sweep_health["due_count"],
            "suggested_command": sweep_health["suggested_command"],
            "review": sweep_health["review"],
        },
        "code_review": {
            "config_path": review_health["config_path"],
            "checks": review_health["checks"],
            "latest_run": review_health["latest_run"],
            "latest_success": review_health["latest_success"],
            "latest_unclosed_run": review_health["latest_unclosed_run"],
            "pending_finding_count": review_health["pending_finding_count"],
            "unresolved_finding_count": review_health["unresolved_finding_count"],
            "top_pending_finding": review_health["top_pending_finding"],
            "top_unresolved_finding": review_health["top_unresolved_finding"],
        },
        "chat_surfaces": {
            "config_path": chat_health["config_path"],
            "checks": chat_health["checks"],
            "issue_count": chat_health["issue_count"],
            "top_issue": chat_health["top_issue"],
        },
        "memory_care": {
            "config_path": memory_health["config_path"],
            "scan_path": memory_health["scan_path"],
            "queue_path": memory_health["queue_path"],
            "valid": memory_health["valid"],
            "issue_count": memory_health["issue_count"],
            "top_issue": memory_health["top_issue"],
            "autofix_plan": memory_health.get("autofix_plan"),
        },
        "security_health": {
            "config_path": security_health["config_path"],
            "valid": security_health["valid"],
            "issue_count": security_health["issue_count"],
            "top_issue": security_health["top_issue"],
            "top_finding": security_health["top_finding"],
        },
        "backup_health": {
            "config_path": backup_health["config_path"],
            "issue_count": backup_health["issue_count"],
            "raw_issue_count": backup_health.get("raw_issue_count"),
            "quieted_issue_count": backup_health.get("quieted_issue_count"),
            "restore_rehearsal_issue_count": backup_health.get("restore_rehearsal_issue_count"),
            "changed_fingerprint_count": backup_health.get("changed_fingerprint_count"),
            "operator_summary": backup_health.get("operator_summary"),
            "top_issue": backup_health["top_issue"],
            "valid": backup_health["valid"],
        },
        "tool_catalog": {
            "config_path": tool_health["config_path"],
            "valid": tool_health["valid"],
            "tool_count": tool_health["tool_count"],
            "issue_count": tool_health["issue_count"],
            "top_issue": tool_health["top_issue"],
            "call_queue": tool_health.get("call_queue"),
            "run_history": tool_health.get("run_history"),
            "checkpoints": tool_health.get("checkpoints"),
        },
        "roadmap_completion": {
            "issue_count": roadmap_health["issue_count"],
            "top_issue": roadmap_health["top_issue"],
            "audit": roadmap_health["audit"],
            "patterns": roadmap_health["patterns"],
        },
        "repo_fleet": {
            **_compact_repo_fleet_health(repo_health),
        },
        "pantry": pantry_health,
        "notifications": notification_health,
        "context_packs": {
            "pack_count": context_health["pack_count"],
            "issue_count": context_health["issue_count"],
            "top_issue": context_health["top_issue"],
            "latest": context_health["latest"],
        },
        "project_consolidation": {
            "project_count": projects_health["project_count"],
            "issue_count": projects_health["issue_count"],
            "top_issue": projects_health["top_issue"],
        },
        "learning": {
            "candidate_count": learning_health["candidate_count"],
            "issue_count": learning_health["issue_count"],
            "top_issue": learning_health["top_issue"],
        },
        "research_handoffs": {
            "run_count": research_health["run_count"],
            "issue_count": research_health["issue_count"],
            "top_issue": research_health["top_issue"],
        },
        "operator_report": {
            "issue_count": center_report_health["issue_count"],
            "top_issue": center_report_health["top_issue"],
            "latest": _compact_operator_report_latest(center_report_health["latest"]),
            "latest_diff": _compact_repo_fleet_latest(center_report_health.get("latest_diff")),
        },
        "operator_actions": {
            "actions_path": center_actions_health["actions_path"],
            "action_count": center_actions_health["action_count"],
            "open_count": center_actions_health["open_count"],
            "counts": center_actions_health["counts"],
            "top_action": center_actions_health["top_action"],
            "issue_count": center_actions_health["issue_count"],
            "top_issue": center_actions_health["top_issue"],
        },
        "daily_driver": {
            "config_path": daily_health["config_path"],
            "run_count": daily_health["run_count"],
            "plan_count": daily_health["plan_count"],
            "issue_count": daily_health["issue_count"],
            "top_issue": daily_health["top_issue"],
            "latest_run": daily_health["latest_run"],
            "latest_plan": daily_health["latest_plan"],
            "approvals": daily_health.get("approvals"),
            "telemetry": daily_health.get("telemetry"),
        },
        "phase_ledger": {
            "records_path": phase_health["records_path"],
            "record_count": phase_health["record_count"],
            "open_count": phase_health["open_count"],
            "issue_count": phase_health["issue_count"],
            "top_issue": phase_health["top_issue"],
            "latest": phase_health["latest"],
            "latest_session": phase_health.get("latest_session"),
            "latest_session_checkpoint": phase_health.get("latest_session_checkpoint"),
            "latest_session_checkpoint_compare": phase_health.get("latest_session_checkpoint_compare"),
            "latest_session_report": phase_health.get("latest_session_report"),
            "open_action_count": phase_health.get("open_action_count", 0),
            "top_action": phase_health.get("top_action"),
            "action_counts": phase_health.get("action_counts", {}),
        },
        "handoff_issues": {
            "count": len(new_handoff_issues),
            "known_count": len(handoff_issues) - len(new_handoff_issues),
            "total_count": len(handoff_issues),
            "by_category": handoff_cmd._issue_counts(new_handoff_issues),
            "known_by_category": handoff_cmd._issue_counts(
                [issue for issue in handoff_issues if issue.id in known_handoff_issue_ids]
            ),
        },
        "handoff_drafts": {
            "counts": handoff_drafts["counts"],
            "issue_count": handoff_drafts["issue_count"],
            "top_issue": handoff_drafts["top_issue"],
            "latest_ingest_run": handoff_drafts.get("latest_ingest_run"),
            "drafts": handoff_drafts["drafts"][:limit],
        },
        "dogfood": resolved["dogfood"],
        "next_source": resolved["source"],
        "task_id": resolved.get("task_id"),
        "next_task": ledger_mod._task_summary(ledger_task) if ledger_task else None,
        "next_issue": ledger_mod._task_issue_metadata(ledger_task) if ledger_task else None,
        "next": str(resolved["task"]),
        "suggested_command": suggested,
    }


def _print_bootstrap_line(level: str, name: str, detail: object) -> None:
    print(f"[{level}] {name}: {detail}")




def start(
    *,
    target: Path,
    title: str | None = None,
    force: bool = False,
    task_snapshot: dict[str, Any] | None = None,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    root = helpers._work_root(target)
    current = helpers._current_path(target)
    if current.exists() and not force:
        print(f"error: work session already active: {current.read_text().strip()}", file=sys.stderr)
        return 2

    started = helpers._now()
    session_id = f"{started.strftime('%Y%m%d-%H%M%S')}-{helpers._slug(title or 'work-session')}"
    session_dir = root / session_id
    session_dir.mkdir(parents=True, exist_ok=False)
    payload: dict[str, Any] = {
        "id": session_id,
        "title": title,
        "target": str(target),
        "status": "active",
        "started_at": started.isoformat(),
        "start": helpers._session_snapshot(target),
    }
    if task_snapshot is not None:
        payload["task"] = task_snapshot
    helpers._write_json(session_dir / "session.json", payload)
    _write_session_markdown(session_dir / "start.md", title="Brigade Work Session Start", payload=payload, key="start")
    current.write_text(session_id + "\n")
    print(f"session: {session_dir}")
    print(f"status: active")
    return 0


def end(*, target: Path, note: str | None = None, handoff: bool = False, handoff_inbox: Path | None = None) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    current = helpers._current_path(target)
    if not current.exists():
        print(f"error: no active work session in {helpers._work_root(target)}", file=sys.stderr)
        return 1
    session_id = current.read_text().strip()
    session_dir = helpers._work_root(target) / session_id
    session_json = session_dir / "session.json"
    try:
        payload = json.loads(session_json.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: invalid active work session: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("error: invalid active work session: session.json must contain an object", file=sys.stderr)
        return 2

    payload["status"] = "ended"
    payload["ended_at"] = helpers._now().isoformat()
    payload["note"] = note
    payload["end"] = helpers._session_snapshot(target)
    helpers._write_json(session_json, payload)
    _write_session_markdown(session_dir / "end.md", title="Brigade Work Session End", payload=payload, key="end")
    if handoff:
        inbox = helpers._handoff_inbox(target, payload, handoff_inbox)
        handoff_path = _write_work_handoff(target, session_dir, payload, inbox)
        payload["handoff"] = str(handoff_path)
        helpers._write_json(session_json, payload)
    current.unlink()
    print(f"session: {session_dir}")
    if handoff:
        print(f"handoff: {payload['handoff']}")
    print("status: ended")
    return 0


def note(*, target: Path, text: str) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    rendered = text.strip()
    if not rendered:
        print("error: note text is required", file=sys.stderr)
        return 2

    current = helpers._current_path(target)
    if not current.exists():
        print(f"error: no active work session in {helpers._work_root(target)}", file=sys.stderr)
        return 1
    session_id = current.read_text().strip()
    session_dir = helpers._work_root(target) / session_id
    session_json = session_dir / "session.json"
    try:
        payload = json.loads(session_json.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: invalid active work session: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("error: invalid active work session: session.json must contain an object", file=sys.stderr)
        return 2

    entry = {
        "created_at": helpers._now().isoformat(),
        "text": rendered,
    }
    notes = payload.setdefault("notes", [])
    if not isinstance(notes, list):
        print("error: invalid active work session: notes must be a list", file=sys.stderr)
        return 2
    notes.append(entry)
    helpers._write_json(session_json, payload)

    notes_path = session_dir / "notes.md"
    prefix = "" if notes_path.exists() and notes_path.read_text().endswith("\n") else "\n"
    with notes_path.open("a") as handle:
        if notes_path.stat().st_size == 0:
            handle.write("# Brigade Work Session Notes\n")
        else:
            handle.write(prefix)
        handle.write(f"\n## {entry['created_at']}\n\n{rendered}\n")
    print(f"session: {session_dir}")
    print(f"note: {helpers._short(rendered)}")
    return 0


def list_sessions(*, target: Path, limit: int = 10) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    root = helpers._work_root(target)
    sessions, skipped = helpers._collect_sessions(root)
    for path, payload in sessions[:limit]:
        snapshot = payload.get("end") if isinstance(payload.get("end"), dict) else payload.get("start", {})
        dirty = helpers._dirty_count(snapshot) if isinstance(snapshot, dict) else 0
        title = helpers._short(str(payload.get("title") or ""))
        ended = payload.get("ended_at") or "active"
        print(
            f"{payload.get('started_at', path.name)} [{payload.get('status', 'unknown')}] "
            f"dirty={dirty} ended={ended} {path}"
        )
        if title:
            print(f"  {title}")
    if not sessions:
        print(f"no work sessions found in {root}")
    if skipped:
        print(f"skipped {skipped} invalid work session{'s' if skipped != 1 else ''}", file=sys.stderr)
    return 0


def latest(*, target: Path) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    root = helpers._work_root(target)
    sessions, skipped = helpers._collect_sessions(root)
    if skipped:
        print(f"skipped {skipped} invalid work session{'s' if skipped != 1 else ''}", file=sys.stderr)
    if not sessions:
        print(f"error: no work sessions found in {root}", file=sys.stderr)
        return 1
    path, payload = sessions[0]
    _display_session(path, payload)
    return 0


def show(*, target: Path, session: str | Path) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = helpers._resolve_session(target, session)
    if not path.is_dir():
        print(f"error: work session not found: {path}", file=sys.stderr)
        return 2
    payload = helpers._read_session(path)
    if payload is None:
        print(f"error: session.json not found or invalid in {path}", file=sys.stderr)
        return 2
    _display_session(path, payload)
    return 0


def recap(*, target: Path, limit: int = 5, since: str | None = None) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    try:
        since_dt = helpers._parse_since(since)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    root = helpers._work_root(target)
    sessions, skipped = helpers._collect_sessions(root)
    if since_dt is not None:
        sessions = [
            (path, payload)
            for path, payload in sessions
            if (helpers._parse_iso_datetime(payload.get("ended_at") or payload.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc))
            >= since_dt
        ]
    sessions = sessions[:limit]

    print(f"work recap: {target}")
    if since:
        print(f"since: {since}")
    print(f"sessions: {len(sessions)}")
    if skipped:
        print(f"skipped: {skipped}", file=sys.stderr)
    if not sessions:
        print(f"no work sessions found in {root}")
        return 0

    branches = sorted({branch for _, payload in sessions if (branch := helpers._branch(helpers._snapshot(payload)))})
    if branches:
        print(f"branches: {', '.join(branches)}")
    handoffs = [str(payload.get("handoff")) for _, payload in sessions if payload.get("handoff")]
    if handoffs:
        print(f"handoffs: {len(handoffs)}")

    print("items:")
    for path, payload in sessions:
        snapshot = helpers._snapshot(payload)
        title = str(payload.get("title") or payload.get("id") or path.name)
        print(f"- {title}")
        print(f"  id: {payload.get('id', path.name)}")
        print(f"  status: {payload.get('status', 'unknown')}")
        print(f"  started: {payload.get('started_at', '')}")
        if payload.get("ended_at"):
            print(f"  ended: {payload['ended_at']}")
        branch = helpers._branch(snapshot)
        if branch:
            print(f"  branch: {branch}")
        print(f"  dirty_files: {helpers._dirty_count(snapshot)}")
        if payload.get("note"):
            print(f"  note: {helpers._short(str(payload['note']))}")
        if payload.get("handoff"):
            print(f"  handoff: {payload['handoff']}")
        next_text = helpers._next_step(snapshot)
        if next_text:
            print(f"  next: {helpers._short(next_text)}")
    return 0


def _print_resume_session(label: str, path: Path, payload: dict[str, Any]) -> None:
    print(f"{label}: {path}")
    print(f"{label}_status: {payload.get('status', 'unknown')}")
    if payload.get("title"):
        print(f"{label}_title: {helpers._short(str(payload['title']))}")
    print(f"{label}_started: {payload.get('started_at', '')}")
    if payload.get("ended_at"):
        print(f"{label}_ended: {payload['ended_at']}")
    if payload.get("note"):
        print(f"{label}_note: {helpers._short(str(payload['note']))}")
    notes = payload.get("notes")
    if isinstance(notes, list):
        print(f"{label}_notes: {len(notes)}")
        if notes and isinstance(notes[-1], dict) and notes[-1].get("text"):
            print(f"{label}_latest_note: {helpers._short(str(notes[-1]['text']))}")
    if payload.get("handoff"):
        print(f"{label}_handoff: {payload['handoff']}")


def resume(*, target: Path) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    print(f"work resume: {target}")
    root = helpers._work_root(target)
    current = helpers._current_path(target)
    active_payload: dict[str, Any] | None = None
    if current.exists():
        active_dir = root / current.read_text().strip()
        active_payload = helpers._read_session(active_dir)
        if active_payload is None:
            print(f"active_session: invalid ({active_dir})")
        else:
            _print_resume_session("active_session", active_dir, active_payload)
    else:
        print("active_session: none")

    sessions, skipped = helpers._collect_sessions(root)
    if skipped:
        print(f"skipped: {skipped}", file=sys.stderr)
    if sessions:
        latest_path, latest_payload = sessions[0]
        if active_payload is None or latest_payload.get("id") != active_payload.get("id"):
            _print_resume_session("latest_session", latest_path, latest_payload)
    else:
        print(f"latest_session: none ({root})")

    dogfood = helpers._dogfood_snapshot(target)
    print(f"dogfood_ready: {dogfood.get('ready')}")
    if dogfood.get("error"):
        print(f"dogfood_error: {dogfood['error']}")
    if dogfood.get("target"):
        print(f"dogfood_target: {dogfood['target']}")
    if dogfood.get("artifacts_dir"):
        print(f"dogfood_artifacts: {dogfood['artifacts_dir']}")
    latest_run = dogfood.get("latest_run")
    if isinstance(latest_run, dict):
        print(
            "latest_run: "
            f"{latest_run.get('started_at', '')} "
            f"[{latest_run.get('status', 'unknown')}] {latest_run.get('path')}"
        )
        if latest_run.get("task"):
            print(f"latest_task: {helpers._short(str(latest_run['task']))}")
    else:
        print("latest_run: none")

    next_step = dogfood.get("next") if isinstance(dogfood.get("next"), str) else None
    print(f"next: {helpers._short(next_step) if next_step else 'none'}")
    if active_payload is not None:
        print('suggested_command: brigade work end --note "..." --handoff')
    elif next_step:
        print(f"suggested_command: brigade work run {shlex.quote(next_step)}")
    else:
        print("suggested_command: brigade work run")
    return 0


def brief(*, target: Path, limit: int = 3, json_output: bool = False) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    payload = _brief_payload(target, limit=limit)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"work brief: {target}")
    git = payload["git"]
    if isinstance(git, dict) and git.get("available"):
        print(f"branch: {git.get('branch')}")
        dirty = git.get("dirty_files") if isinstance(git.get("dirty_files"), list) else []
        print(f"dirty_files: {len(dirty)}")
        for item in dirty[:8]:
            print(f"  {item}")
        if len(dirty) > 8:
            print(f"  ... {len(dirty) - 8} more")
    else:
        print("git: unavailable")

    active = payload["active_session"]
    if isinstance(active, dict):
        if active.get("valid"):
            print(f"active_session: {active.get('path')}")
            if active.get("title"):
                print(f"active_session_title: {helpers._short(str(active['title']))}")
        else:
            print(f"active_session: invalid ({active.get('path')})")
    else:
        print("active_session: none")

    latest_session = payload["latest_session"]
    if isinstance(latest_session, dict):
        print(f"latest_session: {latest_session.get('path')}")
        if latest_session.get("title"):
            print(f"latest_session_title: {helpers._short(str(latest_session['title']))}")
        if latest_session.get("note"):
            print(f"latest_session_note: {helpers._short(str(latest_session['note']))}")
        if latest_session.get("handoff"):
            print(f"latest_session_handoff: {latest_session['handoff']}")
    else:
        print(f"latest_session: none ({helpers._work_root(target)})")

    dogfood = payload["dogfood"]
    print(f"dogfood_ready: {dogfood.get('ready')}")
    if dogfood.get("error"):
        print(f"dogfood_error: {dogfood['error']}")
    latest_run = dogfood.get("latest_run")
    if isinstance(latest_run, dict):
        print(
            "latest_run: "
            f"{latest_run.get('started_at', '')} "
            f"[{latest_run.get('status', 'unknown')}] {latest_run.get('path')}"
        )
        if latest_run.get("task"):
            print(f"latest_task: {helpers._short(str(latest_run['task']))}")
    else:
        print("latest_run: none")

    pantry = payload.get("pantry") if isinstance(payload.get("pantry"), dict) else {}
    if pantry:
        print(f"pantry: {pantry.get('summary')}")
    notifications = payload.get("notifications") if isinstance(payload.get("notifications"), dict) else {}
    if notifications:
        print(f"notifications: {notifications.get('status')} configured={notifications.get('configured')}")
        top_notification = notifications.get("top_issue") if isinstance(notifications.get("top_issue"), dict) else None
        if top_notification:
            print(f"notifications_top_issue: {top_notification.get('name')} {helpers._short(str(top_notification.get('detail', '')))}")

    print(f"next_source: {payload['next_source']}")
    if payload.get("task_id"):
        print(f"task_id: {payload['task_id']}")
    next_task = payload.get("next_task") if isinstance(payload.get("next_task"), dict) else None
    if next_task:
        print(f"next_type: {next_task.get('type')}")
        print(f"next_priority: {next_task.get('priority')}")
        if next_task.get("template"):
            print(f"next_template: {next_task.get('template')}")
        if next_task.get("acceptance_missing"):
            print("next_acceptance: missing")
        else:
            print(f"next_acceptance: {next_task.get('acceptance_count')}")
    next_issue = payload.get("next_issue") if isinstance(payload.get("next_issue"), dict) else None
    if next_issue:
        print(f"issue: {next_issue.get('url') or next_issue.get('number')}")
        if next_issue.get("state"):
            print(f"issue_state: {next_issue['state']}")
        labels = next_issue.get("labels")
        if isinstance(labels, list) and labels:
            print(f"issue_labels: {', '.join(str(label) for label in labels)}")
    print(f"next: {helpers._short(str(payload['next']))}")
    print(f"suggested_command: {payload['suggested_command']}")

    pending = payload["pending_tasks"]
    if isinstance(pending, list) and pending:
        print("pending_tasks:")
        for task in pending[:5]:
            if not isinstance(task, dict):
                continue
            summary = ledger_mod._task_summary(task)
            print(
                "  - "
                f"{task.get('id')} "
                f"[{summary['type']} {summary['priority']} acceptance={summary['acceptance_count']}] "
                f"{helpers._short(str(task.get('text', '')))}"
            )
        if len(pending) > 5:
            print(f"  ... {len(pending) - 5} more")

    plan_coverage = payload.get("plan_coverage")
    if isinstance(plan_coverage, dict):
        if plan_coverage.get("significant_without_plan"):
            ids = ", ".join(plan_coverage.get("task_ids", [])[:5])
            print(f"plans: {plan_coverage['significant_without_plan']} pending task(s) without a plan artifact ({ids})")
        elif not plan_coverage.get("pending_total"):
            print("plans: no pending tasks")
        else:
            print("plans: significant pending tasks have plan artifacts")

    pending_imports = payload["pending_imports"]
    if isinstance(pending_imports, list) and pending_imports:
        counts = payload.get("pending_import_counts")
        if isinstance(counts, dict):
            print(f"pending_import_count: {counts.get('total', len(pending_imports))}")
            by_source = counts.get("by_source") if isinstance(counts.get("by_source"), dict) else {}
            if by_source:
                print("pending_imports_by_source:")
                for source, count in by_source.items():
                    print(f"  {source}: {count}")
            by_kind = counts.get("by_kind") if isinstance(counts.get("by_kind"), dict) else {}
            if by_kind:
                print("pending_imports_by_kind:")
                for kind, count in by_kind.items():
                    print(f"  {kind}: {count}")
        print("pending_imports:")
        for item in pending_imports[:5]:
            if not isinstance(item, dict):
                continue
            source = item.get("source") or "unknown"
            kind = item.get("kind") or "task"
            print(f"  - {item.get('id')} [{kind}] {source}: {helpers._short(str(item.get('text', '')))}")
        if len(pending_imports) > 5:
            print(f"  ... {len(pending_imports) - 5} more")
    scanner_candidate = payload.get("scanner_candidate")
    if isinstance(scanner_candidate, dict):
        print(f"scanner_next_import: {scanner_candidate.get('id')}")
        print(f"scanner_next_source: {scanner_candidate.get('source')}")
        print(f"scanner_next_kind: {scanner_candidate.get('kind')}")
        if scanner_candidate.get("kind") == "task":
            print(
                "scanner_next_task: "
                f"[{scanner_candidate.get('type')} {scanner_candidate.get('priority')} "
                f"acceptance={scanner_candidate.get('acceptance_count')}] "
                f"{helpers._short(str(scanner_candidate.get('text', '')))}"
            )
            print(f"scanner_next_command: brigade work import plan {scanner_candidate.get('id')}")
    handoff_candidate = payload.get("handoff_candidate")
    pending_tasks = payload.get("pending_tasks") if isinstance(payload.get("pending_tasks"), list) else []
    if isinstance(handoff_candidate, dict) and not pending_tasks:
        print(f"handoff_next_import: {handoff_candidate.get('id')}")
        print(f"handoff_next_source: {handoff_candidate.get('source')}")
        print(f"handoff_next_kind: {handoff_candidate.get('kind')}")
        print(f"handoff_next_target: {handoff_candidate.get('target_document')}")
        print(f"handoff_next_command: brigade work import plan-handoff {handoff_candidate.get('id')}")

    inbox_hygiene = payload.get("inbox_hygiene") if isinstance(payload.get("inbox_hygiene"), dict) else {}
    if inbox_hygiene:
        issue_count = inbox_hygiene.get("issue_count")
        print(f"inbox_hygiene: {helpers._count_status(issue_count)}")
        top_inbox = inbox_hygiene.get("top_issue") if isinstance(inbox_hygiene.get("top_issue"), dict) else None
        if top_inbox:
            print(
                "inbox_top_issue: "
                f"{top_inbox.get('name')} "
                f"{helpers._short(str(top_inbox.get('detail', '')))}"
            )

    scanner_health = payload.get("scanner_health") if isinstance(payload.get("scanner_health"), dict) else {}
    scanner_checks = scanner_health.get("checks") if isinstance(scanner_health.get("checks"), list) else []
    if scanner_checks:
        warnings = [check for check in scanner_checks if isinstance(check, dict) and check.get("status") != OK]
        print(f"scanner_config: {scanner_health.get('config_path')}")
        print(f"scanner_health: {helpers._count_status(len(warnings), 'warning')}")
        next_scanner = scanner_health.get("next_run") if isinstance(scanner_health.get("next_run"), dict) else None
        if next_scanner:
            print(
                "scanner_next_run: "
                f"{next_scanner.get('id')} {next_scanner.get('start')} {next_scanner.get('cadence')}"
            )
        latest_scanner_run = scanner_health.get("latest_run") if isinstance(scanner_health.get("latest_run"), dict) else None
        if latest_scanner_run:
            print(
                "scanner_latest_run: "
                f"{latest_scanner_run.get('scanner_id')} "
                f"[{latest_scanner_run.get('status')}] {latest_scanner_run.get('run_id')}"
            )
        due_scanners = scanner_health.get("due") if isinstance(scanner_health.get("due"), list) else []
        if due_scanners:
            print(f"scanner_due: {', '.join(str(item.get('id')) for item in due_scanners[:5] if isinstance(item, dict))}")

    scanner_sweeps = payload.get("scanner_sweeps") if isinstance(payload.get("scanner_sweeps"), dict) else {}
    if scanner_sweeps:
        latest_sweep = scanner_sweeps.get("latest") if isinstance(scanner_sweeps.get("latest"), dict) else None
        if latest_sweep:
            print(f"scanner_latest_sweep: {latest_sweep.get('sweep_id')} [{latest_sweep.get('status')}]")
        if scanner_sweeps.get("suggested_command"):
            print(f"scanner_sweep_command: {scanner_sweeps.get('suggested_command')}")
        review = scanner_sweeps.get("review") if isinstance(scanner_sweeps.get("review"), dict) else {}
        top_pending = review.get("top_pending_import") if isinstance(review.get("top_pending_import"), dict) else None
        if top_pending and latest_sweep:
            print(f"scanner_unreviewed_sweep: {latest_sweep.get('sweep_id')}")
            print(f"scanner_sweep_import: {top_pending.get('id')} {top_pending.get('source')} {helpers._short(str(top_pending.get('text', '')))}")
            print(f"scanner_sweep_review: brigade work sweep-review {latest_sweep.get('sweep_id')}")

    chat_surfaces = payload.get("chat_surfaces") if isinstance(payload.get("chat_surfaces"), dict) else {}
    if chat_surfaces:
        print(f"chat_surfaces_config: {chat_surfaces.get('config_path')}")
        chat_issue_count = int(chat_surfaces.get("issue_count", 0) or 0)
        print(f"chat_surfaces_health: {helpers._count_status(chat_issue_count)}")
        top_chat = chat_surfaces.get("top_issue") if isinstance(chat_surfaces.get("top_issue"), dict) else None
        if top_chat:
            print(f"chat_surfaces_top_issue: {top_chat.get('name')} {helpers._short(str(top_chat.get('detail', '')))}")

    memory_care = payload.get("memory_care") if isinstance(payload.get("memory_care"), dict) else {}
    if memory_care:
        print(f"memory_care_config: {memory_care.get('config_path')}")
        issue_count = memory_care.get("issue_count")
        print(f"memory_care_health: {helpers._count_status(issue_count)}")
        top_memory = memory_care.get("top_issue") if isinstance(memory_care.get("top_issue"), dict) else None
        if top_memory:
            print(
                "memory_care_top_issue: "
                f"{top_memory.get('issue_type') or top_memory.get('name')} "
                f"{top_memory.get('file') or helpers._short(str(top_memory.get('detail', '')))}"
            )
        autofix_plan = memory_care.get("autofix_plan") if isinstance(memory_care.get("autofix_plan"), dict) else {}
        if autofix_plan.get("plan_count"):
            print(
                "memory_care_fix_plan: "
                f"planned={autofix_plan.get('plan_count')} "
                f"blocked={autofix_plan.get('blocked_count')} "
                f"command={autofix_plan.get('suggested_next_command')}"
            )

    security_health = payload.get("security_health") if isinstance(payload.get("security_health"), dict) else {}
    if security_health:
        print(f"security_config: {security_health.get('config_path')}")
        issue_count = security_health.get("issue_count")
        print(f"security_health: {helpers._count_status(issue_count)}")
        top_security = security_health.get("top_finding") if isinstance(security_health.get("top_finding"), dict) else None
        if top_security:
            print(
                "security_top_finding: "
                f"{top_security.get('id')} [{top_security.get('severity')}] "
                f"{top_security.get('path')}:{top_security.get('line')} "
                f"{helpers._short(str(top_security.get('title', '')))}"
            )

    backup_health = payload.get("backup_health") if isinstance(payload.get("backup_health"), dict) else {}
    if backup_health:
        print(f"backup_config: {backup_health.get('config_path')}")
        issue_count = backup_health.get("issue_count")
        print(f"backup_health: {helpers._count_status(issue_count)}")
        if backup_health.get("operator_summary"):
            print(f"backup_summary: {backup_health.get('operator_summary')}")
        top_backup = backup_health.get("top_issue") if isinstance(backup_health.get("top_issue"), dict) else None
        if top_backup:
            print(
                "backup_top_issue: "
                f"{top_backup.get('destination')}/{top_backup.get('issue_type')} "
                f"{helpers._short(str(top_backup.get('detail', '')))}"
            )

    daily_driver = payload.get("daily_driver") if isinstance(payload.get("daily_driver"), dict) else {}
    if daily_driver:
        print(f"daily_config: {daily_driver.get('config_path')}")
        print(f"daily_driver: {helpers._count_status(daily_driver.get('issue_count'))}")
        latest_daily = daily_driver.get("latest_run") if isinstance(daily_driver.get("latest_run"), dict) else None
        if latest_daily:
            print(f"daily_latest_run: {latest_daily.get('run_id')} [{latest_daily.get('status')}]")
        top_daily = daily_driver.get("top_issue") if isinstance(daily_driver.get("top_issue"), dict) else None
        if top_daily:
            print(f"daily_top_issue: {top_daily.get('name')} {helpers._short(str(top_daily.get('detail', '')))}")
        approvals = daily_driver.get("approvals") if isinstance(daily_driver.get("approvals"), dict) else {}
        if approvals.get("pending_count"):
            top_approval = approvals.get("top_pending") if isinstance(approvals.get("top_pending"), dict) else {}
            print(f"daily_pending_approval: {top_approval.get('approval_id')} {helpers._short(str(top_approval.get('safe_summary', '')))}")

    phase_ledger = payload.get("phase_ledger") if isinstance(payload.get("phase_ledger"), dict) else {}
    if phase_ledger:
        print(f"phase_ledger: {helpers._count_status(phase_ledger.get('issue_count'))}")
        print(f"phase_records: {phase_ledger.get('record_count', 0)}")
        print(f"phase_actions: {phase_ledger.get('open_action_count', 0)}")
        latest_phase_session = phase_ledger.get("latest_session") if isinstance(phase_ledger.get("latest_session"), dict) else None
        if latest_phase_session:
            print(f"phase_session: {latest_phase_session.get('session_id')} [{latest_phase_session.get('status')}]")
        latest_checkpoint = phase_ledger.get("latest_session_checkpoint") if isinstance(phase_ledger.get("latest_session_checkpoint"), dict) else None
        latest_checkpoint_compare = phase_ledger.get("latest_session_checkpoint_compare") if isinstance(phase_ledger.get("latest_session_checkpoint_compare"), dict) else None
        if latest_checkpoint:
            compare_count = latest_checkpoint_compare.get("issue_count") if isinstance(latest_checkpoint_compare, dict) else 0
            print(f"phase_checkpoint: {latest_checkpoint.get('checkpoint_id')} [{latest_checkpoint.get('status')}] issues={compare_count}")
        top_phase = phase_ledger.get("top_issue") if isinstance(phase_ledger.get("top_issue"), dict) else None
        if top_phase:
            print(f"phase_top_issue: {top_phase.get('name')} {helpers._short(str(top_phase.get('detail', '')))}")
        top_phase_action = phase_ledger.get("top_action") if isinstance(phase_ledger.get("top_action"), dict) else None
        if top_phase_action:
            print(f"phase_top_action: {top_phase_action.get('action_id')} {helpers._short(str(top_phase_action.get('safe_summary', '')))}")

    tool_catalog = payload.get("tool_catalog") if isinstance(payload.get("tool_catalog"), dict) else {}
    if tool_catalog:
        print(f"tool_config: {tool_catalog.get('config_path')}")
        issue_count = tool_catalog.get("issue_count")
        print(f"tool_catalog: {helpers._count_status(issue_count)}")
        top_tool = tool_catalog.get("top_issue") if isinstance(tool_catalog.get("top_issue"), dict) else None
        if top_tool:
            print(
                "tool_top_issue: "
                f"{top_tool.get('tool_id') or 'catalog'}/{top_tool.get('issue_type')} "
                f"{helpers._short(str(top_tool.get('detail', '')))}"
            )
        call_queue = tool_catalog.get("call_queue") if isinstance(tool_catalog.get("call_queue"), dict) else {}
        if call_queue:
            print(f"tool_call_pending: {call_queue.get('pending_count', 0)}")
            call_top = call_queue.get("top_issue") if isinstance(call_queue.get("top_issue"), dict) else None
            if call_top:
                print(
                    "tool_call_top_issue: "
                    f"{call_top.get('call_id')} {call_top.get('issue_type')} "
                    f"{helpers._short(str(call_top.get('detail', '')))}"
                )
        run_history = tool_catalog.get("run_history") if isinstance(tool_catalog.get("run_history"), dict) else {}
        if run_history:
            print(f"tool_runs: {run_history.get('run_count', 0)}")
            run_top = run_history.get("top_issue") if isinstance(run_history.get("top_issue"), dict) else None
            if run_top:
                print(
                    "tool_run_top_issue: "
                    f"{run_top.get('run_id')} {run_top.get('issue_type')} "
                    f"{helpers._short(str(run_top.get('detail', '')))}"
                )
        checkpoints = tool_catalog.get("checkpoints") if isinstance(tool_catalog.get("checkpoints"), dict) else {}
        if checkpoints:
            print(f"tool_checkpoints: {checkpoints.get('checkpoint_count', 0)}")
            checkpoint_top = checkpoints.get("top_issue") if isinstance(checkpoints.get("top_issue"), dict) else None
            if checkpoint_top:
                print(
                    "tool_checkpoint_top_issue: "
                    f"{checkpoint_top.get('checkpoint_id')} {checkpoint_top.get('issue_type')} "
                    f"{helpers._short(str(checkpoint_top.get('detail', '')))}"
                )

    roadmap_completion = payload.get("roadmap_completion") if isinstance(payload.get("roadmap_completion"), dict) else {}
    if roadmap_completion:
        issue_count = roadmap_completion.get("issue_count")
        print(
            "roadmap_completion: "
            f"{helpers._count_status(issue_count)}"
        )
        top_roadmap = roadmap_completion.get("top_issue") if isinstance(roadmap_completion.get("top_issue"), dict) else None
        if top_roadmap:
            print(f"roadmap_top_issue: {top_roadmap.get('name')} {helpers._short(str(top_roadmap.get('detail', '')))}")

    repo_fleet = payload.get("repo_fleet") if isinstance(payload.get("repo_fleet"), dict) else {}
    if repo_fleet:
        print(f"repo_fleet_config: {repo_fleet.get('config_path')}")
        issue_count = repo_fleet.get("issue_count")
        print(f"repo_fleet: {helpers._count_status(issue_count)}")
        top_repo = repo_fleet.get("top_issue") if isinstance(repo_fleet.get("top_issue"), dict) else None
        if top_repo:
            print(f"repo_fleet_top_issue: {top_repo.get('name')} {helpers._short(str(top_repo.get('detail', '')))}")

    context_packs = payload.get("context_packs") if isinstance(payload.get("context_packs"), dict) else {}
    if context_packs:
        print(f"context_packs: {context_packs.get('pack_count', 0)}")
        if context_packs.get("issue_count"):
            top_context = context_packs.get("top_issue") if isinstance(context_packs.get("top_issue"), dict) else None
            if top_context:
                print(f"context_top_issue: {top_context.get('name')} {helpers._short(str(top_context.get('detail', '')))}")

    project_consolidation = payload.get("project_consolidation") if isinstance(payload.get("project_consolidation"), dict) else {}
    if project_consolidation:
        issue_count = project_consolidation.get("issue_count")
        print(f"project_consolidation: {helpers._count_status(issue_count)}")
        top_project = project_consolidation.get("top_issue") if isinstance(project_consolidation.get("top_issue"), dict) else None
        if top_project:
            print(f"project_consolidation_top_issue: {top_project.get('name')} {helpers._short(str(top_project.get('detail', '')))}")

    learning = payload.get("learning") if isinstance(payload.get("learning"), dict) else {}
    if learning:
        print(f"learning_candidates: {learning.get('candidate_count', 0)}")
        top_learning = learning.get("top_issue") if isinstance(learning.get("top_issue"), dict) else None
        if top_learning:
            print(f"learning_top_issue: {top_learning.get('name')} {helpers._short(str(top_learning.get('detail', '')))}")

    research_handoffs = payload.get("research_handoffs") if isinstance(payload.get("research_handoffs"), dict) else {}
    if research_handoffs:
        print(f"research_handoffs: {helpers._count_status(research_handoffs.get('issue_count'))}")
        top_research = research_handoffs.get("top_issue") if isinstance(research_handoffs.get("top_issue"), dict) else None
        if top_research:
            print(f"research_handoff_top_issue: {top_research.get('run_id')} {top_research.get('status')}")
            if top_research.get("suggested_next_command"):
                print(f"research_handoff_command: {top_research.get('suggested_next_command')}")

    operator_report = payload.get("operator_report") if isinstance(payload.get("operator_report"), dict) else {}
    if operator_report:
        latest_report = operator_report.get("latest") if isinstance(operator_report.get("latest"), dict) else None
        if latest_report:
            print(f"operator_report_latest: {latest_report.get('report_id')} {latest_report.get('created_at')}")
        issue_count = operator_report.get("issue_count")
        print(f"operator_report: {helpers._count_status(issue_count)}")
        top_report = operator_report.get("top_issue") if isinstance(operator_report.get("top_issue"), dict) else None
        if top_report:
            print(f"operator_report_top_issue: {top_report.get('name')} {helpers._short(str(top_report.get('detail', '')))}")
            if top_report.get("suggested_next_command"):
                print(f"operator_report_command: {top_report.get('suggested_next_command')}")

    operator_actions = payload.get("operator_actions") if isinstance(payload.get("operator_actions"), dict) else {}
    if operator_actions:
        print(f"operator_actions: {operator_actions.get('open_count', 0)} open")
        top_action = operator_actions.get("top_action") if isinstance(operator_actions.get("top_action"), dict) else None
        if top_action:
            print(f"operator_action_top: {top_action.get('action_id')} {top_action.get('source_group')} {helpers._short(str(top_action.get('safe_summary', '')))}")
            if top_action.get("suggested_command"):
                print(f"operator_action_command: {top_action.get('suggested_command')}")

    code_review = payload.get("code_review")
    if isinstance(code_review, dict):
        latest_review = code_review.get("latest_run") if isinstance(code_review.get("latest_run"), dict) else None
        if latest_review:
            print(
                f"review_latest: {latest_review.get('run_id')} "
                f"{latest_review.get('reviewer_id')} [{latest_review.get('status')}]"
            )
        unclosed_review = code_review.get("latest_unclosed_run") if isinstance(code_review.get("latest_unclosed_run"), dict) else None
        if unclosed_review:
            print(f"review_unclosed: {unclosed_review.get('run_id')} {unclosed_review.get('reviewer_id')}")
        if code_review.get("pending_finding_count"):
            print(f"review_pending_findings: {code_review.get('pending_finding_count')}")
        if code_review.get("unresolved_finding_count"):
            print(f"review_unresolved_findings: {code_review.get('unresolved_finding_count')}")
        top_review = code_review.get("top_pending_finding") if isinstance(code_review.get("top_pending_finding"), dict) else None
        if not top_review:
            top_review = code_review.get("top_unresolved_finding") if isinstance(code_review.get("top_unresolved_finding"), dict) else None
        if top_review:
            finding_id = top_review.get("id") or top_review.get("import_id")
            print(f"review_top_finding: {finding_id} {helpers._short(str(top_review.get('text', '')))}")
            print(f"review_top_command: brigade work review finding-show {finding_id}")

    handoff_issues = payload.get("handoff_issues")
    if isinstance(handoff_issues, dict) and handoff_issues.get("count"):
        print(f"handoff_ingest_issues_new: {handoff_issues.get('count')}")
        by_category = handoff_issues.get("by_category")
        if isinstance(by_category, dict) and by_category:
            print("handoff_ingest_issues_by_category:")
            for category, count in by_category.items():
                print(f"  {category}: {count}")
    if isinstance(handoff_issues, dict) and handoff_issues.get("known_count"):
        print(f"handoff_ingest_issues_known: {handoff_issues.get('known_count')}")
    handoff_drafts = payload.get("handoff_drafts")
    if isinstance(handoff_drafts, dict):
        counts = handoff_drafts.get("counts") if isinstance(handoff_drafts.get("counts"), dict) else {}
        total = int(counts.get("total", 0) or 0)
        if total:
            print(f"handoff_drafts_pending: {counts.get('pending', 0)}")
            print(f"handoff_drafts_reviewed: {counts.get('reviewed', 0)}")
            latest_ingest = handoff_drafts.get("latest_ingest_run") if isinstance(handoff_drafts.get("latest_ingest_run"), dict) else None
            if latest_ingest:
                print(
                    f"handoff_ingest_latest: {latest_ingest.get('run_id')} "
                    f"completed={latest_ingest.get('completed_at')}"
                )
            top_issue = handoff_drafts.get("top_issue") if isinstance(handoff_drafts.get("top_issue"), dict) else None
            if top_issue:
                print(f"handoff_draft_top_issue: {top_issue.get('name')} {helpers._short(str(top_issue.get('detail', '')))}")
            drafts = handoff_drafts.get("drafts") if isinstance(handoff_drafts.get("drafts"), list) else []
            if drafts:
                first = drafts[0]
                print(f"handoff_draft_next: {first.get('id')} {first.get('status')} {first.get('path')}")
                print(f"handoff_draft_next_command: brigade handoff show {first.get('id')}")

    recent = payload["recent_sessions"]
    if isinstance(recent, list) and recent:
        print("recent_sessions:")
        for item in recent:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("id")
            print(f"  - {item.get('started_at')} [{item.get('status')}] {helpers._short(str(title))}")
    if payload.get("skipped_sessions"):
        print(f"skipped_sessions: {payload['skipped_sessions']}", file=sys.stderr)
    return 0


def tasks(*, target: Path, all_tasks: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    ledger = ledger_mod._read_task_ledger(target)
    task_items = [task for task in ledger["tasks"] if isinstance(task, dict)]
    task_items.sort(key=ledger_mod._task_sort_key)
    if not all_tasks:
        task_items = [task for task in task_items if task.get("status", "pending") == "pending"]

    if json_output:
        print(json.dumps({"tasks_path": str(helpers._tasks_path(target)), "tasks": task_items}, indent=2, sort_keys=True))
        return 0

    print(f"work tasks: {target}")
    print(f"tasks_path: {helpers._tasks_path(target)}")
    if not task_items:
        print("tasks: none")
        return 0
    for task in task_items:
        status_text = task.get("status", "pending")
        summary = ledger_mod._task_summary(task)
        print(
            f"- {task.get('id')} [{status_text}] "
            f"[{summary['type']} {summary['priority']} acceptance={summary['acceptance_count']}] "
            f"{helpers._short(str(task.get('text', '')))}"
        )
        if task.get("source"):
            print(f"  source: {task['source']}")
        if task.get("template"):
            print(f"  template: {task['template']}")
        issue = ledger_mod._task_issue_metadata(task)
        if issue:
            print(f"  issue: {issue.get('url') or issue.get('number')}")
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        if metadata.get("run_path"):
            print(f"  run: {metadata['run_path']}")
        if metadata.get("session_path"):
            print(f"  session: {metadata['session_path']}")
        if task.get("completed_at"):
            print(f"  completed_at: {task['completed_at']}")
    return 0


def task_add(
    *,
    target: Path,
    text: str | None = None,
    from_next: bool = False,
    from_issue: str | None = None,
    task_type: str = "task",
    priority: str = "normal",
    acceptance: list[str] | None = None,
    template: str | None = None,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if template and template not in TASK_TEMPLATES:
        print(f"error: --template must be one of: {', '.join(TASK_TEMPLATES)}", file=sys.stderr)
        return 2
    import_sources = [bool(from_next), bool(from_issue)]
    if sum(import_sources) > 1 or ((from_next or from_issue) and text):
        print("error: pass task text, --from-next, or --from-issue, not more than one", file=sys.stderr)
        return 2
    if task_type not in TASK_TYPES:
        print(f"error: --type must be one of: {', '.join(TASK_TYPES)}", file=sys.stderr)
        return 2
    if priority not in TASK_PRIORITIES:
        print(f"error: --priority must be one of: {', '.join(TASK_PRIORITIES)}", file=sys.stderr)
        return 2
    task_text = (text or "").strip()
    source = "manual"
    dedupe = True
    if from_next:
        next_step, metadata = _latest_run_next_metadata(target)
        if not next_step:
            print("error: no extracted next step is available", file=sys.stderr)
            return 1
        task_text = next_step
        source = "latest_dogfood_run"
    elif from_issue:
        issue_ref = from_issue.strip()
        if not issue_ref:
            print("error: --from-issue requires an issue URL or number", file=sys.stderr)
            return 2
        issue, issue_acceptance, error = ledger_mod._read_github_issue(target, issue_ref)
        if issue is None:
            print(f"error: could not read GitHub issue {issue_ref}: {error}", file=sys.stderr)
            return 1
        task_text = str(issue["title"]).strip()
        source = "github_issue"
        metadata = {"github_issue": issue}
        acceptance = [*issue_acceptance, *(acceptance or [])]
        dedupe = False
    else:
        metadata = None
    if not task_text:
        print("error: task text is required", file=sys.stderr)
        return 2
    task, created = ledger_mod._add_task(
        target,
        task_text,
        source=source,
        metadata=metadata,
        task_type=task_type,
        priority=priority,
        acceptance=ledger_mod._combined_acceptance(template, acceptance),
        template=template,
        dedupe=dedupe,
    )
    print(f"task: {task['id']}")
    print(f"status: {task['status']}")
    print(f"created: {created}")
    print(f"type: {ledger_mod._normalize_task_type(task.get('type'))}")
    print(f"priority: {ledger_mod._normalize_task_priority(task.get('priority'))}")
    if task.get("template"):
        print(f"template: {task['template']}")
    criteria = ledger_mod._task_acceptance(task)
    print(f"acceptance: {len(criteria)}")
    issue = ledger_mod._task_issue_metadata(task)
    if issue:
        print(f"issue: {issue.get('url') or issue.get('number')}")
    print(f"text: {task['text']}")
    return 0


def task_show(*, target: Path, task_id: str) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    task, _ = ledger_mod._find_task(target, task_id)
    if task is None:
        print(f"error: task not found: {task_id}", file=sys.stderr)
        return 1
    print(f"task: {task.get('id')}")
    print(f"status: {task.get('status', 'pending')}")
    print(f"source: {task.get('source', '')}")
    print(f"type: {ledger_mod._normalize_task_type(task.get('type'))}")
    print(f"priority: {ledger_mod._normalize_task_priority(task.get('priority'))}")
    if task.get("template"):
        print(f"template: {task['template']}")
    print(f"created_at: {task.get('created_at', '')}")
    print(f"updated_at: {task.get('updated_at', '')}")
    criteria = ledger_mod._task_acceptance(task)
    print(f"acceptance: {len(criteria)}")
    for item in criteria:
        print(f"  - {item}")
    issue = ledger_mod._task_issue_metadata(task)
    if issue:
        print("issue:")
        print(f"  url: {issue.get('url', '')}")
        print(f"  number: {issue.get('number', '')}")
        print(f"  title: {issue.get('title', '')}")
        print(f"  state: {issue.get('state', '')}")
        labels = issue.get("labels")
        if isinstance(labels, list) and labels:
            print(f"  labels: {', '.join(str(label) for label in labels)}")
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    if metadata:
        print("metadata:")
        for key in sorted(metadata):
            print(f"  {key}: {metadata[key]}")
    closeouts = metadata.get("review_closeouts")
    if isinstance(closeouts, list) and closeouts:
        print(f"review_closeouts: {len(closeouts)}")
        for item in closeouts:
            if not isinstance(item, dict):
                continue
            print(
                "  - "
                f"{item.get('review_run_id')} "
                f"resolved={item.get('resolved')} "
                f"findings={item.get('finding_count')} "
                f"unresolved={item.get('unresolved_count')}"
            )
    if task.get("completed_at"):
        print(f"completed_at: {task['completed_at']}")
    if task.get("completed_session_title"):
        print(f"completed_session_title: {task['completed_session_title']}")
    if task.get("completed_session_path"):
        print(f"completed_session_path: {task['completed_session_path']}")
    if task.get("completed_run_path"):
        print(f"completed_run_path: {task['completed_run_path']}")
    completed_acceptance = task.get("completed_acceptance")
    if isinstance(completed_acceptance, list):
        print(f"completed_acceptance: {len(completed_acceptance)}")
        for item in completed_acceptance:
            print(f"  - {item}")
    print(f"text: {task.get('text', '')}")
    return 0




















def task_plan(
    *,
    target: Path,
    task_id: str,
    json_output: bool = False,
    write: bool = False,
    title: str | None = None,
    assumptions: list[str] | None = None,
    risks: list[str] | None = None,
    sources: list[str] | None = None,
    next_command: str | None = None,
    accept: bool = False,
    kind: str = "plan",
    steps: list[str] | None = None,
    from_research: str | None = None,
) -> int:
    if write:
        return ledger_mod._write_plan_artifact(
            target=target,
            task_id=task_id,
            title=title,
            assumptions=assumptions,
            risks=risks,
            sources=sources,
            next_command=next_command,
            accept=accept,
            json_output=json_output,
            kind=kind,
            steps=steps,
            from_research=from_research,
        )
    payload, rc = _task_plan_payload(target, task_id)
    if payload is None:
        return rc
    resolved_target = target.expanduser().resolve()
    resolved_id = str(payload.get("id") or task_id)
    artifact = ledger_mod._plan_artifact_summary(resolved_target, resolved_id)
    meta_artifact = ledger_mod._plan_artifact_summary(resolved_target, resolved_id, kind="meta")
    payload["plan_artifact"] = artifact
    payload["meta_artifact"] = meta_artifact
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"task: {payload['id']}")
    print(f"type: {payload['type']}")
    print(f"priority: {payload['priority']}")
    if payload.get("template"):
        print(f"template: {payload['template']}")
    print(f"status: {payload['status']}")
    print(f"source: {payload['source']}")
    print(f"text: {payload['text']}")
    if payload.get("issue"):
        issue = payload["issue"]
        print("issue:")
        print(f"  url: {issue.get('url', '')}")
        print(f"  number: {issue.get('number', '')}")
        print(f"  title: {issue.get('title', '')}")
        print(f"  state: {issue.get('state', '')}")
        labels = issue.get("labels")
        if isinstance(labels, list) and labels:
            print(f"  labels: {', '.join(str(label) for label in labels)}")
    if payload.get("guidance"):
        print("guidance:")
        for item in payload["guidance"]:
            print(f"  - {item}")
    print("acceptance:")
    if payload["acceptance"]:
        for item in payload["acceptance"]:
            print(f"  - {item}")
    else:
        print("  missing")
    print(f"suggested_command: {payload['suggested_command']}")
    if artifact is None:
        print("plan_artifact: none")
    else:
        print(f"plan_artifact: {artifact['status']} ({artifact['path']})")
    if meta_artifact is None:
        print("meta_artifact: none")
    else:
        print(f"meta_artifact: {meta_artifact['status']} ({meta_artifact['path']})")
    return 0


def task_done(*, target: Path, task_id: str) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    task, ledger = ledger_mod._find_task(target, task_id)
    if task is None:
        print(f"error: task not found: {task_id}", file=sys.stderr)
        return 1
    now = helpers._now().isoformat()
    task["status"] = "done"
    task["updated_at"] = now
    task["completed_at"] = now
    ledger_mod._write_task_ledger(target, ledger)
    print(f"task: {task.get('id')}")
    print("status: done")
    return 0


































































































































































































def next(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    if json_output:
        print(json.dumps(_next_payload(target), indent=2, sort_keys=True))
        return 0

    print(f"work next: {target}")
    payload = _next_payload(target)
    active = payload["active_session"]
    if isinstance(active, dict):
        if not active.get("valid"):
            print(f"active_session: invalid ({active.get('path')})")
        else:
            print(f"active_session: {active.get('path')}")
            print(f"active_session_status: {active.get('status')}")
            if active.get("title"):
                print(f"active_session_title: {helpers._short(str(active['title']))}")
    else:
        print("active_session: none")

    dogfood = payload["dogfood"]
    print(f"dogfood_ready: {dogfood.get('ready')}")
    if dogfood.get("error"):
        print(f"dogfood_error: {dogfood['error']}")
    latest_run = dogfood.get("latest_run")
    if isinstance(latest_run, dict):
        print(
            "latest_run: "
            f"{latest_run.get('started_at', '')} "
            f"[{latest_run.get('status', 'unknown')}] {latest_run.get('path')}"
        )
        if latest_run.get("task"):
            print(f"latest_task: {helpers._short(str(latest_run['task']))}")
    else:
        print("latest_run: none")

    task = str(payload["next"])
    print(f"next_source: {payload['next_source']}")
    if payload.get("task_id"):
        print(f"task_id: {payload['task_id']}")
    print(f"next: {helpers._short(task)}")
    print(f"suggested_command: {payload['suggested_command']}")
    return 0


def bootstrap(
    *,
    target: Path,
    artifacts_dir: Path | None = None,
    handoff_inbox: Path | None = None,
    force: bool = False,
    handoff: bool = True,
    inspect: bool = True,
    native_read_only_sandbox: bool = False,
    timeout_seconds: float = dogfood_cmd.DEFAULT_TIMEOUT_SECONDS,
    update_gitignore: bool = True,
) -> int:
    if timeout_seconds <= 0:
        print("error: --timeout-seconds must be positive", file=sys.stderr)
        return 2

    target = target.expanduser().resolve()
    print(f"work bootstrap: {target}")
    if not target.is_dir():
        _print_bootstrap_line(FAIL, "target", f"not a directory: {target}")
        return 2
    _print_bootstrap_line(OK, "target", target)

    failures = 0
    repo_root = helpers._git_value(target, "rev-parse", "--show-toplevel")
    if repo_root is None:
        failures += 1
        _print_bootstrap_line(FAIL, "git", "not a git repository")
    else:
        _print_bootstrap_line(OK, "git", repo_root)

    config = dogfood_cmd.config_path(target)
    if config.exists() and not force:
        _print_bootstrap_line(OK, "dogfood_config", f"exists at {config}")
    else:
        rc = dogfood_cmd.init(
            target=target,
            artifacts_dir=artifacts_dir,
            handoff_inbox=handoff_inbox,
            force=force,
            handoff=handoff,
            inspect=inspect,
            native_read_only_sandbox=native_read_only_sandbox,
            timeout_seconds=timeout_seconds,
        )
        if rc != 0:
            failures += 1
            _print_bootstrap_line(FAIL, "dogfood_config", f"init failed with exit code {rc}")
        else:
            _print_bootstrap_line(OK, "dogfood_config", config)

    try:
        effective_target, effective_artifacts_dir, cfg = dogfood_cmd._load_effective_paths(target)
    except (FileNotFoundError, ValueError) as exc:
        failures += 1
        effective_target = target
        effective_artifacts_dir = artifacts_dir or (target / ".brigade" / "runs")
        cfg = None
        _print_bootstrap_line(FAIL, "dogfood_paths", exc)
    else:
        _print_bootstrap_line(OK, "dogfood_target", effective_target)
        _print_bootstrap_line(OK, "dogfood_artifacts", effective_artifacts_dir)

    work_root = helpers._work_root(effective_target)
    effective_artifacts_dir.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)
    _print_bootstrap_line(OK, "artifacts_dir", effective_artifacts_dir)
    _print_bootstrap_line(OK, "work_root", work_root)

    effective_handoff = cfg.handoff if cfg is not None else handoff
    effective_handoff_inbox = (
        cfg.handoff_inbox
        if cfg is not None and cfg.handoff_inbox is not None
        else handoff_inbox.expanduser()
        if handoff_inbox is not None
        else dogfood_cmd.default_handoff_inbox(effective_target)
    )
    if effective_handoff:
        effective_handoff_inbox.mkdir(parents=True, exist_ok=True)
        _print_bootstrap_line(OK, "handoff_inbox", effective_handoff_inbox)
    else:
        _print_bootstrap_line(WARN, "handoff_inbox", "handoff disabled")

    if update_gitignore:
        result = apply_gitignore(effective_target, helpers._work_selection(effective_target, effective_handoff_inbox))
        _print_bootstrap_line(OK, "gitignore", result)
    else:
        _print_bootstrap_line(WARN, "gitignore", "skipped")

    codex_path = shutil.which("codex")
    if codex_path is None:
        failures += 1
        _print_bootstrap_line(FAIL, "codex", "missing on PATH")
    else:
        _print_bootstrap_line(OK, "codex", codex_path)

    config_ignored = dogfood_cmd._check_git_ignored(effective_target, config)
    artifacts_ignored = dogfood_cmd._check_git_ignored(effective_target, effective_artifacts_dir)
    work_ignored = dogfood_cmd._check_git_ignored(effective_target, work_root)
    handoff_ignored = (
        dogfood_cmd._check_git_ignored(effective_target, effective_handoff_inbox)
        if effective_handoff
        else "disabled"
    )
    ignore_values = {
        "config_ignored": config_ignored,
        "artifacts_ignored": artifacts_ignored,
        "work_ignored": work_ignored,
        "handoff_ignored": handoff_ignored,
    }
    for name, value in ignore_values.items():
        level = OK if value in {"yes", "outside-target", "disabled"} else WARN
        _print_bootstrap_line(level, name, value)

    ready = failures == 0
    _print_bootstrap_line(OK if ready else FAIL, "ready", "daily work loop is usable" if ready else f"{failures} blocker{'s' if failures != 1 else ''}")
    print("next_command: brigade work run")
    return 0 if ready else 1


def run(
    task: str | None,
    *,
    target: Path,
    task_id: str | None = None,
    title: str | None = None,
    output_dir: Path | None = None,
    handoff: bool = True,
    handoff_inbox: Path | None = None,
    dogfood_handoff: bool = False,
    inspect: bool = True,
    native_read_only_sandbox: bool = False,
    timeout_seconds: float = dogfood_cmd.DEFAULT_TIMEOUT_SECONDS,
    recap_limit: int = 1,
    queue_next: bool = False,
) -> int:
    if recap_limit < 1:
        print("error: --recap-limit must be a positive integer", file=sys.stderr)
        return 2

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    resolved = _resolve_next_task(target)
    if task_id is not None:
        if task:
            print("error: pass a task or task_id, not both", file=sys.stderr)
            return 2
        selected_task, _ = ledger_mod._find_task(target, task_id)
        if selected_task is None or selected_task.get("status", "pending") != "pending":
            print(f"error: pending task not found: {task_id}", file=sys.stderr)
            return 1
        resolved = {
            "task": str(selected_task.get("text", "")).strip(),
            "source": "task_ledger",
            "task_id": selected_task.get("id"),
            "ledger_task": selected_task,
            "dogfood": helpers._dogfood_snapshot(target),
        }
    task_text = task or str(resolved["task"])
    consumed_task_id = resolved.get("task_id") if task is None and resolved.get("source") == "task_ledger" else None
    ledger_task = resolved.get("ledger_task") if consumed_task_id and isinstance(resolved.get("ledger_task"), dict) else None
    run_task_text = (
        _render_task_run_prompt(ledger_task)
        if ledger_task is not None and ledger_mod._task_acceptance(ledger_task)
        else task_text
    )
    task_snapshot = ledger_mod._task_snapshot(ledger_task) if ledger_task is not None else None
    session_title = title or task_text
    start_rc = start(target=target, title=session_title, task_snapshot=task_snapshot)
    if start_rc != 0:
        return start_rc
    session_dir = helpers._active_session_dir(target)

    dogfood_rc = 1
    try:
        dogfood_rc = dogfood_cmd.run(
            run_task_text,
            target=target,
            output_dir=output_dir,
            handoff=dogfood_handoff,
            handoff_inbox=handoff_inbox if dogfood_handoff else None,
            inspect=inspect,
            native_read_only_sandbox=native_read_only_sandbox,
            timeout_seconds=timeout_seconds,
        )
    finally:
        note = f"brigade work run completed with dogfood exit code {dogfood_rc}"
        end_rc = end(target=target, note=note, handoff=handoff, handoff_inbox=handoff_inbox)

    if end_rc != 0:
        return end_rc if dogfood_rc == 0 else dogfood_rc
    if dogfood_rc == 0 and isinstance(consumed_task_id, str):
        task, ledger = ledger_mod._find_task(target, consumed_task_id)
        if task is not None:
            now = helpers._now().isoformat()
            task["status"] = "done"
            task["updated_at"] = now
            task["completed_at"] = now
            task["completed_session_title"] = session_title
            if session_dir is not None:
                task["completed_session_path"] = str(session_dir)
            completed_run_path = _latest_completed_run_path(target, output_dir)
            if completed_run_path is not None:
                task["completed_run_path"] = completed_run_path
            task["completed_acceptance"] = ledger_mod._task_acceptance(task)
            ledger_mod._write_task_ledger(target, ledger)
    if dogfood_rc == 0 and queue_next:
        queued_task, created, reason = _queue_latest_next(
            target,
            session_dir=session_dir,
            session_title=session_title,
        )
        if queued_task is None:
            print(f"queued_next: skipped ({reason})")
        else:
            print(f"queued_next: {queued_task.get('id')} ({'created' if created else 'existing'})")
    recap(target=target, limit=recap_limit)
    return dogfood_rc


def status(*, target: Path, limit: int = 12) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    print(f"work: {target}")
    repo_root = helpers._git_value(target, "rev-parse", "--show-toplevel")
    if repo_root is None:
        print("git: unavailable")
    else:
        print(f"repo: {repo_root}")
        branch = helpers._git_value(target, "branch", "--show-current")
        if branch is None:
            branch = helpers._git_value(target, "rev-parse", "--short", "HEAD") or "unknown"
            branch = f"detached:{branch}"
        print(f"branch: {branch}")
        status_out = helpers._git_value(target, "status", "--short") or ""
        _print_dirty(status_out.splitlines(), limit=limit)

    try:
        effective_target, artifacts_dir, cfg = dogfood_cmd._load_effective_paths(target)
    except (FileNotFoundError, ValueError) as exc:
        print(f"dogfood: not ready ({exc})")
        return 0

    config = dogfood_cmd.config_path(target)
    codex_path = shutil.which("codex")
    dogfood_ready = config.exists() and codex_path is not None and effective_target.is_dir()
    print(f"dogfood: {'ready' if dogfood_ready else 'not ready'}")
    print(f"dogfood_config: {config if config.exists() else str(config) + ' (missing)'}")
    print(f"dogfood_target: {effective_target}")
    print(f"dogfood_artifacts: {artifacts_dir}")
    print(f"codex: {codex_path or 'missing'}")
    if cfg and cfg.handoff:
        handoff_inbox = cfg.handoff_inbox or dogfood_cmd.default_handoff_inbox(effective_target)
        print(f"handoff_inbox: {handoff_inbox}")

    latest = dogfood_cmd._latest_run(artifacts_dir)
    if latest is None:
        print("latest_run: none")
        print("next: none")
        return 0

    latest_path, latest_meta = latest
    print(
        "latest_run: "
        f"{latest_meta.get('started_at', latest_path.name)} "
        f"[{latest_meta.get('status', 'unknown')}] {latest_path}"
    )
    task = helpers._short(str(latest_meta.get("task") or ""))
    if task:
        print(f"latest_task: {task}")
    next_step = dogfood_cmd.extract_next_step(dogfood_cmd._read_final(latest_path))
    print(f"next: {helpers._short(next_step) if next_step else 'none'}")
    print("next_command: brigade dogfood next")
    print("inspect_command: brigade dogfood latest")
    return 0


def doctor(*, target: Path) -> int:
    from .. import center_cmd, chat_cmd, context_cmd, daily_cmd, handoff_cmd, learn_cmd, memory_cmd, phases_cmd, projects_cmd, repos_cmd, roadmap_cmd, security_cmd, tools_cmd

    target = target.expanduser().resolve()
    failures = 0

    print(f"work doctor: {target}")
    if not target.is_dir():
        helpers._doctor_line(FAIL, "target", f"not a directory: {target}")
        return 2
    helpers._doctor_line(OK, "target", target)

    repo_root = helpers._git_value(target, "rev-parse", "--show-toplevel")
    if repo_root is None:
        failures += 1
        helpers._doctor_line(FAIL, "git", "not a git repository")
    else:
        helpers._doctor_line(OK, "git", repo_root)

    config = dogfood_cmd.config_path(target)
    try:
        effective_target, artifacts_dir, cfg = dogfood_cmd._load_effective_paths(target)
    except (FileNotFoundError, ValueError) as exc:
        failures += 1
        helpers._doctor_line(FAIL, "dogfood_config", exc)
        effective_target = target
        artifacts_dir = target / ".brigade" / "runs"
        cfg = None
    else:
        if config.is_file():
            helpers._doctor_line(OK, "dogfood_config", config)
        else:
            failures += 1
            helpers._doctor_line(FAIL, "dogfood_config", f"missing, run `brigade dogfood init --target {target}`")
        helpers._doctor_line(OK, "dogfood_target", effective_target)
        helpers._doctor_line(OK, "dogfood_artifacts", artifacts_dir)

    security_config = security_cmd.config_path(effective_target)
    security_config_valid = True
    if security_config.is_file():
        try:
            loaded_security = security_cmd.load_config(effective_target)
        except ValueError as exc:
            security_config_valid = False
            failures += 1
            helpers._doctor_line(FAIL, "security_config", f"invalid {security_config}: {exc}")
        else:
            policy = loaded_security.policy if loaded_security is not None else "personal"
            helpers._doctor_line(OK, "security_config", f"{security_config} (policy={policy})")
            enrichment = security_cmd.enrichment_health(effective_target)
            helpers._doctor_line(
                OK if enrichment.get("configured") else WARN,
                "security_enrichment",
                f"{enrichment.get('provider') or 'none'} ({enrichment.get('status')})",
            )
    else:
        helpers._doctor_line(WARN, "security_config", f"missing, run `brigade security init --target {effective_target}`")

    if security_config_valid:
        try:
            suppression_health = security_cmd.suppression_health(effective_target)
        except ValueError as exc:
            failures += 1
            helpers._doctor_line(FAIL, "security_suppressions", f"invalid: {exc}")
        else:
            stale = suppression_health["stale"]
            missing_reasons = suppression_health["missing_reasons"]
            if stale:
                helpers._doctor_line(WARN, "security_stale_suppressions", f"{len(stale)} no longer match current findings: {', '.join(stale[:5])}")
            if missing_reasons:
                helpers._doctor_line(WARN, "security_suppression_reasons", f"{len(missing_reasons)} missing reason: {', '.join(missing_reasons[:5])}")
            if not stale and not missing_reasons:
                helpers._doctor_line(OK, "security_suppressions", f"{suppression_health['suppression_count']} configured")

    security_artifacts = security_cmd.default_artifacts_dir(effective_target)
    security_bundle = security_cmd.inspect_evidence_bundle(security_artifacts)
    if security_bundle.get("ready"):
        helpers._doctor_line(
            OK,
            "security_evidence",
            f"{security_artifacts} "
            f"(generated_at={security_bundle.get('generated_at')}, findings={security_bundle.get('finding_count')})",
        )
    else:
        helpers._doctor_line(
            WARN,
            "security_evidence",
            f"{security_bundle.get('reason')}; run `brigade security scan --target {effective_target} --output-dir {security_artifacts}`",
        )
    security_health = security_cmd.health(effective_target)
    open_finding_check = None
    for check in security_health["checks"]:
        if check.get("name") == "security_open_findings":
            open_finding_check = check
            break
    if open_finding_check is not None:
        helpers._doctor_line(str(open_finding_check.get("status")), "security_open_findings", open_finding_check.get("detail"))

    codex_path = shutil.which("codex")
    if codex_path is None:
        failures += 1
        helpers._doctor_line(FAIL, "codex", "missing on PATH")
    else:
        helpers._doctor_line(OK, "codex", codex_path)

    work_root = helpers._work_root(effective_target)
    helpers._doctor_line(OK if work_root.parent.exists() else WARN, "work_root", work_root)
    current = helpers._current_path(effective_target)
    if current.exists():
        active_dir = work_root / current.read_text().strip()
        active_payload = helpers._read_session(active_dir)
        if active_payload is None:
            failures += 1
            helpers._doctor_line(FAIL, "active_session", f"invalid: {active_dir}")
        else:
            helpers._doctor_line(WARN, "active_session", f"active: {active_dir}")
            started = helpers._parse_iso_datetime(active_payload.get("started_at"))
            if started is not None:
                age_hours = (helpers._now() - started).total_seconds() / 3600
                if age_hours > ACTIVE_SESSION_STALE_HOURS:
                    helpers._doctor_line(
                        WARN,
                        "active_session_age",
                        f"open for {age_hours:.1f} hours, close or resume it",
                    )
    else:
        helpers._doctor_line(OK, "active_session", "none")

    pending_tasks = ledger_mod._pending_tasks(effective_target)
    missing_acceptance = [task for task in pending_tasks if not ledger_mod._task_acceptance(task)]
    if missing_acceptance:
        sample = ", ".join(str(task.get("id")) for task in missing_acceptance[:5])
        helpers._doctor_line(WARN, "task_acceptance", f"{len(missing_acceptance)} pending task(s) missing acceptance criteria: {sample}")
    else:
        helpers._doctor_line(OK, "task_acceptance", "pending tasks have acceptance criteria or no tasks are pending")

    plan_coverage = ledger_mod._plan_coverage_payload(effective_target)
    if plan_coverage["significant_without_plan"] > 0:
        plan_sample = ", ".join(plan_coverage["task_ids"][:5])
        helpers._doctor_line(
            WARN,
            "plan_coverage",
            f"{plan_coverage['significant_without_plan']} significant pending task(s) without a plan artifact: {plan_sample}",
        )
    else:
        helpers._doctor_line(OK, "plan_coverage", "significant pending tasks have plan artifacts")

    workflow_rules = _workflow_rule_health(effective_target)
    helpers._doctor_line(str(workflow_rules["status"]), str(workflow_rules["name"]), workflow_rules["detail"])

    issue_tasks = [(task, issue) for task in pending_tasks if (issue := ledger_mod._task_issue_metadata(task))]
    if issue_tasks:
        gh_path = shutil.which("gh")
        if gh_path is None:
            sample = ", ".join(str(task.get("id")) for task, _ in issue_tasks[:5])
            helpers._doctor_line(WARN, "github_issues", f"{len(issue_tasks)} issue-backed task(s) cannot be checked because gh is missing: {sample}")
        else:
            closed: list[str] = []
            unchecked: list[str] = []
            for task, issue in issue_tasks:
                issue_ref = ledger_mod._github_issue_ref(issue)
                if issue_ref is None:
                    unchecked.append(str(task.get("id")))
                    continue
                remote_issue, _, error = ledger_mod._read_github_issue(effective_target, issue_ref)
                if remote_issue is None:
                    unchecked.append(f"{task.get('id')} ({error})")
                    continue
                state = str(remote_issue.get("state") or "").lower()
                if state == "closed":
                    closed.append(str(task.get("id")))
            if closed:
                helpers._doctor_line(WARN, "github_issues_closed", f"{len(closed)} remote issue(s) are closed: {', '.join(closed[:5])}")
            if unchecked:
                helpers._doctor_line(WARN, "github_issues_unchecked", f"{len(unchecked)} issue-backed task(s) could not be checked: {', '.join(unchecked[:5])}")
            if not closed and not unchecked:
                helpers._doctor_line(OK, "github_issues", f"{len(issue_tasks)} issue-backed task(s) checked")
    else:
        helpers._doctor_line(OK, "github_issues", "none")

    pending_imports = ledger_mod._pending_imports(effective_target)
    now = helpers._now()
    stale_imports = [
        item
        for item in pending_imports
        if (created := helpers._parse_iso_datetime(item.get("created_at"))) is not None
        and (now - created).total_seconds() / 3600 > IMPORT_STALE_HOURS
    ]
    if stale_imports:
        sample = ", ".join(str(item.get("id")) for item in stale_imports[:5])
        helpers._doctor_line(WARN, "scanner_imports_stale", f"{len(stale_imports)} pending import(s) older than {IMPORT_STALE_HOURS}h: {sample}")
    else:
        helpers._doctor_line(OK, "scanner_imports_stale", "none")
    task_imports_missing_acceptance = [
        item
        for item in pending_imports
        if item.get("kind") == "task" and not ledger_mod._import_task_acceptance(item)
    ]
    if task_imports_missing_acceptance:
        sample = ", ".join(str(item.get("id")) for item in task_imports_missing_acceptance[:5])
        helpers._doctor_line(WARN, "scanner_import_acceptance", f"{len(task_imports_missing_acceptance)} pending task import(s) missing acceptance criteria: {sample}")
    else:
        helpers._doctor_line(OK, "scanner_import_acceptance", "pending task imports have acceptance criteria or no task imports are pending")
    dismissed_by_source: dict[str, int] = {}
    for item in ledger_mod._read_imports(effective_target):
        if not isinstance(item, dict) or item.get("status") != "dismissed":
            continue
        source = str(item.get("source") or "manual")
        dismissed_by_source[source] = dismissed_by_source.get(source, 0) + 1
    noisy_sources = {
        source: count
        for source, count in dismissed_by_source.items()
        if count >= DISMISSED_SOURCE_WARN_THRESHOLD
    }
    if noisy_sources:
        detail = ", ".join(f"{source}={count}" for source, count in sorted(noisy_sources.items()))
        helpers._doctor_line(WARN, "scanner_import_noise", f"dismissed import threshold {DISMISSED_SOURCE_WARN_THRESHOLD}: {detail}")
    else:
        helpers._doctor_line(OK, "scanner_import_noise", "none")

    inbox_hygiene = services_mod._inbox_hygiene_payload(effective_target)
    for check in inbox_hygiene["checks"]:
        if check.get("status") != OK:
            helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))

    scanner_health = services_mod._scanner_health(effective_target)
    for check in scanner_health["checks"]:
        if check.get("status") == FAIL:
            failures += 1
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))

    sweep_health = services_mod._scanner_sweep_health(effective_target)
    for check in sweep_health["checks"]:
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))

    review_health = services_mod._review_health(effective_target)
    for check in review_health["checks"]:
        if check.get("status") == FAIL:
            failures += 1
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))

    chat_health = chat_cmd.health(effective_target)
    for check in chat_health["checks"]:
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))

    memory_health = memory_cmd.health(effective_target)
    for check in memory_health["checks"]:
        if check.get("status") == FAIL:
            failures += 1
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))

    backup_health = config_mod._backup_health(effective_target)
    for check in backup_health.get("active_checks", backup_health["checks"]):
        if check.get("status") == FAIL:
            failures += 1
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))

    tool_health = tools_cmd.health(effective_target)
    if tool_health["issues"]:
        for issue in tool_health["issues"]:
            if issue.get("status") == FAIL:
                failures += 1
            helpers._doctor_line(str(issue.get("status")), str(issue.get("name")), issue.get("detail"))
    else:
        helpers._doctor_line(OK, "tool_catalog", f"{tool_health['tool_count']} configured")

    roadmap_health = roadmap_cmd.health(effective_target)
    for issue in roadmap_health["checks"]:
        helpers._doctor_line(str(issue.get("status")), str(issue.get("name")), issue.get("detail"))

    repo_health = repos_cmd.health(effective_target)
    for check in repo_health["checks"]:
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    for bucket in (repo_health.get("report"), repo_health.get("actions")):
        if isinstance(bucket, dict):
            for check in bucket.get("checks", []):
                helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    sweep_bucket = repo_health.get("sweep")
    if isinstance(sweep_bucket, dict):
        for check in sweep_bucket.get("checks", []):
            helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    release_bucket = repo_health.get("release_train")
    if isinstance(release_bucket, dict):
        for check in release_bucket.get("checks", []):
            helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))

    context_health = context_cmd.health(effective_target)
    for issue in context_health.get("issues", []):
        helpers._doctor_line(str(issue.get("status")), str(issue.get("name")), issue.get("detail"))
    if not context_health.get("issues"):
        helpers._doctor_line(OK, "context_packs", f"{context_health.get('pack_count', 0)} local pack(s)")

    projects_health = projects_cmd.health(effective_target)
    for check in projects_health.get("checks", []):
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))

    learning_health = learn_cmd.health(effective_target)
    if learning_health.get("issue_count"):
        top_learning = learning_health.get("top_issue") if isinstance(learning_health.get("top_issue"), dict) else {}
        helpers._doctor_line(WARN, "learning_candidates", top_learning.get("detail") or f"{learning_health.get('candidate_count', 0)} candidate(s)")
    else:
        helpers._doctor_line(OK, "learning_candidates", "none")

    center_report_health = center_cmd.report_health(effective_target)
    for check in center_report_health.get("checks", []):
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    if not center_report_health.get("checks"):
        latest_report = center_report_health.get("latest") if isinstance(center_report_health.get("latest"), dict) else {}
        helpers._doctor_line(OK, "operator_report", latest_report.get("report_id") or "none")

    center_actions_health = center_cmd.actions_health(effective_target)
    for check in center_actions_health.get("checks", []):
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    if not center_actions_health.get("checks"):
        helpers._doctor_line(OK, "operator_actions", f"{center_actions_health.get('action_count', 0)} action(s)")

    daily_health = daily_cmd.health(effective_target)
    for check in daily_health.get("checks", []):
        if check.get("status") == FAIL:
            failures += 1
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    if not daily_health.get("issue_count"):
        helpers._doctor_line(OK, "daily_driver", f"{daily_health.get('run_count', 0)} run(s)")

    phase_health = phases_cmd.health(effective_target)
    for check in phase_health.get("checks", []):
        if check.get("status") == FAIL:
            failures += 1
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    if not phase_health.get("issue_count"):
        helpers._doctor_line(OK, "phase_ledger", f"{phase_health.get('record_count', 0)} record(s)")

    handoff_inbox = (
        cfg.handoff_inbox
        if cfg and cfg.handoff_inbox is not None
        else dogfood_cmd.default_handoff_inbox(effective_target)
    )
    helpers._doctor_line(OK if handoff_inbox.parent.exists() else WARN, "handoff_inbox", handoff_inbox)

    config_ignored = dogfood_cmd._check_git_ignored(effective_target, config)
    helpers._doctor_line(_doctor_ignore_level(config_ignored), "config_ignored", config_ignored)
    artifacts_ignored = dogfood_cmd._check_git_ignored(effective_target, artifacts_dir)
    helpers._doctor_line(_doctor_ignore_level(artifacts_ignored), "artifacts_ignored", artifacts_ignored)
    security_ignored = dogfood_cmd._check_git_ignored(effective_target, security_artifacts)
    helpers._doctor_line(_doctor_ignore_level(security_ignored), "security_ignored", security_ignored)
    backup_config_ignored = dogfood_cmd._check_git_ignored(effective_target, helpers._backup_config_path(effective_target))
    helpers._doctor_line(_doctor_ignore_level(backup_config_ignored), "backup_config_ignored", backup_config_ignored)
    scanner_config_ignored = dogfood_cmd._check_git_ignored(effective_target, helpers._scanner_config_path(effective_target))
    helpers._doctor_line(_doctor_ignore_level(scanner_config_ignored), "scanner_config_ignored", scanner_config_ignored)
    tools_config_ignored = dogfood_cmd._check_git_ignored(effective_target, tools_cmd.config_path(effective_target))
    helpers._doctor_line(_doctor_ignore_level(tools_config_ignored), "tools_config_ignored", tools_config_ignored)
    work_ignored = dogfood_cmd._check_git_ignored(effective_target, work_root)
    helpers._doctor_line(_doctor_ignore_level(work_ignored), "work_ignored", work_ignored)
    handoff_ignored = dogfood_cmd._check_git_ignored(effective_target, handoff_inbox)
    helpers._doctor_line(_doctor_ignore_level(handoff_ignored), "handoff_ignored", handoff_ignored)

    for status, name, detail in handoff_cmd.doctor_checks(effective_target):
        if status == FAIL:
            failures += 1
        helpers._doctor_line(status, name, detail)

    latest = dogfood_cmd._latest_run(artifacts_dir)
    if latest is None:
        helpers._doctor_line(WARN, "latest_run", "none")
    else:
        latest_path, latest_meta = latest
        helpers._doctor_line(OK, "latest_run", f"{latest_meta.get('started_at', latest_path.name)} {latest_path}")
        next_step = dogfood_cmd.extract_next_step(dogfood_cmd._read_final(latest_path))
        helpers._doctor_line(OK if next_step else WARN, "latest_next", helpers._short(next_step) if next_step else "none")

    if failures:
        helpers._doctor_line(FAIL, "ready", f"{failures} blocker{'s' if failures != 1 else ''}")
        return 1
    helpers._doctor_line(OK, "ready", "daily work loop is usable")
    return 0
