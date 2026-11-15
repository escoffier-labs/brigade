"""Body-free failure signals for automatic verification, never positive proof."""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

from . import memory_evidence_log as ledger
from . import memory_fairness as fairness
from . import memory_proposal_io as io
from .dispatch_memory import validate_refs
from .inbox_lock import held_file_lock

if TYPE_CHECKING:
    from .outcome import OutcomeRecord

PREFIX = ".brigade/memory/lesson-cards/"


def _refs(value: object) -> list[dict[str, str]]:
    try:
        return validate_refs(value)
    except ValueError:
        return []


def _append(target: Path, source: str, receipt: Mapping[str, Any], refs: list[dict[str, str]]) -> None:
    run_id = receipt.get("run_id")
    if not isinstance(run_id, str) or not run_id or len(run_id) > 256 or not refs:
        return
    # Namespace receipt ids across producers. Neither finding prose nor card text
    # is used as a log identifier. Card references carry only ids and hashes.
    evidence_id = "sha256:" + io.sha256(io.json_bytes([source, run_id]))
    timestamp = receipt.get("finished_at") or receipt.get("completed_at") or receipt.get("started_at")
    if ledger._timestamp(timestamp) is None:
        timestamp = datetime.now(timezone.utc).isoformat()
    lock = target / ".brigade/memory/reflect/lesson-rows.lock"
    io.protect_file(lock)
    with held_file_lock(lock, deadline_seconds=1.0):
        rows, warnings, unsupported = ledger.read_evidence(target)
        if warnings or unsupported:
            raise ValueError("evidence ledger cannot establish deduplication")
        seen = {(row["card_path"], row["evidence_id"]) for row in rows}
        for ref in refs:
            key = fairness.key_digest(ref["scope"], [ref["card_id"]])
            path = f"{PREFIX}{key}.md"
            if (path, evidence_id) in seen:
                continue
            ledger.append_log_row(
                target,
                io.json_bytes(
                    {
                        "ts": timestamp,
                        "card_id": ref["card_id"],
                        "card_path": path,
                        "evidence_id": evidence_id,
                        "kind": "negative-outcome",
                        "similarity": 1.0,
                    }
                ),
            )
            seen.add((path, evidence_id))


def log_run_receipt(target: Path, receipt: Mapping[str, Any]) -> None:
    """Record cited cards after a failed run receipt has been persisted."""
    if receipt.get("status") not in {"failed", "error", "incomplete", "timeout"} or receipt.get("dry_run"):
        return
    brief = receipt.get("evidence_brief")
    refs = _refs(brief.get("memory_refs")) if isinstance(brief, dict) else []
    _append(target, "run", receipt, refs)


def log_review_findings(target: Path, receipt: Mapping[str, Any], findings: Iterable[Mapping[str, Any]]) -> None:
    """Record one signal per cited card and review receipt, including on import retries."""
    refs = [ref for finding in findings for ref in _refs(finding.get("memory_refs"))]
    _append(target, "review", receipt, refs)


def pending_keys(target: Path, heads: Iterable[Mapping[str, Any]]) -> set[str]:
    """Match pending rows to exact scoped ids, and legacy file rows to card ids.

    A later review or consolidation consumes a row. New scoped signals use a
    synthetic path containing the fairness digest, never a path to open.
    """
    rows, _, _ = ledger.read_evidence(target)
    negative: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["kind"] == "negative-outcome":
            negative.setdefault(row["card_id"], []).append(row)
    result = set()
    from .memory_cmd import _frontmatter_value, _parse_frontmatter

    for head in heads:
        key = fairness.key_digest(str(head["scope"]), [str(head["card_id"])])
        meta, _ = _parse_frontmatter(str(head.get("text", "")))
        cutoff = ledger._timestamp(meta.get("last_consolidated")) or ledger._timestamp(
            _frontmatter_value(meta, "last_reviewed", "last_reviewed_at", "reviewed_at")
        )
        for row in negative.get(str(head["card_id"]), []):
            if row["card_path"].startswith(PREFIX) and row["card_path"] != f"{PREFIX}{key}.md":
                continue
            stamp = ledger._timestamp(row["ts"])
            if stamp is not None and (cutoff is None or stamp > cutoff):
                result.add(key)
                break
    return result


def warn_append_failed() -> None:
    """Keep optional logging failures free of card or receipt contents."""
    print("warning: memory failure evidence append failed", file=sys.stderr)


def log_captured_run(target: Path, record: OutcomeRecord, receipt: Mapping[str, Any]) -> None:
    """Use the existing outcome capture citation for file-backed cards."""
    from .outcome_cmd import _artifact_content_path

    if receipt.get("dry_run") or receipt.get("status") not in {"failed", "error", "incomplete", "timeout"}:
        return
    path = _artifact_content_path(target, record.artifact_id, "card")
    if path is None:
        return
    ref = "sha256:" + io.sha256(io.json_bytes(["run", Path(record.evidence_ref).parent.name]))
    rel = path.relative_to(target).as_posix()
    lock = target / ".brigade/memory/reflect/lesson-rows.lock"
    io.protect_file(lock)
    with held_file_lock(lock, deadline_seconds=1.0):
        rows, warnings, unsupported = ledger.read_evidence(target)
        if warnings or unsupported:
            raise ValueError("evidence ledger cannot establish deduplication")
        if any(row["card_path"] == rel and row["evidence_id"] == ref for row in rows):
            return
        ledger.append_evidence(
            target, card_path=path, evidence_id=ref, kind="negative-outcome", similarity=1.0, ts=record.ts
        )
