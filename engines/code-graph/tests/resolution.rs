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
