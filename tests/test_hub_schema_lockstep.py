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


def test_generation_covers_legacy_builders_and_rejects_hybrids(tmp_path: Path) -> None:
    lockstep = runpy.run_path(str(LOCKSTEP))
    required = lockstep["generate"](tmp_path)
    assert {"test_fleet_hub_sealed", "test_fleet_messages", "test_fleet_cloud", "test_fleet_model_roster"} <= {
        name for name, _ in lockstep["upgrade_inputs"]()
    }
    import copy

    actual = {key: copy.deepcopy(required[key]) for key in ("user_version", "tables", "triggers")}
    variants = required["variants"]["fleet_messages"]
    legacy = next(v for v in variants if any(c[0] == "client_id" and c[2] == 0 for c in v["columns"]))
    hybrid = copy.deepcopy(legacy)
    hybrid["columns"] = required["tables"]["fleet_messages"]["columns"]
    actual["tables"]["fleet_messages"] = hybrid
    assert not lockstep["compatible"](actual, required)


def test_findings_are_required_startup_objects(tmp_path: Path) -> None:
    lockstep = runpy.run_path(str(LOCKSTEP))
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    try:
        actual = lockstep["snapshot"](conn)
        import json

        required = json.loads(lockstep["REQUIRED"].read_text(encoding="utf-8"))
        for name in ("fleet_findings", "fleet_finding_grants"):
            assert name in required["tables"]
            assert name not in required["optional"]["tables"]
            removed = {**actual, "tables": {k: v for k, v in actual["tables"].items() if k != name}}
            assert not lockstep["compatible"](removed, required)
    finally:
        conn.close()
