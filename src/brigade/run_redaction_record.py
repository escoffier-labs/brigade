"""Redaction record encoding and affected sequence bounds."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

REDACTION_SCHEMA = "brigade.run_redaction.v1"
REDACTION_SCHEMA_VERSION = 1


def record_bytes(
    *,
    error_type: type[RuntimeError],
    operation_id: str,
    run_id: str,
    sequence_start: int,
    sequence_end: int,
    reason: str,
    rewritten_sha256: str | None,
    quarantine_retained: bool,
    parent_operation_id: str | None,
    parent_record_sha256: str | None = None,
    rewritten_digest_retired_by: str | None = None,
    affected_range: tuple[int, int] | None = None,
) -> bytes:
    if (rewritten_sha256 is None) == (rewritten_digest_retired_by is None):
        raise error_type("redaction record digest is invalid")
    if (parent_operation_id is None) != (parent_record_sha256 is None):
        raise error_type("redaction record parent reference is invalid")
    if parent_record_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", parent_record_sha256):
        raise error_type("redaction record parent reference is invalid")
    payload = {
        "schema": REDACTION_SCHEMA,
        "schema_version": REDACTION_SCHEMA_VERSION,
        "operation_id": operation_id,
        "run_id": run_id,
        "sequence_start": sequence_start,
        "sequence_end": sequence_end,
        "reason_code": reason,
        "parent_operation_id": parent_operation_id,
        "chain_verified": True,
        "projection_verified": True,
        "quarantine_retained": quarantine_retained,
    }
    if affected_range is not None:
        payload["affected_first_sequence"], payload["affected_last_sequence"] = affected_range
    if parent_record_sha256 is not None:
        payload["parent_record_sha256"] = parent_record_sha256
    if rewritten_sha256 is not None:
        payload["rewritten_journal_sha256"] = rewritten_sha256
    if rewritten_digest_retired_by is not None:
        payload["rewritten_digest_retired_by"] = rewritten_digest_retired_by
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def affected_range(record: Mapping[str, Any], *, error_type: type[RuntimeError]) -> tuple[int, int] | None:
    """Read optional expanded bounds while accepting existing records."""
    if "affected_first_sequence" not in record and "affected_last_sequence" not in record:
        return None
    first = record.get("affected_first_sequence")
    last = record.get("affected_last_sequence")
    if (
        type(first) is not int
        or type(last) is not int
        or not 1 <= first <= record["sequence_start"] <= record["sequence_end"] <= last
    ):
        raise error_type("redaction record affected sequence range is invalid")
    return first, last
