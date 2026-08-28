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

from . import inbox_provenance, queries_handoffs, authority_store, descriptor_anchors, locking

_GITHUB_ISSUE_VIEW_TIMEOUT_SECONDS = 30.0


def _metadata(item: Mapping[str, Any]) -> dict[str, Any]:
    value = item.get("metadata")
    return value if isinstance(value, dict) else {}


def _stale_claim_payload(target: Path, *, stale_after_hours: float | None = None) -> dict[str, Any]:
    from ..claiming import list_stale_claims

    hours = constants.CLAIM_STALE_HOURS if stale_after_hours is None else stale_after_hours
    ledger = locking._read_task_ledger(target)
    stale = list_stale_claims(ledger, stale_after_hours=hours)
    return {
        "stale_after_hours": hours,
        "stale_count": len(stale),
        "stale": stale,
    }


class _ReviewedImportSnapshot:
    """One inbox generation: exact bytes, identity, digest, and parsed rows."""

    def __init__(
        self,
        items: list[dict[str, Any]],
        *,
        raw: bytes,
        digest: str,
        identity: tuple[int, int, int, int] | None,
        exists: bool,
    ) -> None:
        self.items = items
        self.raw = raw
        self.digest = digest
        self.identity = identity
        self.exists = exists

    @property
    def generation(self) -> tuple[str, tuple[int, int, int, int] | None, bool]:
        return (self.digest, self.identity, self.exists)


def _import_inbox_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _parse_import_inbox_bytes(raw: bytes) -> list[dict[str, Any]]:
    imports: list[dict[str, Any]] = []
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            imports.append(item)
    return imports


def _read_import_inbox_raw(target: Path) -> tuple[bytes, tuple[int, int, int, int] | None, bool]:
    """Read inbox bytes once from a retained descriptor. Raises if the object moves."""
    parent = -1
    descriptor = -1
    try:
        parent, name = inbox_provenance._open_import_inbox_parent(target, create=False)
        try:
            descriptor = authority_store._dirfd_open_file(
                parent,
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
            )
        except FileNotFoundError:
            return b"", None, False
        inbox_provenance._validate_import_inbox_descriptor(descriptor)
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        snapshot_total = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            snapshot_total += len(chunk)
            if snapshot_total > locking._IMPORT_INBOX_SNAPSHOT_LIMIT_BYTES:
                raise locking.ImportInboxSnapshotLimitExceeded(
                    f"import inbox exceeds {locking._IMPORT_INBOX_SNAPSHOT_LIMIT_BYTES} byte snapshot limit"
                )
            chunks.append(chunk)
        after = os.fstat(descriptor)
        identity = (before.st_dev, before.st_ino, before.st_mode, before.st_nlink)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise OSError("import inbox changed while snapshotting")
        raw = b"".join(chunks)
        if len(raw) != before.st_size:
            raise OSError("import inbox changed while snapshotting")
        return raw, identity, True
    finally:
        if descriptor != -1:
            os.close(descriptor)
        if parent != -1:
            os.close(parent)


def _capture_reviewed_import_snapshot(target: Path) -> _ReviewedImportSnapshot:
    """Bind parsed rows to the exact inbox generation that was read."""
    try:
        raw, identity, exists = _read_import_inbox_raw(target)
        items = _parse_import_inbox_bytes(raw)
    except FileNotFoundError:
        raw, identity, exists, items = b"", None, False, []
    except UnicodeDecodeError as exc:
        raise locking.TaskLedgerError(
            "import inbox is not valid UTF-8",
            reason=locking.REASON_IMPORT_SNAPSHOT_CHANGED,
            details={"target": str(target)},
        ) from exc
    except OSError as exc:
        raise locking.TaskLedgerError(
            "import inbox could not be snapshotted",
            reason=locking.REASON_IMPORT_SNAPSHOT_CHANGED,
            details={"target": str(target), "error": str(exc)},
        ) from exc
    return _ReviewedImportSnapshot(
        items,
        raw=raw,
        digest=_import_inbox_digest(raw),
        identity=identity,
        exists=exists,
    )


def _read_imports(target: Path) -> list[dict[str, Any]]:
    try:
        raw, _identity, _exists = _read_import_inbox_raw(target)
        return _parse_import_inbox_bytes(raw)
    except (OSError, UnicodeDecodeError):
        return []


def _write_imports(target: Path, imports: list[dict[str, Any]]) -> None:
    """Publish the inbox under the canonical writer exclusion.

    Every canonical writer holds the writer lock (outsiders take the run lock
    first) so a concurrent import, promote, dismiss, or scanner run cannot
    interleave with scanner stamping or rollback (reentrant, so callers
    already inside the lock are unaffected).
    """
    with locking._canonical_inbox_write(target):
        verify_canonical_write_locks(target)
        parent, name, previous_raw, previous_exists = inbox_provenance._snapshot_import_inbox(target)
        rendered = "".join(json.dumps(item, sort_keys=True) + "\n" for item in imports).encode("utf-8")
        try:
            inbox_provenance._write_import_inbox_bytes_at(
                parent,
                name,
                rendered,
                previous_raw=previous_raw,
                previous_exists=previous_exists,
            )
        finally:
            os.close(parent)


def _append_archived_imports(target: Path, imports: list[dict[str, Any]]) -> None:
    if not imports:
        return
    path = helpers._imports_archive_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        for item in imports:
            handle.write(json.dumps(item, sort_keys=True) + "\n")


def _task_sort_key(task: dict[str, Any]) -> str:
    return str(task.get("created_at") or task.get("id") or "")


def _import_sort_key(item: dict[str, Any]) -> str:
    return str(item.get("created_at") or item.get("id") or "")


def _task_text_key(text: str) -> str:
    return " ".join(text.casefold().split())


def _string_field(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _confidence_rank(value: object) -> int:
    text = value.strip().casefold() if isinstance(value, str) else ""
    return constants.CONFIDENCE_RANK.get(text, 1)


def _normalize_task_type(value: object) -> str:
    if isinstance(value, str) and value.strip() in constants.TASK_TYPES:
        return value.strip()
    return "task"


def _normalize_task_priority(value: object) -> str:
    if isinstance(value, str) and value.strip() in constants.TASK_PRIORITIES:
        return value.strip()
    return "normal"


def _normalize_seat_class(value: object) -> str | None:
    """Return a valid seat-class hint, or None when absent.

    Raises ValueError when a value is present but not in TASK_SEAT_CLASSES.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"seat_class must be one of: {', '.join(constants.TASK_SEAT_CLASSES)}")
    text = value.strip()
    if not text:
        return None
    if text not in constants.TASK_SEAT_CLASSES:
        raise ValueError(f"seat_class must be one of: {', '.join(constants.TASK_SEAT_CLASSES)}")
    return text


def _normalize_spend_by(value: object) -> str | None:
    """Return a validated ISO-8601 spend-by deadline, or None when absent.

    Accepts date or datetime strings (Z suffix allowed). Raises ValueError when
    a value is present but not parseable as ISO-8601.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("spend_by must be an ISO-8601 date or datetime")
    text = value.strip()
    if not text:
        return None
    if helpers._parse_iso_datetime(text) is None:
        raise ValueError("spend_by must be an ISO-8601 date or datetime")
    return text


def _dispatch_annotations(task: dict[str, Any]) -> dict[str, str]:
    """Project present, valid seat_class / spend_by hints for ready output.

    Invalid or missing annotations are omitted so absent annotations change
    nothing for dispatchers.
    """
    metadata = _metadata(task)
    annotations: dict[str, str] = {}
    raw_seat = metadata.get("seat_class")
    if isinstance(raw_seat, str) and raw_seat.strip() in constants.TASK_SEAT_CLASSES:
        annotations["seat_class"] = raw_seat.strip()
    raw_spend = metadata.get("spend_by")
    if isinstance(raw_spend, str) and helpers._parse_iso_datetime(raw_spend.strip()) is not None:
        annotations["spend_by"] = raw_spend.strip()
    return annotations


def _sanitize_dispatch_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Normalize seat_class / spend_by keys in a metadata bag; raise on invalid."""
    cleaned = dict(metadata)
    if "seat_class" in cleaned:
        seat = _normalize_seat_class(cleaned.get("seat_class"))
        if seat is None:
            cleaned.pop("seat_class", None)
        else:
            cleaned["seat_class"] = seat
    if "spend_by" in cleaned:
        spend = _normalize_spend_by(cleaned.get("spend_by"))
        if spend is None:
            cleaned.pop("spend_by", None)
        else:
            cleaned["spend_by"] = spend
    return cleaned


def _merge_dispatch_annotations(
    metadata: dict[str, Any] | None,
    *,
    seat_class: object = None,
    spend_by: object = None,
    clear_seat_class: bool = False,
    clear_spend_by: bool = False,
) -> dict[str, Any] | None:
    """Merge or clear dispatch annotations into a metadata bag.

    Returns None when the resulting metadata would be empty. Raises ValueError
    on invalid seat_class / spend_by values.
    """
    merged: dict[str, Any] = dict(metadata) if isinstance(metadata, dict) else {}
    if clear_seat_class:
        merged.pop("seat_class", None)
    elif seat_class is not None:
        normalized = _normalize_seat_class(seat_class)
        if normalized is None:
            merged.pop("seat_class", None)
        else:
            merged["seat_class"] = normalized
    if clear_spend_by:
        merged.pop("spend_by", None)
    elif spend_by is not None:
        normalized_spend = _normalize_spend_by(spend_by)
        if normalized_spend is None:
            merged.pop("spend_by", None)
        else:
            merged["spend_by"] = normalized_spend
    return merged or None


def _normalize_acceptance(values: object) -> list[str]:
    if values is None:
        return []
    raw_values = values if isinstance(values, list) else [values]
    accepted: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        text = str(value).strip()
        if not text:
            continue
        key = _task_text_key(text)
        if key in seen:
            continue
        accepted.append(text)
        seen.add(key)
    return accepted


def _task_acceptance(task: dict[str, Any]) -> list[str]:
    values = task.get("acceptance")
    if values is None:
        metadata = _metadata(task)
        values = metadata.get("acceptance")
    return _normalize_acceptance(values)


def _task_summary(task: dict[str, Any]) -> dict[str, Any]:
    acceptance = _task_acceptance(task)
    summary = {
        "id": task.get("id"),
        "text": str(task.get("text") or ""),
        "status": task.get("status", "pending"),
        "source": task.get("source", "manual"),
        "type": _normalize_task_type(task.get("type")),
        "priority": _normalize_task_priority(task.get("priority")),
        "acceptance": acceptance,
        "acceptance_count": len(acceptance),
        "acceptance_missing": len(acceptance) == 0,
    }
    if isinstance(task.get("template"), str):
        summary["template"] = task["template"]
    assignee = _string_field(task.get("assignee"))
    if assignee:
        summary["assignee"] = assignee
    claim = task.get("claim") if isinstance(task.get("claim"), dict) else None
    if isinstance(claim, dict) and _string_field(claim.get("claim_id")):
        summary["claim_id"] = claim.get("claim_id")
        if isinstance(claim.get("claimed_at"), str):
            summary["claimed_at"] = claim["claimed_at"]
    metadata = _metadata(task)
    closeouts = metadata.get("review_closeouts")
    if isinstance(closeouts, list):
        review_count = len([item for item in closeouts if isinstance(item, dict)])
        unresolved = sum(int(item.get("unresolved_count") or 0) for item in closeouts if isinstance(item, dict))
        summary["review_closeout_count"] = review_count
        summary["review_unresolved_count"] = unresolved
    issue = _task_issue_metadata(task)
    if issue:
        summary["issue"] = issue
    return summary


def _import_task_acceptance(item: dict[str, Any]) -> list[str]:
    template = item.get("template") if isinstance(item.get("template"), str) else None
    acceptance = item.get("acceptance") if isinstance(item.get("acceptance"), list) else []
    return _combined_acceptance(template if template in constants.TASK_TEMPLATES else None, acceptance)


def _import_task_type(item: dict[str, Any]) -> str:
    return _normalize_task_type(item.get("type"))


def _import_task_priority(item: dict[str, Any]) -> str:
    return _normalize_task_priority(item.get("priority"))


def _import_task_template(item: dict[str, Any]) -> str | None:
    template = item.get("template")
    return template if isinstance(template, str) and template in constants.TASK_TEMPLATES else None


def _import_context(item: dict[str, Any]) -> dict[str, Any]:
    metadata = _metadata(item)
    keys = (
        "provider",
        "surface",
        "workspace",
        "channel",
        "thread",
        "message_range",
        "confidence",
        "evidence_summary",
        "card_file",
        "card_id",
        "refresh_reason",
        "reason",
    )
    return {key: metadata[key] for key in keys if metadata.get(key) not in (None, "")}


def _import_summary(item: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    created_at = item.get("created_at")
    created_dt = helpers._parse_iso_datetime(created_at)
    age_hours = None
    if created_dt is not None:
        age_hours = ((now or helpers._now()) - created_dt).total_seconds() / 3600
    summary: dict[str, Any] = {
        "id": item.get("id"),
        "text": str(item.get("text") or ""),
        "kind": item.get("kind", "task"),
        "source": item.get("source", "manual"),
        "status": item.get("status", "pending"),
        "created_at": created_at,
        "updated_at": item.get("updated_at"),
        "age_hours": round(age_hours, 2) if age_hours is not None else None,
        "metadata": _metadata(item),
        "context": _import_context(item),
    }
    if item.get("kind") == "task":
        acceptance = _import_task_acceptance(item)
        summary.update(
            {
                "type": _import_task_type(item),
                "priority": _import_task_priority(item),
                "template": _import_task_template(item),
                "acceptance": acceptance,
                "acceptance_count": len(acceptance),
                "acceptance_missing": len(acceptance) == 0,
            }
        )
    elif item.get("kind") in constants.HANDOFF_READY_KINDS:
        summary.update(
            {
                "handoff_ready": True,
                "target_document": queries_handoffs._handoff_target_document(item),
            }
        )
    if item.get("handoff_path"):
        summary["handoff_path"] = item.get("handoff_path")
    if item.get("handoff_target_document"):
        summary["handoff_target_document"] = item.get("handoff_target_document")
    return summary


def _task_preview_from_import(item: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "import_id": item.get("id"),
        "import_kind": item.get("kind"),
        "import_source": item.get("source"),
    }
    item_metadata = _metadata(item)
    metadata.update(item_metadata)
    template = _import_task_template(item)
    return {
        "text": str(item.get("text") or "").strip(),
        "source": f"import:{item.get('source') or 'manual'}",
        "type": _import_task_type(item),
        "priority": _import_task_priority(item),
        "template": template,
        "acceptance": _import_task_acceptance(item),
        "metadata": metadata,
    }


def _scanner_candidate(imports: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [
        item
        for item in imports
        if item.get("kind") == "task" and isinstance(item.get("text"), str) and item["text"].strip()
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            0 if _import_task_acceptance(item) else 1,
            _confidence_rank(_metadata(item).get("confidence")),
            0 if item.get("source") in {"chat-memory-sweep", "memory-refresh", "memory-care"} else 1,
            constants.PRIORITY_RANK.get(_import_task_priority(item), 2),
            str(item.get("created_at") or item.get("id") or ""),
        )
    )
    return candidates[0]


def _handoff_ready_imports(imports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = [
        item
        for item in imports
        if item.get("kind") in constants.HANDOFF_READY_KINDS
        and item.get("status", "pending") == "pending"
        and isinstance(item.get("text"), str)
        and item["text"].strip()
    ]
    candidates.sort(
        key=lambda item: (
            0 if item.get("source") in {"chat-memory-sweep", "memory-refresh", "memory-care"} else 1,
            _confidence_rank(_metadata(item).get("confidence")),
            str(item.get("created_at") or item.get("id") or ""),
        )
    )
    return candidates


def _handoff_candidate(imports: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = _handoff_ready_imports(imports)
    return candidates[0] if candidates else None


def _task_snapshot(task: dict[str, Any]) -> dict[str, Any]:
    summary = _task_summary(task)
    snapshot: dict[str, Any] = {
        "id": summary.get("id"),
        "text": summary.get("text"),
        "source": summary.get("source"),
        "type": summary.get("type"),
        "priority": summary.get("priority"),
        "acceptance": summary.get("acceptance", []),
        "acceptance_count": summary.get("acceptance_count", 0),
    }
    if summary.get("template"):
        snapshot["template"] = summary["template"]
    if summary.get("issue"):
        snapshot["issue"] = summary["issue"]
    return snapshot


def _template_acceptance(template: str | None) -> list[str]:
    if not template:
        return []
    item = constants.TASK_TEMPLATES.get(template)
    if item is None:
        return []
    return list(item["acceptance"])


def _combined_acceptance(template: str | None, explicit: list[str] | None) -> list[str]:
    return _normalize_acceptance([*_template_acceptance(template), *(explicit or [])])


def _normalize_issue_heading(text: str) -> str:
    value = text.strip().strip("#").strip().rstrip(":").casefold()
    value = re.sub(r"[*_`]+", "", value)
    return " ".join(value.split())


def _is_issue_acceptance_heading(text: str) -> bool:
    value = _normalize_issue_heading(text)
    if value in constants.ISSUE_ACCEPTANCE_HEADINGS or value in constants.ISSUE_TEST_HEADINGS:
        return True
    return "acceptance" in value or value.startswith("test ")


def _issue_heading(line: str) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    markdown = re.fullmatch(r"#{1,6}\s+(.+?)\s*#*", stripped)
    if markdown:
        return markdown.group(1)
    plain = re.fullmatch(r"([A-Za-z][A-Za-z0-9 _/-]{1,80}):", stripped)
    if plain:
        return plain.group(1)
    return None


def _issue_list_item(line: str) -> str | None:
    checkbox = re.fullmatch(r"\s*[-*+]\s+\[[ xX]\]\s+(.+?)\s*", line)
    if checkbox:
        return checkbox.group(1).strip()
    bullet = re.fullmatch(r"\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*", line)
    if bullet:
        return bullet.group(1).strip()
    return None


def _extract_issue_acceptance(body: object) -> list[str]:
    if not isinstance(body, str) or not body.strip():
        return []
    extracted: list[str] = []
    in_relevant_section = False
    for line in body.splitlines():
        heading = _issue_heading(line)
        if heading is not None:
            in_relevant_section = _is_issue_acceptance_heading(heading)
            continue
        item = _issue_list_item(line)
        if item is None:
            continue
        if re.fullmatch(r"\s*[-*+]\s+\[[ xX]\]\s+.+?\s*", line) or in_relevant_section:
            extracted.append(item)
    return _normalize_acceptance(extracted)


def _task_issue_metadata(task: dict[str, Any]) -> dict[str, Any] | None:
    metadata = _metadata(task)
    issue = metadata.get("github_issue") if isinstance(metadata.get("github_issue"), dict) else None
    if issue is None and metadata.get("github_issue_url"):
        issue = {
            "url": metadata.get("github_issue_url"),
            "number": metadata.get("github_issue_number"),
            "title": metadata.get("github_issue_title"),
            "labels": metadata.get("github_issue_labels"),
            "state": metadata.get("github_issue_state"),
            "source": metadata.get("github_issue_source"),
            "ref": metadata.get("github_issue_ref"),
        }
    if not isinstance(issue, dict):
        return None
    return {
        key: value
        for key, value in issue.items()
        if key in {"url", "number", "title", "labels", "state", "source", "ref"} and value is not None
    }


def _github_issue_ref(issue: dict[str, Any]) -> str | None:
    url = issue.get("url")
    if isinstance(url, str) and url.strip():
        return url.strip()
    number = issue.get("number")
    if isinstance(number, int):
        return str(number)
    if isinstance(number, str) and number.strip():
        return number.strip()
    return None


def _read_github_issue(target: Path, issue_ref: str) -> tuple[dict[str, Any] | None, list[str], str | None]:
    if shutil.which("gh") is None:
        return None, [], "gh CLI is not available on PATH"
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                issue_ref,
                "--json",
                "url,number,title,labels,state,body",
            ],
            cwd=target,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_GITHUB_ISSUE_VIEW_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return None, [], f"gh issue view timed out after {exc.timeout:g}s"
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"gh issue view exited {result.returncode}"
        return None, [], detail
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return None, [], f"gh issue view returned invalid JSON: {exc.msg}"
    if not isinstance(payload, dict):
        return None, [], "gh issue view returned invalid JSON object"
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        return None, [], "gh issue view did not return an issue title"
    labels = payload.get("labels")
    label_names: list[str] = []
    if isinstance(labels, list):
        for label in labels:
            if isinstance(label, dict) and isinstance(label.get("name"), str):
                label_names.append(label["name"])
            elif isinstance(label, str):
                label_names.append(label)
    return (
        {
            "url": payload.get("url"),
            "number": payload.get("number"),
            "title": title.strip(),
            "labels": label_names,
            "state": payload.get("state"),
            "source": "gh",
            "ref": issue_ref,
        },
        _extract_issue_acceptance(payload.get("body")),
        None,
    )


def _safe_issue_task_id(task: dict[str, Any]) -> str:
    value = task.get("id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return helpers._stable_hash({"text": task.get("text"), "created_at": task.get("created_at")})


def _issue_repair_record(
    task: dict[str, Any],
    *,
    issue_type: str,
    detail: str,
    issue: dict[str, Any] | None = None,
    remote_issue: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    task_id = _safe_issue_task_id(task)
    issue_ref = _github_issue_ref(issue or {}) if issue else None
    source_key = f"{task_id}:{issue_type}:{issue_ref or 'missing-ref'}"
    fingerprint_payload = {
        "task_id": task_id,
        "task_text": task.get("text"),
        "issue_type": issue_type,
        "issue_ref": issue_ref,
        "stored_state": (issue or {}).get("state") if issue else None,
        "stored_title": (issue or {}).get("title") if issue else None,
        "remote_state": (remote_issue or {}).get("state") if remote_issue else None,
        "remote_title": (remote_issue or {}).get("title") if remote_issue else None,
        "error": error,
    }
    metadata: dict[str, Any] = {
        "source_item_key": source_key,
        "source_item_id": source_key,
        "source_fingerprint": helpers._stable_hash(fingerprint_payload),
        "task_id": task_id,
        "issue_type": issue_type,
        "safe_summary": detail,
    }
    if issue_ref:
        metadata["github_issue_ref"] = issue_ref
    if issue:
        for key in ("url", "number", "title", "state"):
            value = issue.get(key)
            if value not in (None, ""):
                metadata[f"github_issue_{key}"] = value
    if remote_issue:
        for key in ("url", "number", "title", "state"):
            value = remote_issue.get(key)
            if value not in (None, ""):
                metadata[f"remote_issue_{key}"] = value
    if error:
        metadata["check_error"] = helpers._short(error, 240)
    return {
        "kind": "task",
        "source": "github-issue-repair",
        "text": f"Repair issue-backed task context for {task_id}: {detail}",
        "type": "workflow",
        "priority": "high" if issue_type == "closed_remote_issue" else "normal",
        "template": "bugfix",
        "acceptance": [
            f"Review local task {task_id} against its issue context without mutating GitHub.",
            "Refresh, complete, dismiss, or replace the local task with explicit local evidence.",
            "`brigade work doctor` no longer reports the same issue-backed task warning.",
        ],
        "metadata": metadata,
    }


def _issue_repair_records(target: Path) -> list[dict[str, Any]]:
    pending = queries_handoffs._pending_tasks(target)
    records: list[dict[str, Any]] = []
    issue_tasks: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for task in pending:
        issue = _task_issue_metadata(task)
        if issue is None:
            if str(task.get("source") or "") == "github_issue":
                records.append(
                    _issue_repair_record(
                        task,
                        issue_type="missing_issue_context",
                        detail="task is marked issue-backed but has no usable GitHub issue metadata",
                    )
                )
            continue
        issue_ref = _github_issue_ref(issue)
        title = issue.get("title")
        if issue_ref is None or not isinstance(title, str) or not title.strip():
            records.append(
                _issue_repair_record(
                    task,
                    issue_type="missing_issue_context",
                    detail="task has incomplete GitHub issue metadata",
                    issue=issue,
                )
            )
            continue
        issue_tasks.append((task, issue))
    if not issue_tasks:
        return records
    if shutil.which("gh") is None:
        for task, issue in issue_tasks:
            records.append(
                _issue_repair_record(
                    task,
                    issue_type="gh_unavailable",
                    detail="gh CLI is unavailable, so issue context cannot be checked",
                    issue=issue,
                    error="gh CLI is not available on PATH",
                )
            )
        return records
    for task, issue in issue_tasks:
        issue_ref = _github_issue_ref(issue)
        if issue_ref is None:
            continue
        remote_issue, _, error = _read_github_issue(target, issue_ref)
        if remote_issue is None:
            records.append(
                _issue_repair_record(
                    task,
                    issue_type="issue_check_failed",
                    detail="remote issue context could not be read",
                    issue=issue,
                    error=error or "issue check failed",
                )
            )
            continue
        remote_state = str(remote_issue.get("state") or "").lower()
        if remote_state == "closed":
            records.append(
                _issue_repair_record(
                    task,
                    issue_type="closed_remote_issue",
                    detail="remote issue is closed while the local task is still pending",
                    issue=issue,
                    remote_issue=remote_issue,
                )
            )
            continue
        stored_title = str(issue.get("title") or "").strip()
        remote_title = str(remote_issue.get("title") or "").strip()
        stored_state = str(issue.get("state") or "").strip().lower()
        if (stored_title and remote_title and stored_title != remote_title) or (
            stored_state and remote_state and stored_state != remote_state
        ):
            records.append(
                _issue_repair_record(
                    task,
                    issue_type="stale_issue_context",
                    detail="stored issue title or state differs from the current issue context",
                    issue=issue,
                    remote_issue=remote_issue,
                )
            )
    return records


def _import_record_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(item.get("source") or "manual"),
        str(item.get("kind") or "task"),
        _task_text_key(str(item.get("text") or "")),
    )


def _import_source_key(item: dict[str, Any]) -> str | None:
    metadata = _metadata(item)
    for key in (
        "source_item_key",
        "source_item_id",
        "scanner_item_id",
        "sweep_issue_id",
        "issue_id",
        "card_id",
        "card_file",
    ):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return None


_UNTRUSTED_IMPORT_OPERATIONAL_METADATA_KEYS = frozenset(
    {
        "proposed_edges",
        "edges",
        "seat_class",
        "spend_by",
        "handoff_target_document",
        "target_document",
        "handoff_type",
        "category",
        "issue_type",
        "handoff_category",
        "memory_target",
        "reason",
        "dispatch",
        "dispatch_id",
        "dispatch_status",
        "handoff_id",
        "handoff_issue_id",
        "promotion",
        "promoted_at",
        "promoted_from",
    }
)
_UNTRUSTED_IMPORT_IDENTITY_METADATA_KEYS = (
    "source_item_key",
    "source_item_id",
    "scanner_item_id",
    "sweep_issue_id",
    "issue_id",
    "card_id",
    "card_file",
    "source_fingerprint",
)
_UNTRUSTED_IMPORT_PROVENANCE_METADATA_KEYS = frozenset(
    {
        *_UNTRUSTED_IMPORT_IDENTITY_METADATA_KEYS,
        *provenance.ENVELOPE_METADATA_RESERVED_KEYS,
        "provenance",
    }
)


def _is_untrusted_import_metadata_key_allowed(key: object) -> bool:
    return not (
        key in _UNTRUSTED_IMPORT_OPERATIONAL_METADATA_KEYS
        or key in _UNTRUSTED_IMPORT_PROVENANCE_METADATA_KEYS
        or (
            isinstance(key, str)
            and (key.startswith("declared_") or key == "github_issue" or key.startswith("github_issue_"))
        )
    )


def _sanitize_untrusted_import_metadata(value: object) -> object:
    """Recursively retain only non-authoritative untrusted import evidence."""
    if isinstance(value, dict):
        return {
            key: _sanitize_untrusted_import_metadata(item)
            for key, item in value.items()
            if _is_untrusted_import_metadata_key_allowed(key)
        }
    if isinstance(value, list):
        return [_sanitize_untrusted_import_metadata(item) for item in value]
    return value


def _bound_declared_import_alias(value: object) -> str | None:
    """Return a bounded display value for an untrusted declared identity alias."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return inbox_provenance._bound_import_identity(value)
    if isinstance(value, int):
        return inbox_provenance._bound_import_identity(str(value))
    if isinstance(value, float) and math.isfinite(value):
        return inbox_provenance._bound_import_identity(str(value))
    return None


def _untrusted_import_canonical_hash(record: dict[str, Any]) -> str:
    """Hash immutable import content, never record-controlled metadata or source."""
    return helpers._stable_hash(
        {
            "text": record["text"],
            "kind": record["kind"],
            "type": record.get("type"),
            "priority": record.get("priority"),
            "template": record.get("template"),
            "acceptance": record.get("acceptance"),
        }
    )


def _sanitize_untrusted_import_record(record: dict[str, Any], *, importer_source: str) -> dict[str, Any]:
    """Assign source and identity to hostile JSONL records at the import boundary."""
    raw_metadata = record.get("metadata")
    metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    stamped_metadata = {
        key: _sanitize_untrusted_import_metadata(value)
        for key, value in metadata.items()
        if _is_untrusted_import_metadata_key_allowed(key)
    }
    canonical_hash = _untrusted_import_canonical_hash(record)
    declared_source = inbox_provenance._bound_import_identity(record.get("source"))
    if declared_source is not None:
        stamped_metadata["declared_source"] = declared_source
    for key in _UNTRUSTED_IMPORT_IDENTITY_METADATA_KEYS:
        declared_value = _bound_declared_import_alias(metadata.get(key))
        if declared_value is not None:
            stamped_metadata[f"declared_{key}"] = declared_value
    stamped_metadata["source_item_key"] = f"{importer_source}:{canonical_hash}"
    stamped_metadata["source_fingerprint"] = canonical_hash
    sanitized = dict(record)
    sanitized["source"] = importer_source
    sanitized["metadata"] = stamped_metadata
    return sanitized


class _LocallyStampedImportProof:
    """An in-memory verifier capability for one scanner-emitted immutable row."""

    def __init__(self, *, importer_source: str, content_hash: str) -> None:
        self.importer_source = importer_source
        self.content_hash = content_hash


def _locally_stamped_import_content_hash(record: dict[str, Any]) -> str:
    """Hash every immutable import field, leaving lifecycle changes out of scope."""
    immutable = {
        key: value
        for key, value in record.items()
        if key
        not in {
            "status",
            "updated_at",
            "dismissed_at",
            "dismiss_reason",
            "promoted_at",
            "task_id",
            "handoff_path",
            "handoff_target_document",
            "handoff_source_fingerprint",
        }
    }
    return provenance.content_sha256(json.dumps(immutable, sort_keys=True, separators=(",", ":"), default=str))


def _make_locally_stamped_import_proof(record: dict[str, Any], *, importer_source: str) -> _LocallyStampedImportProof:
    """Create the non-serialized capability used by the scanner verifier."""
    return _LocallyStampedImportProof(
        importer_source=importer_source,
        content_hash=_locally_stamped_import_content_hash(record),
    )


def _is_locally_stamped_self_import(
    record: dict[str, Any], *, importer_source: str, proof: _LocallyStampedImportProof | None = None
) -> bool:
    """Return whether a self-import was emitted by the local work-inbox producer."""
    return (
        descriptor_anchors._has_local_import_envelope(record, importer_source=importer_source)
        and isinstance(proof, _LocallyStampedImportProof)
        and proof.importer_source == importer_source
        and proof.content_hash == _locally_stamped_import_content_hash(record)
    )


def _safe_self_import_document_target(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value if queries_handoffs._handoff_is_document_target(value) else None


def _safe_self_import_path(value: object, *, target: Path | None = None) -> str | None:
    if not isinstance(value, str):
        return None
    path = Path(value)
    if path.is_absolute():
        if target is None:
            return None
        try:
            return path.resolve(strict=False).relative_to(target.resolve()).as_posix()
        except ValueError:
            return None
    return value if provenance.is_safe_identity_label(value) else None


def _safe_self_import_source_item_key(value: object, *, importer_source: str) -> str | None:
    value = inbox_provenance._safe_import_identity(value)
    prefix = f"{importer_source}:"
    if value is None or not value.startswith(prefix) or len(value) == len(prefix):
        return None
    return value


def _safe_stable_source_fingerprint(value: object) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-f]{16}|[0-9a-f]{64})", value):
        return None
    return value


def _trusted_self_import_metadata(
    record: dict[str, Any],
    *,
    importer_source: str,
    target: Path | None = None,
    trusted_producer: bool = False,
    proof: _LocallyStampedImportProof | None = None,
) -> dict[str, Any]:
    """Retain the narrow source-owned metadata required by self-import producers."""
    if not trusted_producer or not _is_locally_stamped_self_import(
        record, importer_source=importer_source, proof=proof
    ):
        return {}
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    retained: dict[str, Any] = {}
    value = _safe_self_import_source_item_key(metadata.get("source_item_key"), importer_source=importer_source)
    if value is not None:
        retained["source_item_key"] = value
    value = _safe_stable_source_fingerprint(metadata.get("source_fingerprint"))
    if value is not None:
        retained["source_fingerprint"] = value
    if importer_source == "handoff-ingest":
        for key in ("handoff_issue_id", "handoff_issue_category", "handoff_type"):
            value = inbox_provenance._safe_import_identity(metadata.get(key))
            if value is not None:
                retained[key] = value
        for key in ("handoff_target_document", "target_document"):
            value = _safe_self_import_document_target(metadata.get(key))
            if value is not None:
                retained[key] = value
    elif importer_source == "memory-refresh":
        value = inbox_provenance._safe_import_identity(metadata.get("card_id"))
        if value is not None:
            retained["card_id"] = value
        value = _safe_self_import_path(metadata.get("card_file"))
        if value is not None:
            retained["card_file"] = value
        value = _safe_self_import_path(metadata.get("queue_path"), target=target)
        if value is not None:
            retained["queue_path"] = value
        value = inbox_provenance._safe_import_identity(metadata.get("refresh_reason"))
        if value is not None:
            retained["refresh_reason"] = value
    return retained


def _sanitize_self_import_record(
    record: dict[str, Any],
    *,
    importer_source: str,
    target: Path | None = None,
    trusted_producer: bool = False,
    proof: _LocallyStampedImportProof | None = None,
) -> dict[str, Any]:
    """Sanitize a self-import, preserving only authenticated source-owned fields."""
    sanitized = _sanitize_untrusted_import_record(record, importer_source=importer_source)
    if not trusted_producer:
        for key in ("queue_path", "refresh_reason"):
            sanitized["metadata"].pop(key, None)
    trusted_metadata = _trusted_self_import_metadata(
        record,
        importer_source=importer_source,
        target=target,
        trusted_producer=trusted_producer,
        proof=proof,
    )
    if trusted_metadata:
        sanitized["metadata"].update(trusted_metadata)
    return sanitized


def _has_canonical_untrusted_import_identity(record: dict[str, Any]) -> bool:
    """Return whether a sanitized record retains its importer-owned identity."""
    source = record.get("source")
    metadata = record.get("metadata")
    if not isinstance(source, str) or not source or not isinstance(metadata, dict):
        return False
    canonical_hash = _untrusted_import_canonical_hash(record)
    return (
        metadata.get("source_item_key") == f"{source}:{canonical_hash}"
        and metadata.get("source_fingerprint") == canonical_hash
    )


_IMPORT_PROOF_SCHEMA_VERSION = 1
_IMPORT_PROOF_OPERATION_PATTERN = re.compile(r"[0-9a-f]{32}")
_DIRECTORY_AUTHORITY_SCHEMA_VERSION = 1
_EXTERNAL_DIRECTORY_AUTHORITY_SCHEMA_VERSION = 1


def _import_proof_name(item_id: object) -> str | None:
    """Return an opaque proof filename, never a path chosen by an import row."""
    if not isinstance(item_id, str) or not item_id:
        return None
    return f"{helpers._stable_hash({'item_id': item_id})}.json"
