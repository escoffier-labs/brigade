"""Worker event stream field classification and fail-closed scrubbed projections.

Implements the privacy boundary for Codex app-server worker streams recorded
under ``events/<worker>.jsonl`` (issue #592, first slice).

Raw streams stay local. Scrubbed projections keep only public metadata, stable
IDs, safe operation names, schema versions, and source digests. Unknown event
types or fields fail closed with bounded diagnostics. Legacy raw streams remain
inspectable locally but are marked unclassified until successfully scrubbed.

Standard library only. Brigade is zero-runtime-dependency.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

TRANSPORT = "codex-app-server"
SCHEMA = "brigade.worker_event_scrubbed.v1"
SCHEMA_VERSION = 1
STREAM_SCHEMA = "brigade.worker_event_stream_scrubbed.v1"
STREAM_SCHEMA_VERSION = 1
CLASSIFICATION_MATRIX_VERSION = 1

RAW_MEDIA_TYPE = "application/vnd.brigade.worker-events.appserver+jsonl"
SCRUBBED_MEDIA_TYPE = "application/vnd.brigade.worker-events.appserver.scrubbed+jsonl"
RAW_ARTIFACT_CLASS = "worker-event-stream-raw"
SCRUBBED_ARTIFACT_CLASS = "worker-event-stream-scrubbed"
UNCLASSIFIED_ARTIFACT_CLASS = "worker-event-stream-unclassified"

FIELD_PUBLIC = "public_metadata"
FIELD_PRIVATE = "private_content"
FIELD_SECRET = "secret"
FIELD_PROHIBITED = "prohibited"

FIELD_CLASSES = frozenset({FIELD_PUBLIC, FIELD_PRIVATE, FIELD_SECRET, FIELD_PROHIBITED})

STATUS_SCRUBBED = "scrubbed"
STATUS_UNCLASSIFIED = "unclassified"
STATUS_RAW = "raw"

POLICY_SCRUBBED_ONLY = "scrubbed-only"
POLICY_LOCAL_ONLY = "local-only"
CONSUMER_POLICIES = frozenset({POLICY_SCRUBBED_ONLY, POLICY_LOCAL_ONLY})

MAX_DIAGNOSTIC_LEN = 240
MAX_LINE_BYTES = 1_048_576
MAX_PUBLIC_STRING_LEN = 256
MAX_PUBLIC_LIST_DEPTH = 64
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PUBLIC_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PUBLIC_METHOD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:#-]*$")
_SECRET_LIKE_PUBLIC_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)sk-[a-z0-9][a-z0-9._-]{7,}"), "provider API key"),
    (re.compile(r"(?i)bearer\s+[a-z0-9._\-]+"), "bearer token"),
    (re.compile(r"(?i)(api[_-]?key|password|secret)\s*[:=]"), "credential material"),
    (re.compile(r"(?i)authorization\s*[:=]"), "authorization material"),
)
_AUTO_DECLINED_SUFFIX = "#auto-declined"
_AUTO_DECLINED_METHOD_BASES = frozenset({"item/commandExecution/requestApproval"})

_SCRUBBED_EVENT_TOP_LEVEL_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "transport",
        "classification_status",
        "method",
        "source_digest",
        "params",
        "jsonrpc",
    }
)
_SCRUBBED_STREAM_TOP_LEVEL_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "transport",
        "classification_status",
        "media_type",
        "artifact_class",
        "classification_matrix_version",
        "source_digest",
        "event_count",
        "events",
    }
)

# Top-level JSON-RPC notification envelope keys only.
_ENVELOPE_FIELDS: dict[str, str] = {
    "jsonrpc": FIELD_PUBLIC,
    "method": FIELD_PUBLIC,
    "params": FIELD_PUBLIC,  # nested classification is method-specific
}

# ---------------------------------------------------------------------------
# Field-classification matrix (Codex app-server notification envelope)
# ---------------------------------------------------------------------------
#
# Classes:
#   public_metadata  - safe to retain in scrubbed projections
#   private_content  - prompts, model text, tool args/output, paths, diffs
#   secret           - credentials, auth headers, cookies, env values
#   prohibited       - provider error bodies and other never-export payloads
#
# Unknown methods or fields fail closed. Delta methods are never recorded by
# Brigade's app-server transport and are therefore absent from this matrix.

_TURN_FIELDS: dict[str, str] = {
    "id": FIELD_PUBLIC,
    "status": FIELD_PUBLIC,
    "items": FIELD_PUBLIC,  # each element classified via item matrix
    "error": FIELD_PROHIBITED,
}

_ITEM_TYPE_FIELDS: dict[str, dict[str, str]] = {
    "userMessage": {
        "id": FIELD_PUBLIC,
        "type": FIELD_PUBLIC,
        "content": FIELD_PRIVATE,
        "clientId": FIELD_PUBLIC,
    },
    "agentMessage": {
        "id": FIELD_PUBLIC,
        "type": FIELD_PUBLIC,
        "text": FIELD_PRIVATE,
        "phase": FIELD_PUBLIC,
    },
    "plan": {
        "id": FIELD_PUBLIC,
        "type": FIELD_PUBLIC,
        "text": FIELD_PRIVATE,
    },
    "reasoning": {
        "id": FIELD_PUBLIC,
        "type": FIELD_PUBLIC,
        "summary": FIELD_PRIVATE,
        "content": FIELD_PRIVATE,
    },
    "commandExecution": {
        "id": FIELD_PUBLIC,
        "type": FIELD_PUBLIC,
        "command": FIELD_PRIVATE,
        "cwd": FIELD_PRIVATE,  # absolute paths incl. home
        "status": FIELD_PUBLIC,
        "commandActions": FIELD_PRIVATE,
        "aggregatedOutput": FIELD_PRIVATE,
        "exitCode": FIELD_PUBLIC,
        "durationMs": FIELD_PUBLIC,
    },
    "fileChange": {
        "id": FIELD_PUBLIC,
        "type": FIELD_PUBLIC,
        "changes": FIELD_PRIVATE,
        "status": FIELD_PUBLIC,
    },
    "mcpToolCall": {
        "id": FIELD_PUBLIC,
        "type": FIELD_PUBLIC,
        "server": FIELD_PUBLIC,
        "tool": FIELD_PUBLIC,
        "status": FIELD_PUBLIC,
        "arguments": FIELD_PRIVATE,
        "appContext": FIELD_PRIVATE,
        "pluginId": FIELD_PUBLIC,
        "result": FIELD_PRIVATE,
        "error": FIELD_PROHIBITED,
        "mcpAppResourceUri": FIELD_PRIVATE,
    },
    "dynamicToolCall": {
        "id": FIELD_PUBLIC,
        "type": FIELD_PUBLIC,
        "tool": FIELD_PUBLIC,
        "arguments": FIELD_PRIVATE,
        "status": FIELD_PUBLIC,
        "contentItems": FIELD_PRIVATE,
        "success": FIELD_PUBLIC,
        "durationMs": FIELD_PUBLIC,
    },
    "collabToolCall": {
        "id": FIELD_PUBLIC,
        "type": FIELD_PUBLIC,
        "tool": FIELD_PUBLIC,
        "status": FIELD_PUBLIC,
        "senderThreadId": FIELD_PUBLIC,
        "receiverThreadId": FIELD_PUBLIC,
        "newThreadId": FIELD_PUBLIC,
        "prompt": FIELD_PRIVATE,
        "agentStatus": FIELD_PUBLIC,
    },
    "webSearch": {
        "id": FIELD_PUBLIC,
        "type": FIELD_PUBLIC,
        "query": FIELD_PRIVATE,
        "action": FIELD_PRIVATE,
    },
    "imageView": {
        "id": FIELD_PUBLIC,
        "type": FIELD_PUBLIC,
        "path": FIELD_PRIVATE,
    },
    "enteredReviewMode": {
        "id": FIELD_PUBLIC,
        "type": FIELD_PUBLIC,
        "review": FIELD_PRIVATE,
    },
    "exitedReviewMode": {
        "id": FIELD_PUBLIC,
        "type": FIELD_PUBLIC,
        "review": FIELD_PRIVATE,
    },
    "contextCompaction": {
        "id": FIELD_PUBLIC,
        "type": FIELD_PUBLIC,
    },
}

_METHOD_PARAM_FIELDS: dict[str, dict[str, str]] = {
    "turn/started": {
        "threadId": FIELD_PUBLIC,
        "turn": FIELD_PUBLIC,
    },
    "turn/completed": {
        "threadId": FIELD_PUBLIC,
        "turn": FIELD_PUBLIC,
    },
    "item/started": {
        "threadId": FIELD_PUBLIC,
        "turnId": FIELD_PUBLIC,
        "item": FIELD_PUBLIC,
    },
    "item/completed": {
        "threadId": FIELD_PUBLIC,
        "turnId": FIELD_PUBLIC,
        "completedAtMs": FIELD_PUBLIC,
        "item": FIELD_PUBLIC,
    },
}

# Synthetic Brigade notification emitted when app-server approvals are
# auto-declined (see codex_appserver._handle_server_request).
_AUTO_DECLINED_PARAM_FIELDS: dict[str, str] = {
    "threadId": FIELD_PUBLIC,
    "turnId": FIELD_PUBLIC,
    "itemId": FIELD_PUBLIC,
    "reason": FIELD_PRIVATE,
    "command": FIELD_PRIVATE,
    "cwd": FIELD_PRIVATE,
    "commandActions": FIELD_PRIVATE,
    "proposedExecpolicyAmendment": FIELD_PRIVATE,
    "networkApprovalContext": FIELD_PRIVATE,
    "availableDecisions": FIELD_PUBLIC,
    "additionalPermissions": FIELD_PRIVATE,
    "grantRoot": FIELD_PRIVATE,
    "headers": FIELD_SECRET,
    "authorization": FIELD_SECRET,
    "cookie": FIELD_SECRET,
    "cookies": FIELD_SECRET,
    "credentials": FIELD_SECRET,
    "env": FIELD_SECRET,
    "environment": FIELD_SECRET,
    "prompt": FIELD_PRIVATE,
    "apiKey": FIELD_SECRET,
    "token": FIELD_SECRET,
}

# Keys that are always secret/prohibited wherever they appear under params.
_GLOBAL_SECRET_KEYS: dict[str, str] = {
    "authorization": FIELD_SECRET,
    "Authorization": FIELD_SECRET,
    "cookie": FIELD_SECRET,
    "Cookie": FIELD_SECRET,
    "cookies": FIELD_SECRET,
    "set-cookie": FIELD_SECRET,
    "Set-Cookie": FIELD_SECRET,
    "credentials": FIELD_SECRET,
    "apiKey": FIELD_SECRET,
    "api_key": FIELD_SECRET,
    "token": FIELD_SECRET,
    "accessToken": FIELD_SECRET,
    "refreshToken": FIELD_SECRET,
    "password": FIELD_SECRET,
    "secret": FIELD_SECRET,
    "env": FIELD_SECRET,
    "environment": FIELD_SECRET,
    "headers": FIELD_SECRET,
}

# Documented matrix export for tests and docs generation.
FIELD_CLASSIFICATION_MATRIX: dict[str, Any] = {
    "version": CLASSIFICATION_MATRIX_VERSION,
    "transport": TRANSPORT,
    "field_classes": sorted(FIELD_CLASSES),
    "envelope": dict(_ENVELOPE_FIELDS),
    "methods": {name: {"params": dict(fields)} for name, fields in sorted(_METHOD_PARAM_FIELDS.items())},
    "auto_declined_methods": sorted(_AUTO_DECLINED_METHOD_BASES),
    "auto_declined_params": dict(_AUTO_DECLINED_PARAM_FIELDS),
    "turn_fields": dict(_TURN_FIELDS),
    "item_types": {name: dict(fields) for name, fields in sorted(_ITEM_TYPE_FIELDS.items())},
    "global_secret_keys": dict(_GLOBAL_SECRET_KEYS),
    "notes": (
        "Delta methods are never recorded by Brigade and are intentionally absent. "
        "Unknown methods or fields fail closed. Only "
        "item/commandExecution/requestApproval#auto-declined is accepted; other "
        "*#auto-declined bases fail closed. Absolute home paths travel in cwd/path/"
        "grantRoot and are classified private_content. Provider error bodies are prohibited."
    ),
}


class WorkerEventError(ValueError):
    """Fail-closed scrub or policy error with a bounded diagnostic."""

    def __init__(self, message: str, *, category: str = "scrub") -> None:
        super().__init__(_bound(message))
        self.category = category
        self.diagnostic = _bound(message)


class WorkerEventPolicyError(WorkerEventError):
    """Consumer policy rejected a raw or unclassified worker stream."""

    def __init__(self, message: str) -> None:
        super().__init__(message, category="policy")


@dataclass(frozen=True)
class StreamArtifactInfo:
    """Classification of a worker-event stream artifact on disk."""

    path: Path
    status: str
    media_type: str
    artifact_class: str
    transport: str
    event_count: int
    source_digest: str | None
    diagnostic: str | None = None


def _bound(msg: str) -> str:
    if len(msg) <= MAX_DIAGNOSTIC_LEN:
        return msg
    return msg[: MAX_DIAGNOSTIC_LEN - 1] + "\u2026"


def _validate_public_string(value: str, *, field: str, kind: str = "identifier") -> None:
    if not isinstance(value, str) or not value:
        raise WorkerEventError(_bound(f"{field} must be a non-empty string"), category="type")
    if len(value) > MAX_PUBLIC_STRING_LEN:
        raise WorkerEventError(_bound(f"{field} exceeds {MAX_PUBLIC_STRING_LEN} characters"), category="bound")
    if any(ch in value for ch in ("\n", "\r", "\0")):
        raise WorkerEventError(_bound(f"{field} contains invalid control characters"), category="type")
    pattern = _PUBLIC_METHOD_RE if kind == "method" else _PUBLIC_IDENTIFIER_RE
    if not pattern.fullmatch(value):
        raise WorkerEventError(_bound(f"{field} has invalid public format"), category="type")
    for secret_pattern, label in _SECRET_LIKE_PUBLIC_PATTERNS:
        if secret_pattern.search(value):
            raise WorkerEventError(_bound(f"{field} retains {label}"), category="secret")


def _validate_scrubbed_scalar_public(path: str, value: Any, *, depth: int = 0) -> list[str]:
    errors: list[str] = []
    if depth > MAX_PUBLIC_LIST_DEPTH:
        return [_bound(f"{path} public list nesting exceeds {MAX_PUBLIC_LIST_DEPTH}")]
    if isinstance(value, str):
        try:
            kind = "method" if path.endswith(".method") or path == "method" else "identifier"
            _validate_public_string(value, field=path, kind=kind)
        except WorkerEventError as exc:
            errors.append(exc.diagnostic)
    elif value is None or isinstance(value, int) and not isinstance(value, bool):
        return errors
    elif isinstance(value, bool):
        return errors
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            errors.extend(_validate_scrubbed_scalar_public(f"{path}[{index}]", nested, depth=depth + 1))
    else:
        errors.append(_bound(f"{path} has unsupported type {type(value).__name__}"))
    return errors


def _validate_scrubbed_object(obj: Mapping[str, Any], matrix: Mapping[str, str], *, path: str) -> list[str]:
    errors: list[str] = []
    for key, value in obj.items():
        if not isinstance(key, str):
            errors.append(_bound(f"{path} has non-string key"))
            continue
        global_class = _GLOBAL_SECRET_KEYS.get(key)
        classification = global_class or matrix.get(key)
        if classification is None:
            errors.append(_bound(f"unknown field {path}.{key}"))
            continue
        if classification != FIELD_PUBLIC:
            errors.append(_bound(f"scrubbed projection retains private field {path}.{key}"))
            continue
        if key == "item":
            errors.extend(_validate_scrubbed_item(value, path=f"{path}.{key}"))
        elif key == "turn":
            errors.extend(_validate_scrubbed_turn(value, path=f"{path}.{key}"))
        else:
            errors.extend(_validate_scrubbed_scalar_public(f"{path}.{key}", value))
    return errors


def _validate_scrubbed_item(item: Any, *, path: str) -> list[str]:
    if not isinstance(item, Mapping):
        return [_bound(f"{path} must be an object")]
    item_type = item.get("type")
    if not isinstance(item_type, str) or not item_type:
        return [_bound(f"{path}.type must be a non-empty string")]
    matrix = _ITEM_TYPE_FIELDS.get(item_type)
    if matrix is None:
        return [_bound(f"unknown item type {item_type!r}")]
    return _validate_scrubbed_object(item, matrix, path=path)


def _validate_scrubbed_turn(turn: Any, *, path: str) -> list[str]:
    if not isinstance(turn, Mapping):
        return [_bound(f"{path} must be an object")]
    errors: list[str] = []
    for key, value in turn.items():
        if not isinstance(key, str):
            errors.append(_bound(f"{path} has non-string key"))
            continue
        classification = _TURN_FIELDS.get(key)
        if classification is None:
            errors.append(_bound(f"unknown field {path}.{key}"))
            continue
        if classification != FIELD_PUBLIC:
            errors.append(_bound(f"scrubbed projection retains private field {path}.{key}"))
            continue
        if key == "items":
            if not isinstance(value, list):
                errors.append(_bound(f"{path}.items must be an array"))
                continue
            for index, entry in enumerate(value):
                errors.extend(_validate_scrubbed_item(entry, path=f"{path}.items[{index}]"))
            continue
        errors.extend(_validate_scrubbed_scalar_public(f"{path}.{key}", value))
    return errors


def _validate_scrubbed_params_structure(method: str, params: Mapping[str, Any], *, path: str) -> list[str]:
    try:
        matrix = _param_matrix_for_method(method)
    except WorkerEventError as exc:
        return [exc.diagnostic]
    return _validate_scrubbed_object(params, matrix, path=path)


def _scrubbed_single_event_document_validation_errors(payload: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    unknown_keys = sorted(set(payload.keys()) - _SCRUBBED_EVENT_TOP_LEVEL_KEYS)
    if unknown_keys:
        errors.append(_bound(f"unknown scrubbed event keys: {', '.join(unknown_keys[:8])}"))
    errors.extend(validate_scrubbed_event(payload))
    return errors


def _scrubbed_stream_document_validation_errors(payload: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    unknown_keys = sorted(set(payload.keys()) - _SCRUBBED_STREAM_TOP_LEVEL_KEYS)
    if unknown_keys:
        errors.append(_bound(f"unknown scrubbed stream keys: {', '.join(unknown_keys[:8])}"))
    if payload.get("schema") != STREAM_SCHEMA:
        errors.append(_bound(f"schema must be {STREAM_SCHEMA!r}"))
    if payload.get("schema_version") != STREAM_SCHEMA_VERSION:
        errors.append(_bound(f"schema_version must be {STREAM_SCHEMA_VERSION}"))
    if payload.get("transport") != TRANSPORT:
        errors.append(_bound(f"transport must be {TRANSPORT!r}"))
    if payload.get("classification_status") != STATUS_SCRUBBED:
        errors.append("classification_status must be scrubbed")
    if payload.get("media_type") != SCRUBBED_MEDIA_TYPE:
        errors.append(_bound(f"media_type must be {SCRUBBED_MEDIA_TYPE!r}"))
    if payload.get("artifact_class") != SCRUBBED_ARTIFACT_CLASS:
        errors.append(_bound(f"artifact_class must be {SCRUBBED_ARTIFACT_CLASS!r}"))
    digest = payload.get("source_digest")
    if not (isinstance(digest, str) and _HEX64.fullmatch(digest)):
        errors.append("source_digest must be a 64-char lowercase hex string")
    events = payload.get("events")
    if not isinstance(events, list):
        errors.append("events must be an array")
        return errors
    event_count = payload.get("event_count")
    if not isinstance(event_count, int):
        errors.append("event_count must be an integer")
    elif event_count != len(events):
        errors.append("event_count must match events length")
    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            errors.append(_bound(f"events[{index}] must be an object"))
            continue
        for diagnostic in validate_scrubbed_event(event):
            errors.append(_bound(f"events[{index}]: {diagnostic}"))
    return errors


def _scrubbed_document_validation_errors(payload: Mapping[str, Any]) -> list[str]:
    schema = payload.get("schema")
    if schema == SCHEMA:
        return _scrubbed_single_event_document_validation_errors(payload)
    if schema == STREAM_SCHEMA:
        return _scrubbed_stream_document_validation_errors(payload)
    return [_bound(f"schema must be {SCHEMA!r} or {STREAM_SCHEMA!r}")]


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_event_bytes(obj: Any) -> bytes:
    """Canonical UTF-8 JSON for source digests: sorted keys, compact separators."""
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise WorkerEventError(_bound(f"event cannot be canonicalized: {exc}")) from exc


def source_digest_for_event(raw: Mapping[str, Any]) -> str:
    return _sha256_hex(canonical_event_bytes(dict(raw)))


def source_digest_for_stream_bytes(raw_bytes: bytes) -> str:
    return _sha256_hex(raw_bytes)


def classification_matrix() -> dict[str, Any]:
    """Return a deep copy of the documented field-classification matrix."""
    return json.loads(json.dumps(FIELD_CLASSIFICATION_MATRIX))


def _is_auto_declined(method: str) -> bool:
    if not method.endswith(_AUTO_DECLINED_SUFFIX):
        return False
    base = method[: -len(_AUTO_DECLINED_SUFFIX)]
    return base in _AUTO_DECLINED_METHOD_BASES


def _param_matrix_for_method(method: str) -> dict[str, str]:
    if method in _METHOD_PARAM_FIELDS:
        return _METHOD_PARAM_FIELDS[method]
    if _is_auto_declined(method):
        return _AUTO_DECLINED_PARAM_FIELDS
    raise WorkerEventError(_bound(f"unknown event method {method!r}"), category="unknown-method")


def _scrub_scalar_public(key: str, value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_PUBLIC_LIST_DEPTH:
        raise WorkerEventError(
            _bound(f"public field {key!r} list nesting exceeds {MAX_PUBLIC_LIST_DEPTH}"),
            category="bound",
        )
    if isinstance(value, str):
        _validate_public_string(value, field=key)
        return value
    if value is None or isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        # Duration-like numbers occasionally appear as floats; refuse rather than coerce.
        raise WorkerEventError(_bound(f"public field {key!r} must not be float"), category="type")
    if isinstance(value, bool):
        # JSON-RPC sometimes carries booleans (e.g. success). Allow only for known public bools.
        return value
    if isinstance(value, list):
        return [_scrub_scalar_public(f"{key}[]", item, depth=depth + 1) for item in value]
    raise WorkerEventError(
        _bound(f"public field {key!r} has unsupported type {type(value).__name__}"),
        category="type",
    )


def _scrub_item(item: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise WorkerEventError(_bound(f"{path} must be an object"), category="type")
    item_type = item.get("type")
    if not isinstance(item_type, str) or not item_type:
        raise WorkerEventError(_bound(f"{path}.type must be a non-empty string"), category="type")
    matrix = _ITEM_TYPE_FIELDS.get(item_type)
    if matrix is None:
        raise WorkerEventError(_bound(f"unknown item type {item_type!r}"), category="unknown-field")
    return _scrub_object(item, matrix, path=path)


def _scrub_turn(turn: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(turn, Mapping):
        raise WorkerEventError(_bound(f"{path} must be an object"), category="type")
    out: dict[str, Any] = {}
    for key, value in turn.items():
        if not isinstance(key, str):
            raise WorkerEventError(_bound(f"{path} has non-string key"), category="unknown-field")
        classification = _TURN_FIELDS.get(key)
        if classification is None:
            raise WorkerEventError(_bound(f"unknown field {path}.{key}"), category="unknown-field")
        if classification in (FIELD_PRIVATE, FIELD_SECRET, FIELD_PROHIBITED):
            continue
        if key == "items":
            if not isinstance(value, list):
                raise WorkerEventError(_bound(f"{path}.items must be an array"), category="type")
            out["items"] = [_scrub_item(entry, path=f"{path}.items[]") for entry in value]
            continue
        out[key] = _scrub_scalar_public(f"{path}.{key}", value)
    return out


def _scrub_object(obj: Mapping[str, Any], matrix: Mapping[str, str], *, path: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in obj.items():
        if not isinstance(key, str):
            raise WorkerEventError(_bound(f"{path} has non-string key"), category="unknown-field")
        global_class = _GLOBAL_SECRET_KEYS.get(key)
        classification = global_class or matrix.get(key)
        if classification is None:
            raise WorkerEventError(_bound(f"unknown field {path}.{key}"), category="unknown-field")
        if classification in (FIELD_PRIVATE, FIELD_SECRET, FIELD_PROHIBITED):
            continue
        if key == "item":
            out[key] = _scrub_item(value, path=f"{path}.{key}")
            continue
        if key == "turn":
            out[key] = _scrub_turn(value, path=f"{path}.{key}")
            continue
        out[key] = _scrub_scalar_public(f"{path}.{key}", value)
    return out


def scrub_event(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return a scrubbed projection of one app-server notification event.

    Raises ``WorkerEventError`` when the event method or any field cannot be
    classified. Private, secret, and prohibited fields are omitted.
    """
    if not isinstance(raw, Mapping):
        raise WorkerEventError("event must be a JSON object", category="type")

    unknown_envelope = sorted(set(raw.keys()) - set(_ENVELOPE_FIELDS))
    if unknown_envelope:
        shown = ", ".join(unknown_envelope[:8])
        raise WorkerEventError(_bound(f"unknown envelope keys: {shown}"), category="unknown-field")

    method = raw.get("method")
    if not isinstance(method, str) or not method:
        raise WorkerEventError("method must be a non-empty string", category="type")
    _validate_public_string(method, field="method", kind="method")

    params = raw.get("params")
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        raise WorkerEventError("params must be an object", category="type")

    matrix = _param_matrix_for_method(method)
    scrubbed_params = _scrub_object(params, matrix, path="params")

    digest = source_digest_for_event(raw)
    projection: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "transport": TRANSPORT,
        "classification_status": STATUS_SCRUBBED,
        "method": method,
        "source_digest": digest,
        "params": scrubbed_params,
    }
    if "jsonrpc" in raw:
        jsonrpc = raw["jsonrpc"]
        if jsonrpc != "2.0":
            raise WorkerEventError(_bound(f"unsupported jsonrpc {jsonrpc!r}"), category="type")
        projection["jsonrpc"] = jsonrpc
    validation_errors = validate_scrubbed_event(projection)
    if validation_errors:
        raise WorkerEventError(validation_errors[0], category="schema")
    return projection


def _iter_raw_lines(raw_text: str) -> Iterator[tuple[int, str]]:
    for index, line in enumerate(raw_text.splitlines(), start=1):
        if not line.strip():
            continue
        yield index, line


def scrub_stream_text(raw_text: str) -> dict[str, Any]:
    """Scrub an entire NDJSON worker stream into a bounded projection document."""
    raw_bytes = raw_text.encode("utf-8")
    if len(raw_bytes) > MAX_LINE_BYTES * 4096:
        raise WorkerEventError("worker event stream exceeds bounded size", category="bound")

    events: list[dict[str, Any]] = []
    for line_no, line in _iter_raw_lines(raw_text):
        line_bytes = line.encode("utf-8")
        if len(line_bytes) > MAX_LINE_BYTES:
            raise WorkerEventError(_bound(f"line {line_no} exceeds {MAX_LINE_BYTES} bytes"), category="bound")
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkerEventError(_bound(f"line {line_no} is not valid JSON: {exc}"), category="parse") from exc
        if not isinstance(parsed, Mapping):
            raise WorkerEventError(_bound(f"line {line_no} must be a JSON object"), category="type")
        try:
            events.append(scrub_event(parsed))
        except WorkerEventError as exc:
            raise WorkerEventError(
                _bound(f"line {line_no}: {exc.diagnostic}"),
                category=exc.category,
            ) from exc

    return {
        "schema": STREAM_SCHEMA,
        "schema_version": STREAM_SCHEMA_VERSION,
        "transport": TRANSPORT,
        "classification_status": STATUS_SCRUBBED,
        "media_type": SCRUBBED_MEDIA_TYPE,
        "artifact_class": SCRUBBED_ARTIFACT_CLASS,
        "classification_matrix_version": CLASSIFICATION_MATRIX_VERSION,
        "source_digest": source_digest_for_stream_bytes(raw_bytes),
        "event_count": len(events),
        "events": events,
    }


def scrub_stream_lines(lines: Iterable[str | Mapping[str, Any]]) -> dict[str, Any]:
    """Scrub an iterable of NDJSON lines or already-parsed event objects."""
    text_parts: list[str] = []
    for item in lines:
        if isinstance(item, Mapping):
            text_parts.append(json.dumps(item, sort_keys=True, separators=(",", ":")))
        elif isinstance(item, str):
            text_parts.append(item.rstrip("\n"))
        else:
            raise WorkerEventError(
                _bound(f"stream line has unsupported type {type(item).__name__}"),
                category="type",
            )
    return scrub_stream_text("\n".join(text_parts) + ("\n" if text_parts else ""))


def scrub_stream_file(path: Path) -> dict[str, Any]:
    """Scrub a worker event JSONL file into a scrubbed projection document."""
    path = Path(path)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkerEventError(_bound(f"cannot read worker event stream: {exc}"), category="io") from exc
    except UnicodeDecodeError as exc:
        raise WorkerEventError("worker event stream is not valid UTF-8", category="parse") from exc
    return scrub_stream_text(raw_text)


def _looks_like_scrubbed_document(payload: Any) -> bool:
    return (
        isinstance(payload, Mapping)
        and payload.get("schema") in {SCHEMA, STREAM_SCHEMA}
        and payload.get("classification_status") == STATUS_SCRUBBED
    )


def _looks_like_raw_notification(payload: Any) -> bool:
    return isinstance(payload, Mapping) and isinstance(payload.get("method"), str)


def inspect_stream_file(path: Path) -> StreamArtifactInfo:
    """Classify a worker-event file without requiring a successful scrub.

    Legacy raw streams are marked ``unclassified`` until scrubbing succeeds.
    Already-scrubbed projection documents are marked ``scrubbed``.
    """
    path = Path(path)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return StreamArtifactInfo(
            path=path,
            status=STATUS_UNCLASSIFIED,
            media_type=RAW_MEDIA_TYPE,
            artifact_class=UNCLASSIFIED_ARTIFACT_CLASS,
            transport=TRANSPORT,
            event_count=0,
            source_digest=None,
            diagnostic=_bound(f"unreadable worker event stream: {exc}"),
        )
    except UnicodeDecodeError:
        return StreamArtifactInfo(
            path=path,
            status=STATUS_UNCLASSIFIED,
            media_type=RAW_MEDIA_TYPE,
            artifact_class=UNCLASSIFIED_ARTIFACT_CLASS,
            transport=TRANSPORT,
            event_count=0,
            source_digest=None,
            diagnostic="worker event stream is not valid UTF-8",
        )

    raw_bytes = raw_text.encode("utf-8")
    digest = source_digest_for_stream_bytes(raw_bytes)

    # Whole-file scrubbed projection document (JSON object, not NDJSON).
    stripped = raw_text.lstrip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            payload = None
        if _looks_like_scrubbed_document(payload):
            assert isinstance(payload, Mapping)
            validation_errors = _scrubbed_document_validation_errors(payload)
            if validation_errors:
                return StreamArtifactInfo(
                    path=path,
                    status=STATUS_UNCLASSIFIED,
                    media_type=RAW_MEDIA_TYPE,
                    artifact_class=UNCLASSIFIED_ARTIFACT_CLASS,
                    transport=TRANSPORT,
                    event_count=0,
                    source_digest=digest,
                    diagnostic=validation_errors[0],
                )
            if payload.get("schema") == SCHEMA:
                return StreamArtifactInfo(
                    path=path,
                    status=STATUS_SCRUBBED,
                    media_type=SCRUBBED_MEDIA_TYPE,
                    artifact_class=SCRUBBED_ARTIFACT_CLASS,
                    transport=str(payload.get("transport") or TRANSPORT),
                    event_count=1,
                    source_digest=str(payload.get("source_digest") or digest),
                )
            event_count = payload.get("event_count")
            if not isinstance(event_count, int):
                events = payload.get("events")
                event_count = len(events) if isinstance(events, list) else 0
            return StreamArtifactInfo(
                path=path,
                status=STATUS_SCRUBBED,
                media_type=SCRUBBED_MEDIA_TYPE,
                artifact_class=SCRUBBED_ARTIFACT_CLASS,
                transport=str(payload.get("transport") or TRANSPORT),
                event_count=event_count,
                source_digest=str(payload.get("source_digest") or digest),
            )

    lines = [line for _, line in _iter_raw_lines(raw_text)]
    if not lines:
        return StreamArtifactInfo(
            path=path,
            status=STATUS_UNCLASSIFIED,
            media_type=RAW_MEDIA_TYPE,
            artifact_class=UNCLASSIFIED_ARTIFACT_CLASS,
            transport=TRANSPORT,
            event_count=0,
            source_digest=digest,
            diagnostic="empty worker event stream",
        )

    # Attempt classification. Success still leaves the on-disk raw NDJSON
    # unclassified until a distinct scrubbed projection exists; failure keeps
    # the stream locally inspectable with a bounded diagnostic.
    try:
        for line in lines:
            parsed = json.loads(line)
            scrub_event(parsed)
    except (json.JSONDecodeError, WorkerEventError) as exc:
        detail = exc.diagnostic if isinstance(exc, WorkerEventError) else str(exc)
        return StreamArtifactInfo(
            path=path,
            status=STATUS_UNCLASSIFIED,
            media_type=RAW_MEDIA_TYPE,
            artifact_class=UNCLASSIFIED_ARTIFACT_CLASS,
            transport=TRANSPORT,
            event_count=len(lines),
            source_digest=digest,
            diagnostic=_bound(detail),
        )

    return StreamArtifactInfo(
        path=path,
        status=STATUS_UNCLASSIFIED,
        media_type=RAW_MEDIA_TYPE,
        artifact_class=UNCLASSIFIED_ARTIFACT_CLASS,
        transport=TRANSPORT,
        event_count=len(lines),
        source_digest=digest,
        diagnostic="raw worker stream; unclassified until scrubbed projection exists",
    )


def require_consumer_policy(
    info: StreamArtifactInfo,
    *,
    consumer: str,
    policy: str = POLICY_SCRUBBED_ONLY,
) -> None:
    """Reject raw/unclassified streams as portable evidence unless local-only.

    Replay and audit must pass ``policy=scrubbed-only`` (the default) and only
    accept already-scrubbed artifacts (or scrub into one before use). Local
    salvage paths such as resume may pass ``policy=local-only``.
    """
    if policy not in CONSUMER_POLICIES:
        raise WorkerEventPolicyError(_bound(f"unknown consumer policy {policy!r}"))
    if policy == POLICY_LOCAL_ONLY:
        return
    if info.status == STATUS_SCRUBBED and info.artifact_class == SCRUBBED_ARTIFACT_CLASS:
        return
    raise WorkerEventPolicyError(
        _bound(
            f"{consumer} rejects {info.status} worker event stream "
            f"({info.artifact_class}); scrub or set policy={POLICY_LOCAL_ONLY}"
        )
    )


def load_stream_for_consumer(
    path: Path,
    *,
    consumer: str,
    policy: str = POLICY_SCRUBBED_ONLY,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Load a worker stream under a consumer policy.

    ``scrubbed-only`` never returns raw notifications: it loads an existing
    scrubbed document or fails closed while scrubbing NDJSON into one.
    ``local-only`` may return the parsed raw notification list for salvage.
    """
    path = Path(path)
    if policy not in CONSUMER_POLICIES:
        raise WorkerEventPolicyError(_bound(f"unknown consumer policy {policy!r}"))

    info = inspect_stream_file(path)

    if policy == POLICY_LOCAL_ONLY:
        if info.status == STATUS_SCRUBBED and _file_is_scrubbed_document(path):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                raise WorkerEventError("scrubbed document must be a JSON object", category="schema")
            validation_errors = _scrubbed_document_validation_errors(payload)
            if validation_errors:
                raise WorkerEventError(validation_errors[0], category="schema")
            return dict(payload)
        try:
            raw_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise WorkerEventError(_bound(f"cannot read worker event stream: {exc}"), category="io") from exc
        events: list[dict[str, Any]] = []
        for line_no, line in _iter_raw_lines(raw_text):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                raise WorkerEventError(_bound(f"line {line_no} is not valid JSON"), category="parse") from exc
            if not _looks_like_raw_notification(parsed):
                raise WorkerEventError(_bound(f"line {line_no} is not a notification"), category="type")
            events.append(dict(parsed))
        return events

    # scrubbed-only: portable consumers must not receive raw notifications.
    if info.status == STATUS_SCRUBBED and _file_is_scrubbed_document(path):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise WorkerEventError("scrubbed document must be a JSON object", category="schema")
        validation_errors = _scrubbed_document_validation_errors(payload)
        if validation_errors:
            raise WorkerEventError(validation_errors[0], category="schema")
        return dict(payload)

    # Raw NDJSON is not admissible as audit/replay evidence. Scrub into a
    # distinct scrubbed artifact via scrub_stream_file first, or pass
    # policy=local-only for local salvage.
    require_consumer_policy(info, consumer=consumer, policy=policy)
    raise AssertionError("require_consumer_policy must reject non-scrubbed streams")  # pragma: no cover


def _file_is_scrubbed_document(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if not text.lstrip().startswith("{"):
        return False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False
    return _looks_like_scrubbed_document(payload)


def list_worker_event_streams(events_dir: Path) -> list[Path]:
    """Return worker event JSONL paths under an events directory (excludes lifecycle)."""
    events_dir = Path(events_dir)
    if not events_dir.is_dir():
        return []
    out: list[Path] = []
    for path in sorted(events_dir.glob("*.jsonl")):
        if path.name == "lifecycle.jsonl":
            continue
        out.append(path)
    return out


def assert_audit_rejects_raw_streams(
    events_dir: Path,
    *,
    policy: str = POLICY_SCRUBBED_ONLY,
    consumer: str = "run_audit",
) -> list[StreamArtifactInfo]:
    """Gate used by audit/replay: raw streams are rejected under scrubbed-only.

    Inspects each worker JSONL under ``events_dir``. Under the default policy,
    any raw or unclassified stream raises ``WorkerEventPolicyError``. Local-only
    policy allows inspection without treating the bytes as portable evidence.
    """
    infos: list[StreamArtifactInfo] = []
    for path in list_worker_event_streams(events_dir):
        info = inspect_stream_file(path)
        infos.append(info)
        require_consumer_policy(info, consumer=consumer, policy=policy)
    return infos


def validate_scrubbed_event(event: Mapping[str, Any]) -> list[str]:
    """Return bounded diagnostics for a scrubbed event projection. Empty = valid."""
    errors: list[str] = []
    if not isinstance(event, Mapping):
        return ["scrubbed event must be a JSON object"]
    unknown_keys = sorted(set(event.keys()) - _SCRUBBED_EVENT_TOP_LEVEL_KEYS)
    if unknown_keys:
        errors.append(_bound(f"unknown scrubbed event keys: {', '.join(unknown_keys[:8])}"))
    if event.get("schema") != SCHEMA:
        errors.append(_bound(f"schema must be {SCHEMA!r}"))
    if event.get("schema_version") != SCHEMA_VERSION:
        errors.append(_bound(f"schema_version must be {SCHEMA_VERSION}"))
    if event.get("transport") != TRANSPORT:
        errors.append(_bound(f"transport must be {TRANSPORT!r}"))
    if event.get("classification_status") != STATUS_SCRUBBED:
        errors.append("classification_status must be scrubbed")
    method = event.get("method")
    if not isinstance(method, str) or not method:
        errors.append("method must be a non-empty string")
    else:
        try:
            _validate_public_string(method, field="method", kind="method")
        except WorkerEventError as exc:
            errors.append(exc.diagnostic)
    digest = event.get("source_digest")
    if not (isinstance(digest, str) and _HEX64.fullmatch(digest)):
        errors.append("source_digest must be a 64-char lowercase hex string")
    jsonrpc = event.get("jsonrpc")
    if jsonrpc is not None and jsonrpc != "2.0":
        errors.append(_bound(f"jsonrpc must be '2.0'"))
    params = event.get("params")
    if not isinstance(params, Mapping):
        errors.append("params must be an object")
    elif isinstance(method, str) and method:
        errors.extend(_validate_scrubbed_params_structure(method, params, path="params"))
    errors.extend(scrubbed_projection_omits_sensitive_material(event))
    return errors


def scrubbed_projection_omits_sensitive_material(event: Mapping[str, Any]) -> list[str]:
    """Return diagnostics if scrubbed projection still contains sensitive material."""
    errors: list[str] = []
    blob = json.dumps(event, sort_keys=True, ensure_ascii=False)

    patterns = [
        (re.compile(r"(?i)authorization\s*[:=]"), "authorization material"),
        (re.compile(r"(?i)bearer\s+[a-z0-9._\-]+"), "bearer token"),
        (re.compile(r"(?i)(api[_-]?key|password|secret)\s*[:=]\s*\S+"), "credential material"),
        (re.compile(r"(?i)set-cookie\s*:"), "cookie material"),
        (re.compile(r"(?i)cookie\s*[:=]\s*\S+"), "cookie material"),
        (re.compile(r"/home/[^\s\"']+"), "absolute home path"),
        (re.compile(r"/Users/[^\s\"']+"), "absolute home path"),
        (re.compile(r"(?i)sk-[a-z0-9][a-z0-9._-]{7,}"), "provider API key"),
        (re.compile(r"(?i)\"text\"\s*:\s*\"[^\"]+\""), "private text content"),
        (re.compile(r"(?i)\"command\"\s*:\s*\"[^\"]+\""), "private command content"),
        (re.compile(r"(?i)\"prompt\"\s*:\s*\"[^\"]+\""), "raw prompt"),
        (re.compile(r"(?i)\"message\"\s*:\s*\"[^\"]+\""), "provider error body"),
        (re.compile(r"(?i)\"aggregatedOutput\"\s*:"), "retrieved private content"),
        (re.compile(r"(?i)\"env\"\s*:\s*\{"), "environment values"),
    ]
    for pattern, label in patterns:
        if pattern.search(blob):
            errors.append(_bound(f"scrubbed projection retains {label}"))
    return errors


def media_types() -> dict[str, str]:
    return {
        "raw": RAW_MEDIA_TYPE,
        "scrubbed": SCRUBBED_MEDIA_TYPE,
    }


def artifact_classes() -> dict[str, str]:
    return {
        "raw": RAW_ARTIFACT_CLASS,
        "scrubbed": SCRUBBED_ARTIFACT_CLASS,
        "unclassified": UNCLASSIFIED_ARTIFACT_CLASS,
    }


__all__ = [
    "CLASSIFICATION_MATRIX_VERSION",
    "CONSUMER_POLICIES",
    "FIELD_CLASSIFICATION_MATRIX",
    "FIELD_CLASSES",
    "FIELD_PRIVATE",
    "FIELD_PROHIBITED",
    "FIELD_PUBLIC",
    "FIELD_SECRET",
    "POLICY_LOCAL_ONLY",
    "POLICY_SCRUBBED_ONLY",
    "RAW_ARTIFACT_CLASS",
    "RAW_MEDIA_TYPE",
    "SCHEMA",
    "SCHEMA_VERSION",
    "SCRUBBED_ARTIFACT_CLASS",
    "SCRUBBED_MEDIA_TYPE",
    "STATUS_RAW",
    "STATUS_SCRUBBED",
    "STATUS_UNCLASSIFIED",
    "STREAM_SCHEMA",
    "STREAM_SCHEMA_VERSION",
    "TRANSPORT",
    "UNCLASSIFIED_ARTIFACT_CLASS",
    "StreamArtifactInfo",
    "WorkerEventError",
    "WorkerEventPolicyError",
    "assert_audit_rejects_raw_streams",
    "artifact_classes",
    "canonical_event_bytes",
    "classification_matrix",
    "inspect_stream_file",
    "list_worker_event_streams",
    "load_stream_for_consumer",
    "media_types",
    "require_consumer_policy",
    "scrub_event",
    "scrub_stream_file",
    "scrub_stream_lines",
    "scrub_stream_text",
    "scrubbed_projection_omits_sensitive_material",
    "source_digest_for_event",
    "source_digest_for_stream_bytes",
    "validate_scrubbed_event",
]
