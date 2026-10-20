"""HTTP boundary for stats, kept outside the Hub's routing module."""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import fleet_admin_stats as stats, fleet_admin_stats_page as page, fleet_hub


def serve(handler: Any, db_path: Path, query: str) -> None:
    if not handler._roster_auth()[0]:
        handler._send_html(401, "Unauthorized: use brigade fleet enroll.\n", content_type="text/plain; charset=utf-8")
        return
    if query:
        handler._send_html(400, "Invalid stats page query.\n", content_type="text/plain; charset=utf-8")
        return
    try:
        conn = fleet_hub.open_db(db_path)
        try:
            conn.execute("PRAGMA query_only=ON")
            view = stats.load_view(conn)
        finally:
            conn.close()
        nonce = secrets.token_urlsafe(16)
        handler._send_html(200, page.render(view, nonce=nonce, now=datetime.now(timezone.utc)), nonce=nonce)
    except (sqlite3.Error, fleet_hub.FleetHubError, ValueError):
        handler._send_html(503, "Stats unavailable.\n", content_type="text/plain; charset=utf-8")


def post(handler: Any, db_path: Path, query: str) -> None:
    if query:
        handler._send_json(400, {"error": "invalid stats query"})
        return
    try:
        conn = fleet_hub.open_db(db_path)
        try:
            caller = handler._caller(conn)
            if caller is None:
                return
            collectors = stats.config().get("collector_nodes", [])
            if caller[0] or not isinstance(collectors, list) or caller[1] not in collectors:
                handler._send_json(403, {"error": "stats collector node required"})
                return
            value = handler._read_json_body(4 * 1024 * 1024)
            if value is None:
                return
            stats.store(conn, value)
            handler._send_json(200, {"snapshot_id": value["snapshot_id"]})
        finally:
            conn.close()
    except (ValueError, TypeError, KeyError):
        handler._send_json(400, {"error": "invalid aggregate snapshot"})
    except (sqlite3.Error, fleet_hub.FleetHubError):
        handler._send_json(503, {"error": "stats store unavailable"})
