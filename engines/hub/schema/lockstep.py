"""Compare Python's fresh and migrated schema with the reviewed Rust contract."""

import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

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
        assert snapshot(conn) == required, f"schema drift: {path.name}"
        conn.close()


if __name__ == "__main__":
    fixtures(Path(sys.argv[1]))
