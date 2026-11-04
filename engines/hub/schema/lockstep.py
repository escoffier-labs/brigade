"""Compare Python's fresh and migrated schema with the reviewed Rust contract."""

import ast
import importlib
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[3]), str(Path(__file__).resolve().parents[3] / "src")]

from brigade import fleet_hub

ROOT = Path(__file__).resolve().parents[3]
REQUIRED = Path(__file__).with_name("required.json")


def checks(sql: str) -> list[str]:
    """Extract balanced CHECK expressions, preserving nested parentheses."""
    result = []
    for match in re.finditer(r"\bCHECK\s*\(", sql, re.IGNORECASE):
        start = match.end()
        depth, quote, i = 1, "", start
        while i < len(sql) and depth:
            char = sql[i]
            if quote:
                if char == quote:
                    if i + 1 < len(sql) and sql[i + 1] == quote:
                        i += 1
                    else:
                        quote = ""
            elif char in "'\"`":
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            i += 1
        result.append(" ".join(sql[start : i - 1].split()))
    return sorted(result)


def snapshot(conn: sqlite3.Connection) -> dict:
    objects = conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = {}
    triggers = {}
    for kind, name, _, sql in objects:
        if kind == "trigger":
            triggers[name] = " ".join(sql.split())
        if kind != "table":
            continue
        columns = sorted(
            [list(row)[1:] for row in conn.execute(f'PRAGMA table_xinfo("{name}")')]
        )
        indexes = {}
        for row in conn.execute(f'PRAGMA index_list("{name}")'):
            index = row[1]
            ddl = conn.execute(
                "SELECT sql FROM sqlite_schema WHERE name=?", (index,)
            ).fetchone()
            indexes[index] = {
                "unique": row[2],
                "origin": row[3],
                "partial": row[4],
                "columns": [
                    [r[0], *r[2:]]
                    for r in conn.execute(f'PRAGMA index_xinfo("{index}")')
                ],
                "sql": " ".join(((ddl[0] if ddl else None) or "").split()),
            }
        tables[name] = {
            "columns": columns,
            "indexes": indexes,
            "foreign_keys": [
                list(r) for r in conn.execute(f'PRAGMA foreign_key_list("{name}")')
            ],
            "checks": checks(sql),
            "autoincrement": "AUTOINCREMENT" in sql.upper(),
        }
    return {
        "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
        "tables": tables,
        "triggers": triggers,
    }


def compatible(actual: dict, required: dict) -> bool:
    expected = {key: value for key, value in required.items() if key != "optional"}
    for kind, objects in required["optional"].items():
        expected[kind] = dict(expected[kind])
        for name, structure in objects.items():
            if name in actual[kind]:
                expected[kind][name] = structure
    return actual == expected


def use_lazy_features(conn: sqlite3.Connection) -> None:
    """Exercise supported feature entry points, without copying their DDL."""
    from brigade import (
        fleet_hub_policy_api,
        fleet_mcp_inventory,
        fleet_policy_migration,
        fleet_project_board,
        fleet_skills_review,
    )
    from tests.fixtures import fleet_project_board as board_fixture

    fleet_mcp_inventory.store_report(conn, "schema-probe", {
        "schema_version": 1, "brigade_version": "0.0.0",
        "observed_at": "2026-01-01T00:00:00+00:00", "catalog_present": False,
        "servers": [], "harnesses": [],
    })
    fleet_hub_policy_api.read_inventory(conn)
    fleet_skills_review.load_view(conn)
    fleet_project_board.store_snapshot(conn, board_fixture.snapshot())
    fleet_policy_migration.ensure_schema(conn)
    conn.commit()


def discover_schema_helpers(conn: sqlite3.Connection) -> None:
    """Exercise all fleet schema helpers, including future lazy families.

    CI-node caches and export sinks own separate databases, not the hub file.
    Other CREATE-bearing fleet modules must expose a schema helper or be the
    hub's startup/migration owner. A new unclassified DDL owner fails lockstep.
    """
    for path in sorted((ROOT / "src/brigade").glob("fleet*.py")):
        source = path.read_text()
        tree = ast.parse(source)
        helpers = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name in {"ensure_schema", "init_schema"}]
        if helpers:
            module = importlib.import_module(f"brigade.{path.stem}")
            for name in helpers:
                getattr(module, name)(conn)
        elif re.search(r"CREATE\s+(?:TABLE|INDEX|TRIGGER)\b", source, re.I):
            assert path.stem in {"fleet_hub", "fleet_hub_claims", "fleet_ci_node", "fleet_export"}, (
                f"unclassified schema owner: {path.name}"
            )
    conn.commit()


def fixtures(directory: Path) -> None:
    required = json.loads(REQUIRED.read_text())
    assert required["user_version"] == fleet_hub.SCHEMA_VERSION, (
        "Python schema bump needs Rust review"
    )
    for fixture in [
        None,
        *sorted(
            (ROOT / "registry/skills/verify-brigade/storage-parity-fixtures").glob(
                "*.sql"
            )
        ),
    ]:
        path = directory / f"{fixture.stem if fixture else 'fresh'}.db"
        if fixture:
            with sqlite3.connect(path) as conn:
                conn.executescript(fixture.read_text())
        if fixture:
            with (
                sqlite3.connect(path) as source,
                sqlite3.connect(directory / f"{fixture.stem}.raw.db") as target,
            ):
                source.backup(target)
        conn = fleet_hub.init_db(path)
        assert compatible(snapshot(conn), required), f"schema drift: {path.name}"
        conn.close()
    lazy_path = directory / "lazy.db"
    conn = fleet_hub.init_db(lazy_path)
    use_lazy_features(conn)
    discover_schema_helpers(conn)
    actual = snapshot(conn)
    assert compatible(actual, required), "schema drift: lazy.db"
    for kind, objects in required["optional"].items():
        assert set(objects) <= set(actual[kind]), f"unexercised optional {kind}"
    conn.close()
    conn = fleet_hub.init_db(lazy_path)
    assert snapshot(conn) == actual, "Python reopen changed lazy schema"
    conn.close()


if __name__ == "__main__":
    fixtures(Path(sys.argv[1]))
