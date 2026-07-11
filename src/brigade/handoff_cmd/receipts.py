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


def runs(*, target: Path, json_output: bool = False, limit: int = 20) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    receipts = _ingest_receipts(target)
    payload = {
        "target": str(target),
        "runs_root": str(_handoff_ingest_runs_root(target)),
        "count": len(receipts),
        "runs": [_receipt_summary(receipt) for receipt in receipts[:limit]],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"handoff ingest runs: {target}")
    print(f"runs_root: {payload['runs_root']}")
    print(f"runs: {payload['count']}")
    for item in payload["runs"]:
        print(
            f"- {item.get('run_id')} completed={item.get('completed_at')} "
            f"processed={item.get('processed')} skipped={item.get('skipped')} "
            f"failed={item.get('failed')} warnings={item.get('warning_count')}"
        )
    return 0


def run_show(*, target: Path, run_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    matches = [
        receipt
        for receipt in _ingest_receipts(target)
        if str(receipt.get("run_id")) == run_id or str(receipt.get("run_id", "")).startswith(run_id)
    ]
    if not matches:
        print(f"error: handoff ingest run not found: {run_id}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"error: handoff ingest run id is ambiguous: {run_id}", file=sys.stderr)
        return 2
    receipt = matches[0]
    payload = {"target": str(target), "run": receipt}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"handoff ingest run: {receipt.get('run_id')}")
    print(f"started_at: {receipt.get('started_at')}")
    print(f"completed_at: {receipt.get('completed_at')}")
    print(f"source_root: {receipt.get('source_root')}")
    print(f"warning_count: {receipt.get('warning_count')}")
    if receipt.get("safe_summary"):
        print(f"safe_summary: {receipt.get('safe_summary')}")
    if receipt.get("log_path"):
        print(f"log_path: {receipt.get('log_path')}")
    print(f"processed: {len(receipt.get('processed_handoff_paths') or [])}")
    print(f"skipped: {len(receipt.get('skipped_handoff_paths') or [])}")
    print(f"failed: {len(receipt.get('failed_handoff_paths') or [])}")
    print(f"malformed: {len(receipt.get('malformed_handoff_paths') or [])}")
    print(f"unreachable_sources: {len(receipt.get('unreachable_sources') or [])}")
    print(f"no_reply: {'yes' if receipt.get('no_reply') else 'no'}")
    for outcome in receipt.get("outcomes") or []:
        if isinstance(outcome, dict):
            print(f"- {outcome.get('status')} {outcome.get('path')}")
    return 0


def _safe_label(value: str, *, fallback: str = "external") -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("-")
    return slug or fallback


def _select_receipt_drafts(
    target: Path,
    *,
    draft_ids: list[str] | None,
    all_reviewed: bool,
    sources: Path | None,
) -> tuple[list[HandoffDraft], str | None]:
    if all_reviewed and draft_ids:
        return [], "pass draft ids or --all-reviewed, not both"
    if not all_reviewed and not draft_ids:
        return [], "pass at least one draft id/path or --all-reviewed"
    if all_reviewed:
        drafts, errors, _ = _drafts(target, sources=sources)
        if errors:
            return [], "; ".join(errors)
        selected = [draft for draft in drafts if draft.lint.valid]
    else:
        selected = []
        for draft_id in draft_ids or []:
            draft, error = _find_draft(target, draft_id, sources=sources)
            if draft is None:
                return [], error or f"handoff draft not found: {draft_id}"
            selected.append(draft)
    deduped: dict[str, HandoffDraft] = {}
    for draft in selected:
        deduped[str(draft.path)] = draft
    selected = list(deduped.values())
    if not selected:
        return [], "no reviewed handoff drafts selected"
    invalid = [draft.id for draft in selected if not draft.lint.valid]
    if invalid:
        return [], f"selected handoff draft is not lint-valid: {', '.join(invalid)}"
    return selected, None


def _receipt_payload_for_drafts(
    target: Path,
    *,
    drafts: list[HandoffDraft],
    status: str,
    owner: str,
    run_id: str | None,
    safe_summary: str | None,
    log_path: str | None,
) -> dict[str, Any]:
    owner_label = _safe_label(owner)
    run_id = _safe_label(
        run_id or f"{owner_label}-handoff-ingest-{_timestamp_id()}", fallback=f"handoff-ingest-{_timestamp_id()}"
    )
    now = _iso_from_timestamp()
    inbox_paths = sorted({str(draft.path.parent) for draft in drafts})
    handoff_paths = [str(draft.path) for draft in drafts]
    payload: dict[str, Any] = {
        "run_id": run_id,
        "started_at": now,
        "completed_at": now,
        "source_root": str(target),
        "inbox_paths": inbox_paths,
        "processed_handoff_paths": handoff_paths if status == "ingested" else [],
        "promoted_card_targets": [],
        "routed_document_targets": [],
        "skipped_handoff_paths": handoff_paths if status == "skipped" else [],
        "failed_handoff_paths": handoff_paths if status == "failed" else [],
        "malformed_handoff_paths": [],
        "unreachable_sources": [],
        "warning_events": [],
        "warning_count": 0,
        "no_reply": False,
        "safe_summary": safe_summary
        or f"{owner_label} recorded {len(drafts)} {status} reviewed handoff{'s' if len(drafts) != 1 else ''}.",
        "log_path": log_path or "",
        "owner": owner_label,
        "recorded_by": "brigade handoff receipt record",
        "recorded_at": now,
    }
    if status == "ingested":
        for draft in drafts:
            if draft.target_card:
                payload["promoted_card_targets"].append({"handoff_path": str(draft.path), "target": draft.target_card})
            if draft.target_document:
                payload["routed_document_targets"].append(
                    {"handoff_path": str(draft.path), "target": draft.target_document}
                )
    return _normalize_ingest_receipt(target, payload)


def _receipt_plan_payload(
    target: Path,
    *,
    draft_ids: list[str] | None,
    all_reviewed: bool,
    sources: Path | None,
    status: str,
    owner: str,
    run_id: str | None,
    safe_summary: str | None,
    log_path: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    drafts, error = _select_receipt_drafts(
        target,
        draft_ids=draft_ids,
        all_reviewed=all_reviewed,
        sources=sources,
    )
    if error:
        return None, error
    receipt = _receipt_payload_for_drafts(
        target,
        drafts=drafts,
        status=status,
        owner=owner,
        run_id=run_id,
        safe_summary=safe_summary,
        log_path=log_path,
    )
    path = _ingest_receipt_path(target, str(receipt["run_id"]))
    return {
        "target": str(target),
        "status": status,
        "owner": receipt.get("owner"),
        "would_write": False,
        "receipt_path": str(path),
        "drafts": [draft.as_dict() for draft in drafts],
        "run": receipt,
        "summary": _receipt_summary(receipt),
    }, None


def receipt_plan(
    *,
    target: Path,
    draft_ids: list[str] | None = None,
    all_reviewed: bool = False,
    sources: Path | None = None,
    status: str = "ingested",
    owner: str = "external",
    run_id: str | None = None,
    safe_summary: str | None = None,
    log_path: str | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, error = _receipt_plan_payload(
        target,
        draft_ids=draft_ids,
        all_reviewed=all_reviewed,
        sources=sources,
        status=status,
        owner=owner,
        run_id=run_id,
        safe_summary=safe_summary,
        log_path=log_path,
    )
    if error or payload is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"handoff receipt plan: {target}")
    print("would_write: false")
    print(f"receipt: {payload['receipt_path']}")
    print(f"run_id: {payload['run']['run_id']}")
    print(f"owner: {payload['owner']}")
    print(f"status: {status}")
    print(f"drafts: {len(payload['drafts'])}")
    for draft in payload["drafts"]:
        print(f"- {draft.get('id')} -> {draft.get('path')}")
    return 0


def receipt_record(
    *,
    target: Path,
    draft_ids: list[str] | None = None,
    all_reviewed: bool = False,
    sources: Path | None = None,
    status: str = "ingested",
    owner: str = "external",
    run_id: str | None = None,
    safe_summary: str | None = None,
    log_path: str | None = None,
    force: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, error = _receipt_plan_payload(
        target,
        draft_ids=draft_ids,
        all_reviewed=all_reviewed,
        sources=sources,
        status=status,
        owner=owner,
        run_id=run_id,
        safe_summary=safe_summary,
        log_path=log_path,
    )
    if error or payload is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    path = Path(str(payload["receipt_path"]))
    if path.exists() and not force:
        print(f"error: handoff ingest receipt already exists: {path}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload["run"], indent=2, sort_keys=True) + "\n")
    archives = _refresh_archive_ingest_outcomes(target)
    written = dict(payload)
    written["would_write"] = True
    written["written"] = True
    written["archive_records_refreshed"] = len(archives)
    if json_output:
        print(json.dumps(written, indent=2, sort_keys=True))
        return 0
    print(f"handoff receipt record: {target}")
    print(f"receipt: {path}")
    print(f"run_id: {written['run']['run_id']}")
    print(f"owner: {written['owner']}")
    print(f"status: {status}")
    print(f"drafts: {len(written['drafts'])}")
    print(f"archive_records_refreshed: {len(archives)}")
    return 0


def reconcile(*, target: Path, sources: Path | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    config, sources_path, errors, _ = _load_source_config_for_drafts(target, sources=sources)
    if errors:
        print(f"error: {'; '.join(errors)}", file=sys.stderr)
        return 2
    if config.ingestor is None:
        print("error: ingestor.last_run_log is not configured", file=sys.stderr)
        return 2
    log_path = config.ingestor.log_path
    if not log_path.is_file():
        print(f"error: ingestor last_run_log not found: {log_path}", file=sys.stderr)
        return 1
    try:
        text = log_path.read_text(errors="replace")
    except OSError as exc:
        print(f"error: cannot read ingestor log: {exc}", file=sys.stderr)
        return 1
    receipt = _parse_ingestor_log_receipt(target, config, log_path, text)
    path = _ingest_receipt_path(target, str(receipt["run_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    archives = _refresh_archive_ingest_outcomes(target)
    payload = {
        "target": str(target),
        "sources_path": str(sources_path) if sources_path else None,
        "receipt_path": str(path),
        "run": receipt,
        "archive_records_refreshed": len(archives),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"handoff reconcile: {target}")
    print(f"receipt: {path}")
    print(f"run_id: {receipt['run_id']}")
    print(f"processed: {len(receipt['processed_handoff_paths'])}")
    print(f"skipped: {len(receipt['skipped_handoff_paths'])}")
    print(f"failed: {len(receipt['failed_handoff_paths'])}")
    print(f"warnings: {receipt['warning_count']}")
    return 0
