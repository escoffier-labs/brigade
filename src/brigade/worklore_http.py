"""Isolated /work handlers: path parsing, authz, JSON envelopes, and status codes."""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs, urlsplit

from . import toml_compat
from . import worklore_store as store
from .fleet_client import FLEET_CONFIG_REL_PATH, brigade_home
from .worklore_validate import WorkloreValidationError

_ENABLED_TRUE = frozenset({"1", "true", "yes", "on"})
_ENABLED_FALSE = frozenset({"0", "false", "no", "off"})
_OPERATOR_NODES_ENV = "BRIGADE_WORKLORE_OPERATOR_NODES"
_OPERATOR_NODES_MAX = 32
_OPERATOR_NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# ``admin`` is the actor id and the import owner_node this module writes for the hub admin
# token, so a node presenting it as its own id would author events and own adapter
# namespaces indistinguishable from the admin's. The id is reserved: a node actor may not
# use it, and it is never accepted as a configured operator node.
RESERVED_NODE_ID = "admin"
# Numeric request fields are small counters. Bounding the digit count before any
# int() keeps a caller from spending CPU on a million-digit literal, and keeps
# CPython's 4300-digit conversion guard from surfacing as an unhandled ValueError.
_MAX_NUMBER_DIGITS = 10
# An opaque page cursor is base64 of a small JSON object (~120 chars in practice).
_MAX_CURSOR_CHARS = 512

_STATUS_BY_CODE = {
    "unauthorized": 401,
    "forbidden": 403,
    "not-found": 404,
    "version-conflict": 409,
    "if-match-required": 400,
    "invalid-transition": 400,
    "unknown-field": 400,
    "field-bound": 400,
    "private-data": 400,
    "acceptance-required": 400,
    "import-conflict": 409,
    "adapter-owner-mismatch": 403,
    "execution-mismatch": 403,
    "attempt-forbidden": 403,
    "attempt-conflict": 409,
    "link-forbidden": 403,
    "link-conflict": 409,
    "hub-unavailable": 503,
    "internal-error": 500,
}

_STORE_ERRORS = (
    store.WorkloreConflict,
    store.WorkloreNotFound,
    store.WorkloreForbidden,
    WorkloreValidationError,
)


@dataclass(frozen=True)
class Request:
    """Authenticated caller plus a parsed /work request."""

    method: str
    path: str
    is_admin: bool = False
    node_id: str | None = None
    is_operator: bool = False
    operator_authorization_resolved: bool = False
    body: Mapping[str, Any] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)


def enabled(
    environ: Mapping[str, str] | None = None,
    config_path: str | Path | None = None,
) -> bool:
    """Fail-closed Worklore feature gate: recognized env wins, else TOML boolean."""
    env = os.environ if environ is None else environ
    raw = env.get("BRIGADE_WORKLORE_ENABLED")
    if raw is not None:
        value = str(raw).strip().lower()
        if value in _ENABLED_TRUE:
            return True
        if value in _ENABLED_FALSE:
            return False
    return _worklore_section(config_path).get("enabled") is True


def operator_nodes(
    environ: Mapping[str, str] | None = None,
    config_path: str | Path | None = None,
) -> tuple[str, ...]:
    """Worklore-only operator node ids from env override or ``fleet.worklore.operator_nodes``."""
    env = os.environ if environ is None else environ
    if _OPERATOR_NODES_ENV in env:
        return _parse_operator_nodes_env(env.get(_OPERATOR_NODES_ENV, ""))
    return _normalize_operator_nodes(_worklore_section(config_path).get("operator_nodes"))


def _worklore_section(config_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(config_path) if config_path is not None else brigade_home() / FLEET_CONFIG_REL_PATH.name
    try:
        payload = toml_compat.loads(path.read_text(encoding="utf-8"))
    except (OSError, toml_compat.TOMLDecodeError):
        return {}
    fleet = payload.get("fleet") if isinstance(payload, dict) else None
    section = fleet.get("worklore") if isinstance(fleet, dict) else None
    return section if isinstance(section, dict) else {}


def _parse_operator_nodes_env(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, str) or not raw.strip():
        return ()
    return _normalize_operator_nodes(part.strip() for part in raw.split(","))


def _normalize_operator_nodes(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, (str, bytes, Mapping)) or not isinstance(raw, Iterable):
        return ()
    seen: set[str] = set()
    nodes: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            continue
        node_id = value.strip()
        if node_id in {"unknown", RESERVED_NODE_ID} or not _OPERATOR_NODE_RE.fullmatch(node_id):
            continue
        if node_id in seen:
            continue
        seen.add(node_id)
        nodes.append(node_id)
        if len(nodes) >= _OPERATOR_NODES_MAX:
            break
    return tuple(nodes)


def handle(conn: sqlite3.Connection, request: Request) -> tuple[int, dict[str, Any]]:
    """Dispatch one /work request and map store errors to the stable JSON contract."""
    request = _snapshot_operator_authorization(request)
    try:
        return _dispatch(conn, request)
    except _STORE_ERRORS as exc:
        return _mapped_error(exc)
    except sqlite3.Error:
        return _error("internal-error", "internal error")
    except (ValueError, OverflowError):
        # A number that survived the digit bound but is still out of range (an
        # If-Match or cursor sequence wider than SQLite's INTEGER, say) becomes the
        # ordinary field-bound refusal instead of a traceback.
        return _error("field-bound", "request field is out of bounds")


def _dispatch(conn: sqlite3.Connection, request: Request) -> tuple[int, dict[str, Any]]:
    if not request.is_admin and not request.node_id:
        return _error("unauthorized")
    if not request.is_admin and request.node_id == RESERVED_NODE_ID:
        return _error("forbidden", f"node id {RESERVED_NODE_ID!r} is reserved for the hub admin token")
    parsed = urlsplit(request.path)
    parts = [part for part in parsed.path.split("/") if part]
    query = {key: values[-1] for key, values in parse_qs(parsed.query, keep_blank_values=True).items() if values}
    method = request.method.upper()
    if parts[:1] != ["work"]:
        return _error("not-found")
    rest = parts[1:]
    if rest == ["items"] and method == "GET":
        _validate_query(query, {"status", "kind", "burn_eligible", "source", "limit", "cursor"})
        return 200, store.list_items(
            conn,
            status=query.get("status"),
            kind=query.get("kind"),
            burn_eligible=query.get("burn_eligible"),
            source=query.get("source"),
            limit=_bounded_limit(query.get("limit", 50)),
            cursor=_bounded_cursor(query.get("cursor")),
        )
    if rest == ["items"] and method == "POST":
        _require_operator(request)
        item = store.create_item(
            conn,
            dict(request.body),
            actor_id=_actor_id(request),
            actor_type=_actor_type(request),
            node_id=_event_node_id(request),
            idempotency_key=_idempotency_key(request),
        )
        return 201, {"item": item}
    if rest == ["events"] and method == "GET":
        _validate_query(query, {"work_id", "event_type", "limit", "cursor"})
        return 200, store.list_all_events(
            conn,
            work_id=query.get("work_id"),
            event_type=query.get("event_type"),
            limit=_bounded_limit(query.get("limit", 50)),
            cursor=_bounded_cursor(query.get("cursor")),
        )
    if rest == ["queue", "burn"] and method == "GET":
        _validate_query(query, {"limit", "cursor"})
        return 200, store.burn_queue(
            conn,
            limit=_bounded_limit(query.get("limit", store.BURN_PAGE_DEFAULT)),
            cursor=_bounded_cursor(query.get("cursor")),
        )
    if rest == ["imports"] and method == "POST":
        return _import_batch(conn, request)
    if len(rest) >= 2 and rest[0] == "items":
        return _item_route(conn, request, method, rest[1], rest[2:], query)
    return _error("not-found")


def _item_route(
    conn: sqlite3.Connection,
    request: Request,
    method: str,
    work_id: str,
    tail: list[str],
    query: Mapping[str, str],
) -> tuple[int, dict[str, Any]]:
    if not tail and method == "GET":
        _validate_query(query, {"links_limit", "links_cursor"})
        item = store.get_item(conn, work_id)
        links = store.list_links_page(
            conn,
            work_id,
            limit=_bounded_limit(query.get("links_limit", store.LINK_PAGE_DEFAULT)),
            cursor=_bounded_cursor(query.get("links_cursor")),
        )
        return 200, {
            "item": item,
            "links": links["links"],
            "links_next_cursor": links["next_cursor"],
            "recent_events": store.recent_events(conn, work_id),
        }
    if not tail and method == "PATCH":
        _require_operator(request)
        item = store.patch_item(
            conn,
            work_id,
            dict(request.body),
            expected_version=_if_match(request),
            actor_id=_actor_id(request),
            actor_type=_actor_type(request),
            node_id=_event_node_id(request),
        )
        return 200, {"item": item}
    if tail == ["events"] and method == "GET":
        _validate_query(query, {"limit", "cursor"})
        return 200, store.list_events_page(
            conn,
            work_id,
            limit=_bounded_limit(query.get("limit", 50)),
            cursor=_bounded_cursor(query.get("cursor")),
        )
    if tail == ["transitions"] and method == "POST":
        _require_operator(request)
        body = _object_body(request.body, allowed={"to_status"})
        item = store.transition(
            conn,
            work_id,
            to_status=str(body.get("to_status") or ""),
            expected_version=_if_match(request),
            actor_id=_actor_id(request),
            actor_type=_actor_type(request),
            node_id=_event_node_id(request),
        )
        return 200, {"item": item}
    if tail == ["attempts"] and method == "POST":
        return _record_attempt(conn, request, work_id)
    if tail == ["links"] and method == "POST":
        _require_operator(request)
        added = store.add_link(
            conn,
            work_id,
            dict(request.body),
            actor_id=_actor_id(request),
            actor_type=_actor_type(request),
            node_id=_event_node_id(request),
        )
        return 201, {"link": added}
    if len(tail) == 2 and tail[0] == "links" and method == "DELETE":
        _require_operator(request)
        expected_version = _optional_if_match(request)
        store.delete_link(
            conn,
            work_id,
            tail[1],
            expected_version=expected_version,
            actor_id=_actor_id(request),
            actor_type=_actor_type(request),
            node_id=_event_node_id(request),
        )
        return 200, {"ok": True}
    if tail == ["execution"] and method == "POST":
        return _link_execution(conn, request, work_id)
    return _error("not-found")


def _record_attempt(
    conn: sqlite3.Connection,
    request: Request,
    work_id: str,
) -> tuple[int, dict[str, Any]]:
    body = _object_body(request.body, allowed={"action", "run_id"})
    action = body.get("action")
    if action == "reset" and not request.is_admin and not _is_operator(request):
        return _error("forbidden")
    expected_version = _if_match(request)
    run_id = body.get("run_id")
    item = store.record_attempt(
        conn,
        work_id,
        action=str(action or ""),
        expected_version=expected_version,
        actor_id=_actor_id(request),
        actor_type=_actor_type(request),
        node_id=None if request.is_admin else request.node_id,
        run_id=run_id,
    )
    return 200, {"item": item}


def _import_batch(conn: sqlite3.Connection, request: Request) -> tuple[int, dict[str, Any]]:
    # Importing writes work items and claims an adapter namespace, so it needs a node the
    # operator configured for Worklore, not merely a node enrolled in the fleet.
    _require_operator(request)
    body = _object_body(
        request.body,
        allowed={"adapter_id", "source_type", "idempotency_key", "observations"},
    )
    owner = request.node_id or "admin"
    return 200, store.import_batch(
        conn,
        adapter_id=str(body.get("adapter_id") or ""),
        source_type=str(body.get("source_type") or ""),
        idempotency_key=str(body.get("idempotency_key") or ""),
        observations=body.get("observations"),
        actor_id=_actor_id(request),
        owner_node=owner,
    )


def _link_execution(
    conn: sqlite3.Connection,
    request: Request,
    work_id: str,
) -> tuple[int, dict[str, Any]]:
    body = _object_body(request.body, allowed={"node_id", "run_id"})
    node_id = str(body.get("node_id") or "")
    run_id = str(body.get("run_id") or "")
    if not request.is_admin and node_id != request.node_id:
        return _error("execution-mismatch", "node may link only its own run")
    link = store.link_execution(
        conn,
        work_id,
        node_id=node_id,
        run_id=run_id,
        actor_id=_actor_id(request),
        actor_type=_actor_type(request),
        is_admin=request.is_admin,
    )
    return 201, {"link": link}


def _object_body(raw: object, *, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WorkloreValidationError("body must be an object", code="field-bound")
    unknown = [key for key in raw if key not in allowed]
    if unknown:
        raise WorkloreValidationError(f"unknown field: {unknown[0]}", code="unknown-field")
    return dict(raw)


def _bounded_limit(value: object) -> object:
    """Pass ``limit`` through untouched once its digit count is bounded; the store
    still owns the 1-100 range check and its message."""
    if isinstance(value, str) and len(value) > _MAX_NUMBER_DIGITS:
        raise WorkloreValidationError("limit must be an integer from 1 to 100", code="field-bound")
    return value


def _bounded_cursor(value: str | None) -> str | None:
    """Refuse an oversized cursor before it is decoded, so no wide integer can be
    smuggled into the page sequence."""
    if value is not None and len(value) > _MAX_CURSOR_CHARS:
        raise WorkloreValidationError("cursor is invalid", code="field-bound")
    return value


def _validate_query(query: Mapping[str, str], allowed: set[str]) -> None:
    unknown = next((key for key in query if key not in allowed), None)
    if unknown is not None:
        raise WorkloreValidationError(f"unknown field: {unknown}", code="unknown-field")


def _require_operator(request: Request) -> None:
    if request.is_admin or _is_operator(request):
        return
    raise store.WorkloreForbidden("operator token required", code="forbidden")


def _is_operator(request: Request) -> bool:
    return request.is_operator


def _snapshot_operator_authorization(request: Request) -> Request:
    """Read operator configuration once so every authorization use agrees for this request."""
    if request.operator_authorization_resolved:
        return request
    if request.is_admin or not request.node_id:
        return replace(request, operator_authorization_resolved=True)
    return replace(
        request,
        is_operator=request.node_id in operator_nodes(),
        operator_authorization_resolved=True,
    )


def _event_node_id(request: Request) -> str | None:
    return None if request.is_admin else request.node_id


def _idempotency_key(request: Request) -> str | None:
    raw = _header(request, "Idempotency-Key")
    if raw is None or raw == "":
        return None
    return raw


def _if_match(request: Request) -> int:
    raw = _header(request, "If-Match")
    if raw is None or raw == "":
        raise WorkloreValidationError("If-Match is required", code="if-match-required")
    text = str(raw).strip()
    if not text.isdigit() or len(text) > _MAX_NUMBER_DIGITS:
        raise WorkloreValidationError("If-Match is required", code="if-match-required")
    return int(text)


def _optional_if_match(request: Request) -> int | None:
    raw = _header(request, "If-Match")
    return None if raw is None or raw == "" else _if_match(request)


def _header(request: Request, name: str) -> str | None:
    wanted = name.lower()
    for key, value in request.headers.items():
        if str(key).lower() == wanted:
            return None if value is None else str(value)
    return None


def _actor_id(request: Request) -> str:
    return "admin" if request.is_admin else str(request.node_id)


def _actor_type(request: Request) -> str:
    return "operator" if request.is_admin or _is_operator(request) else "node"


def _error(code: str, message: str | None = None) -> tuple[int, dict[str, Any]]:
    return _STATUS_BY_CODE[code], {"error": message or code.replace("-", " "), "code": code}


def _mapped_error(exc: Exception) -> tuple[int, dict[str, Any]]:
    code = exc.code  # type: ignore[attr-defined]
    if code not in _STATUS_BY_CODE:
        return 400, {"error": "request failed", "code": "field-bound"}
    return _STATUS_BY_CODE[code], {"error": str(exc), "code": code}
