"""HTTP boundary for the PR rules page, kept outside the Hub's routing module.

Reads and writes go through the policy page's own flow: the same admin-only,
same-origin, CSRF-checked form gate, the same preview and review token, and the
same compare-and-swap save. Nothing here talks to GitHub.
"""

from __future__ import annotations

import hmac
import secrets
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from . import fleet_hub, fleet_hub_policy, fleet_model_inventory
from . import fleet_pr_rules_page as page
from . import fleet_policy_page as policy_page

PLAIN = "text/plain; charset=utf-8"
SAVED_NOTE = "Saved as revision {revision}. Rules are published for agents to read; nothing is enforced on GitHub."


def _load(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], int]:
    current = fleet_hub_policy.current_policy(conn)
    return list(current["document"].get("pr_rules") or []), int(current["revision"])


def _render(
    handler: Any,
    db_path: Path,
    token: str,
    *,
    status: int,
    editable: bool,
    banner: str | None = None,
    error: str | None = None,
    plan: Any = None,
    draft: Any = None,
    review: str | None = None,
) -> None:
    try:
        conn = fleet_hub.open_db(db_path)
        try:
            rules, revision = _load(conn)
        finally:
            conn.close()
    except (sqlite3.Error, fleet_hub.FleetHubError) as exc:
        handler._send_html(500, f"hub database error: {exc}\n", content_type=PLAIN)
        return
    nonce = secrets.token_urlsafe(16)
    body = page.render(
        rules,
        revision=revision,
        nonce=nonce,
        now=datetime.now(timezone.utc),
        csrf=page.csrf_value(token) if editable else "",
        editable=editable,
        banner=banner,
        error=error,
        plan=plan,
        draft=draft,
        review=review,
    )
    handler._send_html(status, body, nonce=nonce)


def serve(handler: Any, db_path: Path, token: str, query: str) -> None:
    authorized, editable = handler._roster_auth()
    if not authorized:
        handler._send_html(
            401, "Unauthorized: use brigade fleet enroll for read-only dashboard access.\n", content_type=PLAIN
        )
        return
    saved = parse_qs(query, keep_blank_values=False).get("saved", [""])[0]
    banner = SAVED_NOTE.format(revision=saved) if saved.isdigit() and len(saved) <= 12 else None
    _render(handler, db_path, token, status=200, editable=editable, banner=banner)


def post_form(
    handler: Any,
    db_path: Path,
    token: str,
    policy_inventory: Callable[[], policy_page.Inventory] | None = None,
) -> None:
    """Same admin-only, same-origin, CSRF-checked gate as the policy page."""
    body = handler._read_form_body(policy_page.MAX_FORM_BYTES, policy_page.FORM_CONTENT_TYPE)
    if body is None:
        return
    try:
        submission = policy_page.parse_form(body)
    except policy_page.FormError as exc:
        handler._send_html(400, f"{exc}\n", content_type=PLAIN)
        return
    if submission.scope != "pr_rules":
        handler._send_html(400, "This form only edits PR rules.\n", content_type=PLAIN)
        return
    expected = page.csrf_value(token)
    if not hmac.compare_digest(submission.csrf.encode("utf-8"), expected.encode("utf-8")):
        handler._send_html(403, "form token mismatch: reload the page\n", content_type=PLAIN)
        return
    try:
        conn = fleet_hub.open_db(db_path)
        try:
            fleet_model_inventory.ensure_schema(conn)
            result = policy_page.apply(
                conn,
                submission,
                actor=policy_page.UPDATED_BY,
                inventory=(
                    policy_inventory() if policy_inventory is not None else policy_page.load_server_inventory(conn)
                ),
                activation=policy_page.probe_activation(conn),
                review_key=policy_page.review_secret(token),
            )
        finally:
            conn.close()
    except (fleet_hub.FleetHubError, sqlite3.Error) as exc:
        handler._send_html(500, f"hub database error: {exc}\n", content_type=PLAIN)
        return
    if result.status == "saved":
        handler._send_html(
            303, "", content_type=PLAIN, extra_headers={"Location": f"{page.FORM_ACTION}?saved={result.revision}"}
        )
        return
    previewed = result.status == "previewed"
    plan = result.plan
    banner = None
    if previewed and plan is not None:
        banner = f"Preview only: nothing was written. Review the change below, then confirm to create revision {plan.next_revision}."
    status = {"previewed": 200, "conflict": 409}.get(result.status, 422)
    _render(
        handler,
        db_path,
        token,
        status=status,
        editable=True,
        banner=banner,
        error=None if previewed else result.message,
        plan=plan,
        draft=submission,
        review=result.review,
    )
