//! Schema v7 migration: line-independent symbol ids rewritten in place,
//! re-parsing nothing, plus the identity property the change buys.

use std::fs;

use graphtrail::extractors::common::{hex_hash, symbol_id};
use graphtrail::store::{init_schema, open_db, sync_repo};
use rusqlite::Connection;

fn synced_fixture() -> (tempfile::TempDir, Connection) {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    fs::write(root.join("lib.py"), "def helper():\n    return 1\n").unwrap();
    fs::write(
        root.join("app.py"),
        "from lib import helper\n\ndef run():\n    return helper()\n",
    )
    .unwrap();
    let conn = open_db(&root.join("g.db")).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();
    (dir, conn)
}

/// Rewrite the database to look like a v6 index: line-baked symbol ids
/// everywhere a symbol id lives, and a stored schema_version of 6.
fn downgrade_to_v6_ids(conn: &Connection) {
    // The fixture rewrites live rows out from under the FK graph; enforcement
    // comes back on before the migration under test runs.
    conn.pragma_update(None, "foreign_keys", "OFF").unwrap();
    let rows: Vec<(String, String, String, String, i64)> = {
        let mut stmt = conn
            .prepare("SELECT id, file_path, qualified_name, kind, start_line FROM symbols")
            .unwrap();
        let mapped = stmt
            .query_map([], |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                ))
            })
            .unwrap();
        mapped.collect::<Result<_, _>>().unwrap()
    };
    for (new_id, file_path, qualified_name, kind, start_line) in rows {
        let old_id =
            hex_hash(format!("{file_path}:{qualified_name}:{start_line}:{kind}").as_bytes());
        for sql in [
            "UPDATE symbols SET id = ?2 WHERE id = ?1",
            "UPDATE pending_calls SET source_id = ?2 WHERE source_id = ?1",
            "UPDATE symbols_fts SET symbol_id = ?2 WHERE symbol_id = ?1",
            "UPDATE edges SET source = ?2 WHERE source = ?1",
            "UPDATE edges SET target = ?2 WHERE target = ?1",
        ] {
            conn.execute(sql, rusqlite::params![new_id, old_id])
                .unwrap();
        }
    }
    conn.execute(
        "UPDATE meta SET value = '6' WHERE key = 'schema_version'",
        [],
    )
    .unwrap();
    conn.pragma_update(None, "foreign_keys", "ON").unwrap();
}

#[test]
fn v6_to_v7_rewrites_ids_without_reextracting_files() {
    let (dir, conn) = synced_fixture();
    downgrade_to_v6_ids(&conn);
    let indexed_at_before: Vec<i64> = {
        let mut stmt = conn
            .prepare("SELECT indexed_at FROM files ORDER BY path")
            .unwrap();
        let rows = stmt.query_map([], |row| row.get(0)).unwrap();
        rows.collect::<Result<_, _>>().unwrap()
    };

    // The next sync performs the v7 migration.
    let summary = sync_repo(&conn, dir.path()).unwrap();
    assert!(!summary.unchanged, "migration must count as a change");

    let run_id: String = conn
        .query_row(
            "SELECT id FROM symbols WHERE qualified_name = 'run'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(run_id, symbol_id("app.py", "run", "function", 0));

    // pending_calls follow their symbols, so edges re-derive correctly.
    let edge_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM edges e
             JOIN symbols s ON s.id = e.source
             JOIN symbols t ON t.id = e.target
             WHERE s.qualified_name = 'run' AND t.qualified_name = 'helper'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(edge_count, 1);

    // FTS rows were remapped: search still joins back to real symbols.
    let fts_join: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM symbols_fts f JOIN symbols s ON s.id = f.symbol_id",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(fts_join, 2);

    // No file was re-parsed: indexed_at is untouched.
    let indexed_at_after: Vec<i64> = {
        let mut stmt = conn
            .prepare("SELECT indexed_at FROM files ORDER BY path")
            .unwrap();
        let rows = stmt.query_map([], |row| row.get(0)).unwrap();
        rows.collect::<Result<_, _>>().unwrap()
    };
    assert_eq!(indexed_at_before, indexed_at_after);
}

#[test]
fn moving_a_symbol_down_the_file_keeps_its_id() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    fs::write(root.join("a.py"), "def stable():\n    return 1\n").unwrap();
    let conn = open_db(&root.join("g.db")).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();
    let id_before: String = conn
        .query_row("SELECT id FROM symbols WHERE name = 'stable'", [], |row| {
            row.get(0)
        })
        .unwrap();

    // Push the symbol down two lines; content changes, identity must not.
    fs::write(root.join("a.py"), "\n\ndef stable():\n    return 1\n").unwrap();
    sync_repo(&conn, root).unwrap();
    let id_after: String = conn
        .query_row("SELECT id FROM symbols WHERE name = 'stable'", [], |row| {
            row.get(0)
        })
        .unwrap();

    assert_eq!(id_before, id_after);
}
