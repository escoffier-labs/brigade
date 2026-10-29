"""Stateless HTTP boundary for the admin audit reader."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from .fleet_hub import FleetHubError


def serve_get(handler: Any, query: str, db_path: Path, *, open_db: Callable[[Path], sqlite3.Connection]) -> None:
    if not handler._authorized():
        handler._send_json(401 if handler._bearer() is None else 403, {"error": "admin token required"})
        return
    from . import fleet_hub_audit

    try:
        params = parse_qs(query, keep_blank_values=True)
        if set(params) - {"after_id", "limit"} or any(len(v) != 1 for v in params.values()):
            raise ValueError("invalid audit query")
        after_id = int(params.get("after_id", ["0"])[0])
        limit = int(params.get("limit", ["100"])[0])
        conn = open_db(Path(db_path))
        try:
            rows = fleet_hub_audit.page(conn, after_id=after_id, limit=limit)
        finally:
            conn.close()
        handler._send_json(200, {"audit": rows})
    except ValueError as exc:
        handler._send_json(400, {"error": str(exc)})
    except (FleetHubError, sqlite3.Error):
        handler._send_json(500, {"error": "hub database error"})
