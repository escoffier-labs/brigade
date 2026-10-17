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


# Apply to every JSON container, including fields a domain validator ignores.
MAX_JSON_DEPTH = 32
MAX_JSON_CONTAINER_ITEMS = 1000
MAX_JSON_CONTAINERS = 32768
MAX_JSON_ITEMS = 65536
WORKLORE_BODY_BYTES = 262144
WORKLORE_IMPORT_BODY_BYTES = 1048576


class RequestBoundsError(ValueError):
    """An HTTP body cannot be read or parsed within the Hub's bounds."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _scan_json(raw: bytes) -> None:
    """Bound decoded object growth in one byte pass with a depth-bounded stack.

    Each nonempty container contributes its first item and each comma adds
    another. Object members count once, including duplicate keys. Quoted
    punctuation and escaped quotes cannot consume the structural budgets.
    The decoder remains responsible for JSON syntax validation.
    """
    empty: list[bool] = []
    containers = items = 0
    quoted = escaped = False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        else:
            if byte in (9, 10, 13, 32):
                continue
            if byte in (93, 125):
                if empty:
                    empty.pop()
                continue
            if byte == 44 or (empty and empty[-1] and byte != 58):
                items += 1
                if items > MAX_JSON_ITEMS:
                    raise RequestBoundsError(f"JSON aggregate items exceed {MAX_JSON_ITEMS}")
                if empty:
                    empty[-1] = False
            if byte == 34:
                quoted = True
            elif byte in (91, 123):
                containers += 1
                if containers > MAX_JSON_CONTAINERS:
                    raise RequestBoundsError(f"JSON containers exceed {MAX_JSON_CONTAINERS}")
                empty.append(True)
                if len(empty) > MAX_JSON_DEPTH:
                    raise RequestBoundsError(f"JSON nesting exceeds {MAX_JSON_DEPTH}")


def bounded_json_loads(raw: bytes, **kwargs: Any) -> Any:
    """Bound structural complexity before decoding and check the result too."""
    import json

    _scan_json(raw)
    try:
        value = json.loads(raw.decode("utf-8"), **kwargs)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise RequestBoundsError("body is not valid JSON") from exc
    pending = [value]
    containers = items = 0
    while pending:
        item = pending.pop()
        if isinstance(item, (list, dict)):
            containers += 1
            items += len(item)
            if containers > MAX_JSON_CONTAINERS:
                raise RequestBoundsError(f"JSON containers exceed {MAX_JSON_CONTAINERS}")
            if items > MAX_JSON_ITEMS:
                raise RequestBoundsError(f"JSON aggregate items exceed {MAX_JSON_ITEMS}")
            if len(item) > MAX_JSON_CONTAINER_ITEMS:
                raise RequestBoundsError(f"JSON container exceeds {MAX_JSON_CONTAINER_ITEMS} items")
            pending.extend(item.values() if isinstance(item, dict) else item)
    return value


def read_json_body(handler: Any, *, limit: int, empty_delete: bool = False) -> Any:
    """Refuse ambiguous framing and oversize lengths before reading any bytes."""
    lengths = handler.headers.get_all("Content-Length", [])
    if handler.headers.get_all("Transfer-Encoding", []) or len(lengths) != 1:
        if empty_delete and not lengths and not handler.headers.get_all("Transfer-Encoding", []):
            return {}
        raise RequestBoundsError("bad Content-Length or Transfer-Encoding")
    try:
        length = int(lengths[0])
    except ValueError as exc:
        raise RequestBoundsError("bad Content-Length") from exc
    if length > limit:
        raise RequestBoundsError(f"request body exceeds {limit} bytes", status=413)
    if length == 0 and empty_delete:
        return {}
    if length <= 0:
        raise RequestBoundsError("missing body")
    raw = handler.rfile.read(length)
    if len(raw) != length:
        raise RequestBoundsError("incomplete body")
    value = bounded_json_loads(raw)
    if value is None:
        raise RequestBoundsError("body must be a JSON object or array")
    return value
