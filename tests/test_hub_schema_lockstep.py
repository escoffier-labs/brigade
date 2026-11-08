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


def test_recovery_floor_bump_is_in_the_rust_contract(tmp_path: Path) -> None:
    # v50 adds no DDL: the claim recovery floor is a hub_meta row. The version
    # bump alone keeps a Python or Rust hub that predates the floor rule from
    # serving a restored file.
    lockstep = runpy.run_path(str(LOCKSTEP))
    import json

    required = json.loads(lockstep["REQUIRED"].read_text(encoding="utf-8"))
    assert required["user_version"] == fleet_hub.SCHEMA_VERSION >= 50
    assert "hub_meta" in required["tables"] and "hub_meta" not in required["optional"]["tables"]
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    try:
        conn.execute("""INSERT INTO hub_meta (key, value) VALUES ('recovery', '{"claim_floor": 4294967296}')""")
        conn.commit()
        assert lockstep["compatible"](lockstep["snapshot"](conn), required)
    finally:
        conn.close()


SCHEMA_DIR = LOCKSTEP.parent
PRODUCTION = SCHEMA_DIR / "upgrade-fixtures/production-v50.sql"


def test_normalize_matches_rust_cases() -> None:
    import json

    normalize = runpy.run_path(str(LOCKSTEP))["normalize"]
    cases = json.loads((SCHEMA_DIR / "normalize-cases.json").read_text(encoding="utf-8"))["cases"]
    assert cases
    for case in cases:
        assert normalize(case["input"]) == case["output"], case["input"]


def _production_hub(tmp_path: Path, ddl: str) -> sqlite3.Connection:
    path = tmp_path / "production.db"
    with sqlite3.connect(path) as raw:
        raw.executescript(ddl)
    return fleet_hub.init_db(path)


def test_python_init_keeps_production_layouts_and_gate_accepts_them(tmp_path: Path) -> None:
    import json

    lockstep = runpy.run_path(str(LOCKSTEP))
    ddl = PRODUCTION.read_text(encoding="utf-8")
    with sqlite3.connect(tmp_path / "raw.db") as raw:
        raw.executescript(ddl)
        before = {name: sql for name, sql in raw.execute("SELECT name, sql FROM sqlite_schema WHERE type='table'")}
    conn = _production_hub(tmp_path, ddl)
    try:
        # Python's own startup accepts the layouts as-is and rewrites nothing.
        for name in ("claims", "model_policy"):
            stored = conn.execute("SELECT sql FROM sqlite_schema WHERE name=?", (name,)).fetchone()[0]
            assert stored == before[name]
        actual = lockstep["snapshot"](conn)
    finally:
        conn.close()
    required = json.loads(lockstep["REQUIRED"].read_text(encoding="utf-8"))
    assert lockstep["compatible"](actual, required)

    # Without the punctuation rule, the production claims DDL matches no variant.
    legacy = " ".join(before["claims"].split())
    assert legacy != lockstep["normalize"](before["claims"])
    refused = {**actual, "tables": {**actual["tables"], "claims": {**actual["tables"]["claims"], "sql": legacy}}}
    assert not lockstep["compatible"](refused, required)

    # Without its recorded variant, the production model_policy order is refused.
    production = actual["tables"]["model_policy"]
    variants = required["variants"]["model_policy"]
    assert variants.count(production) == 1
    variants.remove(production)
    assert not lockstep["compatible"](actual, required)


@pytest.mark.parametrize(
    ("old", "new", "after"),
    [
        # init_db re-adds a missing claims column, so drop one after startup.
        ("", "", "ALTER TABLE claims DROP COLUMN session"),
        ("", "", "ALTER TABLE model_policy DROP COLUMN notes"),
        ("expires_at REAL NOT NULL", "expires_at TEXT NOT NULL", ""),
        ("enabled INTEGER NOT NULL", "enabled TEXT NOT NULL", ""),
        ("t3_model TEXT NOT NULL DEFAULT ''", "t3_model TEXT", ""),
    ],
)
def test_gate_still_refuses_different_production_schema(tmp_path: Path, old: str, new: str, after: str) -> None:
    import json

    lockstep = runpy.run_path(str(LOCKSTEP))
    ddl = PRODUCTION.read_text(encoding="utf-8")
    assert old in ddl
    conn = _production_hub(tmp_path, ddl.replace(old, new, 1) if old else ddl)
    try:
        if after:
            conn.execute(after)
            conn.commit()
        actual = lockstep["snapshot"](conn)
    finally:
        conn.close()
    required = json.loads(lockstep["REQUIRED"].read_text(encoding="utf-8"))
    assert not lockstep["compatible"](actual, required)
