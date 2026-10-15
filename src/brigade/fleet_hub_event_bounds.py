"""Per-field length bounds for fleet hub event ingestion (#1151).

Every string field a node posts to ``/events`` is stored verbatim in the
hub's events table and echoed by ``/status`` and the boards, so a bearer
holder could otherwise bloat SQLite with multi-megabyte values inside the
8 MiB body limit. Limits are measured on the raw value, before any strip,
because the raw value is what gets stored.

Each bound admits everything a well-behaved client can forward: node ids
follow the 128-character claim identity pattern, digests and capability
fingerprints are SHA-256 hex, and journal-sourced seat and harness labels
inherit the run-event payload string bound. Journal run ids have no length
rule of their own; they are ASCII and must fit in one journal line, so the
journal line bound is the tightest limit no valid envelope can exceed.
"""

from __future__ import annotations

from typing import Any, Mapping

from .run_events import MAX_LINE_BYTES, MAX_PAYLOAD_STR_LEN

EVENT_FIELD_MAX_CHARS: Mapping[str, int] = {
    "node_id": 128,
    "run_id": MAX_LINE_BYTES,
    "state": 128,
    "ts": 64,
    "digest": 128,
    "repo": 1024,
    "seat": MAX_PAYLOAD_STR_LEN,
    "harness": MAX_PAYLOAD_STR_LEN,
    "capability_fingerprint": 128,
    "repo_identity": 512,
}


def oversized_event_field(field: str, value: Any) -> str | None:
    """Return an operator-readable error when ``value`` exceeds ``field``'s bound, else None."""
    limit = EVENT_FIELD_MAX_CHARS.get(field)
    if limit is None or not isinstance(value, str) or len(value) <= limit:
        return None
    return f"event field {field!r} must be at most {limit} characters"
