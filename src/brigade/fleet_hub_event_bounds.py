"""Per-field length bounds for fleet hub event ingestion (#1151).

Every string field a node posts to ``/events`` is stored verbatim in the
hub's events table and echoed by ``/status`` and the boards, so a bearer
holder could otherwise bloat SQLite with multi-megabyte values inside the
8 MiB body limit. Limits are measured on the raw value, before any strip,
because the raw value is what gets stored.

Each bound sits well above anything a well-behaved client produces: node ids
follow the 128-character claim identity pattern, digests and capability
fingerprints are SHA-256 hex, and opaque labels share the claim label bound.
"""

from __future__ import annotations

from typing import Any, Mapping

EVENT_FIELD_MAX_CHARS: Mapping[str, int] = {
    "node_id": 128,
    "run_id": 256,
    "state": 128,
    "ts": 64,
    "digest": 128,
    "repo": 1024,
    "seat": 128,
    "harness": 128,
    "capability_fingerprint": 128,
    "repo_identity": 512,
}


def oversized_event_field(field: str, value: Any) -> str | None:
    """Return an operator-readable error when ``value`` exceeds ``field``'s bound, else None."""
    limit = EVENT_FIELD_MAX_CHARS.get(field)
    if limit is None or not isinstance(value, str) or len(value) <= limit:
        return None
    return f"event field {field!r} must be at most {limit} characters"
