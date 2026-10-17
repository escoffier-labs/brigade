"""Collect Hub list pages without following server-supplied URLs."""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import urlencode
import urllib.request

MAX_LIST_PAGES = 10000


def collect_pages(read: Callable[[str], Any], path: str, key: str) -> list[dict[str, Any]]:
    """Accept legacy single-page replies, and reject repeated/missing cursors."""
    from .fleet_client import FleetClientError

    result: list[dict[str, Any]] = []
    cursor = -1
    current_path = path
    for _ in range(MAX_LIST_PAGES):
        payload = read(current_path)
        if not isinstance(payload, dict):
            return result
        items = payload.get(key)
        if isinstance(items, list):
            result.extend(items)
        next_cursor = payload.get("next_cursor")
        if next_cursor is None:
            if payload.get("truncated"):
                raise FleetClientError(f"fleet hub {key} response was truncated without a cursor")
            return result
        if not isinstance(next_cursor, str) or not next_cursor.isascii() or not next_cursor.isdecimal():
            raise FleetClientError(f"fleet hub {key} returned an invalid cursor")
        try:
            position = int(next_cursor)
        except ValueError as exc:
            raise FleetClientError(f"fleet hub {key} returned an invalid cursor") from exc
        if position <= cursor:
            raise FleetClientError(f"fleet hub {key} returned a repeated cursor")
        cursor = position
        current_path = path + ("&" if "?" in path else "?") + urlencode({"cursor": next_cursor})
    raise FleetClientError(f"fleet hub {key} exceeded the page limit")


def fetch_list(hub: str, token: str, path: str, key: str, *, response_limit: int | None = None) -> list[dict[str, Any]]:
    """Read same-origin authenticated pages using the client's approved opener."""
    from . import fleet_client as client

    def read(page_path: str) -> Any:
        request = urllib.request.Request(hub.rstrip("/") + page_path, headers={"Authorization": f"Bearer {token}"})
        try:
            with client._hub_open(request, timeout=client.REPORT_TIMEOUT_SECONDS) as response:
                raw = (
                    response.read()
                    if response_limit is None
                    else client._read_bounded_response(response, limit=response_limit)
                )
                return json.loads(raw.decode("utf-8"))
        except client.FleetClientError:
            raise
        except Exception as exc:
            raise client.FleetClientError(f"fleet hub {key} failed: {type(exc).__name__}") from exc

    return collect_pages(read, path, key)
