"""HTTP boundary for the skills review, kept outside the Hub's routing module."""

from __future__ import annotations

import hmac
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from . import fleet_hub, fleet_skills_page as page, fleet_skills_review as review

PLAIN = "text/plain; charset=utf-8"
MAX_REPORT_BODY = 2 * 1024 * 1024
MAX_FIELDS = 1200


def serve(handler: Any, db_path: Path, token: str, path: str, query: str) -> None:
    authorized, editable = handler._roster_auth()
    if not authorized:
        handler._send_html(
            401, "Unauthorized: use brigade fleet enroll for read-only dashboard access.\n", content_type=PLAIN
        )
        return
    params = parse_qs(query, keep_blank_values=False)
    filter_key = params.get("filter", ["needs"])[0]
    applied = params.get("applied", [""])[0]
    banner = None
    if applied.isdigit() and len(applied) <= 6:
        banner = f"Recorded {applied} reviewed change{'' if applied == '1' else 's'} as applied. The Hub ran nothing."
    try:
        conn = fleet_hub.open_db(db_path)
        try:
            view = review.load_view(conn)
            draft = review.load_draft(conn)
        finally:
            conn.close()
    except (sqlite3.Error, fleet_hub.FleetHubError):
        handler._send_html(503, "Skills review unavailable.\n", content_type=PLAIN)
        return
    nonce = secrets.token_urlsafe(16)
    csrf = page.csrf_value(token) if editable else ""
    now = datetime.now(timezone.utc)
    if path == "/deck/skills/plan":
        body = page.render_plan(view, draft, nonce=nonce, now=now, csrf=csrf, editable=editable, banner=banner)
    else:
        body = page.render(
            view, draft, nonce=nonce, now=now, csrf=csrf, editable=editable, filter_key=filter_key, banner=banner
        )
    handler._send_html(200, body, nonce=nonce)


def post_form(handler: Any, db_path: Path, token: str) -> None:
    """Same admin-only, same-origin, CSRF-checked gate as the policy page."""
    body = handler._read_form_body(page.MAX_FORM_BYTES, page.FORM_CONTENT_TYPE)
    if body is None:
        return
    try:
        fields = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=False, max_num_fields=MAX_FIELDS)
    except (UnicodeDecodeError, ValueError):
        handler._send_html(400, "Malformed form body.\n", content_type=PLAIN)
        return

    def one(name: str) -> str:
        return fields.get(name, [""])[0]

    expected = page.csrf_value(token)
    if not hmac.compare_digest(one("csrf").encode("utf-8"), expected.encode("utf-8")):
        handler._send_html(403, "form token mismatch: reload the page\n", content_type=PLAIN)
        return
    action = one("action")
    if action not in {"save", "discard", "applied"}:
        handler._send_html(400, "Unknown action.\n", content_type=PLAIN)
        return
    try:
        conn = fleet_hub.open_db(db_path)
        try:
            view = review.load_view(conn)
            if action == "save":
                overrides = {name[3:]: values[0] for name, values in fields.items() if name.startswith("ov:")}
                try:
                    review.save_decision(conn, view, one("skill"), one("decision"), overrides)
                except review.DecisionError as exc:
                    handler._send_html(422, f"{exc}\n", content_type=PLAIN)
                    return
                location = page.redirect_target(one("filter"), one("skill"))
            elif action == "discard":
                review.discard_draft(conn)
                location = "/deck/skills"
            else:
                plan = review.build_plan(view, review.load_draft(conn))
                review.record_applied(conn, plan)
                location = f"/deck/skills?applied={plan.count}"
        finally:
            conn.close()
    except (sqlite3.Error, fleet_hub.FleetHubError):
        handler._send_html(503, "Skills review unavailable.\n", content_type=PLAIN)
        return
    handler._send_html(303, "", content_type=PLAIN, extra_headers={"Location": location})


def post_report(handler: Any, db_path: Path, query: str) -> None:
    """Node-authenticated report intake. The admin token cannot post for a node."""
    if query:
        handler._send_json(400, {"error": "invalid skills report query"})
        return
    try:
        conn = fleet_hub.open_db(db_path)
        try:
            caller = handler._caller(conn)
            if caller is None:
                return
            is_admin, node_id = caller
            if is_admin or node_id is None:
                handler._send_json(403, {"error": "a node token is required to report skills"})
                return
            value = handler._read_json_body(MAX_REPORT_BODY)
            if value is None:
                return
            try:
                result = review.store_report(conn, node_id, value)
            except review.SkillsReportError as exc:
                handler._send_json(400, {"error": f"invalid skills report: {exc}"})
                return
            handler._send_json(200, result)
        finally:
            conn.close()
    except (sqlite3.Error, fleet_hub.FleetHubError):
        handler._send_json(503, {"error": "skills store unavailable"})
