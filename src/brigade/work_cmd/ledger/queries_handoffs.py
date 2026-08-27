"""Stage 1 compatibility seam for the task and import ledger."""
# ruff: noqa: F401

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence, cast
from uuid import uuid4

from .. import constants, edges as edges_mod, helpers, inbox_lock
from ..inbox_lock import verify_canonical_write_locks
from ... import component_paths, evidence_redaction, provenance, runguard, trust_gate
from ...untrusted import scan_handoff_injection_heuristics

from . import inbox_provenance, tasks_plans, import_model, locking


def _metadata(item: Mapping[str, Any]) -> dict[str, Any]:
    value = item.get("metadata")
    return value if isinstance(value, dict) else {}


def _pending_tasks(target: Path) -> list[dict[str, Any]]:
    ledger = locking._read_task_ledger(target)
    tasks = [
        task
        for task in ledger["tasks"]
        if isinstance(task, dict)
        and task.get("status", "pending") == "pending"
        and isinstance(task.get("text"), str)
        and task["text"].strip()
    ]
    tasks.sort(key=import_model._task_sort_key)
    return tasks


def _ready_tasks(target: Path) -> list[dict[str, Any]]:
    """Pending tasks with no open readiness blockers (wraps ``_pending_tasks`` semantics)."""
    ledger = locking._read_task_ledger(target)
    resolution = edges_mod.resolve_readiness(ledger)
    ready_ids = {item["id"] for item in resolution.ready}
    tasks = [task for task in _pending_tasks(target) if task.get("id") in ready_ids]
    tasks.sort(key=import_model._task_sort_key)
    return tasks


def _startable_tasks(target: Path) -> list[dict[str, Any]]:
    """Ready tasks that also pass decision and plan-receipt start gates (#1034)."""
    from ..claiming import ClaimError

    ledger = locking._read_task_ledger(target)
    resolution = edges_mod.resolve_readiness(ledger)
    ready_ids = {item["id"] for item in resolution.ready}
    startable: list[dict[str, Any]] = []
    for task in _pending_tasks(target):
        if task.get("id") not in ready_ids:
            continue
        try:
            _evaluate_task_start_gates(target, ledger, task, require_pending=True)
        except ClaimError:
            continue
        startable.append(task)
    startable.sort(key=import_model._task_sort_key)
    return startable


def _evaluate_task_start_gates(
    target: Path,
    ledger: dict[str, Any],
    task: dict[str, Any],
    *,
    require_pending: bool = False,
) -> None:
    """Raise ``ClaimError`` if the task cannot start. Caller holds or accepts a snapshot.

    Shared by claim, default run, and explicit ``work run --task-id`` (#1034, #1035).
    Claim retries leave ``require_pending`` false so ``apply_claim_to_task``
    can still honor idempotent claim ids and already-claimed conflicts.
    """
    from ..claiming import ClaimError, REASON_NOT_READY, readiness_block_for_task

    status = str(task.get("status") or "pending")
    if status != "pending":
        if require_pending:
            raise ClaimError(
                f"task {task.get('id')} is not ready to start (status={status})",
                reason=REASON_NOT_READY,
                details={"task_id": task.get("id"), "status": status, "block": {"reason": "wrong_status"}},
            )
        return
    try:
        unresolved = tasks_plans._unresolved_plan_decisions(target, str(task.get("id")), task=task)
    except tasks_plans.PlanDecisionError as exc:
        raise ClaimError(
            str(exc),
            reason=exc.reason,
            details={"task_id": task.get("id"), **exc.details},
            exit_code=exc.exit_code,
        ) from exc
    if unresolved:
        raise ClaimError(
            tasks_plans._decision_gate_message(unresolved),
            reason="unresolved_plan_decisions",
            details={
                "task_id": task.get("id"),
                "unresolved_decision_ids": [item.get("id") for item in unresolved],
            },
            exit_code=2,
        )
    block = readiness_block_for_task(ledger, str(task.get("id")))
    if block is not None:
        raise ClaimError(
            f"task {task.get('id')} is not ready to start (reason={block.get('reason')})",
            reason=REASON_NOT_READY,
            details={"task_id": task.get("id"), "status": status, "block": block},
        )


def _authorize_task_start(target: Path, task_id: str) -> dict[str, Any]:
    """Locked authorize-and-start CAS used by ``work run`` (#1034)."""
    from ..claiming import ClaimError

    with locking._task_ledger_lock(target):
        task, ledger = tasks_plans._find_task(target, task_id)
        if task is None:
            raise ClaimError(
                f"pending task not found: {task_id}",
                reason="unknown_task",
                details={"task_id": task_id},
                exit_code=1,
            )
        _evaluate_task_start_gates(target, ledger, task, require_pending=True)
        return task


def _readiness_payload(
    target: Path,
    *,
    explain: bool = False,
    parallel_safe: bool = False,
    impact_runner: Any | None = None,
) -> dict[str, Any]:
    from .. import partition as partition_mod

    ledger = locking._read_task_ledger(target)
    resolution = edges_mod.resolve_readiness(ledger)
    payload = resolution.as_dict(explain=explain)
    payload["target"] = str(target.expanduser().resolve())
    payload["tasks_path"] = str(helpers._tasks_path(target))
    payload["edges"] = edges_mod.ensure_ledger_edges(ledger)
    payload["pending_count"] = len(_pending_tasks(target))
    if parallel_safe:
        tasks_by_id = {
            str(task["id"]): task
            for task in ledger.get("tasks") or []
            if isinstance(task, dict) and isinstance(task.get("id"), str)
        }
        partition_payload = partition_mod.partition_ready(
            resolution.ready,
            tasks_by_id,
            target,
            impact_runner=impact_runner,
        )
        payload["parallel_safe"] = True
        payload.update(partition_payload)
    return payload


def _pending_imports_from_items(imports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pending = [
        item
        for item in imports
        if isinstance(item, dict)
        and item.get("status", "pending") == "pending"
        and isinstance(item.get("text"), str)
        and item["text"].strip()
    ]
    pending.sort(key=import_model._import_sort_key)
    return pending


def _pending_imports(target: Path, imports: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    return _pending_imports_from_items(import_model._read_imports(target) if imports is None else imports)


def _import_counts(imports: list[dict[str, Any]]) -> dict[str, Any]:
    by_source: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for item in imports:
        source = str(item.get("source") or "manual")
        kind = str(item.get("kind") or "task")
        by_source[source] = by_source.get(source, 0) + 1
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return {
        "total": len(imports),
        "by_source": dict(sorted(by_source.items())),
        "by_kind": dict(sorted(by_kind.items())),
    }


def _matching_pending_imports(
    target: Path,
    *,
    kind: str | None = None,
    source: str | None = None,
    metadata_filters: dict[str, str] | None = None,
    imports: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    items = _pending_imports(target, imports=imports)
    if kind:
        items = [item for item in items if item.get("kind") == kind]
    if source:
        items = [item for item in items if item.get("source") == source]
    if metadata_filters:
        items = [item for item in items if _import_metadata_matches(item, metadata_filters)]
    return items


def _import_item_content_digest(item: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(item, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _import_snapshot_join_set(
    items: list[dict[str, Any]],
    *,
    kind: str | None = None,
    source: str | None = None,
    metadata_filters: dict[str, str] | None = None,
) -> frozenset[tuple[object, str]]:
    """Stable (id, content-digest) set for the filtered pending rows of one snapshot."""
    matching = _matching_pending_imports(
        Path(),
        kind=kind,
        source=source,
        metadata_filters=metadata_filters,
        imports=items,
    )
    return frozenset((item.get("id"), _import_item_content_digest(item)) for item in matching)


def _after_reviewed_import_snapshot(target: Path, snapshot: import_model._ReviewedImportSnapshot) -> None:
    """Test seam between binding the reviewed snapshot and the promote CAS."""
    del target, snapshot


def _after_batch_import_promote_applied(target: Path, snapshot: import_model._ReviewedImportSnapshot) -> None:
    """Test seam after in-memory batch apply and before the late-window CAS + flush."""
    del target, snapshot


def _require_reviewed_import_snapshot(
    target: Path,
    snapshot: import_model._ReviewedImportSnapshot,
    reviewed_join: frozenset[tuple[object, str]],
    *,
    kind: str | None = None,
    source: str | None = None,
    metadata_filters: dict[str, str] | None = None,
) -> None:
    current = import_model._capture_reviewed_import_snapshot(target)
    current_join = _import_snapshot_join_set(
        current.items,
        kind=kind,
        source=source,
        metadata_filters=metadata_filters,
    )
    if current.generation != snapshot.generation or current_join != reviewed_join:
        raise locking.TaskLedgerError(
            "import inbox changed since the reviewed snapshot",
            reason=locking.REASON_IMPORT_SNAPSHOT_CHANGED,
            details={
                "reviewed_digest": snapshot.digest,
                "current_digest": current.digest,
            },
        )


def _import_metadata_matches(item: dict[str, Any], filters: dict[str, str]) -> bool:
    metadata = _metadata(item)
    for key, expected in filters.items():
        if str(metadata.get(key, "")) != expected:
            return False
    return True


def _parse_metadata_filters(values: list[str] | None) -> tuple[dict[str, str], list[str]]:
    filters: dict[str, str] = {}
    errors: list[str] = []
    for raw in values or []:
        if "=" not in raw:
            errors.append(f"--metadata filter must be key=value: {raw}")
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            errors.append(f"--metadata filter key cannot be empty: {raw}")
            continue
        filters[key] = value.strip()
    return filters, errors


def _parse_or_report_metadata_filters(values: list[str] | None) -> tuple[dict[str, str] | None, int]:
    filters, errors = _parse_metadata_filters(values)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return None, 2
    return filters, 0


def _find_pending_task_by_text(target: Path, text: str) -> dict[str, Any] | None:
    wanted = import_model._task_text_key(text)
    if not wanted:
        return None
    for task in _pending_tasks(target):
        if import_model._task_text_key(str(task.get("text") or "")) == wanted:
            return task
    return None


def _find_import(target: Path, import_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    imports = import_model._read_imports(target)
    matches: list[dict[str, Any]] = []
    for item in imports:
        if not isinstance(item, dict):
            continue
        if item.get("id") == import_id:
            return item, imports
        if isinstance(item.get("id"), str) and item["id"].startswith(import_id):
            matches.append(item)
    if len(matches) == 1:
        return matches[0], imports
    return None, imports


def _import_trust_blocker(item: Mapping[str, Any] | None) -> str | None:
    """Refuse promotion unless the current bytes still match a complete envelope."""

    return trust_gate.promotion_blocker(item)


def _mark_import_promoted(
    target: Path,
    item: dict[str, Any],
    *,
    ledger: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    blocker = _import_trust_blocker(item)
    if blocker:
        raise locking.TaskLedgerError(blocker, reason=locking.REASON_TRUST_POLICY)
    text = str(item.get("text") or "").strip()
    metadata: dict[str, Any] = {
        "import_id": item.get("id"),
        "import_kind": item.get("kind"),
        "import_source": item.get("source"),
    }
    item_metadata = _metadata(item)
    metadata.update(item_metadata)
    template = (
        item.get("template")
        if isinstance(item.get("template"), str) and item.get("template") in constants.TASK_TEMPLATES
        else None
    )
    acceptance = item.get("acceptance") if isinstance(item.get("acceptance"), list) else None
    proposed = edges_mod.proposed_edges_from_metadata(item_metadata, new_task_id="self")
    source = f"import:{item.get('source') or 'manual'}"
    task_type = str(item.get("type") or "task")
    priority = str(item.get("priority") or "normal")
    combined_acceptance = import_model._combined_acceptance(template, acceptance)
    if ledger is None:
        task, created = tasks_plans._add_task(
            target,
            text,
            source=source,
            metadata=metadata,
            task_type=task_type,
            priority=priority,
            acceptance=combined_acceptance,
            template=template,
            proposed_edges=proposed,
        )
    else:
        # Batch callers flush after the late-window CAS succeeds (#1059).
        task, created = tasks_plans._apply_add_task_to_ledger(
            target,
            ledger,
            text,
            source=source,
            metadata=metadata,
            task_type=task_type,
            priority=priority,
            acceptance=combined_acceptance,
            template=template,
            proposed_edges=proposed,
        )
    now = helpers._now().isoformat()
    item["status"] = "promoted"
    item["updated_at"] = now
    item["promoted_at"] = now
    item["task_id"] = task["id"]
    return task, created


def _promote_matching_imports(
    target: Path,
    *,
    kind: str | None = None,
    source: str | None = None,
    metadata_filters: dict[str, str] | None = None,
) -> tuple[list[tuple[dict[str, Any], dict[str, Any], bool]], list[tuple[dict[str, Any], Exception]]]:
    """Promote --all from one locked inbox generation. Fail closed if it changes.

    Each matching item is applied all-or-nothing in memory. The task ledger is
    flushed only after the late-window CAS succeeds, so a refused promote writes
    no task files and a failed item cannot ride a later success. Lock order is
    task-ledger first, then the canonical writer locks (run then writer for an
    outside promoter; writer only inside a run) spanning the snapshot through
    the final publication so scanner stamping or rollback cannot interleave
    with the promote.
    """
    with locking._task_ledger_lock(target), locking._canonical_inbox_write(target):
        return _promote_matching_imports_locked(
            target,
            kind=kind,
            source=source,
            metadata_filters=metadata_filters,
        )


def _promote_matching_imports_locked(
    target: Path,
    *,
    kind: str | None = None,
    source: str | None = None,
    metadata_filters: dict[str, str] | None = None,
) -> tuple[list[tuple[dict[str, Any], dict[str, Any], bool]], list[tuple[dict[str, Any], Exception]]]:
    verify_canonical_write_locks(target)
    snapshot = import_model._capture_reviewed_import_snapshot(target)
    reviewed_join = _import_snapshot_join_set(
        snapshot.items,
        kind=kind,
        source=source,
        metadata_filters=metadata_filters,
    )
    _after_reviewed_import_snapshot(target, snapshot)
    _require_reviewed_import_snapshot(
        target,
        snapshot,
        reviewed_join,
        kind=kind,
        source=source,
        metadata_filters=metadata_filters,
    )
    matching = _matching_pending_imports(
        target,
        kind=kind,
        source=source,
        metadata_filters=metadata_filters,
        imports=snapshot.items,
    )
    ledger = locking._read_task_ledger(target)
    promoted: list[tuple[dict[str, Any], dict[str, Any], bool]] = []
    failed: list[tuple[dict[str, Any], Exception]] = []
    created_any = False
    for item in matching:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        tasks_before = list(ledger["tasks"])
        edges_before = list(edges_mod.ensure_ledger_edges(ledger))
        try:
            task, created = _mark_import_promoted(target, item, ledger=ledger)
        except (edges_mod.EdgeError, locking.TaskLedgerError) as exc:
            ledger["tasks"][:] = tasks_before
            ledger["edges"] = edges_before
            failed.append((item, exc))
            continue
        promoted.append((item, task, created))
        created_any = created_any or created
    _after_batch_import_promote_applied(target, snapshot)
    _require_reviewed_import_snapshot(
        target,
        snapshot,
        reviewed_join,
        kind=kind,
        source=source,
        metadata_filters=metadata_filters,
    )
    if created_any:
        locking._write_task_ledger(target, ledger)
    import_model._write_imports(target, snapshot.items)
    return promoted, failed


def _handoff_is_document_target(value: str) -> bool:
    if value.startswith("/") or ".." in Path(value).parts:
        return False
    if value in {"TOOLS.md", "USER.md"}:
        return True
    return (value.startswith("rules/") or value.startswith(".learnings/")) and value.endswith(".md")


def _handoff_target_document(item: dict[str, Any]) -> str:
    metadata = _metadata(item)
    override = metadata.get("handoff_target_document") or metadata.get("target_document")
    if isinstance(override, str) and _handoff_is_document_target(override.strip()):
        return override.strip()
    kind = str(item.get("kind") or "finding")
    category = " ".join(
        str(metadata.get(key) or "")
        for key in ("category", "issue_type", "handoff_category", "memory_target", "reason")
    ).casefold()
    if "feature" in category or "request" in category:
        return ".learnings/FEATURE_REQUESTS.md"
    if "workflow" in category or "rule" in category or "policy" in category:
        return "rules/scanner-imports.md"
    if "failure" in category or "error" in category or "bug" in category:
        return ".learnings/ERRORS.md"
    if kind == "finding" and str(item.get("source") or "") == "security-scan":
        return ".learnings/ERRORS.md"
    return constants.HANDOFF_TARGETS.get(kind, ".learnings/LEARNINGS.md")


def _handoff_type(item: dict[str, Any], target_document: str) -> str:
    kind = str(item.get("kind") or "finding")
    source = str(item.get("source") or "")
    if kind == "preference":
        return "preference"
    if kind == "incident" or target_document.endswith("ERRORS.md"):
        return "bugfix"
    if source == "security-scan":
        return "security"
    if target_document.startswith("rules/"):
        return "workflow"
    if kind == "decision":
        return "decision"
    return "project-context"


# Closed canonical provenance envelope shape for handoff privacy walks.
# Only these keys (and nested closed sets) may bypass private-field detection.
_PROVENANCE_ENVELOPE_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "source",
        "origin",
        "repository",
        "session",
        "collection_id",
        "item_id",
        "locator",
        "attribution",
        "modality",
        "trust",
        "hashes",
        "redaction",
        "captured_at",
        "ingested_at",
    }
)
_PROVENANCE_SOURCE_KEYS = frozenset({"system", "kind", "producer"})
_PROVENANCE_REPOSITORY_KEYS = frozenset({"id", "revision"})
_PROVENANCE_SESSION_KEYS = frozenset({"id", "harness"})
_PROVENANCE_LOCATOR_KEYS = frozenset({"kind", "value"})
_PROVENANCE_TRUST_KEYS = frozenset({"label", "assigned_by", "assigned_at", "trust_policy", "injection"})
_PROVENANCE_TRUST_POLICY_KEYS = frozenset({"schema", "schema_version"})
_PROVENANCE_INJECTION_KEYS = frozenset({"status", "count", "rules"})
_PROVENANCE_REDACTION_KEYS = frozenset(
    {"schema", "schema_version", "policy_version", "origin", "status", "count", "detectors"}
)
_PROVENANCE_HASHES_KEYS = frozenset(
    {"content_algorithm", "content_scope", "content", "raw_algorithm", "raw_scope", "raw"}
)


def _handoff_private_fields_in_validated_provenance(
    envelope: dict[str, Any],
    *,
    path: tuple[str, ...],
) -> list[str]:
    """Walk a validated envelope; only closed canonical fields are exempt."""

    def walk(node: object, *, allowed: frozenset[str] | None, node_path: tuple[str, ...]) -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            for key, item in node.items():
                key_text = str(key)
                child_path = (*node_path, key_text)
                if allowed is not None and key_text not in allowed:
                    # Non-canonical extras never inherit the envelope exemption.
                    found.append(".".join(child_path))
                    found.extend(_handoff_private_fields(item, path=child_path))
                    continue
                child_allowed: frozenset[str] | None
                if key_text == "source":
                    child_allowed = _PROVENANCE_SOURCE_KEYS
                elif key_text == "repository":
                    child_allowed = _PROVENANCE_REPOSITORY_KEYS
                elif key_text == "session":
                    child_allowed = _PROVENANCE_SESSION_KEYS
                elif key_text == "locator":
                    child_allowed = _PROVENANCE_LOCATOR_KEYS
                elif key_text == "trust":
                    child_allowed = _PROVENANCE_TRUST_KEYS
                elif key_text == "trust_policy":
                    child_allowed = _PROVENANCE_TRUST_POLICY_KEYS
                elif key_text == "injection":
                    child_allowed = _PROVENANCE_INJECTION_KEYS
                elif key_text == "hashes":
                    child_allowed = _PROVENANCE_HASHES_KEYS
                elif key_text == "redaction":
                    child_allowed = _PROVENANCE_REDACTION_KEYS
                else:
                    child_allowed = None
                if isinstance(item, (dict, list)) and child_allowed is not None:
                    found.extend(walk(item, allowed=child_allowed, node_path=child_path))
                elif isinstance(item, (dict, list)) and allowed is None:
                    found.extend(_handoff_private_fields(item, path=child_path))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                found.extend(walk(item, allowed=None, node_path=(*node_path, str(index))))
        return found

    return walk(envelope, allowed=_PROVENANCE_ENVELOPE_KEYS, node_path=path)


def _handoff_private_fields(value: object, *, path: tuple[str, ...] = ()) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            normalized = key_text.strip().casefold()
            is_top_text = not path and normalized == "text"
            # Envelope schema fields include hashes.raw / raw_* digests (often null).
            # Only closed canonical fields of a validated envelope are exempt; extra
            # keys such as raw_text/secret/path remain subject to detection.
            if (
                normalized == "provenance"
                and isinstance(item, dict)
                and item.get("schema") == provenance.SCHEMA
                and not provenance.validate_envelope(item)
            ):
                found.extend(_handoff_private_fields_in_validated_provenance(item, path=(*path, key_text)))
                continue
            if not is_top_text and (normalized in constants.RAW_CHAT_FIELDS or normalized.startswith("raw_")):
                found.append(".".join((*path, key_text)))
                continue
            found.extend(_handoff_private_fields(item, path=(*path, key_text)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_handoff_private_fields(item, path=(*path, str(index))))
    return sorted(set(found))


def _handoff_redact_value(value: object, *, key: str | None = None) -> object:
    normalized = (key or "").strip().casefold()
    if normalized in constants.HANDOFF_UNSAFE_FIELD_NAMES or any(
        token in normalized for token in ("password", "secret", "token", "webhook")
    ):
        return "[redacted]"
    if isinstance(value, str):
        return constants.HANDOFF_UNSAFE_VALUE_RE.sub("[redacted]", value)
    if isinstance(value, list):
        return [_handoff_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(item_key): _handoff_redact_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    return value


def _handoff_render_value(value: object) -> str:
    redacted = _handoff_redact_value(value)
    if isinstance(redacted, str):
        return redacted.replace("\n", " ").strip()
    return json.dumps(redacted, sort_keys=True, default=str)


def _handoff_provenance(item: dict[str, Any]) -> dict[str, Any]:
    metadata = _metadata(item)
    keys = (
        "source_fingerprint",
        "scanner_id",
        "scanner_source",
        "scanner_run_id",
        "scanner_receipt_path",
        "scanner_output_path_snapshot",
        "scanner_import_path",
        "sweep_id",
        "sweep_issue_id",
        "sweep_path",
        "evidence_summary",
        "evidence",
        "local_evidence_path",
        "provider",
        "workspace",
        "channel",
        "thread",
        "message_range",
        "confidence",
    )
    provenance: dict[str, Any] = {
        "import_id": item.get("id"),
        "source": item.get("source"),
        "kind": item.get("kind"),
    }
    for key in keys:
        value = metadata.get(key)
        if value not in (None, ""):
            provenance[key] = _handoff_redact_value(value, key=key)
    fingerprint = inbox_provenance._import_fingerprint(item)
    if fingerprint and "source_fingerprint" not in provenance:
        provenance["source_fingerprint"] = fingerprint
    return provenance


def _handoff_safe_text(value: object) -> str:
    return _handoff_render_value(value)[:500]


def _handoff_title(item: dict[str, Any]) -> str:
    text = _handoff_safe_text(item.get("text") or "scanner import")
    return helpers._short(text, 80) or "Reviewed scanner import"


def _handoff_suggested_document_content(item: dict[str, Any], target_document: str) -> str:
    metadata = _metadata(item)
    provenance = _handoff_provenance(item)
    title = _handoff_title(item)
    lines = [
        f"### Reviewed scanner import: {title}",
        "",
        f"- source: {_handoff_safe_text(item.get('source') or 'manual')}",
        f"- kind: {_handoff_safe_text(item.get('kind') or 'finding')}",
        f"- import: {_handoff_safe_text(item.get('id') or '')}",
        f"- summary: {_handoff_safe_text(item.get('text') or '')}",
    ]
    for key in ("evidence_summary", "safe_summary", "reason", "issue_type", "category"):
        if metadata.get(key) not in (None, ""):
            lines.append(f"- {key}: {_handoff_safe_text(metadata[key])}")
    if target_document.startswith("rules/"):
        lines.append(
            "- rule: Review this scanner import and convert the durable workflow correction into a concise rule."
        )
    elif target_document == "TOOLS.md":
        lines.append("- operational note: Review this command or tool detail before adding it to durable tool notes.")
    elif target_document == "USER.md":
        lines.append("- preference note: Review this preference before adding it to durable user context.")
    else:
        lines.append("- memory note: Review this item before adding it to durable memory.")
    if provenance:
        lines.append("- provenance:")
        for key in sorted(provenance):
            lines.append(f"  - {key}: {_handoff_safe_text(provenance[key])}")
    return "\n".join(lines)


def _render_import_handoff(target: Path, item: dict[str, Any], target_document: str) -> str:
    title = _handoff_title(item)
    provenance = _handoff_provenance(item)
    evidence_lines = [
        f"- import: {item.get('id')}",
        f"- source: {_handoff_safe_text(item.get('source') or 'manual')}",
        f"- kind: {_handoff_safe_text(item.get('kind') or 'finding')}",
    ]
    for key in sorted(provenance):
        if key in {"import_id", "source", "kind"}:
            continue
        evidence_lines.append(f"- {key}: {_handoff_safe_text(provenance[key])}")
    content = _handoff_suggested_document_content(item, target_document)
    return f"""# Memory Handoff

## Type
{_handoff_type(item, target_document)}

## Title
{title}

## Summary
Reviewed scanner import `{item.get("id")}` from `{_handoff_safe_text(item.get("source") or "manual")}`. This handoff preserves the safe conclusion and local provenance without editing canonical memory directly.

## Durable facts
- Source import kind: {_handoff_safe_text(item.get("kind") or "finding")}
- Source import status at promotion: {_handoff_safe_text(item.get("status") or "pending")}
- Target document: {target_document}

## Evidence
{chr(10).join(evidence_lines)}

## Recommended memory action
no-card

## Target document
{target_document}

## Suggested document content
{content}
"""


def _import_handoff_plan_payload(target: Path, item: dict[str, Any]) -> dict[str, Any]:
    target = target.expanduser().resolve()
    target_document = _handoff_target_document(item)
    inbox = helpers._handoff_inbox(target, {}, None)
    private_fields = _handoff_private_fields(item)
    blockers: list[str] = []
    if item.get("status", "pending") != "pending":
        blockers.append(f"import is not pending: {item.get('status')}")
    if item.get("kind") not in constants.HANDOFF_READY_KINDS:
        blockers.append(f"import kind is not handoff-ready: {item.get('kind')}")
    if not str(item.get("text") or "").strip():
        blockers.append("import text is required")
    if private_fields:
        blockers.append("raw private chat fields are not allowed: " + ", ".join(private_fields))
    if not _handoff_is_document_target(target_document):
        blockers.append(f"handoff target document is invalid: {target_document}")
    trust_blocker = _import_trust_blocker(item)
    if trust_blocker:
        blockers.append(trust_blocker)
    return {
        "target": str(target),
        "imports_path": str(helpers._imports_path(target)),
        "handoff_inbox": str(inbox),
        "import": import_model._import_summary(item),
        "handoff_ready": not blockers,
        "target_document": target_document,
        "handoff_type": _handoff_type(item, target_document),
        "provenance": _handoff_provenance(item),
        "private_fields": private_fields,
        "blockers": blockers,
        "suggested_promote_handoff_command": f"brigade work import promote-handoff {item.get('id')}",
        "suggested_dismiss_command": f'brigade work import dismiss {item.get("id")} --reason "..."',
    }


def _write_import_handoff(target: Path, item: dict[str, Any], target_document: str) -> Path:
    now = helpers._now()
    inbox = helpers._handoff_inbox(target, {}, None)
    inbox.mkdir(parents=True, exist_ok=True)
    path = (
        inbox
        / f"{now.strftime('%Y-%m-%d-%H%M')}-scanner-import-{helpers._slug(str(item.get('kind') or 'finding'))}-{helpers._slug(str(item.get('id') or 'import'))}-{uuid4().hex[:6]}.md"
    )
    path.write_text(_render_import_handoff(target, item, target_document))
    return path


def _mark_import_handoff_promoted(
    target: Path, item: dict[str, Any], *, handoff_path: Path, target_document: str
) -> None:
    now = helpers._now().isoformat()
    item["status"] = "promoted"
    item["updated_at"] = now
    item["promoted_at"] = now
    item["handoff_path"] = str(handoff_path)
    item["handoff_target_document"] = target_document
    item["handoff_source_fingerprint"] = inbox_provenance._import_fingerprint(item)
