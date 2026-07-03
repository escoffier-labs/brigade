use super::graph::*;
use crate::model::Direction;
use crate::store::init_schema;
use rusqlite::{Connection, params};

fn graph_conn() -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    init_schema(&conn).unwrap();
    for path in ["src/a.py", "src/b.py", "src/c.py", "src/d.py"] {
        conn.execute(
            "INSERT INTO files(path, content_hash, size, modified_at, indexed_at, language)
             VALUES (?1, 'hash', 1, 1, 1, 'python')",
            params![path],
        )
        .unwrap();
    }
    conn
}

fn insert_symbol(conn: &Connection, id: &str, file_path: &str) {
    conn.execute(
        "INSERT INTO symbols(id, kind, name, qualified_name, file_path, start_line, end_line, signature, content_hash)
         VALUES (?1, 'function', ?1, ?1, ?2, 1, 2, ?1, 'hash')",
        params![id, file_path],
    )
    .unwrap();
}

fn insert_edge(conn: &Connection, source: &str, target: &str, line: usize) {
    conn.execute(
        "INSERT INTO edges(source, target, kind, line) VALUES (?1, ?2, 'calls', ?3)",
        params![source, target, line as i64],
    )
    .unwrap();
}

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

#[test]
fn depth_one_matches_direct_edges_with_hops() {
    let conn = graph_conn();
    for (id, file_path) in [("a", "src/a.py"), ("b", "src/b.py"), ("c", "src/c.py")] {
        insert_symbol(&conn, id, file_path);
    }
    insert_edge(&conn, "a", "b", 10);
    insert_edge(&conn, "b", "c", 20);

    let direct = edges_for_symbol_id(&conn, "b", Direction::Incoming).unwrap();
    let depth_one = edges_for_symbol_id_with_depth(&conn, "b", Direction::Incoming, 1).unwrap();

    assert_eq!(depth_one.len(), 1);
    assert_eq!(direct.len(), depth_one.len());
    assert_eq!(depth_one[0].source_id, "a");
    assert_eq!(depth_one[0].target_id, "b");
    assert_eq!(depth_one[0].hops, 1);
}

#[test]
fn depth_three_walks_chain_and_records_hops() {
    let conn = graph_conn();
    for (id, file_path) in [
        ("a", "src/a.py"),
        ("b", "src/b.py"),
        ("c", "src/c.py"),
        ("d", "src/d.py"),
    ] {
        insert_symbol(&conn, id, file_path);
    }
    insert_edge(&conn, "a", "b", 10);
    insert_edge(&conn, "b", "c", 20);
    insert_edge(&conn, "c", "d", 30);

    let rows = edges_for_symbol_id_with_depth(&conn, "a", Direction::Outgoing, 3).unwrap();
    let pairs: Vec<(&str, &str, usize)> = rows
        .iter()
        .map(|row| (row.source_id.as_str(), row.target_id.as_str(), row.hops))
        .collect();

    assert_eq!(pairs, vec![("a", "b", 1), ("b", "c", 2), ("c", "d", 3)]);
}

#[test]
fn cycles_do_not_revisit_symbols_forever() {
    let conn = graph_conn();
    for (id, file_path) in [("a", "src/a.py"), ("b", "src/b.py"), ("c", "src/c.py")] {
        insert_symbol(&conn, id, file_path);
    }
    insert_edge(&conn, "a", "b", 10);
    insert_edge(&conn, "b", "c", 20);
    insert_edge(&conn, "c", "a", 30);

    let rows = edges_for_symbol_id_with_depth(&conn, "a", Direction::Outgoing, 5).unwrap();

    assert_eq!(rows.len(), 3);
    assert!(rows.iter().all(|row| row.kind != TRUNCATED_EDGE_KIND));
    assert_eq!(
        rows.iter().map(|row| row.hops).collect::<Vec<_>>(),
        vec![1, 2, 3]
    );
}

#[test]
fn traversal_adds_truncation_marker_after_cap() {
    let conn = graph_conn();
    insert_symbol(&conn, "root", "src/a.py");
    for index in 0..=EDGE_CAP_PER_DIRECTION {
        let id = format!("leaf_{index}");
        insert_symbol(&conn, &id, "src/b.py");
        insert_edge(&conn, "root", &id, index + 1);
    }

    let rows = edges_for_symbol_id_with_depth(&conn, "root", Direction::Outgoing, 1).unwrap();

    assert_eq!(rows.len(), EDGE_CAP_PER_DIRECTION + 1);
    assert_eq!(rows[..EDGE_CAP_PER_DIRECTION].len(), EDGE_CAP_PER_DIRECTION);
    assert_eq!(rows.last().unwrap().kind, TRUNCATED_EDGE_KIND);
    assert_eq!(rows.last().unwrap().hops, 1);
}
