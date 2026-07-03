use super::graph::*;
use crate::store::init_schema;
use rusqlite::{Connection, params};

#[test]
fn file_neighbors_returns_cross_file_edge_counts() {
    let conn = Connection::open_in_memory().unwrap();
    init_schema(&conn).unwrap();
    for path in ["src/app.py", "src/lib.py", "src/cli.py"] {
        conn.execute(
            "INSERT INTO files(path, content_hash, size, modified_at, indexed_at, language)
             VALUES (?1, 'hash', 1, 1, 1, 'python')",
            params![path],
        )
        .unwrap();
    }
    for (id, name, file_path) in [
        ("app_run", "run", "src/app.py"),
        ("lib_helper", "helper", "src/lib.py"),
        ("cli_main", "main", "src/cli.py"),
    ] {
        conn.execute(
            "INSERT INTO symbols(id, kind, name, qualified_name, file_path, start_line, end_line, signature, content_hash)
             VALUES (?1, 'function', ?2, ?2, ?3, 1, 2, ?2, 'hash')",
            params![id, name, file_path],
        )
        .unwrap();
    }
    conn.execute(
        "INSERT INTO edges(source, target, kind, line) VALUES
         ('app_run', 'lib_helper', 'calls', 2),
         ('cli_main', 'app_run', 'calls', 3)",
        [],
    )
    .unwrap();

    let rows = file_neighbors(&conn, "src/app.py").unwrap();

    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0].file_path, "src/cli.py");
    assert_eq!(rows[0].incoming_edges, 1);
    assert_eq!(rows[0].outgoing_edges, 0);
    assert_eq!(rows[1].file_path, "src/lib.py");
    assert_eq!(rows[1].incoming_edges, 0);
    assert_eq!(rows[1].outgoing_edges, 1);
}
