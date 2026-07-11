"""Handoff health checks shared by CLI doctors."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import json
import hashlib
import re
import sys
import time
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import scrub
from ..budgets import HANDOFF_BACKLOG_STALE_SECONDS
from ..config import load_config as load_brigade_config
from ..localio import write_json as _write_json
from ..selection import WRITER_INBOXES as _WRITER_INBOX_MAP

from . import models as _family_base

globals().update({name: value for name, value in vars(_family_base).items() if not name.startswith("__")})


def _handoff_state_root(target: Path) -> Path:
    return target / ".brigade" / "handoffs"


def _handoff_archive_root(target: Path) -> Path:
    return _handoff_state_root(target) / "archive"


def _handoff_archive_records_path(target: Path) -> Path:
    return _handoff_state_root(target) / "archive.jsonl"


def _handoff_ingest_runs_root(target: Path) -> Path:
    return _handoff_state_root(target) / "ingest-runs"


def _handoff_closeouts_root(target: Path) -> Path:
    return _handoff_state_root(target) / "closeouts"


def _load_source_config_for_drafts(
    target: Path, sources: Path | None = None
) -> tuple[SourceConfig, Path | None, list[str], bool]:
    target = target.expanduser().resolve()
    sources_path = sources.expanduser().resolve() if sources is not None else default_sources_path(target)
    if sources_path.is_file():
        try:
            return _load_sources(target, sources_path), sources_path, [], True
        except ValueError as exc:
            return (
                SourceConfig(watched=(), ingestor=None),
                sources_path,
                [f"invalid handoff source config {sources_path}: {exc}"],
                False,
            )
    if sources is not None:
        return (
            SourceConfig(watched=(), ingestor=None),
            sources_path,
            [f"handoff source config not found: {sources_path}"],
            False,
        )
    return SourceConfig(watched=(), ingestor=None), None, [], False


def _draft_inbox_specs(
    target: Path, sources: Path | None = None
) -> tuple[list[tuple[Path, str, bool]], list[str], bool]:
    target = target.expanduser().resolve()
    config, _, errors, loaded = _load_source_config_for_drafts(target, sources=sources)
    specs: dict[tuple[str, str], tuple[Path, str, bool]] = {}
    for rel in WRITER_INBOXES:
        path = (target / rel).resolve()
        specs[(str(path), rel)] = (path, rel, _is_watched(target, rel, config.watched))
    for watched in config.watched:
        path = (watched.root / watched.inbox).resolve()
        label = watched.inbox if watched.root == target.resolve() else str(path)
        specs[(str(path), label)] = (path, label, True)
    return list(specs.values()), errors, loaded


def _draft_paths(target: Path, sources: Path | None = None) -> tuple[list[tuple[Path, str, bool]], list[str], bool]:
    paths: list[tuple[Path, str, bool]] = []
    specs, errors, loaded = _draft_inbox_specs(target, sources=sources)
    for inbox_path, inbox, watched in specs:
        if not inbox_path.is_dir():
            continue
        for candidate in sorted(inbox_path.glob("*.md")):
            if not candidate.is_file():
                continue
            if candidate.name.startswith(".") or candidate.name in IGNORED_HANDOFF_NAMES:
                continue
            paths.append((candidate.resolve(), inbox, watched))
    return paths, errors, loaded


def _path_timestamp(path: Path, attr: str) -> tuple[str | None, float | None]:
    try:
        stat = path.stat()
    except OSError:
        return None, None
    value = stat.st_ctime if attr == "created" else stat.st_mtime
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(value)), value


def _iso_from_timestamp(value: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() if value is None else value))


def _timestamp_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _normalize_receipt_list(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _receipt_path_value(item: object) -> str | None:
    if isinstance(item, str) and item.strip():
        return item.strip()
    if isinstance(item, dict):
        for key in ("handoff_path", "path", "draft_path"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _receipt_target_value(item: object) -> str | None:
    if isinstance(item, dict):
        for key in ("target", "target_card", "target_document", "card", "document"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _path_match_keys(target: Path, value: str | Path) -> set[str]:
    text = str(value)
    raw = Path(text).expanduser()
    resolved = raw if raw.is_absolute() else target / raw
    keys = {text, raw.name}
    try:
        keys.add(str(resolved.resolve()))
    except OSError:
        keys.add(str(resolved))
    return {key for key in keys if key}


def _ingest_receipt_path(target: Path, run_id: str) -> Path:
    return _handoff_ingest_runs_root(target) / f"{run_id}.json"


def _normalize_ingest_receipt(
    target: Path, payload: dict[str, Any], *, source_path: Path | None = None
) -> dict[str, Any]:
    run_id = str(payload.get("run_id") or (source_path.stem if source_path is not None else "")).strip()
    if not run_id:
        raise ValueError("receipt missing run_id")
    normalized = {
        "run_id": run_id,
        "started_at": payload.get("started_at") if isinstance(payload.get("started_at"), str) else None,
        "completed_at": payload.get("completed_at") if isinstance(payload.get("completed_at"), str) else None,
        "source_root": str(payload.get("source_root") or target),
        "inbox_paths": [str(item) for item in _normalize_receipt_list(payload.get("inbox_paths")) if str(item)],
        "processed_handoff_paths": [
            str(path)
            for item in _normalize_receipt_list(payload.get("processed_handoff_paths"))
            if (path := _receipt_path_value(item))
        ],
        "promoted_card_targets": [
            {
                "handoff_path": path,
                "target": _receipt_target_value(item),
            }
            for item in _normalize_receipt_list(payload.get("promoted_card_targets"))
            if (path := _receipt_path_value(item))
        ],
        "routed_document_targets": [
            {
                "handoff_path": path,
                "target": _receipt_target_value(item),
            }
            for item in _normalize_receipt_list(payload.get("routed_document_targets"))
            if (path := _receipt_path_value(item))
        ],
        "skipped_handoff_paths": [
            str(path)
            for item in _normalize_receipt_list(payload.get("skipped_handoff_paths"))
            if (path := _receipt_path_value(item))
        ],
        "failed_handoff_paths": [
            str(path)
            for item in _normalize_receipt_list(payload.get("failed_handoff_paths"))
            if (path := _receipt_path_value(item))
        ],
        "malformed_handoff_paths": [
            str(path)
            for item in _normalize_receipt_list(payload.get("malformed_handoff_paths"))
            if (path := _receipt_path_value(item))
        ],
        "unreachable_sources": [
            str(item)
            for item in _normalize_receipt_list(payload.get("unreachable_sources"))
            if isinstance(item, str) and item.strip()
        ],
        "no_reply": bool(payload.get("no_reply")),
        "warning_events": [
            item for item in _normalize_receipt_list(payload.get("warning_events")) if isinstance(item, dict)
        ],
        "warning_count": int(payload.get("warning_count") or 0),
        "safe_summary": str(payload.get("safe_summary") or ""),
        "log_path": str(payload.get("log_path") or ""),
    }
    for key in ("owner", "recorded_by", "recorded_at"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = value.strip()
    normalized["outcomes"] = _ingest_receipt_outcomes(target, normalized)
    return normalized


def _load_ingest_receipt(target: Path, path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return _normalize_ingest_receipt(target, raw, source_path=path)
    except ValueError:
        return None


def _ingest_receipts(target: Path) -> list[dict[str, Any]]:
    root = _handoff_ingest_runs_root(target)
    if not root.is_dir():
        return []
    receipts = [
        receipt for path in sorted(root.glob("*.json")) if (receipt := _load_ingest_receipt(target, path)) is not None
    ]
    receipts.sort(
        key=lambda item: str(item.get("completed_at") or item.get("started_at") or item.get("run_id")), reverse=True
    )
    return receipts


def _ingest_receipt_outcomes(target: Path, receipt: dict[str, Any]) -> list[dict[str, Any]]:
    outcomes: dict[str, dict[str, Any]] = {}

    def add(
        path_value: str, status: str, *, target_card: str | None = None, target_document: str | None = None
    ) -> None:
        entry = {
            "path": path_value,
            "status": status,
            "run_id": receipt.get("run_id"),
            "completed_at": receipt.get("completed_at"),
            "log_path": receipt.get("log_path"),
            "target_card": target_card,
            "target_document": target_document,
        }
        for key in _path_match_keys(target, path_value):
            existing = outcomes.get(key, {})
            merged = {**existing, **{k: v for k, v in entry.items() if v is not None}}
            outcomes[key] = merged

    for path_value in receipt.get("processed_handoff_paths") or []:
        if isinstance(path_value, str):
            add(path_value, "ingested")
    for item in receipt.get("promoted_card_targets") or []:
        if isinstance(item, dict) and isinstance(item.get("handoff_path"), str):
            add(item["handoff_path"], "ingested", target_card=_receipt_target_value(item))
    for item in receipt.get("routed_document_targets") or []:
        if isinstance(item, dict) and isinstance(item.get("handoff_path"), str):
            add(item["handoff_path"], "ingested", target_document=_receipt_target_value(item))
    for path_value in receipt.get("skipped_handoff_paths") or []:
        if isinstance(path_value, str):
            add(path_value, "skipped")
    for path_value in receipt.get("failed_handoff_paths") or []:
        if isinstance(path_value, str):
            add(path_value, "failed")

    unique: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for outcome in outcomes.values():
        unique[(outcome.get("path"), outcome.get("status"))] = outcome
    return list(unique.values())


def _ingest_outcomes_by_path(target: Path) -> dict[str, dict[str, Any]]:
    mapped: dict[str, dict[str, Any]] = {}
    for receipt in _ingest_receipts(target):
        for outcome in receipt.get("outcomes") or []:
            path_value = outcome.get("path")
            if not isinstance(path_value, str):
                continue
            for key in _path_match_keys(target, path_value):
                mapped.setdefault(key, outcome)
    return mapped


def _latest_ingest_outcome_for_path(
    target: Path, path: Path, outcomes: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    outcomes = outcomes if outcomes is not None else _ingest_outcomes_by_path(target)
    for key in _path_match_keys(target, path):
        outcome = outcomes.get(key)
        if outcome is not None:
            return outcome
    return None


def _receipt_summary(receipt: dict[str, Any]) -> dict[str, Any]:
    outcomes = receipt.get("outcomes") if isinstance(receipt.get("outcomes"), list) else []
    counts: dict[str, int] = {}
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        status = str(outcome.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "run_id": receipt.get("run_id"),
        "started_at": receipt.get("started_at"),
        "completed_at": receipt.get("completed_at"),
        "source_root": receipt.get("source_root"),
        "inbox_paths": receipt.get("inbox_paths") or [],
        "warning_count": receipt.get("warning_count") or 0,
        "warning_events": receipt.get("warning_events") or [],
        "malformed": len(receipt.get("malformed_handoff_paths") or []),
        "unreachable_sources": len(receipt.get("unreachable_sources") or []),
        "no_reply": bool(receipt.get("no_reply")),
        "safe_summary": receipt.get("safe_summary") or "",
        "log_path": receipt.get("log_path") or "",
        "outcome_counts": counts,
        "processed": len(receipt.get("processed_handoff_paths") or []),
        "skipped": len(receipt.get("skipped_handoff_paths") or []),
        "failed": len(receipt.get("failed_handoff_paths") or []),
    }


def _extract_handoff_key_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            stripped = stripped[2:].strip()
        if not stripped or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip().strip("`").casefold().replace(" ", "_")
        value = value.strip().strip("`")
        if key and value and key not in values:
            values[key] = value
    return values


def _draft_summary(
    path: Path,
    *,
    target: Path,
    inbox: str,
    watched: bool,
    ingest_outcomes: dict[str, dict[str, Any]] | None = None,
) -> HandoffDraft:
    path = path.expanduser().resolve()
    try:
        text = path.read_text(errors="replace")
    except OSError:
        text = ""
    sections = _parse_markdown_sections(text)
    lint_result = lint_file(path)
    action = lint_result.action
    target_card = (
        _section_value(sections, "Target card").splitlines()[0].strip()
        if _section_value(sections, "Target card")
        else None
    )
    target_document = (
        _section_value(sections, "Target document").splitlines()[0].strip()
        if _section_value(sections, "Target document")
        else None
    )
    key_values = _extract_handoff_key_values(text)
    source_import_id = key_values.get("import") or key_values.get("import_id") or key_values.get("source_import_id")
    source_fingerprint = key_values.get("source_fingerprint") or key_values.get("handoff_source_fingerprint")
    scanner_keys = (
        "scanner_id",
        "scanner_source",
        "scanner_run_id",
        "scanner_receipt_path",
        "scanner_output_path_snapshot",
        "scanner_import_path",
        "sweep_id",
        "sweep_issue_id",
    )
    scanner_provenance = {key: key_values[key] for key in scanner_keys if key_values.get(key)}
    created_at, _ = _path_timestamp(path, "created")
    modified_at, modified_seconds = _path_timestamp(path, "modified")
    age_hours = None
    if modified_seconds is not None:
        age_hours = round((time.time() - modified_seconds) / 3600, 2)
    stale = bool(age_hours is not None and age_hours > HANDOFF_DRAFT_STALE_HOURS)
    status = "reviewed" if lint_result.valid else "pending"
    ingest_outcome = _latest_ingest_outcome_for_path(target, path, ingest_outcomes)
    return HandoffDraft(
        id=path.stem,
        path=path,
        inbox=inbox,
        created_at=created_at,
        modified_at=modified_at,
        age_hours=age_hours,
        stale=stale,
        lint=lint_result,
        action=action,
        target_card=target_card,
        target_document=target_document,
        source_import_id=source_import_id,
        source_fingerprint=source_fingerprint,
        scanner_provenance=scanner_provenance,
        status=status,
        watched=watched,
        ingestion_status=str(ingest_outcome.get("status")) if ingest_outcome and ingest_outcome.get("status") else None,
        ingest_run_id=str(ingest_outcome.get("run_id")) if ingest_outcome and ingest_outcome.get("run_id") else None,
        ingest_log_path=str(ingest_outcome.get("log_path"))
        if ingest_outcome and ingest_outcome.get("log_path")
        else None,
    )


def _drafts(target: Path, sources: Path | None = None) -> tuple[list[HandoffDraft], list[str], bool]:
    target = target.expanduser().resolve()
    paths, errors, loaded = _draft_paths(target, sources=sources)
    ingest_outcomes = _ingest_outcomes_by_path(target)
    drafts = [
        _draft_summary(path, target=target, inbox=inbox, watched=watched, ingest_outcomes=ingest_outcomes)
        for path, inbox, watched in paths
    ]
    drafts.sort(key=lambda item: str(item.modified_at or item.id), reverse=True)
    return drafts, errors, loaded


def _archive_records(target: Path) -> list[dict[str, Any]]:
    path = _handoff_archive_records_path(target)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _write_archive_records(target: Path, records: list[dict[str, Any]]) -> None:
    path = _handoff_archive_records_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def _append_archive_record(target: Path, record: dict[str, Any]) -> None:
    path = _handoff_archive_records_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _archive_record_with_ingest_outcome(
    target: Path, record: dict[str, Any], outcomes: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    outcomes = outcomes if outcomes is not None else _ingest_outcomes_by_path(target)
    outcome = None
    for key in ("archive_path", "path"):
        value = record.get(key)
        if isinstance(value, str) and value:
            outcome = _latest_ingest_outcome_for_path(target, Path(value), outcomes)
            if outcome is not None:
                break
    if outcome is None:
        return record
    updated = dict(record)
    updated["ingestion_status"] = outcome.get("status")
    updated["ingest_run_id"] = outcome.get("run_id")
    updated["ingest_log_path"] = outcome.get("log_path")
    if outcome.get("target_card") and not updated.get("target_card"):
        updated["target_card"] = outcome.get("target_card")
    if outcome.get("target_document") and not updated.get("target_document"):
        updated["target_document"] = outcome.get("target_document")
    return updated


def _refresh_archive_ingest_outcomes(target: Path) -> list[dict[str, Any]]:
    records = _archive_records(target)
    if not records:
        return []
    outcomes = _ingest_outcomes_by_path(target)
    refreshed = [_archive_record_with_ingest_outcome(target, record, outcomes) for record in records]
    if refreshed != records:
        _write_archive_records(target, refreshed)
    return refreshed


def _draft_source_import_issues(target: Path, drafts: list[HandoffDraft]) -> tuple[list[str], list[str]]:
    from .. import work_cmd

    imports_by_id = {
        str(item.get("id")): item
        for item in work_cmd._read_imports(target)
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    missing: list[str] = []
    changed: list[str] = []
    for draft in drafts:
        if not draft.source_import_id:
            continue
        item = imports_by_id.get(draft.source_import_id)
        if item is None:
            missing.append(draft.id)
            continue
        if draft.source_fingerprint:
            current = work_cmd._import_fingerprint(item)
            if current and current != draft.source_fingerprint:
                changed.append(draft.id)
    return missing, changed


def draft_queue_payload(target: Path, sources: Path | None = None) -> dict[str, Any]:
    target = target.expanduser().resolve()
    drafts, errors, sources_loaded = _drafts(target, sources=sources)
    archives = _refresh_archive_ingest_outcomes(target)
    receipts = _ingest_receipts(target)
    stale = [draft.id for draft in drafts if draft.stale and draft.status != "archived"]
    unreconciled = [
        draft.id for draft in drafts if draft.stale and draft.ingestion_status is None and draft.status != "archived"
    ]
    invalid = [draft.id for draft in drafts if not draft.lint.valid]
    uncovered = [draft.id for draft in drafts if not draft.watched]
    missing_imports, changed_fingerprints = _draft_source_import_issues(target, drafts)
    checks = [
        {
            "status": WARN if errors else OK,
            "name": "handoff_draft_sources",
            "detail": "; ".join(errors) if errors else ("configured" if sources_loaded else "default writer inboxes"),
            "items": errors,
        },
        {
            "status": WARN if stale else OK,
            "name": "handoff_draft_stale",
            "detail": f"{len(stale)} stale pending handoff draft(s)" if stale else "none",
            "items": stale[:10],
        },
        {
            "status": WARN if unreconciled else OK,
            "name": "handoff_draft_unreconciled",
            "detail": f"{len(unreconciled)} stale handoff draft(s) not represented in recent ingest receipts"
            if unreconciled
            else "none",
            "items": unreconciled[:10],
        },
        {
            "status": WARN if invalid else OK,
            "name": "handoff_draft_invalid",
            "detail": f"{len(invalid)} invalid handoff draft(s)" if invalid else "none",
            "items": invalid[:10],
        },
        {
            "status": WARN if missing_imports else OK,
            "name": "handoff_draft_missing_source_import",
            "detail": f"{len(missing_imports)} handoff draft(s) reference missing source imports"
            if missing_imports
            else "none",
            "items": missing_imports[:10],
        },
        {
            "status": WARN if changed_fingerprints else OK,
            "name": "handoff_draft_changed_source_fingerprint",
            "detail": f"{len(changed_fingerprints)} handoff draft(s) have changed source fingerprints"
            if changed_fingerprints
            else "none",
            "items": changed_fingerprints[:10],
        },
        {
            "status": WARN if uncovered else OK,
            "name": "handoff_draft_uncovered_inbox",
            "detail": f"{len(uncovered)} handoff draft(s) are in inboxes not covered by source config"
            if uncovered
            else "none",
            "items": uncovered[:10],
        },
    ]
    issues = [check for check in checks if check["status"] != OK]
    return {
        "target": str(target),
        "handoff_root": str(_handoff_state_root(target)),
        "drafts": [draft.as_dict() for draft in drafts],
        "archives": archives,
        "ingest_runs_root": str(_handoff_ingest_runs_root(target)),
        "latest_ingest_run": _receipt_summary(receipts[0]) if receipts else None,
        "counts": {
            "pending": len([draft for draft in drafts if draft.status == "pending"]),
            "reviewed": len([draft for draft in drafts if draft.status == "reviewed"]),
            "archived": len(archives),
            "ingested": len([draft for draft in drafts if draft.ingestion_status == "ingested"]),
            "skipped": len([draft for draft in drafts if draft.ingestion_status == "skipped"]),
            "failed": len([draft for draft in drafts if draft.ingestion_status == "failed"]),
            "total": len(drafts),
        },
        "checks": checks,
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def _find_draft(
    target: Path, draft_id_or_path: str, sources: Path | None = None
) -> tuple[HandoffDraft | None, str | None]:
    target = target.expanduser().resolve()
    raw_path = Path(draft_id_or_path).expanduser()
    candidates: list[HandoffDraft] = []
    drafts, errors, _ = _drafts(target, sources=sources)
    if errors:
        return None, "; ".join(errors)
    if raw_path.is_absolute() or len(raw_path.parts) > 1:
        path = raw_path if raw_path.is_absolute() else target / raw_path
        resolved = path.resolve()
        candidates = [draft for draft in drafts if draft.path == resolved]
    else:
        candidates = [
            draft
            for draft in drafts
            if draft.id == draft_id_or_path
            or draft.path.name == draft_id_or_path
            or draft.id.startswith(draft_id_or_path)
        ]
    if not candidates:
        return None, f"handoff draft not found: {draft_id_or_path}"
    if len(candidates) > 1:
        return None, f"handoff draft id is ambiguous: {draft_id_or_path}"
    return candidates[0], None


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:48].strip("-") or "handoff"


def _read_suggested_content(content: str | None, content_file: Path | None) -> str:
    if content and content_file:
        raise ValueError("pass --content or --content-file, not both")
    if content is not None:
        return content.strip()
    if content_file is None:
        raise ValueError("--content or --content-file is required")
    try:
        return content_file.expanduser().read_text(errors="replace").strip()
    except OSError as exc:
        raise ValueError(f"cannot read --content-file: {exc}") from exc


def _draft_inbox_path(target: Path, inbox: str) -> tuple[Path, str]:
    inbox = inbox.strip()
    if not inbox:
        inbox = DEFAULT_DRAFT_INBOX
    inbox = _WRITER_INBOX_MAP.get(inbox, inbox)
    path = Path(inbox).expanduser()
    if not path.is_absolute():
        path = target / path
    return path.resolve(), inbox


def _render_bullets(items: list[str]) -> str:
    return "\n".join(f"- {item.strip()}" for item in items if item.strip())


def _render_handoff_draft(
    *,
    handoff_type: str,
    title: str,
    summary: str,
    facts: list[str],
    evidence: list[str],
    action: str,
    target_card: str | None,
    target_document: str | None,
    suggested_content: str,
) -> str:
    parts = [
        "# Memory Handoff",
        "",
        "## Type",
        "",
        handoff_type.strip(),
        "",
        "## Title",
        "",
        title.strip(),
        "",
        "## Summary",
        "",
        summary.strip(),
    ]
    if any(item.strip() for item in facts):
        parts.extend(["", "## Durable facts", "", _render_bullets(facts)])
    if any(item.strip() for item in evidence):
        parts.extend(["", "## Evidence", "", _render_bullets(evidence)])
    parts.extend(["", "## Recommended memory action", "", action])
    if action in CARD_ACTIONS:
        parts.extend(["", "## Target card", "", str(target_card or "").strip()])
        parts.extend(["", "## Suggested card content", "", suggested_content.strip()])
    else:
        parts.extend(["", "## Target document", "", str(target_document or "").strip()])
        parts.extend(["", "## Suggested document content", "", suggested_content.strip()])
    return "\n".join(parts).rstrip() + "\n"


def draft(
    *,
    target: Path,
    title: str,
    summary: str,
    content: str | None = None,
    content_file: Path | None = None,
    handoff_type: str = "workflow",
    action: str = NO_CARD_ACTION,
    target_card: str | None = None,
    target_document: str | None = DEFAULT_DRAFT_DOCUMENT,
    fact: list[str] | None = None,
    evidence: list[str] | None = None,
    inbox: str = DEFAULT_DRAFT_INBOX,
    draft_id: str | None = None,
    force: bool = False,
    guard: bool = False,
    guard_policy: str = "personal",
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    action = action.strip().casefold()
    if action not in HANDOFF_ACTIONS:
        print("error: --action must be one of: " + ", ".join(HANDOFF_ACTIONS), file=sys.stderr)
        return 2
    if not title.strip():
        print("error: --title is required", file=sys.stderr)
        return 2
    if not summary.strip():
        print("error: --summary is required", file=sys.stderr)
        return 2
    if action in CARD_ACTIONS:
        if not target_card:
            print("error: --target-card is required for card handoffs", file=sys.stderr)
            return 2
        target_document = None
    else:
        if not target_document:
            print("error: --target-document is required for no-card handoffs", file=sys.stderr)
            return 2
        target_card = None
    try:
        suggested_content = _read_suggested_content(content, content_file)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not suggested_content:
        print("error: suggested content is empty", file=sys.stderr)
        return 2
    inbox_path, inbox_label = _draft_inbox_path(target, inbox)
    created_at = _iso_from_timestamp()
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    safe_id = _slugify(draft_id or title)
    filename = f"{timestamp}-{safe_id}.md"
    path = inbox_path / filename
    if path.exists() and not force:
        print(f"error: handoff draft already exists: {path}", file=sys.stderr)
        return 2
    text = _render_handoff_draft(
        handoff_type=handoff_type,
        title=title,
        summary=summary,
        facts=fact or [],
        evidence=evidence or [],
        action=action,
        target_card=target_card,
        target_document=target_document,
        suggested_content=suggested_content,
    )
    inbox_path.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    lint_result = lint_file(path)
    guard_result = _guard_handoff_path(path, target=target, policy=guard_policy) if guard else None
    payload = {
        "target": str(target),
        "created_at": created_at,
        "path": str(path),
        "id": path.stem,
        "inbox": inbox_label,
        "action": action,
        "target_card": target_card,
        "target_document": target_document,
        "lint": lint_result.as_dict(),
        "content_guard": guard_result,
        "valid": lint_result.valid and (guard_result is None or guard_result.get("exit_code") == 0),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"handoff draft: {path}")
    print(f"id: {payload['id']}")
    print(f"inbox: {inbox_label}")
    print(f"action: {action}")
    if target_card:
        print(f"target_card: {target_card}")
    if target_document:
        print(f"target_document: {target_document}")
    print(f"lint: {'ok' if lint_result.valid else 'fail'}")
    if guard_result is not None:
        guard_ok = guard_result.get("exit_code") == 0
        print(f"content_guard: {'ok' if guard_ok else 'fail'} ({guard_policy})")
        if not guard_ok:
            print(f"content_guard_detail: {guard_result.get('detail')}")
    for error in lint_result.errors:
        print(f"error: {error}")
    if not lint_result.valid:
        print(f"note: invalid draft kept at {path}; repair or delete it, then rerun `brigade handoff lint`.")
    return 0 if payload["valid"] else 1


def list_drafts(*, target: Path, sources: Path | None = None, json_output: bool = False, limit: int = 20) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = draft_queue_payload(target, sources=sources)
    payload["drafts"] = payload["drafts"][:limit]
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"handoff drafts: {target}")
    print(f"handoff_root: {payload['handoff_root']}")
    counts = payload["counts"]
    print(f"drafts: {counts['total']}")
    print(f"pending: {counts['pending']}")
    print(f"reviewed: {counts['reviewed']}")
    print(f"archived: {counts['archived']}")
    if payload.get("latest_ingest_run"):
        latest = payload["latest_ingest_run"]
        print(f"latest_ingest_run: {latest.get('run_id')} completed={latest.get('completed_at')}")
    for draft in payload["drafts"]:
        target_value = draft.get("target_document") or draft.get("target_card") or ""
        ingest = draft.get("ingestion_status") or "unreconciled"
        print(
            f"- {draft.get('id')} [{draft.get('status')}] "
            f"lint={'ok' if draft.get('lint', {}).get('valid') else 'fail'} "
            f"ingest={ingest} target={target_value}: {draft.get('path')}"
        )
    return 0


def show_draft(*, target: Path, draft_id: str, sources: Path | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    draft, error = _find_draft(target, draft_id, sources=sources)
    if draft is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    payload = {"target": str(target), "draft": draft.as_dict()}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"handoff: {draft.id}")
    print(f"status: {draft.status}")
    print(f"path: {draft.path}")
    print(f"inbox: {draft.inbox}")
    print(f"modified_at: {draft.modified_at}")
    print(f"age_hours: {draft.age_hours}")
    print(f"stale: {draft.stale}")
    print(f"lint: {'ok' if draft.lint.valid else 'fail'}")
    print(f"ingestion_status: {draft.ingestion_status or 'unreconciled'}")
    if draft.ingest_run_id:
        print(f"ingest_run_id: {draft.ingest_run_id}")
    if draft.ingest_log_path:
        print(f"ingest_log_path: {draft.ingest_log_path}")
    print(f"action: {draft.action}")
    if draft.target_card:
        print(f"target_card: {draft.target_card}")
    if draft.target_document:
        print(f"target_document: {draft.target_document}")
    if draft.source_import_id:
        print(f"source_import_id: {draft.source_import_id}")
    if draft.source_fingerprint:
        print(f"source_fingerprint: {draft.source_fingerprint}")
    if draft.scanner_provenance:
        print("scanner_provenance:")
        for key in sorted(draft.scanner_provenance):
            print(f"  {key}: {draft.scanner_provenance[key]}")
    for error in draft.lint.errors:
        print(f"error: {error}")
    return 0


def _archive_one(target: Path, draft: HandoffDraft, *, reason: str | None = None) -> dict[str, Any]:
    archived_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    archive_dir = _handoff_archive_root(target) / archived_at[:10]
    archive_dir.mkdir(parents=True, exist_ok=True)
    destination = archive_dir / draft.path.name
    if destination.exists():
        destination = (
            archive_dir
            / f"{draft.path.stem}-{hashlib.sha1(str(draft.path).encode()).hexdigest()[:8]}{draft.path.suffix}"
        )
    shutil.move(str(draft.path), str(destination))
    record = {
        "id": draft.id,
        "status": "archived",
        "previous_status": draft.status,
        "path": str(draft.path),
        "archive_path": str(destination),
        "archived_at": archived_at,
        "review_reason": reason or "reviewed handoff draft archived",
        "reviewed_at": archived_at,
        "source_import_id": draft.source_import_id,
        "source_fingerprint": draft.source_fingerprint,
        "target_card": draft.target_card,
        "target_document": draft.target_document,
        "ingestion_status": draft.ingestion_status,
        "ingest_run_id": draft.ingest_run_id,
        "ingest_log_path": draft.ingest_log_path,
    }
    _append_archive_record(target, record)
    return record


def archive_draft(
    *,
    target: Path,
    draft_id: str | None = None,
    all_reviewed: bool = False,
    reason: str | None = None,
    sources: Path | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if all_reviewed and draft_id:
        print("error: pass a handoff id/path or --all-reviewed, not both", file=sys.stderr)
        return 2
    archived: list[dict[str, Any]] = []
    if all_reviewed:
        drafts, errors, _ = _drafts(target, sources=sources)
        if errors:
            print(f"error: {'; '.join(errors)}", file=sys.stderr)
            return 2
        for draft in drafts:
            if draft.lint.valid:
                archived.append(_archive_one(target, draft, reason=reason))
    else:
        if not draft_id:
            print("error: handoff id/path is required unless --all-reviewed is passed", file=sys.stderr)
            return 2
        draft, error = _find_draft(target, draft_id, sources=sources)
        if draft is None:
            print(f"error: {error}", file=sys.stderr)
            return 1 if error and "not found" in error else 2
        archived.append(_archive_one(target, draft, reason=reason))
    payload = {
        "target": str(target),
        "archive_path": str(_handoff_archive_records_path(target)),
        "archived": len(archived),
        "records": archived,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"handoff archive: {target}")
    print(f"archived: {len(archived)}")
    for record in archived:
        print(f"- {record['id']} -> {record['archive_path']}")
    return 0


def _draft_closeout_fingerprint(draft: HandoffDraft) -> str:
    if draft.source_fingerprint:
        return draft.source_fingerprint
    stable = {
        "id": draft.id,
        "path": str(draft.path),
        "modified_at": draft.modified_at,
        "target_card": draft.target_card,
        "target_document": draft.target_document,
        "ingestion_status": draft.ingestion_status,
    }
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:16]


def closeout(
    *,
    target: Path,
    draft_id: str | None = None,
    all_pending: bool = False,
    reason: str | None = None,
    defer: bool = False,
    sources: Path | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if draft_id and all_pending:
        print("error: pass a handoff id/path or --all, not both", file=sys.stderr)
        return 2
    if not draft_id and not all_pending:
        all_pending = True
    if all_pending:
        drafts, errors, _ = _drafts(target, sources=sources)
        if errors:
            print(f"error: {'; '.join(errors)}", file=sys.stderr)
            return 2
        selected = [draft for draft in drafts if draft.status != "archived"]
    else:
        draft, error = _find_draft(target, draft_id or "", sources=sources)
        if draft is None:
            print(f"error: {error}", file=sys.stderr)
            return 1 if error and "not found" in error else 2
        selected = [draft]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    closeout_id = f"{created_at.replace(':', '').replace('+', 'Z')}-handoff-closeout"
    status = "deferred" if defer else "reviewed"
    records = []
    for draft in selected:
        records.append(
            {
                "id": draft.id,
                "path": str(draft.path),
                "status": draft.status,
                "lint_valid": draft.lint.valid,
                "ingestion_status": draft.ingestion_status,
                "target_card": draft.target_card,
                "target_document": draft.target_document,
                "source_import_id": draft.source_import_id,
                "source_fingerprint": draft.source_fingerprint,
                "closeout_fingerprint": _draft_closeout_fingerprint(draft),
            }
        )
    payload = {
        "target": str(target),
        "closeout_id": closeout_id,
        "created_at": created_at,
        "status": status,
        "reason": reason or ("handoff drafts deferred" if defer else "handoff drafts reviewed"),
        "draft_count": len(records),
        "drafts": records,
        "source_fingerprints": [item["closeout_fingerprint"] for item in records],
        "path": str(_handoff_closeouts_root(target) / closeout_id / "closeout.json"),
    }
    _write_json(Path(payload["path"]), payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"handoff closeout: {closeout_id}")
    print(f"status: {status}")
    print(f"drafts: {len(records)}")
    print(f"path: {payload['path']}")
    for record in records[:20]:
        print(f"- {record['id']} [{record['status']}] ingest={record.get('ingestion_status') or 'unreconciled'}")
    return 0
