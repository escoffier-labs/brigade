"""Fleet hub storage for the one-row run preference pin (#1223).

Split from ``fleet_hub`` to keep that module under the size ratchet. The
preference carries seat *names* only; ``run_preference.parse_preference``
rejects secret or path material before anything reaches the table.
"""

import sqlite3
from datetime import datetime, timezone
from typing import Any

# v13 (#1223): one-row fleet run preference. Seat names only; never secrets.
_RUN_PREFERENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_preference (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    impl TEXT,
    review TEXT,
    chef TEXT,
    notes TEXT,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the run_preference table (v12 -> v13 additive migration)."""
    conn.execute(_RUN_PREFERENCE_SCHEMA)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_run_preference(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return the one-row fleet run preference, or empty fields when unset."""
    row = conn.execute("SELECT impl, review, chef, notes FROM run_preference WHERE id = 1").fetchone()
    if row is None:
        return {"impl": None, "review": None, "chef": None, "notes": None}
    return {"impl": row[0], "review": row[1], "chef": row[2], "notes": row[3]}


def set_run_preference(conn: sqlite3.Connection, raw: Any, *, updated_by: str | None = None) -> dict[str, Any]:
    """Replace the fleet run preference. Rejects secret or path material."""
    from . import run_preference
    from .fleet_hub import FleetHubError

    try:
        parsed = run_preference.parse_preference(raw)
    except run_preference.RunPreferenceError as exc:
        raise FleetHubError(str(exc)) from exc
    payload = parsed.payload()
    conn.execute(
        "INSERT INTO run_preference (id, impl, review, chef, notes, updated_at, updated_by) "
        "VALUES (1, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET impl=excluded.impl, review=excluded.review, "
        "chef=excluded.chef, notes=excluded.notes, updated_at=excluded.updated_at, "
        "updated_by=excluded.updated_by",
        (
            payload.get("impl"),
            payload.get("review"),
            payload.get("chef"),
            payload.get("notes"),
            _utc_now(),
            updated_by,
        ),
    )
    conn.commit()
    return get_run_preference(conn)
