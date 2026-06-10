"""Inbox and import-helper operations.

The heavier command families that once lived here now live in sibling modules:
``scanners`` (scanner runs), ``sweeps`` (sweep + plan-proposal), ``reviews``
(code review), ``imports`` (import command surface), ``verification``
(verify/acceptance/closeout), and ``backup``. This module keeps the inbox views
plus the shared import/chat/provenance helpers those families call through.
"""

from __future__ import annotations
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from . import constants, helpers, ledger as ledger_mod, config as config_mod
from . import scanners as scanners_mod
from . import sweeps as sweeps_mod


def _memory_refresh_cards(payload: dict[str, Any], *, queue_path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    cards = payload.get("cards")
    if cards is None:
        cards = payload.get("candidates")
    if cards is None:
        cards = payload.get("refresh_candidates", [])
    if not isinstance(cards, list):
        return [], [f"memory-refresh queue `cards` must be a list: {queue_path}"]

    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, card in enumerate(cards, start=1):
        label = f"memory-refresh card entry {index}"
        if not isinstance(card, dict):
            errors.append(f"{label} must be an object")
            continue
        card_file = (
            ledger_mod._string_field(card.get("file"))
            or ledger_mod._string_field(card.get("path"))
            or ledger_mod._string_field(card.get("card_file"))
        )
        card_id = ledger_mod._string_field(card.get("id")) or ledger_mod._string_field(card.get("card_id")) or card_file
        if not card_file:
            errors.append(f"{label} requires file")
            continue
        reason = (
            ledger_mod._string_field(card.get("refresh_reason"))
            or ledger_mod._string_field(card.get("reason"))
            or ledger_mod._string_field(card.get("category"))
            or "stale memory card"
        )
        acceptance = ledger_mod._normalize_acceptance(card.get("acceptance"))
        if not acceptance:
            acceptance = [
                f"Review {card_file} against current source evidence.",
                "Update the memory card or document why no change is needed.",
            ]
        metadata: dict[str, Any] = {
            "card_file": card_file,
            "card_id": card_id,
            "refresh_reason": reason,
            "reason": reason,
            "queue_path": str(queue_path),
        }
        for key in (
            "confidence",
            "evidence_references",
            "evidence_summary",
            "issue_type",
            "review_after",
            "last_reviewed_at",
            "freshness",
            "safe_summary",
            "source",
            "suggested_refresh_action",
            "safe_autofix_plan",
        ):
            value = card.get(key)
            if value not in (None, ""):
                metadata[key] = value
        source_item_key = ledger_mod._string_field(card.get("source_item_key")) or f"memory-refresh:{card_id}"
        record = {
            "text": f"Refresh memory card {card_file}: {reason}",
            "kind": "task",
            "source": "memory-refresh",
            "type": card.get("type") if isinstance(card.get("type"), str) else "docs",
            "priority": card.get("priority") if isinstance(card.get("priority"), str) else "normal",
            "template": card.get("template") if isinstance(card.get("template"), str) else "docs",
            "acceptance": acceptance,
            "metadata": metadata,
        }
        fingerprint = ledger_mod._string_field(card.get("source_fingerprint")) or helpers._stable_hash(
            {
                "card_id": card_id,
                "card_file": card_file,
                "reason": reason,
                "acceptance": acceptance,
                "evidence_summary": metadata.get("evidence_summary"),
                "issue_type": metadata.get("issue_type"),
            }
        )
        metadata["source_item_key"] = source_item_key
        metadata["source_fingerprint"] = fingerprint
        records.append(record)
    return records, errors


def _import_memory_refresh_queue(
    *,
    target: Path,
    queue: Path | None,
    dry_run: bool,
    json_output: bool,
    source: str,
    command_name: str,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    queue_path = (
        queue.expanduser().resolve()
        if queue is not None
        else target / "memory" / "cards" / "decay" / "refresh-queue.json"
    )
    if not queue_path.is_file():
        print(f"error: memory-care refresh queue not found: {queue_path}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(queue_path.read_text())
    except json.JSONDecodeError as exc:
        print(f"error: invalid memory-care refresh queue JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print(f"error: memory-care refresh queue must be an object: {queue_path}", file=sys.stderr)
        return 2
    records, errors = _memory_refresh_cards(payload, queue_path=queue_path)
    if source != "memory-refresh":
        for record in records:
            record["source"] = source
            metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            if isinstance(metadata.get("source_item_key"), str):
                metadata["source_item_key"] = metadata["source_item_key"].replace("memory-refresh:", f"{source}:", 1)
    if errors:
        if json_output:
            print(
                json.dumps(
                    {
                        "queue": str(queue_path),
                        "imports_path": str(helpers._imports_path(target)),
                        "valid": False,
                        "errors": errors,
                        "created": 0,
                        "skipped": 0,
                        "dismissed": 0,
                        "invalid": len(errors),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            for error in errors:
                print(f"error: {error}", file=sys.stderr)
        return 2
    imported, skipped, skipped_dismissed = ledger_mod._append_import_records(target, records, dry_run=dry_run)
    output = {
        "queue": str(queue_path),
        "imports_path": str(helpers._imports_path(target)),
        "dry_run": dry_run,
        "valid": True,
        "queued_cards": len(records),
        "created": len(imported),
        "imported": len(imported),
        "skipped": len(skipped),
        "skipped_duplicates": len(skipped),
        "dismissed": len(skipped_dismissed),
        "skipped_dismissed": len(skipped_dismissed),
        "invalid": 0,
        "imports": imported,
    }
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    print(f"{command_name} queue: {queue_path}")
    print(f"imports_path: {helpers._imports_path(target)}")
    print(f"dry_run: {dry_run}")
    print(f"queued_cards: {len(records)}")
    print(f"imported: {len(imported)}")
    print(f"skipped_duplicates: {len(skipped)}")
    if skipped_dismissed:
        print(f"skipped_dismissed: {len(skipped_dismissed)}")
    for item in imported:
        print(f"- {item.get('id')} {helpers._short(str(item.get('text', '')))}")
    return 0


def _safe_chat_metadata(issue: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    metadata = issue.get("metadata", {})
    if metadata is None:
        metadata = {}
    safe: dict[str, Any] = {}
    omitted: list[str] = []
    if isinstance(metadata, dict):
        for key, value in metadata.items():
            normalized = str(key).strip().casefold()
            if normalized in constants.RAW_CHAT_FIELDS or normalized.startswith("raw_"):
                omitted.append(str(key))
                continue
            safe[str(key)] = value
    for source_key, dest_key in (
        ("provider", "provider"),
        ("surface", "surface"),
        ("workspace", "workspace"),
        ("channel", "channel"),
        ("thread", "thread"),
        ("message_range", "message_range"),
        ("confidence", "confidence"),
        ("evidence_summary", "evidence_summary"),
        ("local_locator", "local_locator"),
    ):
        value = issue.get(source_key)
        if value not in (None, ""):
            safe[dest_key] = value
    for key in constants.RAW_CHAT_FIELDS:
        if key in issue:
            omitted.append(key)
    return safe, sorted(set(omitted))


def _chat_sweep_records(payload: dict[str, Any], *, sweep_path: Path) -> tuple[list[dict[str, Any]], list[str], int]:
    issues = payload.get("issues", [])
    if not isinstance(issues, list):
        return [], [f"chat memory sweep `issues` must be a list: {sweep_path}"], 0

    generated_at = payload.get("generated_at")
    sweep_id = (
        ledger_mod._string_field(payload.get("sweep_id"))
        or ledger_mod._string_field(payload.get("id"))
        or helpers._stable_hash({"path": str(sweep_path), "generated_at": generated_at})
    )
    provider = ledger_mod._string_field(payload.get("provider"))
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, issue in enumerate(issues, start=1):
        label = f"chat memory sweep issue {index}"
        if not isinstance(issue, dict):
            errors.append(f"{label} must be an object")
            continue
        title = ledger_mod._string_field(issue.get("title"))
        if not title:
            errors.append(f"{label} requires title")
            continue
        issue_id = (
            ledger_mod._string_field(issue.get("id"))
            or ledger_mod._string_field(issue.get("issue_id"))
            or helpers._stable_hash({"sweep_id": sweep_id, "title": title, "index": index})
        )
        actionable = bool(issue.get("actionable")) or bool(issue.get("task")) or issue.get("kind") == "task"
        kind = "task" if actionable else issue.get("kind", "incident")
        if not isinstance(kind, str) or kind not in constants.IMPORT_KINDS:
            errors.append(f"{label} kind must be one of: {', '.join(constants.IMPORT_KINDS)}")
            continue
        metadata = issue.get("metadata", {})
        if metadata is not None and not isinstance(metadata, dict):
            errors.append(f"{label} metadata must be an object")
            continue

        safe_metadata, omitted_fields = _safe_chat_metadata(issue)
        if provider and "provider" not in safe_metadata:
            safe_metadata["provider"] = provider
        summary = ledger_mod._string_field(issue.get("summary"))
        evidence_summary = ledger_mod._string_field(issue.get("evidence_summary"))
        severity = ledger_mod._string_field(issue.get("severity"))
        issue_source = ledger_mod._string_field(issue.get("source"))
        rendered_title = title
        severity_prefix = f" [{severity}]" if severity else ""
        if actionable:
            text = f"Review chat memory sweep task{severity_prefix} {rendered_title}"
        else:
            text = f"Review memory sweep issue{severity_prefix} {rendered_title}"
        if summary:
            text = f"{text}: {summary}"

        record_metadata = dict(safe_metadata)
        record_metadata.update(
            {
                "sweep_id": sweep_id,
                "sweep_issue_id": issue_id,
                "source_item_key": f"chat-memory-sweep:{sweep_id}:{issue_id}",
                "sweep_path": str(sweep_path),
                "issue_title": rendered_title,
            }
        )
        if issue_source:
            record_metadata["issue_source"] = issue_source
        if severity:
            record_metadata["severity"] = severity
        if evidence_summary:
            record_metadata["evidence_summary"] = evidence_summary
        if isinstance(generated_at, str) and generated_at.strip():
            record_metadata["generated_at"] = generated_at.strip()
        if omitted_fields:
            record_metadata["private_fields_omitted"] = omitted_fields
        acceptance = ledger_mod._normalize_acceptance(issue.get("acceptance"))
        if actionable and not acceptance:
            acceptance = [
                "Review the sweep summary and local evidence locator.",
                "Promote only public-safe conclusions or create a memory handoff.",
            ]
        fingerprint_payload = {
            "title": title,
            "summary": summary,
            "kind": kind,
            "severity": severity,
            "source": issue_source,
            "acceptance": acceptance,
            "evidence_summary": evidence_summary,
            "metadata": {
                key: value
                for key, value in record_metadata.items()
                if key not in {"sweep_path", "source_fingerprint", "private_fields_omitted"}
            },
        }
        record_metadata["source_fingerprint"] = helpers._stable_hash(fingerprint_payload)
        record: dict[str, Any] = {
            "text": text,
            "kind": kind,
            "source": "chat-memory-sweep",
            "metadata": record_metadata,
        }
        if kind == "task":
            record["type"] = issue.get("type") if isinstance(issue.get("type"), str) else "workflow"
            record["priority"] = issue.get("priority") if isinstance(issue.get("priority"), str) else "normal"
            record["template"] = issue.get("template") if isinstance(issue.get("template"), str) else "vertical-slice"
            record["acceptance"] = acceptance
        records.append(record)
    return records, errors, len(issues)


def _content_guard_import_records(result: dict[str, Any]) -> list[dict[str, Any]]:
    exit_code = int(result.get("exit_code") or 0)
    if exit_code == 0:
        return []
    target = str(result.get("target") or "")
    policy = str(result.get("policy") or "public-repo")
    stdout_summary = scanners_mod._scanner_run_summary(str(result.get("stdout") or ""), limit=12)
    stderr_summary = scanners_mod._scanner_run_summary(str(result.get("stderr") or ""), limit=8)
    detail = str(result.get("detail") or "content-guard reported findings")
    metadata = {
        "scanner_id": "content-guard",
        "scanner_source": "content-guard",
        "policy": policy,
        "scan_target": target,
        "exit_code": exit_code,
        "detail": detail,
        "stdout_summary": stdout_summary,
        "stderr_summary": stderr_summary,
        "source_item_key": f"content-guard:{policy}:{target}",
        "source_fingerprint": helpers._stable_hash(
            {
                "policy": policy,
                "target": target,
                "exit_code": exit_code,
                "stdout": stdout_summary,
                "stderr": stderr_summary,
            }
        ),
    }
    return [
        {
            "text": f"Review Content Guard findings for {target} using policy {policy}: {detail}",
            "kind": "finding",
            "source": "content-guard",
            "metadata": metadata,
        }
    ]


def _metadata_has_any(metadata: dict[str, Any], keys: set[str]) -> bool:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, list) and value:
            return True
        if isinstance(value, dict) and value:
            return True
        if isinstance(value, (int, float, bool)):
            return True
    return False


def _provenance_audit_sources(target: Path) -> set[str]:
    sources = set(constants.PROVENANCE_AUDIT_SOURCES)
    sources.update(scanners_mod._scanner_source_map(target))
    return sources


def _provenance_audit_item(
    item: dict[str, Any],
    *,
    scanner_sources: dict[str, dict[str, Any]],
    audited_sources: set[str],
) -> dict[str, Any] | None:
    source = str(item.get("source") or "manual")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    is_configured_scanner = source in scanner_sources
    if source not in audited_sources and not is_configured_scanner:
        return None

    missing: list[str] = []
    source_identity = ledger_mod._import_source_identity(item)
    fingerprint = ledger_mod._import_fingerprint(item)
    explicit_fingerprint = metadata.get("source_fingerprint")
    has_explicit_fingerprint = isinstance(explicit_fingerprint, str) and bool(explicit_fingerprint.strip())
    if source_identity is None:
        missing.append("source_item_key")
    if not has_explicit_fingerprint:
        missing.append("source_fingerprint")
    if not _metadata_has_any(metadata, constants.PROVENANCE_SAFE_SUMMARY_KEYS):
        missing.append("safe_summary")
    if not _metadata_has_any(metadata, constants.PROVENANCE_EVIDENCE_KEYS):
        missing.append("evidence_reference")

    if is_configured_scanner:
        for key in ("scanner_id", "scanner_source", "scanner_run_id"):
            if not metadata.get(key):
                missing.append(key)

    missing = sorted(set(missing))
    return {
        "id": item.get("id"),
        "source": source,
        "kind": item.get("kind", "task"),
        "status": item.get("status", "pending"),
        "producer": "scanner" if is_configured_scanner else source,
        "source_identity": list(source_identity) if source_identity else None,
        "source_fingerprint": explicit_fingerprint.strip() if has_explicit_fingerprint else None,
        "effective_source_fingerprint": fingerprint,
        "has_source_identity": source_identity is not None,
        "has_source_fingerprint": has_explicit_fingerprint,
        "has_safe_summary": "safe_summary" not in missing,
        "has_evidence_reference": "evidence_reference" not in missing,
        "dismissed_until_changed_ready": source_identity is not None and has_explicit_fingerprint,
        "provenance_complete": not missing,
        "missing_fields": missing,
    }


def _import_provenance_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    imports = [item for item in ledger_mod._read_imports(target) if isinstance(item, dict)]
    scanner_sources = scanners_mod._scanner_source_map(target)
    audited_sources = _provenance_audit_sources(target)
    items = [
        audit
        for item in imports
        if (audit := _provenance_audit_item(item, scanner_sources=scanner_sources, audited_sources=audited_sources))
        is not None
    ]
    missing_by_field: dict[str, int] = {}
    missing_by_source: dict[str, int] = {}
    incomplete = [item for item in items if not item["provenance_complete"]]
    for item in incomplete:
        source = str(item.get("source") or "manual")
        missing_by_source[source] = missing_by_source.get(source, 0) + 1
        for field in item.get("missing_fields", []):
            missing_by_field[field] = missing_by_field.get(field, 0) + 1
    return {
        "target": str(target),
        "imports_path": str(helpers._imports_path(target)),
        "audited_source_count": len(audited_sources),
        "import_count": len(imports),
        "audited_import_count": len(items),
        "complete_count": len(items) - len(incomplete),
        "incomplete_count": len(incomplete),
        "missing_by_field": dict(sorted(missing_by_field.items())),
        "missing_by_source": dict(sorted(missing_by_source.items())),
        "items": items,
        "issues": incomplete,
    }


def _inbox_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    pending = ledger_mod._pending_imports(target)
    now = helpers._now()
    summaries = [ledger_mod._import_summary(item, now=now) for item in pending]
    by_source: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    acceptance = {"ready": 0, "missing": 0}
    handoff_ready = 0
    stale: list[dict[str, Any]] = []
    for summary in summaries:
        source = str(summary.get("source") or "manual")
        kind = str(summary.get("kind") or "task")
        by_source[source] = by_source.get(source, 0) + 1
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if kind == "task":
            priority = str(summary.get("priority") or "normal")
            by_priority[priority] = by_priority.get(priority, 0) + 1
            if summary.get("acceptance_missing"):
                acceptance["missing"] += 1
            else:
                acceptance["ready"] += 1
        elif kind in constants.HANDOFF_READY_KINDS:
            handoff_ready += 1
        age_hours = summary.get("age_hours")
        if isinstance(age_hours, (int, float)) and age_hours > constants.IMPORT_STALE_HOURS:
            stale.append(summary)
    candidate = ledger_mod._scanner_candidate(pending)
    handoff_candidate = ledger_mod._handoff_candidate(pending)
    return {
        "target": str(target),
        "imports_path": str(helpers._imports_path(target)),
        "counts": {
            "total": len(summaries),
            "by_source": dict(sorted(by_source.items())),
            "by_kind": dict(sorted(by_kind.items())),
            "by_priority": dict(sorted(by_priority.items())),
            "acceptance": acceptance,
            "handoff_ready": handoff_ready,
            "stale": len(stale),
        },
        "candidate": ledger_mod._import_summary(candidate, now=now) if candidate else None,
        "handoff_candidate": ledger_mod._import_summary(handoff_candidate, now=now) if handoff_candidate else None,
        "imports": summaries,
    }


def _import_hygiene_issue(status: str, name: str, detail: str, items: list[str] | None = None) -> dict[str, Any]:
    issue: dict[str, Any] = {"status": status, "name": name, "detail": detail}
    if items is not None:
        issue["items"] = items
    return issue


def _inbox_hygiene_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    imports = [item for item in ledger_mod._read_imports(target) if isinstance(item, dict)]
    scanner_sources = scanners_mod._scanner_source_map(target)
    checks: list[dict[str, Any]] = []
    now: datetime | None = None

    def current_now() -> datetime:
        nonlocal now
        if now is None:
            now = helpers._now()
        return now

    missing_provenance: list[str] = []
    for item in imports:
        if item.get("status", "pending") != "pending":
            continue
        source = str(item.get("source") or "")
        if source not in scanner_sources:
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        required = ("scanner_id", "scanner_source", "source_fingerprint")
        if any(not metadata.get(key) for key in required):
            missing_provenance.append(str(item.get("id")))
    checks.append(
        _import_hygiene_issue(
            constants.WARN if missing_provenance else constants.OK,
            "inbox_missing_provenance",
            f"{len(missing_provenance)} pending scanner import(s) missing provenance"
            if missing_provenance
            else "pending scanner imports have provenance",
            missing_provenance[:10],
        )
    )

    stale_pending = [
        str(item.get("id"))
        for item in imports
        if item.get("status", "pending") == "pending"
        and (created := helpers._parse_iso_datetime(item.get("created_at"))) is not None
        and (current_now() - created).total_seconds() / 3600 > constants.IMPORT_STALE_HOURS
    ]
    checks.append(
        _import_hygiene_issue(
            constants.WARN if stale_pending else constants.OK,
            "inbox_stale_pending",
            f"{len(stale_pending)} pending import(s) older than {constants.IMPORT_STALE_HOURS}h"
            if stale_pending
            else "none",
            stale_pending[:10],
        )
    )
    stale_handoff_ready = [
        str(item.get("id"))
        for item in imports
        if item.get("status", "pending") == "pending"
        and item.get("kind") in constants.HANDOFF_READY_KINDS
        and (created := helpers._parse_iso_datetime(item.get("created_at"))) is not None
        and (current_now() - created).total_seconds() / 3600 > constants.IMPORT_STALE_HOURS
    ]
    checks.append(
        _import_hygiene_issue(
            constants.WARN if stale_handoff_ready else constants.OK,
            "inbox_stale_handoff_ready",
            f"{len(stale_handoff_ready)} handoff-ready import(s) older than {constants.IMPORT_STALE_HOURS}h"
            if stale_handoff_ready
            else "none",
            stale_handoff_ready[:10],
        )
    )

    task_ids = {
        str(task.get("id"))
        for task in ledger_mod._read_task_ledger(target).get("tasks", [])
        if isinstance(task, dict) and isinstance(task.get("id"), str)
    }
    broken_promoted = [
        str(item.get("id"))
        for item in imports
        if item.get("status") == "promoted"
        and isinstance(item.get("task_id"), str)
        and item.get("task_id") not in task_ids
    ]
    checks.append(
        _import_hygiene_issue(
            constants.WARN if broken_promoted else constants.OK,
            "inbox_promoted_task_missing",
            f"{len(broken_promoted)} promoted import(s) point at missing ledger tasks" if broken_promoted else "none",
            broken_promoted[:10],
        )
    )
    missing_handoff_drafts = [
        str(item.get("id"))
        for item in imports
        if item.get("status") == "promoted"
        and isinstance(item.get("handoff_path"), str)
        and not Path(item["handoff_path"]).expanduser().exists()
    ]
    checks.append(
        _import_hygiene_issue(
            constants.WARN if missing_handoff_drafts else constants.OK,
            "inbox_promoted_handoff_missing",
            f"{len(missing_handoff_drafts)} promoted import(s) point at missing handoff drafts"
            if missing_handoff_drafts
            else "none",
            missing_handoff_drafts[:10],
        )
    )

    by_identity: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in imports:
        identity = ledger_mod._import_source_identity(item)
        if identity is not None:
            by_identity.setdefault(identity, []).append(item)
    changed_dismissed: list[str] = []
    for items in by_identity.values():
        dismissed = [item for item in items if item.get("status") == "dismissed"]
        active = [item for item in items if item.get("status", "pending") in {"pending", "promoted"}]
        active_fingerprints = {
            ledger_mod._import_fingerprint(item) for item in active if ledger_mod._import_fingerprint(item)
        }
        for item in dismissed:
            fingerprint = ledger_mod._import_fingerprint(item)
            if fingerprint and active_fingerprints and fingerprint not in active_fingerprints:
                changed_dismissed.append(str(item.get("id")))
    checks.append(
        _import_hygiene_issue(
            constants.WARN if changed_dismissed else constants.OK,
            "inbox_dismissed_changed",
            f"{len(changed_dismissed)} dismissed import(s) have changed source fingerprints"
            if changed_dismissed
            else "none",
            changed_dismissed[:10],
        )
    )

    by_source: dict[str, dict[str, int]] = {}
    for item in imports:
        source = str(item.get("source") or "manual")
        status = str(item.get("status") or "pending")
        by_source.setdefault(source, {"dismissed": 0, "promoted": 0})
        if status in by_source[source]:
            by_source[source][status] += 1
    noisy_sources = [
        f"{source}=dismissed:{counts['dismissed']},promoted:{counts['promoted']}"
        for source, counts in sorted(by_source.items())
        if counts["dismissed"] >= constants.DISMISSED_SOURCE_WARN_THRESHOLD
        and counts["dismissed"] > max(1, counts["promoted"]) * 2
    ]
    checks.append(
        _import_hygiene_issue(
            constants.WARN if noisy_sources else constants.OK,
            "inbox_noisy_sources",
            ", ".join(noisy_sources) if noisy_sources else "none",
            noisy_sources[:10],
        )
    )

    provenance = _import_provenance_payload(target)
    provenance_missing = [
        str(item.get("id")) for item in provenance["issues"] if item.get("status", "pending") == "pending"
    ]
    checks.append(
        _import_hygiene_issue(
            constants.WARN if provenance_missing else constants.OK,
            "inbox_provenance_contract",
            f"{len(provenance_missing)} pending producer import(s) missing provenance contract fields"
            if provenance_missing
            else "producer imports satisfy the provenance contract",
            provenance_missing[:10],
        )
    )

    no_import_runs: list[str] = []
    scanners, errors = config_mod._load_scanner_config(target)
    scanner_by_id = {str(scanner.get("id")): scanner for scanner in scanners if isinstance(scanner.get("id"), str)}
    if not errors:
        imports_by_run = {
            str(metadata.get("scanner_run_id"))
            for item in imports
            if isinstance((metadata := item.get("metadata")), dict) and metadata.get("scanner_run_id")
        }
        for receipt in scanners_mod._scanner_receipts(target):
            run_id = str(receipt.get("run_id") or "")
            scanner = scanner_by_id.get(str(receipt.get("scanner_id") or ""))
            if not run_id or scanner is None or not scanner.get("import_path"):
                continue
            if receipt.get("status") != "completed":
                continue
            ingest = receipt.get("ingest_output") if isinstance(receipt.get("ingest_output"), dict) else {}
            created = int(ingest.get("created", 0) or 0) if ingest else 0
            stamped = int(receipt.get("provenance_imports_stamped", 0) or 0)
            if run_id not in imports_by_run and created == 0 and stamped == 0:
                no_import_runs.append(run_id)
    checks.append(
        _import_hygiene_issue(
            constants.WARN if no_import_runs else constants.OK,
            "inbox_scanner_run_no_imports",
            f"{len(no_import_runs)} scanner run(s) produced no imports despite configured import_path"
            if no_import_runs
            else "none",
            no_import_runs[:10],
        )
    )

    imports_by_id = {str(item.get("id")): item for item in imports if isinstance(item.get("id"), str)}
    sweep_missing_refs: list[str] = []
    sweep_lost_provenance: list[str] = []
    sweep_unclosed: list[str] = []
    for sweep_report in scanners_mod._scanner_sweeps(target):
        sweep_id = str(sweep_report.get("sweep_id") or "unknown")
        references = sweeps_mod._sweep_import_references(sweep_report)
        referenced_pending = False
        for import_id in references.get("created_import_ids", []):
            if not isinstance(import_id, str) or not import_id.strip():
                continue
            item = imports_by_id.get(import_id)
            if item is None:
                sweep_missing_refs.append(f"{sweep_id}:{import_id}")
                continue
            if item.get("status", "pending") == "pending":
                referenced_pending = True
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            required = ("scanner_id", "scanner_source", "scanner_run_id", "source_fingerprint")
            if any(not metadata.get(key) for key in required):
                sweep_lost_provenance.append(f"{sweep_id}:{import_id}")
        if referenced_pending and not sweeps_mod._sweep_is_closed(sweep_report):
            sweep_unclosed.append(sweep_id)
    checks.append(
        _import_hygiene_issue(
            constants.WARN if sweep_missing_refs else constants.OK,
            "inbox_sweep_import_missing",
            f"{len(sweep_missing_refs)} sweep import reference(s) missing from inbox" if sweep_missing_refs else "none",
            sweep_missing_refs[:10],
        )
    )
    checks.append(
        _import_hygiene_issue(
            constants.WARN if sweep_lost_provenance else constants.OK,
            "inbox_sweep_import_provenance",
            f"{len(sweep_lost_provenance)} sweep import reference(s) lost provenance"
            if sweep_lost_provenance
            else "none",
            sweep_lost_provenance[:10],
        )
    )
    checks.append(
        _import_hygiene_issue(
            constants.WARN if sweep_unclosed else constants.OK,
            "inbox_sweep_unclosed",
            f"{len(sweep_unclosed)} sweep(s) have pending imports without review closeout"
            if sweep_unclosed
            else "none",
            sweep_unclosed[:10],
        )
    )

    issues = [check for check in checks if check.get("status") != constants.OK]
    return {
        "target": str(target),
        "imports_path": str(helpers._imports_path(target)),
        "archive_path": str(helpers._imports_archive_path(target)),
        "checks": checks,
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def _inbox_quality_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    imports = [item for item in ledger_mod._read_imports(target) if isinstance(item, dict)]
    pending = [item for item in imports if item.get("status", "pending") == "pending"]
    dismissed_by_source = Counter(
        str(item.get("source") or "unknown") for item in imports if item.get("status") == "dismissed"
    )
    promoted_by_source = Counter(
        str(item.get("source") or "unknown") for item in imports if item.get("status") == "promoted"
    )
    noisy_sources = {
        source for source, count in dismissed_by_source.items() if count >= max(3, promoted_by_source[source] * 3)
    }
    by_identity: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in imports:
        identity = ledger_mod._import_source_identity(item)
        if identity is not None:
            by_identity.setdefault(identity, []).append(item)
    changed_dismissed: list[str] = []
    duplicate_pending: list[str] = []
    for items in by_identity.values():
        pending_items = [item for item in items if item.get("status", "pending") == "pending"]
        if len(pending_items) > 1:
            duplicate_pending.extend(str(item.get("id")) for item in pending_items[1:])
        dismissed_items = [item for item in items if item.get("status") == "dismissed"]
        active_fingerprints = {
            ledger_mod._import_fingerprint(item) for item in pending_items if ledger_mod._import_fingerprint(item)
        }
        for item in dismissed_items:
            fingerprint = ledger_mod._import_fingerprint(item)
            if fingerprint and active_fingerprints and fingerprint not in active_fingerprints:
                changed_dismissed.append(str(item.get("id")))

    scored: list[dict[str, Any]] = []
    now = helpers._now()
    for item in pending:
        import_id = str(item.get("id") or "")
        source = str(item.get("source") or "unknown")
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        acceptance = item.get("acceptance") if isinstance(item.get("acceptance"), list) else []
        has_acceptance = bool(acceptance)
        has_provenance = bool(
            metadata.get("source_fingerprint") or metadata.get("scanner_run_id") or item.get("source")
        )
        created = helpers._parse_iso_datetime(item.get("created_at"))
        age_hours = (now - created).total_seconds() / 3600 if created is not None else None
        flags: list[str] = []
        score = 100
        if has_acceptance:
            flags.append("acceptance-ready")
        else:
            flags.append("missing-acceptance")
            score -= 30
        if has_provenance:
            flags.append("provenance-ready")
        else:
            flags.append("missing-provenance")
            score -= 35
        if age_hours is not None and age_hours > constants.IMPORT_STALE_HOURS:
            flags.append("stale")
            score -= 20
        if bool(metadata.get("deferred") or metadata.get("deferred_at") or item.get("deferred_at")):
            flags.append("deferred")
            score -= 45
        if source in noisy_sources:
            flags.append("noisy-source")
            score -= 40
        if import_id in duplicate_pending:
            flags.append("duplicate-pending")
            score -= 30
        scored.append(
            {
                "import_id": import_id,
                "source": source,
                "kind": item.get("kind", "task"),
                "priority": item.get("priority", "normal"),
                "quality_score": max(0, score),
                "quality_flags": flags,
                "acceptance_count": len(acceptance),
                "has_acceptance": has_acceptance,
                "has_provenance": has_provenance,
                "age_hours": round(age_hours, 2) if age_hours is not None else None,
                "source_fingerprint": metadata.get("source_fingerprint"),
            }
        )
    scored.sort(key=lambda item: (int(item.get("quality_score") or 0), str(item.get("import_id") or "")), reverse=True)
    issue_counts = {
        "missing_acceptance": sum(1 for item in scored if "missing-acceptance" in item["quality_flags"]),
        "missing_provenance": sum(1 for item in scored if "missing-provenance" in item["quality_flags"]),
        "stale": sum(1 for item in scored if "stale" in item["quality_flags"]),
        "deferred": sum(1 for item in scored if "deferred" in item["quality_flags"]),
        "noisy_source": sum(1 for item in scored if "noisy-source" in item["quality_flags"]),
        "duplicate_pending": sum(1 for item in scored if "duplicate-pending" in item["quality_flags"]),
        "changed_dismissed": len(changed_dismissed),
    }
    issues = [
        {"status": constants.WARN, "name": f"inbox_quality_{name}", "detail": str(count)}
        for name, count in issue_counts.items()
        if count
    ]
    return {
        "schema_version": 1,
        "schema": {"name": "work-inbox-quality", "version": 1},
        "target": str(target),
        "pending_count": len(pending),
        "scored_imports": scored,
        "best_import": scored[0] if scored else None,
        "issue_counts": issue_counts,
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "noisy_sources": sorted(noisy_sources),
        "changed_dismissed_import_ids": sorted(set(changed_dismissed)),
    }


def inbox(*, target: Path, json_output: bool = False, limit: int = 20) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _inbox_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    counts = payload["counts"]
    print(f"work inbox: {target}")
    print(f"imports_path: {payload['imports_path']}")
    print(f"pending_imports: {counts['total']}")
    if counts["by_source"]:
        print("by_source:")
        for source, count in counts["by_source"].items():
            print(f"  {source}: {count}")
    if counts["by_kind"]:
        print("by_kind:")
        for kind, count in counts["by_kind"].items():
            print(f"  {kind}: {count}")
    if counts["by_priority"]:
        print("task_priorities:")
        for priority, count in counts["by_priority"].items():
            print(f"  {priority}: {count}")
    acceptance = counts["acceptance"]
    print(f"task_acceptance_ready: {acceptance['ready']}")
    print(f"task_acceptance_missing: {acceptance['missing']}")
    print(f"handoff_ready: {counts.get('handoff_ready', 0)}")
    candidate = payload.get("candidate") or payload.get("handoff_candidate")
    if isinstance(candidate, dict):
        print("next:")
        print(f"  import: {candidate.get('id')}")
        print(f"  source: {candidate.get('source')}")
        print(f"  kind: {candidate.get('kind')}")
        if candidate.get("kind") == "task":
            print(f"  priority: {candidate.get('priority')}")
            print(f"  acceptance: {candidate.get('acceptance_count')}")
        print(f"  text: {helpers._short(str(candidate.get('text', '')))}")
        context = candidate.get("context") if isinstance(candidate.get("context"), dict) else {}
        if context:
            rendered = ", ".join(f"{key}={context[key]}" for key in sorted(context))
            print(f"  context: {rendered}")
        print(f"  plan: brigade work import plan {candidate.get('id')}")
        if candidate.get("kind") == "task":
            print(f"  promote: brigade work import promote {candidate.get('id')}")
            print(f"  run: brigade work import promote --run {candidate.get('id')}")
        elif candidate.get("kind") in constants.HANDOFF_READY_KINDS:
            print(f"  plan_handoff: brigade work import plan-handoff {candidate.get('id')}")
            print(f"  promote_handoff: brigade work import promote-handoff {candidate.get('id')}")
        print(f'  dismiss: brigade work import dismiss {candidate.get("id")} --reason "..."')
    imports = payload.get("imports") if isinstance(payload.get("imports"), list) else []
    if imports:
        print("items:")
        for item in imports[:limit]:
            detail = f"[{item.get('kind')}] {item.get('source')}"
            if item.get("kind") == "task":
                detail += f" {item.get('priority')} acceptance={item.get('acceptance_count')}"
            print(f"- {item.get('id')} {detail}: {helpers._short(str(item.get('text', '')))}")
            context = item.get("context") if isinstance(item.get("context"), dict) else {}
            if context:
                rendered = ", ".join(f"{key}={context[key]}" for key in sorted(context))
                print(f"  context: {rendered}")
        if len(imports) > limit:
            print(f"... {len(imports) - limit} more")
    return 0


def inbox_doctor(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _inbox_hygiene_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"work inbox doctor: {target}")
    print(f"imports_path: {payload['imports_path']}")
    print(f"archive_path: {payload['archive_path']}")
    for check in payload["checks"]:
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    return 0


def _archive_import_cutoff(item: dict[str, Any]) -> datetime | None:
    for key in ("updated_at", "dismissed_at", "promoted_at", "created_at"):
        parsed = helpers._parse_iso_datetime(item.get(key))
        if parsed is not None:
            return parsed
    return None


def inbox_archive(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    now = helpers._now()
    imports = [item for item in ledger_mod._read_imports(target) if isinstance(item, dict)]
    archived: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for item in imports:
        status = str(item.get("status", "pending"))
        timestamp = _archive_import_cutoff(item)
        age_hours = (now - timestamp).total_seconds() / 3600 if timestamp is not None else 0
        if status in {"promoted", "dismissed", "superseded"} and age_hours >= constants.IMPORT_ARCHIVE_STALE_HOURS:
            archived_item = dict(item)
            archived_item["archived_at"] = now.isoformat()
            archived_item["archive_reason"] = f"{status}_older_than_{constants.IMPORT_ARCHIVE_STALE_HOURS}h"
            archived.append(archived_item)
        else:
            kept.append(item)
    if archived:
        ledger_mod._append_archived_imports(target, archived)
        ledger_mod._write_imports(target, kept)
    payload = {
        "target": str(target),
        "imports_path": str(helpers._imports_path(target)),
        "archive_path": str(helpers._imports_archive_path(target)),
        "archived": len(archived),
        "kept": len(kept),
        "archived_imports": archived,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"work inbox archive: {target}")
    print(f"imports_path: {payload['imports_path']}")
    print(f"archive_path: {payload['archive_path']}")
    print(f"archived: {payload['archived']}")
    print(f"kept: {payload['kept']}")
    for item in archived[:20]:
        print(f"- {item.get('id')} [{item.get('status')}] {helpers._short(str(item.get('text', '')))}")
    return 0


def _import_plan_payload(target: Path, import_id: str) -> tuple[dict[str, Any] | None, int]:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return None, 2
    item, _ = ledger_mod._find_import(target, import_id)
    if item is None:
        print(f"error: import not found: {import_id}", file=sys.stderr)
        return None, 1
    summary = ledger_mod._import_summary(item)
    payload: dict[str, Any] = {
        "target": str(target),
        "imports_path": str(helpers._imports_path(target)),
        "import": summary,
        "suggested_promote_command": f"brigade work import promote {item.get('id')}",
        "suggested_dismiss_command": f'brigade work import dismiss {item.get("id")} --reason "..."',
    }
    if item.get("kind") == "task":
        task = ledger_mod._task_preview_from_import(item)
        template = task.get("template") if isinstance(task.get("template"), str) else None
        payload["task"] = task
        if template:
            payload["guidance"] = list(constants.TASK_TEMPLATES.get(template, {}).get("guidance", ()))
        payload["suggested_run_command"] = f"brigade work import promote --run {item.get('id')}"
        payload["recommended_action"] = "promote-task"
    elif item.get("kind") in constants.HANDOFF_READY_KINDS:
        handoff = ledger_mod._import_handoff_plan_payload(target, item)
        payload["handoff"] = {
            "ready": handoff["handoff_ready"],
            "target_document": handoff["target_document"],
            "handoff_type": handoff["handoff_type"],
            "handoff_inbox": handoff["handoff_inbox"],
            "blockers": handoff["blockers"],
            "provenance": handoff["provenance"],
        }
        payload["recommended_action"] = "promote-handoff" if handoff["handoff_ready"] else "dismiss-or-fix"
        payload["suggested_promote_handoff_command"] = handoff["suggested_promote_handoff_command"]
    else:
        payload["recommended_action"] = "dismiss-or-fix"
    return payload, 0
