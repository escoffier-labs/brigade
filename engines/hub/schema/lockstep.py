"""Compare Python's fresh and migrated schema with the reviewed Rust contract."""

import ast
from contextlib import closing
import importlib
import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[3]), str(Path(__file__).resolve().parents[3] / "src")]

from brigade import fleet_hub  # noqa: E402 - repository-local import after path bootstrap

ROOT = Path(__file__).resolve().parents[3]
REQUIRED = Path(__file__).with_name("required.json")


def normalize(sql: str) -> str:
    """Collapse whitespace, then drop it before `,`/`)` and after `(` outside quotes.

    SQLite's ADD COLUMN splices `, col` in front of the closing parenthesis, so
    a newline there becomes ` ,`. Rust `schema::normalize` must stay identical
    (normalize-cases.json).
    """
    collapsed = " ".join(sql.split())
    out: list[str] = []
    quote = ""
    for i, char in enumerate(collapsed):
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
        elif char == "[":
            quote = "]"
        elif char == " " and ((out and out[-1] == "(") or collapsed[i + 1 : i + 2] in {",", ")"}):
            continue
        out.append(char)
    return "".join(out)


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
        result.append(normalize(sql[start : i - 1]))
    return sorted(result)


def snapshot(conn: sqlite3.Connection) -> dict:
    objects = conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = {}
    triggers = {}
    for kind, name, _, sql in objects:
        if kind == "trigger":
            triggers[name] = normalize(sql)
        if kind != "table":
            continue
        columns = sorted([list(row)[1:] for row in conn.execute(f'PRAGMA table_xinfo("{name}")')])
        indexes = {}
        for row in conn.execute(f'PRAGMA index_list("{name}")'):
            index = row[1]
            ddl = conn.execute("SELECT sql FROM sqlite_schema WHERE name=?", (index,)).fetchone()
            indexes[index] = {
                "unique": row[2],
                "origin": row[3],
                "partial": row[4],
                "columns": [[r[0], *r[2:]] for r in conn.execute(f'PRAGMA index_xinfo("{index}")')],
                "sql": normalize((ddl[0] if ddl else None) or ""),
            }
        tables[name] = {
            "columns": columns,
            "indexes": indexes,
            "foreign_keys": [list(r) for r in conn.execute(f'PRAGMA foreign_key_list("{name}")')],
            "checks": checks(sql),
            "autoincrement": "AUTOINCREMENT" in sql.upper(),
            "column_order": [r[1] for r in conn.execute(f'PRAGMA table_xinfo("{name}")')],
            "sql": normalize(sql),
        }
    return {
        "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
        "tables": tables,
        "triggers": triggers,
    }


def compatible(actual: dict, required: dict) -> bool:
    expected = {key: value for key, value in required.items() if key not in {"optional", "variants"}}
    for kind, objects in required["optional"].items():
        expected[kind] = dict(expected[kind])
        for name, structure in objects.items():
            if name in actual[kind]:
                expected[kind][name] = structure
    expected["tables"] = dict(expected["tables"])
    for name, variants in required["variants"].items():
        if actual["tables"].get(name) in variants:
            expected["tables"][name] = actual["tables"][name]
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

    fleet_mcp_inventory.store_report(
        conn,
        "schema-probe",
        {
            "schema_version": 1,
            "brigade_version": "0.0.0",
            "observed_at": "2026-01-01T00:00:00+00:00",
            "catalog_present": False,
            "servers": [],
            "harnesses": [],
        },
    )
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
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        helpers = [
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in {"ensure_schema", "init_schema"}
        ]
        if helpers:
            module = importlib.import_module(f"brigade.{path.stem}")
            for name in helpers:
                getattr(module, name)(conn)
        elif re.search(r"CREATE\s+(?:TABLE|INDEX|TRIGGER)\b", source, re.I):
            assert path.stem in {"fleet_hub", "fleet_hub_claims", "fleet_ci_node", "fleet_export"}, (
                f"unclassified schema owner: {path.name}"
            )
    conn.commit()


def upgrade_inputs():
    """Release captures plus the supported hand-built regression layouts."""
    for folder in [
        ROOT / "registry/skills/verify-brigade/storage-parity-fixtures",
        REQUIRED.parent / "upgrade-fixtures",
    ]:
        for path in sorted(folder.glob("*.sql")):
            yield path.stem, path.read_text(encoding="utf-8")
    builders = {
        "test_fleet_hub_sealed.py": ["legacy_hub"],
        "test_fleet_messages.py": ["test_schema_migrates_existing_version_26_message_rows"],
        "test_fleet_message_grants.py": ["test_v30_migration_preserves_message_digest"],
        "test_fleet_model_roster.py": ["_v14_database"],
        "test_fleet_claims.py": ["test_pre_holder_token_claims_table_is_recreated", "_schema_13_database"],
        "test_run_preference.py": ["test_hub_preference_v18_row_survives_v19_migration"],
        "test_fleet_deck_badges.py": [],
        "test_fleet_command_deck.py": [],
        "test_fleet_cloud.py": ["test_v4_migration_preserves_existing_rows"],
        "test_fleet_nodes.py": ["_pre_holder_token_db"],
        "test_fleet_events_schema_upgrade.py": [],
        "test_fleet_claim_generations.py": [
            "test_schema33_migration_preserves_retirements_and_starts_conservative_horizon"
        ],
    }
    yield (
        "model-policy-rebuild",
        """CREATE TABLE model_policy (
        seat TEXT, provider TEXT NOT NULL, model TEXT NOT NULL, enabled INTEGER NOT NULL,
        limit_count INTEGER, notes TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(provider, model));""",
    )
    for file, functions in builders.items():
        tree = ast.parse((ROOT / "tests" / file).read_text(encoding="utf-8"))
        if not functions:
            nodes = [n for n in tree.body if isinstance(n, ast.Assign)]
        else:
            nodes = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in functions]
        ddl = []
        for node in nodes:
            for n in ast.walk(node):
                try:
                    value = ast.literal_eval(n)
                except (ValueError, TypeError, SyntaxError):
                    continue
                if isinstance(value, str) and value.strip().upper().startswith("CREATE TABLE"):
                    ddl.append(value)
        if ddl:
            yield file.removesuffix(".py"), ";\n".join(ddl) + ";"

    tree = ast.parse((ROOT / "tests/test_fleet_claim_release.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in {
            "test_v2_claim_rows_survive_the_lease_column_upgrade",
            "test_lease_column_upgrade_is_safe_under_concurrent_init",
        }:
            for value in ast.walk(node):
                try:
                    ddl = ast.literal_eval(value)
                except (ValueError, TypeError, SyntaxError):
                    continue
                if isinstance(ddl, str) and ddl.startswith("CREATE TABLE claims"):
                    yield node.name, ddl + ";"


def databases(directory: Path):
    for name, ddl in [("fresh", ""), *upgrade_inputs()]:
        path = directory / f"{name}.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(ddl)
            conn.commit()
            if ddl:
                with closing(sqlite3.connect(directory / f"{name}.raw.db")) as raw:
                    conn.backup(raw)
        conn = fleet_hub.init_db(path)
        try:
            yield name, conn
        finally:
            conn.close()
    # Existing regression builders remove additive columns from a current DB.
    # Replay their complete old-table shapes, rather than accepting column-wise unions.
    drops = {
        "messages-sealed": ("fleet_messages", ["envelope", "envelope_bytes", "content_state"]),
        "sessions-cloud": ("interactive_sessions", ["cloud_context_json"]),
        "sessions-provider": ("interactive_sessions", ["provider_link_json"]),
        "events-context": ("events", ["context"]),
        "model-native": ("model_policy", ["t3_model"]),
        "principals-v45": ("fleet_message_principals", ["authorization_enrollment_epoch", "grant_epoch"]),
        "principal-enrollment": ("fleet_message_principals", ["enrollment_id"]),
        "message-sealed-reply": ("fleet_messages", ["sealed_reply_to"]),
        "paging-access": ("fleet_read_snapshots", ["last_access"]),
        "work-links-owner": ("work_links", ["adapter_id", "owner_node"]),
        "work-import-owner": ("work_import_keys", ["owner_node"]),
        "work-links-version": ("work_links", ["source_version"]),
        "questions-late": (
            "fleet_questions",
            [
                "late_answer_message_id",
                "late_answered_at",
                "operator_reply_after_desktop_message_id",
                "operator_reply_after_desktop_at",
            ],
        ),
    }
    for name, (table, columns) in drops.items():
        path = directory / f"{name}.db"
        conn = fleet_hub.init_db(path)
        cleanup = {
            "work-links-owner": ["DROP INDEX work_links_adapter", "DROP INDEX work_links_owner"],
            "work-import-owner": ["DROP INDEX work_import_keys_adapter", "DROP INDEX work_import_keys_owner"],
            "messages-sealed": ["DROP INDEX fleet_messages_sealed_acked"],
            "questions-late": ["DROP TRIGGER fleet_questions_reply"],
        }
        for sql in cleanup.get(name, []):
            conn.execute(sql)
        for column in columns:
            conn.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
        conn.execute("PRAGMA user_version=0")
        conn.commit()
        conn.close()
        conn = fleet_hub.init_db(path)
        try:
            yield name, conn
        finally:
            conn.close()
    conn = fleet_hub.init_db(directory / "lazy.db")
    use_lazy_features(conn)
    discover_schema_helpers(conn)
    try:
        yield "lazy", conn
    finally:
        conn.close()


def generate(directory: Path) -> dict:
    snapshots = {name: snapshot(conn) for name, conn in databases(directory)}
    fresh = snapshots["fresh"]
    assert {"fleet_findings", "fleet_finding_grants"} <= set(fresh["tables"])
    lazy = snapshots["lazy"]
    required = dict(fresh)
    required["optional"] = {
        kind: {name: value for name, value in lazy[kind].items() if name not in fresh[kind]}
        for kind in ["tables", "triggers"]
    }
    variants = {}
    for actual in snapshots.values():
        for name, table in actual["tables"].items():
            values = variants.setdefault(name, [])
            if table not in values:
                values.append(table)
    required["variants"] = variants
    return required


def fixtures(directory: Path) -> None:
    required = json.loads(REQUIRED.read_text(encoding="utf-8"))
    assert required["user_version"] == fleet_hub.SCHEMA_VERSION, "Python schema bump needs Rust review"
    for name, conn in databases(directory):
        actual = snapshot(conn)
        assert compatible(actual, required), f"schema drift: {name}.db"
        if name == "lazy":
            for kind, objects in required["optional"].items():
                assert set(objects) <= set(actual[kind]), f"unexercised optional {kind}"
    conn = fleet_hub.init_db(directory / "lazy.db")
    assert snapshot(conn) == actual, "Python reopen changed lazy schema"
    conn.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["--generate"]:
        with tempfile.TemporaryDirectory() as directory:
            REQUIRED.write_text(
                json.dumps(generate(Path(directory)), indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
    else:
        fixtures(Path(sys.argv[1]))
