"""Node adapter for the minimal #1940 relay wire contract.

Provider identity is never synthesized from delivery IDs, cursors or SHAs.
Unidentifiable records become durable unknown-scope repair requests before
acknowledgement; an unscopable repository or unordered page is refused.
"""

from __future__ import annotations

import re
from typing import Any

from . import fleet_ci_board as board
from .claude_review_evidence import _head, _timestamp

MAX_PAGE = 100
MAX_CURSOR = 999_999_999_999_999  # Worker's parseCursor accepts at most 15 digits.


def _decimal(value: object, *, maximum: int = (1 << 63) - 1, zero: bool = False) -> int | None:
    if isinstance(value, str):
        if len(value) > 24 or re.fullmatch(r"[0-9]{1,24}", value) is None:
            return None
        value = int(value)
    if type(value) is not int or not (0 if zero else 1) <= value <= maximum:
        return None
    return value


def page_batch(value: Any, *, after: str, limit: int, now: str) -> tuple[dict[str, Any], str]:
    previous = _decimal(after, maximum=MAX_CURSOR, zero=True)
    if not isinstance(value, dict) or previous is None:
        raise ValueError("invalid relay page or cursor")
    records = value.get("events")
    cursor = _decimal(value.get("next_cursor"), maximum=MAX_CURSOR, zero=True)
    if not isinstance(records, list) or len(records) > limit or cursor is None:
        raise ValueError("invalid relay page or cursor")
    events = []
    invalidations = []
    for raw in records:
        if not isinstance(raw, dict):
            raise ValueError("relay record cannot be scoped to a repository")
        item_cursor = _decimal(raw.get("cursor"), maximum=MAX_CURSOR)
        if item_cursor is None or item_cursor <= previous:
            raise ValueError("relay page cursors must increase")
        previous = item_cursor
        # A malformed repository cannot safely be acknowledged as a scoped repair.
        repo = board.repo_name(raw.get("repository"))
        receipt = dict(repo=repo, relay_cursor=item_cursor)
        head = _head(raw.get("head_sha"))
        kind = raw.get("event")
        if not isinstance(kind, str) or kind not in board.EVENTS:
            invalidations.append(dict(receipt, head_sha=head, reason="relay-event-unknown"))
            continue
        identity = _decimal(raw.get("pr_number") if kind == "pull_request" else raw.get("run_id"))
        attempt = _decimal(1 if raw.get("run_attempt") is None else raw["run_attempt"])
        updated = _timestamp(raw.get("provider_updated_at"))
        if identity is None or attempt is None or updated is None or head is None:
            invalidations.append(dict(receipt, head_sha=head, reason="relay-incomplete-fields"))
            continue
        wire = dict(
            **receipt,
            event=kind,
            head_sha=head,
            status=raw.get("status") if isinstance(raw.get("status"), str) else None,
            conclusion=raw.get("conclusion") if isinstance(raw.get("conclusion"), str) else None,
            run_attempt=attempt,
            updated_at=updated,
            created_at=_timestamp(raw.get("provider_created_at")),
            action=raw.get("action"),
            name=None,
            app_id=None,
        )
        wire[board.EVENT_IDS[kind]] = identity
        if kind == "pull_request":
            if not isinstance(raw.get("status"), str) or raw["status"] not in {"open", "closed"}:
                invalidations.append(dict(receipt, head_sha=None, reason="relay-PR-state-unknown"))
                continue
            if raw["status"] == "closed":
                wire["action"] = "closed"
        # Reuse general evidence validators, without managed review assumptions.
        normalized = board.normalize_event(wire, now=now)
        wire["status"] = normalized["status"]
        wire["conclusion"] = normalized["conclusion"]
        wire["action"] = normalized["action"]
        if kind == "status":
            wire["status"] = normalized["conclusion"] if normalized["status"] == "completed" else "pending"
        events.append(wire)
        # #1940 omits check context/app identity. Prior named successes cannot
        # remain authoritative until a node re-observes the complete SHA scope.
        if kind in {"check_run", "check_suite", "status"}:
            invalidations.append(dict(receipt, head_sha=head, reason="relay-check-context-unknown"))
        elif kind == "workflow_run" and (
            normalized["status"] == "unknown"
            or (normalized["status"] == "completed" and normalized["conclusion"] == "unknown")
        ):
            invalidations.append(dict(receipt, head_sha=head, reason="relay-state-unknown"))
    if cursor != previous:
        raise ValueError("relay next_cursor does not match the page")
    return {"events": events, "repositories": [], "invalidations": invalidations}, str(cursor)
