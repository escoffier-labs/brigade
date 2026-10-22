"""Local-only CI routes using the existing Deck and bearer authorization."""

from __future__ import annotations

import secrets
import sqlite3
from pathlib import Path
from typing import Any

from . import fleet_ci_board as board, fleet_ci_page as page, fleet_hub


def route(handler: Any, db_path: Path, *, method: str) -> None:
    path, _, query = handler.path.partition("?")
    if query:
        handler._send_json(400, {"error": "invalid CI query"})
        return
    deck_operator = False
    if path == "/deck/ci":
        authorized, deck_operator = handler._roster_auth()
        if not authorized:
            handler._send_html(
                401, "Unauthorized: use brigade fleet enroll.\n", content_type="text/plain; charset=utf-8"
            )
            return
    try:
        conn = fleet_hub.open_db(db_path)
        try:
            if path == "/deck/ci":
                conn.execute("PRAGMA query_only=ON")
                view = board.load_view(conn, operator=deck_operator)
                nonce = secrets.token_urlsafe(16)
                handler._send_html(200, page.render(view, nonce=nonce), nonce=nonce)
                return
            caller = handler._caller(conn)
            if caller is None:
                return
            is_admin, _ = caller
            if method == "GET":
                conn.execute("PRAGMA query_only=ON")
                handler._send_json(
                    200,
                    board.work_plan(conn, operator=is_admin)
                    if path == "/ci/work"
                    else board.load_view(conn, operator=is_admin),
                )
                return
            if path == "/ci/refresh":
                if not is_admin:
                    handler._send_json(403, {"error": "admin bearer required for CI refresh"})
                    return
                value = handler._read_json_body(4096)
                if value is None:
                    return
                if not isinstance(value, dict) or set(value) != {"repo"}:
                    raise ValueError("invalid refresh request")
                board.request_refresh(conn, value["repo"])
                handler._send_json(202, {"queued": True})
                return
            if is_admin:
                handler._send_json(403, {"error": "node token required for CI observations"})
                return
            value = handler._read_json_body(4 * 1024 * 1024)
            if value is not None:
                handler._send_json(200, board.store(conn, value))
        finally:
            conn.close()
    except (ValueError, TypeError, KeyError):
        handler._send_json(400, {"error": "invalid CI observation"})
    except (sqlite3.Error, fleet_hub.FleetHubError):
        handler._send_json(503, {"error": "CI store unavailable"})
