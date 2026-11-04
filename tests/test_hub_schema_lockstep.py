"""Keep Python schema changes coupled to the Rust hub's reviewed structure."""

from __future__ import annotations

import runpy
import sqlite3
from pathlib import Path

import pytest

from brigade import fleet_hub

LOCKSTEP = Path(__file__).resolve().parents[1] / "engines/hub/schema/lockstep.py"


def test_fresh_and_migrated_hub_schema_lockstep(tmp_path: Path) -> None:
    runpy.run_path(str(LOCKSTEP))["fixtures"](tmp_path)


def test_python_schema_bump_requires_rust_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lockstep = runpy.run_path(str(LOCKSTEP))
    monkeypatch.setattr(fleet_hub, "SCHEMA_VERSION", fleet_hub.SCHEMA_VERSION + 1)
    with pytest.raises(AssertionError, match="Python schema bump needs Rust review"):
        lockstep["fixtures"](tmp_path)


def test_added_column_requires_rust_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lockstep = runpy.run_path(str(LOCKSTEP))
    original = fleet_hub.init_db

    def added_column(path: Path) -> sqlite3.Connection:
        conn = original(path)
        conn.execute("ALTER TABLE events ADD COLUMN unexpected TEXT")
        conn.commit()
        return conn

    monkeypatch.setattr(fleet_hub, "init_db", added_column)
    with pytest.raises(AssertionError, match="schema drift: fresh.db"):
        lockstep["fixtures"](tmp_path)


def test_new_lazy_table_requires_optional_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from brigade import fleet_mcp_inventory

    original = fleet_mcp_inventory.ensure_schema

    def new_lazy_table(conn: sqlite3.Connection) -> None:
        original(conn)
        conn.execute("CREATE TABLE IF NOT EXISTS future_lazy_feature (value TEXT)")

    monkeypatch.setattr(fleet_mcp_inventory, "ensure_schema", new_lazy_table)
    with pytest.raises(AssertionError, match="schema drift: lazy.db"):
        runpy.run_path(str(LOCKSTEP))["fixtures"](tmp_path)
