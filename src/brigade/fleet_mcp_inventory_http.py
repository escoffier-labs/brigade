"""HTTP boundary for the MCP servers page, kept outside the Hub's routing module."""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from . import fleet_hub, fleet_mcp_inventory as inventory, fleet_mcp_inventory_page as page

PLAIN = "text/plain; charset=utf-8"
MAX_REPORT_BODY = 512 * 1024


def serve(handler: Any, db_path: Path, query: str) -> None:
    authorized, _editable = handler._roster_auth()
    if not authorized:
        handler._send_html(
            401, "Unauthorized: use brigade fleet enroll for read-only dashboard access.\n", content_type=PLAIN
        )
        return
    params = parse_qs(query, keep_blank_values=False)
    try:
        conn = fleet_hub.open_db(db_path)
        try:
            view = inventory.load_view(conn)
        finally:
            conn.close()
    except (sqlite3.Error, fleet_hub.FleetHubError):
        handler._send_html(503, "MCP servers unavailable.\n", content_type=PLAIN)
        return
    nonce = secrets.token_urlsafe(16)
    body = page.render(
        view,
        nonce=nonce,
        now=datetime.now(timezone.utc),
        group=params.get("group", ["server"])[0],
        filter_key=params.get("filter", ["needs"])[0],
    )
    handler._send_html(200, body, nonce=nonce)


def post_report(handler: Any, db_path: Path, query: str) -> None:
    """Node-authenticated report intake. The admin token cannot post for a node."""
    if query:
        handler._send_json(400, {"error": "invalid MCP report query"})
        return
    try:
        conn = fleet_hub.open_db(db_path)
        try:
            caller = handler._caller(conn)
            if caller is None:
                return
            is_admin, node_id = caller
            if is_admin or node_id is None:
                handler._send_json(403, {"error": "a node token is required to report MCP servers"})
                return
            value = handler._read_json_body(MAX_REPORT_BODY)
            if value is None:
                return
            try:
                result = inventory.store_report(conn, node_id, value)
            except inventory.McpReportError as exc:
                handler._send_json(400, {"error": f"invalid MCP report: {exc}"})
                return
            handler._send_json(200, result)
        finally:
            conn.close()
    except (sqlite3.Error, fleet_hub.FleetHubError):
        handler._send_json(503, {"error": "MCP store unavailable"})
