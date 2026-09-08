"""Read-only claim rows for Fleet Hub display surfaces.

The core claim list remains the routing occupancy source.  This projection
adds active Grok Bot work only for human-facing claims and dashboard boards.
It deliberately reads a narrow metadata column set and never expires or
otherwise mutates jobs.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import fleet_hub

_ACTIVE_GROKBOT_STATES = ("claimed", "running")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _display_text(value: object, *, maximum: int, fallback: str) -> str:
    """Return bounded metadata text without controls, or a public fallback."""
    if not isinstance(value, str):
        return fallback
    text = value.strip()
    if not text or len(text) > maximum or any(ord(character) < 32 or ord(character) == 127 for character in text):
        return fallback
    return text


def _timestamp_text(value: object) -> str:
    return value if isinstance(value, str) and _parse_timestamp(value) is not None else ""


def _grokbot_rows(
    conn: sqlite3.Connection,
    *,
    include_all: bool,
    now: datetime,
) -> list[dict[str, Any]]:
    """Project active Grok Bot leases without touching their lifecycle state."""
    table = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'grokbot_jobs'").fetchone()
    if table is None:
        return []
    rows = conn.execute(
        "SELECT job_id, role, repository, label, state, claimed_at, lease_expires_at, "
        "owner_node, claimant_node, claimant_worker "
        "FROM grokbot_jobs WHERE state IN (?, ?) ORDER BY repository, label, job_id",
        _ACTIVE_GROKBOT_STATES,
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        (
            raw_job_id,
            raw_role,
            raw_repository,
            raw_label,
            _state,
            claimed_at,
            lease_expires_at,
            raw_owner_node,
            raw_claimant_node,
            raw_claimant_worker,
        ) = row
        job_id = _display_text(raw_job_id, maximum=128, fallback="unknown-job")
        label = _display_text(raw_label, maximum=160, fallback=job_id)
        repository = _display_text(raw_repository, maximum=200, fallback="unknown-repository")
        expires = _parse_timestamp(lease_expires_at)
        # A lease with no parseable deadline is never presumed live.  It is
        # inspectable only with include_all, alongside conventionally expired rows.
        expired = expires is None or expires <= now
        if expired and not include_all:
            continue
        target_label = job_id if label == job_id else f"{label} [{job_id}]"
        claimant_node = _display_text(raw_claimant_node, maximum=128, fallback="")
        owner_node = claimant_node or _display_text(raw_owner_node, maximum=128, fallback="")
        claimant_worker = _display_text(raw_claimant_worker, maximum=128, fallback="")
        result.append(
            {
                # The job id makes repeated repository/label pairs distinct on
                # the repository board as well as in the JSON claim list.
                "target": f"{repository} · {target_label}",
                "owner_node": owner_node,
                "owner_conductor": claimant_worker or "grokbot",
                "harness": "grokbot",
                "role": _display_text(raw_role, maximum=128, fallback=""),
                "job": job_id,
                "acquired_at": _timestamp_text(claimed_at),
                "expires_at": _timestamp_text(lease_expires_at),
                "expired": expired,
            }
        )
    return result


def list_display_claims(
    conn: sqlite3.Connection,
    *,
    include_all: bool = False,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return ordinary claims plus active Grok Bot lease display rows.

    This is intentionally separate from :func:`fleet_hub.list_claims`, whose
    narrower result remains the source of truth for occupancy and routing.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    ordinary = fleet_hub.list_claims(conn, include_all=include_all)
    return ordinary + _grokbot_rows(conn, include_all=include_all, now=current)
