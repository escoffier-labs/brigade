"""Authenticated /work HTTP client. Independent urllib transport; never uses fleet_client._admin_request."""

from __future__ import annotations

import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from .fleet_client import (
    FleetClientError,
    _hub_open,
    _require_encrypted_or_loopback_hub,
    load_fleet_settings,
)

REQUEST_TIMEOUT_SECONDS = 5.0
IMPORT_TIMEOUT_SECONDS = 30.0
LIST_PAGE_SIZE = 100
LIST_MAX_PAGES = 200
LIST_MAX_ITEMS = 20000
# A full listing page is a few hundred KiB; the cap stops a hostile or wedged hub
# from streaming an unbounded body into memory. Refusal bodies are far smaller.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_ERROR_RESPONSE_BYTES = 64 * 1024
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9-]{0,127}$")

__all__ = [
    "FleetClientError",
    "WorkloreListingError",
    "IMPORT_TIMEOUT_SECONDS",
    "REQUEST_TIMEOUT_SECONDS",
    "WorkloreClientError",
    "add_link",
    "burn_queue",
    "create_item",
    "delete_link",
    "get_item",
    "import_batch",
    "link_execution",
    "list_item_links_all",
    "list_all_events",
    "list_events",
    "LIST_MAX_ITEMS",
    "LIST_MAX_PAGES",
    "LIST_PAGE_SIZE",
    "MAX_ERROR_RESPONSE_BYTES",
    "MAX_RESPONSE_BYTES",
    "list_items",
    "list_items_all",
    "load_fleet_settings",
    "patch_item",
    "record_attempt",
    "transition",
]


class WorkloreListingError(FleetClientError):
    """A paged Worklore listing exceeded its bounds or failed to advance."""


class WorkloreClientError(FleetClientError):
    """A Worklore hub refusal with its stable machine-readable code, when supplied."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def _safe_terminal_text(value: object) -> str:
    """Return displayable plain text without C0 or C1 terminal controls."""
    text = str(value)
    without_controls = "".join(" " if ord(char) < 32 or 127 <= ord(char) <= 159 else char for char in text)
    return " ".join(without_controls.split())


def _error_code(value: object) -> str | None:
    return value if isinstance(value, str) and _ERROR_CODE_RE.fullmatch(value) else None


def _hub_url() -> str:
    """The configured hub, refused before a bearer token is attached unless it is
    HTTPS or loopback HTTP (the same gate the rest of the fleet client uses)."""
    hub = load_fleet_settings().get("hub_url", "")
    if not hub:
        raise FleetClientError("no fleet hub configured (~/.brigade/fleet.toml [fleet] hub_url)")
    hub = hub.rstrip("/")
    _require_encrypted_or_loopback_hub(hub)
    return hub


def _token(*, admin: bool | None = None) -> str:
    settings = load_fleet_settings()
    if admin is True:
        token = settings.get("admin_token", "")
        if not token:
            raise FleetClientError(
                "no fleet admin token configured (~/.brigade/fleet.toml [fleet] token_file or BRIGADE_FLEET_TOKEN)"
            )
        return token
    if admin is False:
        token = settings.get("node_token", "")
        if not token:
            raise FleetClientError(
                "no fleet node token configured (~/.brigade/fleet.toml [fleet] node_token_file or BRIGADE_FLEET_NODE_TOKEN)"
            )
        return token
    # Reads carry the least authority that works: the node token, with the admin
    # token used only when nothing else is configured. Callers that need admin
    # authority ask for it explicitly with ``admin=True``.
    token = settings.get("node_token", "") or settings.get("admin_token", "")
    if not token:
        raise FleetClientError("no fleet token configured")
    return token


def _read_bounded(response: Any, *, limit: int) -> bytes:
    """Read at most ``limit`` bytes, refusing a body that runs past the cap."""
    reader = getattr(response, "read", None)
    if reader is None:
        raise FleetClientError("fleet hub work failed: response could not be read")
    try:
        raw = reader(limit + 1)
    except TypeError as exc:
        raise FleetClientError("fleet hub work failed: response does not support bounded reads") from exc
    if not isinstance(raw, (bytes, bytearray)):
        raise FleetClientError("fleet hub work failed: response was not bytes")
    if len(raw) > limit:
        raise FleetClientError(f"fleet hub work failed: response exceeded {limit} bytes")
    return bytes(raw)


def _http_error(exc: urllib.error.HTTPError) -> FleetClientError:
    try:
        refusal = json.loads(_read_bounded(exc, limit=MAX_ERROR_RESPONSE_BYTES).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, FleetClientError):
        refusal = {}
    if isinstance(refusal, dict):
        code = _error_code(refusal.get("code"))
        if code == "hub-unavailable":
            return WorkloreClientError("hub-unavailable", code=code)
        detail = refusal.get("error")
        if isinstance(detail, str) and detail:
            return WorkloreClientError(
                f"fleet hub work failed: HTTP {exc.code}: {_safe_terminal_text(detail)}",
                code=code,
            )
        return WorkloreClientError(f"fleet hub work failed: HTTP {exc.code}", code=code)
    return WorkloreClientError(f"fleet hub work failed: HTTP {exc.code}")


def _new_idempotency_key() -> str:
    return secrets.token_hex(16)


def _request(
    method: str,
    path: str,
    *,
    token: str,
    body: Mapping[str, Any] | None = None,
    if_match: object = None,
    query: Mapping[str, str] | None = None,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    url = _hub_url() + path
    if query:
        filtered = {key: value for key, value in query.items() if value}
        if filtered:
            url = f"{url}?{urllib.parse.urlencode(filtered)}"
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(dict(body)).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if if_match is not None and str(if_match) != "":
        headers["If-Match"] = str(if_match)
    if idempotency_key is not None and str(idempotency_key) != "":
        headers["Idempotency-Key"] = str(idempotency_key)
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _hub_open(request, timeout=timeout) as response:
            raw = _read_bounded(response, limit=MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        raise _http_error(exc) from exc
    except (OSError, TimeoutError) as exc:
        raise FleetClientError("hub-unavailable") from exc
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FleetClientError("fleet hub work failed: invalid JSON") from exc
    return payload if isinstance(payload, dict) else {}


def _item_path(work_id: str, *parts: str) -> str:
    segments = ["/work/items", urllib.parse.quote(work_id, safe="")]
    segments.extend(urllib.parse.quote(part, safe="") for part in parts)
    return "/".join(segments)


def create_item(body: Mapping[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
    key = idempotency_key if idempotency_key else _new_idempotency_key()
    return _request(
        "POST",
        "/work/items",
        token=_token(admin=False),
        body=body,
        idempotency_key=key,
    )


def get_item(
    work_id: str,
    *,
    links_limit: int | None = None,
    links_cursor: str | None = None,
) -> dict[str, Any]:
    query: dict[str, str] = {}
    if links_limit is not None:
        query["links_limit"] = str(links_limit)
    if links_cursor:
        query["links_cursor"] = links_cursor
    return _request("GET", _item_path(work_id), token=_token(), query=query or None)


def list_items(
    source: str | None = None,
    *,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Return one page of Worklore items. Callers that need every page use ``list_items_all``."""
    query: dict[str, str] = {}
    if source:
        query["source"] = source
    if limit is not None:
        query["limit"] = str(limit)
    if cursor:
        query["cursor"] = cursor
    return _request("GET", "/work/items", token=_token(), query=query or None)


def list_items_all(
    source: str | None = None,
    *,
    page_size: int = LIST_PAGE_SIZE,
    max_pages: int = LIST_MAX_PAGES,
    max_items: int = LIST_MAX_ITEMS,
) -> list[Any]:
    """Follow ``next_cursor`` to the end of a Worklore listing under explicit bounds.

    Raises ``WorkloreListingError`` when the hub repeats a cursor (a cycle), keeps handing out
    cursors past ``max_pages``, or returns more than ``max_items`` rows, so a misbehaving hub
    cannot spin the adapter forever.
    """
    items: list[Any] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for _ in range(max_pages):
        payload = list_items(source, limit=page_size, cursor=cursor)
        page = payload.get("items")
        if not isinstance(page, list):
            raise WorkloreListingError("fleet hub work failed: items must be an array")
        items.extend(page)
        if len(items) > max_items:
            raise WorkloreListingError(f"fleet hub work failed: listing exceeded {max_items} items")
        next_cursor = payload.get("next_cursor")
        if not next_cursor:
            return items
        if not isinstance(next_cursor, str):
            raise WorkloreListingError("fleet hub work failed: next_cursor must be a string")
        if next_cursor in seen_cursors:
            raise WorkloreListingError("fleet hub work failed: listing cursor did not advance")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise WorkloreListingError(f"fleet hub work failed: listing exceeded {max_pages} pages")


def list_item_links_all(
    work_id: str,
    *,
    page_size: int = LIST_PAGE_SIZE,
    max_pages: int = LIST_MAX_PAGES,
    max_items: int = LIST_MAX_ITEMS,
) -> list[Any]:
    """Follow one item's paged links to completion under the listing bounds."""
    links: list[Any] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for _ in range(max_pages):
        payload = get_item(work_id, links_limit=page_size, links_cursor=cursor)
        page = payload.get("links")
        if not isinstance(page, list):
            raise WorkloreListingError("fleet hub work failed: links must be an array")
        links.extend(page)
        if len(links) > max_items:
            raise WorkloreListingError(f"fleet hub work failed: link listing exceeded {max_items} items")
        next_cursor = payload.get("links_next_cursor")
        if next_cursor is None:
            return links
        if not isinstance(next_cursor, str) or not next_cursor:
            raise WorkloreListingError("fleet hub work failed: links_next_cursor must be a non-empty string")
        if next_cursor in seen_cursors:
            raise WorkloreListingError("fleet hub work failed: link listing cursor did not advance")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise WorkloreListingError(f"fleet hub work failed: link listing exceeded {max_pages} pages")


def list_events(work_id: str) -> dict[str, Any]:
    return _request("GET", _item_path(work_id, "events"), token=_token())


def list_all_events() -> dict[str, Any]:
    return _request("GET", "/work/events", token=_token())


def burn_queue(*, limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
    """Return one page of the burn queue. The hub bounds the page; follow ``next_cursor`` for more."""
    query: dict[str, str] = {}
    if limit is not None:
        query["limit"] = str(limit)
    if cursor:
        query["cursor"] = cursor
    return _request("GET", "/work/queue/burn", token=_token(), query=query or None)


def patch_item(work_id: str, body: Mapping[str, Any], *, if_match: object = None) -> dict[str, Any]:
    return _request(
        "PATCH",
        _item_path(work_id),
        token=_token(admin=False),
        body=body,
        if_match=if_match,
    )


def transition(work_id: str, body: Mapping[str, Any], *, if_match: object = None) -> dict[str, Any]:
    return _request(
        "POST",
        _item_path(work_id, "transitions"),
        token=_token(admin=False),
        body=body,
        if_match=if_match,
    )


def record_attempt(work_id: str, body: Mapping[str, Any], *, if_match: object = None) -> dict[str, Any]:
    return _request(
        "POST",
        _item_path(work_id, "attempts"),
        token=_token(admin=False),
        body=body,
        if_match=if_match,
    )


def add_link(work_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    return _request("POST", _item_path(work_id, "links"), token=_token(admin=False), body=body)


def delete_link(work_id: str, link_id: str, *, if_match: object = None) -> dict[str, Any]:
    return _request(
        "DELETE",
        _item_path(work_id, "links", link_id),
        token=_token(admin=False),
        if_match=if_match,
    )


def import_batch(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _request(
        "POST",
        "/work/imports",
        token=_token(admin=False),
        body=payload,
        timeout=IMPORT_TIMEOUT_SECONDS,
    )


def link_execution(work_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    return _request(
        "POST",
        _item_path(work_id, "execution"),
        token=_token(admin=False),
        body=body,
    )
