"""Closed, metadata-only T3 execution context shared by the CLI and Hub."""

from __future__ import annotations

import json
import re
from typing import Any

from .worklore_validate import contains_credential

SCHEMA = "brigade.t3_execution_context.v1"
MAX_BYTES = 4096
TEXT_FIELDS = frozenset(
    {"request_id", "thread_id", "turn_ref", "controller_id", "origin_node", "target_host", "work_id"}
)
POLICY_FIELDS = frozenset({"session_id", "decision_id", "reservation_id", "delegation_id"})
FIELDS = TEXT_FIELDS | {"schema", "issue_url", "initiator", "policy"}
_ISSUE_URL = re.compile(
    r"https://github\.com/[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*/(?:issues|pull)/[1-9][0-9]*\Z"
)
_SECRET = re.compile(
    r"(?i)(?:token|secret|password|passwd|api[_-]?key|credential)\s*[:=]|\b(?:bearer|basic)\s+\S|\b(?:authorization|cookie)\s*:"
)


class ExecutionContextError(ValueError):
    """Fixed refusal messages never echo caller-supplied values or keys."""


def _object(raw: object, fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.keys() - fields:
        raise ExecutionContextError("execution context requires a closed JSON object")
    return raw


def _text(raw: object, *, url: bool = False) -> str:
    limit = 512 if url else 128
    if not isinstance(raw, str) or len(raw) > limit or (raw and not raw.isprintable()):
        raise ExecutionContextError("execution context strings must be printable and bounded")
    if contains_credential(raw) or _SECRET.search(raw):
        raise ExecutionContextError("execution context must not contain credentials")
    if url:
        if _ISSUE_URL.fullmatch(raw) is None:
            raise ExecutionContextError("execution context issue_url requires a canonical GitHub issue or pull URL")
    elif any(char in raw for char in ("/", "\\", "%")) or raw.startswith("op:"):
        raise ExecutionContextError("execution context must not contain paths or encoded data")
    return raw


def validate_context(raw: object) -> dict[str, Any]:
    """Validate without normalization so correlation identifiers round-trip exactly."""
    obj = _object(raw, FIELDS)
    if obj.get("schema") != SCHEMA:
        raise ExecutionContextError("execution context requires brigade.t3_execution_context.v1")
    context: dict[str, Any] = {"schema": SCHEMA}
    for field in TEXT_FIELDS & obj.keys():
        context[field] = _text(obj[field])
    if "issue_url" in obj:
        context["issue_url"] = _text(obj["issue_url"], url=True)
    if "initiator" in obj:
        initiator = _object(obj["initiator"], frozenset({"kind", "label"}))
        context["initiator"] = {key: _text(value) for key, value in initiator.items()}
        if "kind" in initiator and initiator["kind"] not in {"agent", "harness", "human", "unknown"}:
            raise ExecutionContextError("execution context initiator kind is invalid")
    if "policy" in obj:
        context["policy"] = {key: _text(value) for key, value in _object(obj["policy"], POLICY_FIELDS).items()}
    if len(serialize_context(context).encode("utf-8")) > MAX_BYTES:
        raise ExecutionContextError("execution context exceeds 4096 bytes")
    return context


def serialize_context(context: dict[str, Any]) -> str:
    """Canonical UTF-8 JSON for storage and digesting."""
    return json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
