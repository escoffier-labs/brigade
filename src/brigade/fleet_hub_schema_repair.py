"""Detect additive schema repairs for an existing Hub database."""

from __future__ import annotations

import sqlite3

from . import fleet_hub_events, fleet_hub_messages


def missing_column_ddl(
    conn: sqlite3.Connection, columns: tuple[tuple[str, str, str], ...], *, created: set[str] | None = None
) -> list[str]:
    return [
        ddl
        for table, column, ddl in columns
        if table not in (created or ())
        and column not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    ]


def missing_additive_schema(
    conn: sqlite3.Connection,
    columns: tuple[tuple[str, str, str], ...],
    indexes: tuple[tuple[str, str], ...],
) -> list[str]:
    from . import fleet_hub_paging

    # Order matters: an events table first (a hub at the current schema version may predate one added later on
    # the events branch), then the ALTERs that need their table, then indexes that need their columns.
    existing = {(kind, name) for kind, name in conn.execute("SELECT type, name FROM sqlite_master")}
    absent = [(kind, ddl) for kind, name, ddl in fleet_hub_events.SCHEMA_OBJECTS if (kind, name) not in existing]
    missing = [ddl for kind, ddl in absent if kind == "table"]
    created = {
        name for kind, name, _ddl in fleet_hub_events.SCHEMA_OBJECTS if kind == "table" and (kind, name) not in existing
    }
    missing.extend(missing_column_ddl(conn, columns, created=created))
    missing.extend(fleet_hub_messages.missing_reply_backfills(conn))
    missing.extend(ddl for kind, ddl in absent if kind == "index")
    # Index repair shares the current-version self-heal transaction and its
    # read-only fast path. Claim fencing tables retain their strict validation.
    missing.extend(
        ddl
        for name, ddl in indexes
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)).fetchone() is None
    )
    if ("index", "fleet_messages_sealed_acked") not in existing:
        missing.append(
            "CREATE INDEX IF NOT EXISTS fleet_messages_sealed_acked ON fleet_messages (acked_at,id) WHERE content_state='sealed'"
        )
    from . import fleet_hub_questions

    missing.extend(fleet_hub_questions.missing_schema(conn))
    from . import fleet_hub_identity, fleet_hub_message_keys

    return (
        missing
        + fleet_hub_paging.missing_schema(conn)
        + fleet_hub_message_keys.missing_schema(conn)
        + fleet_hub_identity.missing_schema(conn)
    )
