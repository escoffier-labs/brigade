//! Integration test: call-edge resolution prefers same-file targets over cross-file homonyms.

use std::fs;

use graphtrail::store::{init_schema, open_db, sync_repo};

#[test]
fn same_file_call_resolves_to_same_file_symbol() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();

    // caller.py defines `helper` and calls it -> should link to the LOCAL helper, not other.py's.
    fs::write(
        root.join("caller.py"),
        r#"
def helper():
    return 1

def run():
    return helper()
"#,
    )
    .unwrap();
    fs::write(
        root.join("other.py"),
        r#"
def helper():
    return 2
"#,
    )
    .unwrap();

    let db = root.join("graphtrail.db");
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();

    // The edge from run -> helper must target the helper defined in caller.py.
    let target_file: String = conn
        .query_row(
            r#"
            SELECT dst.file_path
            FROM edges e
            JOIN symbols src ON src.id = e.source
            JOIN symbols dst ON dst.id = e.target
            WHERE src.name = 'run' AND dst.name = 'helper'
            "#,
            [],
            |row| row.get(0),
        )
        .expect("run -> helper edge should exist");

    assert_eq!(target_file, "caller.py");
}

#[test]
fn cross_file_fallback_edges_are_capped_in_stable_order() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();

    fs::write(
        root.join("caller.py"),
        r#"
def run():
    return target()
"#,
    )
    .unwrap();

    for i in (0..10).rev() {
        fs::write(
            root.join(format!("target_{i:02}.py")),
            r#"
def target():
    return 1
"#,
        )
        .unwrap();
    }

    let db = root.join("graphtrail.db");
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();

    let mut stmt = conn
        .prepare(
            r#"
            SELECT dst.file_path
            FROM edges e
            JOIN symbols src ON src.id = e.source
            JOIN symbols dst ON dst.id = e.target
            WHERE src.name = 'run' AND dst.name = 'target'
            ORDER BY dst.file_path
            "#,
        )
        .unwrap();
    let target_files: Vec<String> = stmt
        .query_map([], |row| row.get(0))
        .unwrap()
        .map(|row| row.unwrap())
        .collect();

    assert_eq!(
        target_files,
        vec![
            "target_00.py",
            "target_01.py",
            "target_02.py",
            "target_03.py",
            "target_04.py",
            "target_05.py",
            "target_06.py",
            "target_07.py",
        ]
    );
}
