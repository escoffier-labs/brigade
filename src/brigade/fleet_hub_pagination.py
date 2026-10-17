"""SQL-bounded Hub list pages with backwards-compatible collection keys."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, cast

from . import fleet_claim_display, fleet_hub, fleet_hub_claims, fleet_hub_sessions, fleet_hub_status

MAX_PAGE_ITEMS = 200
MAX_PAGE_OFFSET = fleet_hub_status.BOARD_OFFSET_MAX
LIST_PATHS = {"/status": "runs", "/claims": "claims", "/nodes": "nodes", "/sessions": "sessions"}


def page_parameters(params: dict[str, list[str]]) -> tuple[int, int]:
    """Reject over-limit pages and malformed cursors instead of widening bounds."""
    try:
        if any(len(params.get(key, [])) > 1 for key in ("limit", "cursor")):
            raise ValueError()
        limit = int(params.get("limit", [str(MAX_PAGE_ITEMS)])[0])
        cursor = int(params.get("cursor", ["0"])[0])
        if not 1 <= limit <= MAX_PAGE_ITEMS:
            raise ValueError()
        if not 0 <= cursor <= MAX_PAGE_OFFSET:
            raise ValueError()
    except ValueError as exc:
        raise ValueError(f"limit must be 1..{MAX_PAGE_ITEMS} and cursor must be 0..{MAX_PAGE_OFFSET}") from exc
    return limit, cursor


def _claims(conn: Any, *, include_all: bool, limit: int, offset: int) -> list[dict[str, Any]]:
    # Preserve the authority-wide refusal even when the damaged claim is
    # expired or outside this page. This is the existing read-only validator.
    fleet_hub_claims.validate_schema(conn)
    now = fleet_hub._now_epoch()
    where = " WHERE (? OR c.expires_at > ?)"
    args = (int(include_all), now)
    count = conn.execute("SELECT count(*) FROM claims c" + where, args).fetchone()[0]
    columns = ", ".join("c." + name.strip() for name in fleet_hub_claims.CLAIM_COLUMNS.split(","))
    try:
        rows = conn.execute(
            f"SELECT {columns}, c.holder_token, c.lock_token, c.lock_acquired_at, c.lock_run_dir, f.generation "
            "FROM claims c LEFT JOIN claim_fences f ON f.target = c.target"
            + where
            + " ORDER BY c.target LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        fleet_hub_claims._refuse_missing_schema(exc)
        raise
    result = []
    for row in rows:
        fleet_hub_claims._checked_generation(row[15])
        item = fleet_hub._claim_payload(row)
        item["expired"] = row[10] <= now
        result.append(item)
    remaining = limit - len(result)
    if remaining:
        # Reuse the display projection without loading all jobs. The adapter
        # only windows its one metadata SELECT, never a mutation or authority read.
        class GrokPage:
            def execute(self, sql: str, parameters: Any = ()) -> Any:
                if "FROM grokbot_jobs WHERE state IN" in sql:
                    sql = f"SELECT * FROM ({sql}) WHERE (? OR julianday(lease_expires_at) > julianday(?)) LIMIT ? OFFSET ?"
                    parameters = (
                        *parameters,
                        int(include_all),
                        datetime.fromtimestamp(now, timezone.utc).isoformat(),
                        remaining,
                        max(0, offset - count),
                    )
                return conn.execute(sql, parameters)

        result.extend(
            fleet_claim_display._grokbot_rows(
                cast(Any, GrokPage()), include_all=include_all, now=datetime.fromtimestamp(now, timezone.utc)
            )
        )
    return result


def list_page(
    conn: Any,
    path: str,
    *,
    include_all: bool,
    limit: int,
    offset: int,
    stale_history_after_seconds: int,
) -> dict[str, Any]:
    """Read one page plus one lookahead row, preserving legacy array fields."""
    take = limit + 1
    if path == "/nodes":
        rows = conn.execute(
            f"SELECT {fleet_hub._NODE_COLUMNS} FROM nodes ORDER BY node_id LIMIT ? OFFSET ?", (take, offset)
        ).fetchall()
        items = [fleet_hub._node_payload(row) for row in rows]
    elif path == "/claims":
        items = _claims(conn, include_all=include_all, limit=take, offset=offset)
    elif path == "/sessions":
        now = fleet_hub._now_epoch()
        rows = conn.execute(
            f"SELECT {fleet_hub_sessions._SESSION_COLUMNS} FROM interactive_sessions "
            "WHERE (? OR (state = 'active' AND expires_at > ?)) "
            "ORDER BY heartbeat_at DESC, started_at DESC, node_id, harness, session_id LIMIT ? OFFSET ?",
            (int(include_all), now, take, offset),
        ).fetchall()
        items = [fleet_hub_sessions._session_payload(row, now=now) for row in rows]
    else:
        return fleet_hub_status.status_page(
            conn,
            include_all=include_all,
            limit=limit,
            offset=offset,
            stale_history_after_seconds=stale_history_after_seconds,
        )
    more = len(items) > limit
    return {LIST_PATHS[path]: items[:limit], "truncated": more, "next_cursor": str(offset + limit) if more else None}
