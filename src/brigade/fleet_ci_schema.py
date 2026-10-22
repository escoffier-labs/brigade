"""Additive CI observation storage. No provider clients belong on the Hub."""

from __future__ import annotations

import sqlite3


def ensure_schema(conn: sqlite3.Connection) -> None:
    for statement in (
        "CREATE TABLE IF NOT EXISTS ci_runs (repo TEXT NOT NULL, object_id INTEGER NOT NULL, "
        "attempt INTEGER NOT NULL, updated_at TEXT NOT NULL, payload TEXT NOT NULL, "
        "PRIMARY KEY(repo, object_id, attempt))",
        "CREATE TABLE IF NOT EXISTS ci_check_runs (repo TEXT NOT NULL, kind TEXT NOT NULL, "
        "object_id INTEGER NOT NULL, attempt INTEGER NOT NULL, updated_at TEXT NOT NULL, "
        "payload TEXT NOT NULL, PRIMARY KEY(repo, kind, object_id, attempt))",
        "CREATE TABLE IF NOT EXISTS ci_repo_state (repo TEXT PRIMARY KEY, visibility TEXT NOT NULL DEFAULT 'unknown', "
        "snapshot TEXT, observed_at TEXT, last_event_received TEXT, last_reconciled TEXT, refresh_requested TEXT)",
        "CREATE TABLE IF NOT EXISTS ci_invalidations (repo TEXT NOT NULL, head TEXT NOT NULL, "
        "received_at TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(repo, head))",
        "CREATE TABLE IF NOT EXISTS ci_relay_watermarks (repo TEXT NOT NULL, relay TEXT NOT NULL, "
        "cursor INTEGER NOT NULL, PRIMARY KEY(repo, relay))",
    ):
        conn.execute(statement)
