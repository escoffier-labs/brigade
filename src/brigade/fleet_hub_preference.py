"""Fleet hub storage for the one-row run preference pin (#1223).

Split from ``fleet_hub`` to keep that module under the size ratchet. The
preference carries seat *names* only; ``run_preference.parse_preference``
rejects secret or path material before anything reaches the table.

v19 adds the ``research``, ``security``, and ``scout`` role columns used by
the hub roster page; the migration is additive.
"""

import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import run_preference

# v13 (#1223): one-row fleet run preference. Seat names only; never secrets.
_RUN_PREFERENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_preference (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    impl TEXT,
    review TEXT,
    chef TEXT,
    research TEXT,
    security TEXT,
    scout TEXT,
    notes TEXT,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
"""
_V19_COLUMNS = ("research", "security", "scout")
_FIELDS = run_preference.ALLOWED_FIELDS


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the run_preference table (v12 -> v13) and add the v19 role columns."""
    conn.execute(_RUN_PREFERENCE_SCHEMA)
    present = {str(row[1]) for row in conn.execute("PRAGMA table_info(run_preference)").fetchall()}
    for column in _V19_COLUMNS:
        if column not in present:
            conn.execute(f"ALTER TABLE run_preference ADD COLUMN {column} TEXT")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_run_preference(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return the one-row fleet run preference, or empty fields when unset."""
    columns = ", ".join(_FIELDS)
    row = conn.execute(f"SELECT {columns} FROM run_preference WHERE id = 1").fetchone()
    if row is None:
        return {field: None for field in _FIELDS}
    return {field: row[index] for index, field in enumerate(_FIELDS)}


def get_run_preference_meta(conn: sqlite3.Connection) -> dict[str, str | None]:
    """``updated_at`` and ``updated_by`` of the pin; both ``None`` when unset."""
    row = conn.execute("SELECT updated_at, updated_by FROM run_preference WHERE id = 1").fetchone()
    if row is None:
        return {"updated_at": None, "updated_by": None}
    return {"updated_at": row[0], "updated_by": row[1]}


def upsert_run_preference(conn: sqlite3.Connection, raw: Any, *, updated_by: str | None) -> dict[str, Any]:
    """Validate and write the pin inside the caller's transaction. No commit."""
    from .fleet_hub import FleetHubError

    try:
        parsed = run_preference.parse_preference(raw)
    except run_preference.RunPreferenceError as exc:
        raise FleetHubError(str(exc)) from exc
    payload = parsed.payload()
    columns = ", ".join(_FIELDS)
    placeholders = ", ".join("?" for _ in _FIELDS)
    updates = ", ".join(f"{field}=excluded.{field}" for field in _FIELDS)
    conn.execute(
        f"INSERT INTO run_preference (id, {columns}, updated_at, updated_by) "
        f"VALUES (1, {placeholders}, ?, ?) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}, updated_at=excluded.updated_at, "
        "updated_by=excluded.updated_by",
        (*[payload.get(field) for field in _FIELDS], _utc_now(), updated_by),
    )
    return get_run_preference(conn)


def set_run_preference(conn: sqlite3.Connection, raw: Any, *, updated_by: str | None = None) -> dict[str, Any]:
    """Replace the fleet run preference and commit. Rejects secret or path material."""
    stored = upsert_run_preference(conn, raw, updated_by=updated_by)
    conn.commit()
    return stored
