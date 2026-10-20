"""Read-only bd JSONL snapshot adapter for the receipted work inbox."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import edges, helpers, ledger


class BdImportError(ledger.TaskLedgerError):
    """A bd snapshot failed validation before inbox publication."""


# Explicit interchange subset. New producer shapes require a reviewed mapping.
_ISSUE_FIELDS = frozenset(
    """
    _type id title description design acceptance_criteria notes spec_id status priority issue_type
    assignee owner estimated_minutes created_at created_by updated_at started_at closed_at close_reason
    closed_by_session lease_expires_at heartbeat_at lease_granted_node metadata labels dependencies
    comments dependency_count dependent_count comment_count parent external_ref
""".split()
)
_DEP_FIELDS = frozenset("issue_id depends_on_id type id created_at created_by metadata thread_id".split())
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")


def _invalid(message: str, *, line: int) -> BdImportError:
    return BdImportError(message, reason="invalid_bd_row", details={"line": line})


def _parse_snapshot(raw: bytes, bd_version: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    graph_edges: list[dict[str, str]] = []
    seen: set[str] = set()
    input_digest = hashlib.sha256(raw).hexdigest()
    for line, data in enumerate(raw.splitlines(), 1):
        if not data.strip():
            continue
        try:
            row = json.loads(data)
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise BdImportError(
                "bd export contains invalid JSON", reason="invalid_bd_jsonl", details={"line": line}
            ) from exc
        if not isinstance(row, dict) or row.get("_type") != "issue" or set(row) - _ISSUE_FIELDS:
            raise _invalid("unsupported bd issue row shape", line=line)
        key = row.get("id")
        if not isinstance(key, str) or not _ID.fullmatch(key) or key in seen:
            raise _invalid(
                "bd issue id must be unique and use letters, digits, dots, hyphens or underscores", line=line
            )
        seen.add(key)
        if not isinstance(row.get("title"), str) or not row["title"].strip():
            raise _invalid("bd issue requires a title", line=line)
        if type(row.get("priority")) is not int or row["priority"] not in range(5):
            raise _invalid("bd priority must be an integer from 0 through 4", line=line)
        if not isinstance(row.get("status"), str) or not isinstance(row.get("issue_type"), str):
            raise _invalid("bd issue requires status and issue_type strings", line=line)
        if not isinstance(row.get("metadata", {}), dict):
            raise _invalid("bd metadata must be an object", line=line)
        for field in ("description", "design", "acceptance_criteria", "notes"):
            if field in row and not isinstance(row[field], str):
                raise _invalid(f"bd {field} must be a string", line=line)
        dependencies = row.get("dependencies", [])
        if not isinstance(dependencies, list):
            raise _invalid("bd dependencies must be an array", line=line)
        proposals: list[dict[str, str]] = []
        for dep in dependencies:
            if (
                not isinstance(dep, dict)
                or set(dep) - _DEP_FIELDS
                or dep.get("issue_id") != key
                or not isinstance(dep.get("depends_on_id"), str)
                or not isinstance(dep.get("type"), str)
            ):
                raise _invalid("unsupported bd dependency row shape", line=line)
            other = dep["depends_on_id"]
            edge_type = dep["type"]
            # bd records an issue's prerequisite/parent as depends_on_id.
            source, target = (key, other) if edge_type == "discovered-from" else (other, key)
            proposal = {"type": edge_type, "source": source, "target": target}
            if proposal not in proposals:
                proposals.append(proposal)
                graph_edges.append(proposal)
        text = f"[bd:{key}] {row['title'].strip()}"
        for field in ("description", "design", "notes"):
            if row.get(field):
                text += f"\n\n{field}:\n{row[field]}"
        record = ledger._sanitize_untrusted_import_record(
            {
                "text": text,
                "kind": "task",
                "metadata": row.get("metadata", {}),
                "type": "task",
                "priority": ("high", "high", "normal", "low", "low")[row["priority"]],
                "acceptance": [row["acceptance_criteria"]] if row.get("acceptance_criteria") else [],
            },
            importer_source="bd-jsonl",
        )
        digest = hashlib.sha256(data).hexdigest()
        fingerprint = helpers._stable_hash(
            {
                **{field: record[field] for field in ("text", "kind", "type", "priority", "acceptance")},
                "proposed_edges": sorted(proposals, key=lambda edge: (edge["type"], edge["source"], edge["target"])),
                "status": row["status"],
                "issue_type": row["issue_type"],
            }
        )
        record["metadata"].update(
            {
                "source_item_key": f"bd-jsonl:{key}",
                "source_fingerprint": fingerprint,
                "bd": {
                    "id": key,
                    "version": bd_version,
                    "row_digest": digest,
                    "input_digest": input_digest,
                    "status": row["status"],
                    "issue_type": row["issue_type"],
                },
                "proposed_edges": proposals,
            }
        )
        records.append(record)
    if records:
        try:
            edges.validate_graph_plan(
                {
                    "nodes": [{"key": item["metadata"]["bd"]["id"], "text": item["text"]} for item in records],
                    "edges": graph_edges,
                }
            )
        except edges.EdgeError as exc:
            raise BdImportError(str(exc), reason=exc.reason, details=exc.details) from exc
    return records


def _write_receipt(directory: int, payload: dict[str, Any]) -> None:
    temporary = f"receipt-{uuid4().hex}.tmp"
    descriptor = ledger._dirfd_open_file(directory, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        ledger._dirfd_replace(directory, temporary, "receipt.json")
        ledger._dirfd_fsync(directory)
    finally:
        try:
            ledger._dirfd_unlink(directory, temporary)
        except FileNotFoundError:
            pass


def import_bd_jsonl(*, target: Path, input_path: Path, bd_version: str, json_output: bool = False) -> int:
    """Validate the whole export, then append proposals through the inbox gate."""
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", bd_version):
        raise BdImportError("--bd-version must be a release version", reason="invalid_bd_version")
    target = target.expanduser().resolve()
    try:
        if not target.is_dir():
            raise OSError("--target is not a directory")
        with input_path.expanduser().open("rb") as handle:
            raw = handle.read(ledger._IMPORT_INBOX_SNAPSHOT_LIMIT_BYTES + 1)
        if len(raw) > ledger._IMPORT_INBOX_SNAPSHOT_LIMIT_BYTES:
            raise BdImportError("bd export exceeds the 4 MiB import limit", reason="bd_snapshot_too_large")
        records = _parse_snapshot(raw, bd_version)
        run_id = uuid4().hex
        components = (".brigade", "work", "import-runs", run_id)
        receipt_path = target.joinpath(*components, "receipt.json")
        payload: dict[str, Any] = {
            "source": "bd-jsonl",
            "bd_version": bd_version,
            "input_digest": hashlib.sha256(raw).hexdigest(),
            "receipt_path": str(receipt_path),
            "created_at": helpers._now().isoformat(),
            "status": "pending",
            "rows": [
                {"id": r["metadata"]["bd"]["id"], "row_digest": r["metadata"]["bd"]["row_digest"]} for r in records
            ],
        }
        with ledger._canonical_inbox_write(target):
            directory = ledger._open_verifier_owned_directory(
                target, components=components, anchor_name=".bd-import-runs.authority.json", create=True
            )
            try:
                _write_receipt(directory, payload)
                inbox_parent, inbox_name, previous_raw, previous_exists = ledger._snapshot_import_inbox(target)
                imported: list[dict[str, Any]] = []
                try:
                    try:
                        imported, skipped, dismissed, rejected = ledger._append_import_records(
                            target, records, provenance_source="bd-jsonl"
                        )
                        payload.update(
                            {
                                "status": "completed",
                                "created": len(imported),
                                "skipped_duplicates": len(skipped),
                                "skipped_dismissed": len(dismissed),
                                "rejected": rejected,
                                "import_ids": [item["id"] for item in imported],
                            }
                        )
                        _write_receipt(directory, payload)
                    except Exception as exc:
                        if imported:
                            # Receipt finalization is part of the locked publication.
                            try:
                                ledger._restore_import_inbox_snapshot(
                                    inbox_parent, inbox_name, previous_raw, previous_exists
                                )
                            finally:
                                ledger._remove_persisted_import_proofs(target, imported)
                        payload.update({"status": "failed", "reason": str(exc), "created": 0, "import_ids": []})
                        _write_receipt(directory, payload)
                        raise
                finally:
                    os.close(inbox_parent)
            finally:
                os.close(directory)
    except OSError as exc:
        raise BdImportError(f"bd import I/O failed: {exc}", reason="bd_import_io_error") from exc
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"bd import: created={payload['created']} skipped_duplicates={payload['skipped_duplicates']}")
        print(f"receipt: {receipt_path}")
    return 0
