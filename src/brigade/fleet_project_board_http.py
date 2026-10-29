"""HTTP boundary for Projects board intake, kept outside the Hub's routing module."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from . import fleet_hub, fleet_project_board as board


def post_snapshot(handler: Any, db_path: Path, query: str) -> None:
    """Node-authenticated intake. The admin token cannot post for a node."""
    if query:
        handler._send_json(400, {"error": "invalid project board query"})
        return
    try:
        conn = fleet_hub.open_db(db_path)
        try:
            caller = handler._caller(conn)
            if caller is None:
                return
            is_admin, node_id = caller
            if is_admin or node_id is None:
                handler._send_json(403, {"error": "a node token is required to report a project board"})
                return
            value = handler._read_json_body(board.MAX_BODY_BYTES)
            if value is None:
                return
            try:
                result = board.store_snapshot(conn, value)
            except board.BoardError as exc:
                handler._send_json(400, {"error": f"invalid project board: {exc}"})
                return
            handler._send_json(200, result)
        finally:
            conn.close()
    except (sqlite3.Error, fleet_hub.FleetHubError):
        handler._send_json(503, {"error": "project board store unavailable"})
