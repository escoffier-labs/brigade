"""Strict validation and deterministic encoding for Brigade code references."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any


SCHEMA = "brigade.code-reference.v1"
_REQUIRED = {
    "schema",
    "repository",
    "revision",
    "file_path",
    "qualified_name",
    "symbol_kind",
    "source_span",
    "change_kind",
}
_CHANGE_KINDS = {"added", "changed", "removed"}
_SYMBOL_KINDS = {"class", "enum", "function", "method", "module", "struct", "trait", "type"}
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY = re.compile(r"^[^/\s]+/[^/\s]+$")
MAX_SAFE_JSON_INTEGER = 2**53 - 1


def _require_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _require_relative_posix_path(value: Any, name: str) -> str:
    path = _require_string(value, name)
    if path.startswith("/") or "\\" in path or ".." in path.split("/"):
        raise ValueError(f"{name} must be repository-relative")
    return path


def _require_line(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"wrong source span type: {name} must be a positive integer")
    if isinstance(value, int):
        normalized = value
    elif isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        normalized = int(value)
    else:
        raise ValueError(f"wrong source span type: {name} must be a positive integer")
    if normalized < 1 or normalized > MAX_SAFE_JSON_INTEGER:
        raise ValueError(f"wrong source span type: {name} must be a positive integer")
    return normalized


def normalize_delta_node(node: Any, fallback_change_kind: str | None = None) -> dict[str, Any] | None:
    """Return a valid GraphTrail code-reference candidate, or ``None``."""
    if not isinstance(node, dict):
        return None
    change_kind = fallback_change_kind if fallback_change_kind is not None else node.get("change_kind")
    try:
        file_path = _require_relative_posix_path(node.get("file_path"), "file_path")
        qualified_name = _require_string(node.get("qualified_name"), "qualified_name")
        symbol_kind = _require_string(node.get("kind"), "kind")
    except ValueError:
        return None
    if symbol_kind not in _SYMBOL_KINDS or change_kind not in _CHANGE_KINDS:
        return None
    start_line = node.get("start_line")
    end_line = node.get("end_line")
    if (
        isinstance(start_line, bool)
        or isinstance(end_line, bool)
        or not isinstance(start_line, int)
        or not isinstance(end_line, int)
    ):
        return None
    if start_line < 1 or end_line < start_line or start_line > MAX_SAFE_JSON_INTEGER:
        return None
    if end_line - start_line + 1 > MAX_SAFE_JSON_INTEGER:
        return None
    return {
        "change_kind": change_kind,
        "file_path": file_path,
        "kind": symbol_kind,
        "qualified_name": qualified_name,
        "start_line": start_line,
        "end_line": end_line,
    }


def validate(reference: Any) -> dict[str, Any]:
    """Return a strict v1 reference or raise ``ValueError`` with the violation."""
    value = _require_object(reference, "code reference")
    if value.get("schema") != SCHEMA:
        raise ValueError("unversioned schema")
    missing = sorted(_REQUIRED - set(value))
    if missing:
        raise ValueError(f"missing required field: {missing[0]}")
    unknown = sorted(set(value) - _REQUIRED)
    if unknown:
        raise ValueError(f"unknown field: {unknown[0]}")

    repository = _require_string(value["repository"], "repository")
    if not _REPOSITORY.fullmatch(repository):
        raise ValueError("repository must be owner/name")
    file_path = _require_relative_posix_path(value["file_path"], "file_path")
    _require_string(value["qualified_name"], "qualified_name")
    symbol_kind = _require_string(value["symbol_kind"], "symbol_kind")
    if symbol_kind not in _SYMBOL_KINDS:
        raise ValueError("symbol_kind must be a supported GraphTrail symbol kind")
    if value["change_kind"] not in _CHANGE_KINDS:
        raise ValueError("change_kind must be added, changed, or removed")

    revision = _require_object(value["revision"], "revision")
    if set(revision) != {"commit"}:
        raise ValueError("revision must contain only commit")
    commit = _require_string(revision.get("commit"), "revision.commit")
    if not _COMMIT.fullmatch(commit):
        raise ValueError("revision.commit must be a 40-character lowercase SHA")

    span = _require_object(value["source_span"], "source_span")
    if set(span) != {"start_line", "line_count"}:
        raise ValueError("source_span must contain start_line and line_count")
    start_line = _require_line(span.get("start_line"), "source_span.start_line")
    line_count = _require_line(span.get("line_count"), "source_span.line_count")
    return {
        "schema": SCHEMA,
        "repository": repository,
        "revision": {"commit": commit},
        "file_path": file_path,
        "qualified_name": value["qualified_name"],
        "symbol_kind": symbol_kind,
        "source_span": {"start_line": start_line, "line_count": line_count},
        "change_kind": value["change_kind"],
    }


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON numeric constant: {value}")


def parse_json_reference(raw: str | bytes | bytearray) -> dict[str, Any]:
    """Parse a raw JSON reference without losing source-span numeric lexemes.

    This is the raw-JSON boundary. Callers holding an already-decoded mapping
    must use ``validate`` and therefore cannot safely pass native floats.
    """
    try:
        value = json.loads(raw, parse_int=Decimal, parse_float=Decimal, parse_constant=_reject_json_constant)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid code reference JSON: {error}") from error
    return validate(value)


def canonical_json(reference: Any) -> str:
    """Return strict, compact, sorted-key JSON for a validated reference."""
    return json.dumps(validate(reference), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
