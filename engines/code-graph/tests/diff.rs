//! End-to-end test for `graphtrail diff`: build two indexed DBs from a before/after
//! version of a small repo and assert the structural diff.

use std::fs;

use graphtrail::query::diff_graphs;
use graphtrail::store::{init_schema, meta, open_db, sync_repo};

/// Sync `source` (a single mod.py) into a fresh DB under `dir` and return the connection.
fn index(dir: &std::path::Path, source: &str) -> rusqlite::Connection {
    fs::write(dir.join("mod.py"), source).unwrap();
    let conn = open_db(&dir.join("g.db")).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, dir).unwrap();
    conn
}

#[test]
fn diff_json_contract_matches_inline_golden() {
    let before = "\
def keep():
    return 1

def changed():
    return 1

def removed():
    keep()
";
    let after = "\
def keep():
    return 1

def changed(x):
    return x

def added():
    keep()
";

    let before_dir = tempfile::tempdir().unwrap();
    let after_dir = tempfile::tempdir().unwrap();
    let before_conn = index(before_dir.path(), before);
    let after_conn = index(after_dir.path(), after);

    let diff = diff_graphs(&before_conn, &after_conn).unwrap();
    let json = serde_json::to_string(&diff).unwrap();

    assert_eq!(
        json,
        r#"{"schema_version":3,"summary":{"added_nodes":1,"removed_nodes":1,"changed_nodes":1,"added_edges":1,"removed_edges":1,"added_edges_line_insensitive":1,"removed_edges_line_insensitive":1},"added_nodes":[{"kind":"function","qualified_name":"added","file_path":"mod.py","start_line":7,"signature":"def added():"}],"removed_nodes":[{"kind":"function","qualified_name":"removed","file_path":"mod.py","start_line":7,"signature":"def removed():"}],"changed_nodes":[{"kind":"function","qualified_name":"changed","file_path":"mod.py","start_line":4,"signature":"def changed(x):","previous":{"start_line":4,"signature":"def changed():"}}],"added_edges":[{"source":"added","source_file":"mod.py","target":"keep","target_file":"mod.py","line":8}],"removed_edges":[{"source":"removed","source_file":"mod.py","target":"keep","target_file":"mod.py","line":8}]}"#
    );
}

#[test]
fn diff_reports_added_removed_and_changed() {
    // `old` is deleted, `baz` is added, `foo` changes signature, `bar` is untouched.
    // Line layout is identical for the surviving pieces so the bar->foo edge does
    // not churn (edge identity includes the call line).
    let before = "\
def foo():
    pass

def bar():
    foo()

def old():
    foo()
";
    let after = "\
def foo(y):
    pass

def bar():
    foo()

def baz():
    foo()
";

    let before_dir = tempfile::tempdir().unwrap();
    let after_dir = tempfile::tempdir().unwrap();
    let before_conn = index(before_dir.path(), before);
    let after_conn = index(after_dir.path(), after);

    let diff = diff_graphs(&before_conn, &after_conn).unwrap();

    assert_eq!(diff.summary.added_nodes, 1, "one added node (baz)");
    assert_eq!(diff.summary.removed_nodes, 1, "one removed node (old)");
    assert_eq!(diff.summary.changed_nodes, 1, "one changed node (foo)");
    assert_eq!(diff.summary.added_edges, 1, "one added edge (baz->foo)");
    assert_eq!(diff.summary.removed_edges, 1, "one removed edge (old->foo)");

    assert!(
        diff.added_nodes[0].qualified_name.contains("baz"),
        "added node is baz, got {:?}",
        diff.added_nodes[0].qualified_name
    );
    assert!(
        diff.removed_nodes[0].qualified_name.contains("old"),
        "removed node is old, got {:?}",
        diff.removed_nodes[0].qualified_name
    );
    assert!(
        diff.changed_nodes[0].qualified_name.contains("foo"),
        "changed node is foo, got {:?}",
        diff.changed_nodes[0].qualified_name
    );
    assert!(
        diff.changed_nodes[0].signature.contains("foo(y)"),
        "changed node carries the new signature, got {:?}",
        diff.changed_nodes[0].signature
    );
    let previous = diff.changed_nodes[0]
        .previous
        .as_ref()
        .expect("changed node carries previous metadata");
    assert_eq!(previous.start_line, 1);
    assert_eq!(previous.signature, "def foo():");

    assert!(diff.added_edges[0].source.contains("baz"));
    assert!(diff.added_edges[0].target.contains("foo"));
    assert!(diff.removed_edges[0].source.contains("old"));
    assert!(diff.removed_edges[0].target.contains("foo"));
}

#[test]
fn duplicate_symbol_added_is_not_missed() {
    // Regression: node identity groups by (file, qualified_name, kind), so a second
    // identical `def hook()` must not be swallowed by a set-based fingerprint.
    let before = "def hook():\n    pass\n";
    let after = "def hook():\n    pass\n\ndef hook():\n    pass\n";

    let before_dir = tempfile::tempdir().unwrap();
    let after_dir = tempfile::tempdir().unwrap();
    let before_conn = index(before_dir.path(), before);
    let after_conn = index(after_dir.path(), after);

    let diff = diff_graphs(&before_conn, &after_conn).unwrap();

    // The added duplicate surfaces (as a change under the shared key), never silently.
    assert!(
        diff.summary.changed_nodes > 0,
        "adding a duplicate symbol must register, got {:?}",
        diff.summary
    );
    assert!(
        diff.changed_nodes
            .iter()
            .all(|n| n.qualified_name.contains("hook"))
    );
}

#[test]
fn line_shift_reports_removed_and_added_call_edge() {
    let before = "\
def target():
    return 1

def caller():
    return target()
";
    let after = "\
# inserted above the unchanged call site
def target():
    return 1

def caller():
    return target()
";

    let before_dir = tempfile::tempdir().unwrap();
    let after_dir = tempfile::tempdir().unwrap();
    let before_conn = index(before_dir.path(), before);
    let after_conn = index(after_dir.path(), after);

    let diff = diff_graphs(&before_conn, &after_conn).unwrap();

    assert_eq!(diff.summary.added_nodes, 0);
    assert_eq!(diff.summary.removed_nodes, 0);
    assert_eq!(diff.summary.changed_nodes, 0);

    // Raw edges stay line-sensitive for inspection; the line-insensitive summary
    // cancels an unchanged call pair whose only difference is the line number.
    assert_eq!(diff.summary.added_edges, 1);
    assert_eq!(diff.summary.removed_edges, 1);
    assert_eq!(diff.summary.added_edges_line_insensitive, 0);
    assert_eq!(diff.summary.removed_edges_line_insensitive, 0);
    assert_eq!(diff.added_edges.len(), 1);
    assert_eq!(diff.removed_edges.len(), 1);
    assert_eq!(diff.removed_edges[0].source, "caller");
    assert_eq!(diff.removed_edges[0].target, "target");
    assert_eq!(diff.removed_edges[0].line, 5);
    assert_eq!(diff.added_edges[0].source, "caller");
    assert_eq!(diff.added_edges[0].target, "target");
    assert_eq!(diff.added_edges[0].line, 6);
}

#[test]
fn body_only_change_with_same_signature_and_span_is_a_changed_node() {
    // Fixed in v3: per-symbol body_hash catches edits that keep signature and
    // span unchanged.
    let before = "\
def value():
    total = 1
    return total
";
    let after = "\
def value():
    total = 2
    return total
";

    let before_dir = tempfile::tempdir().unwrap();
    let after_dir = tempfile::tempdir().unwrap();
    let before_conn = index(before_dir.path(), before);
    let after_conn = index(after_dir.path(), after);

    let diff = diff_graphs(&before_conn, &after_conn).unwrap();

    assert_eq!(diff.summary.changed_nodes, 1);
    assert_eq!(diff.changed_nodes[0].qualified_name, "value");
    let previous = diff.changed_nodes[0]
        .previous
        .as_ref()
        .expect("changed node carries previous metadata");
    assert_eq!(previous.start_line, 1);
    assert_eq!(previous.signature, "def value():");
}

#[test]
fn identical_graphs_diff_empty() {
    let src = "\
def foo():
    pass

def bar():
    foo()
";
    let a_dir = tempfile::tempdir().unwrap();
    let b_dir = tempfile::tempdir().unwrap();
    let a = index(a_dir.path(), src);
    let b = index(b_dir.path(), src);

    let diff = diff_graphs(&a, &b).unwrap();

    assert_eq!(diff.summary.added_nodes, 0);
    assert_eq!(diff.summary.removed_nodes, 0);
    assert_eq!(diff.summary.changed_nodes, 0);
    assert_eq!(diff.summary.added_edges, 0);
    assert_eq!(diff.summary.removed_edges, 0);
}

#[test]
fn sync_upgrades_v2_schema_and_populates_body_hashes() {
    let dir = tempfile::tempdir().unwrap();
    fs::write(dir.path().join("mod.py"), "def value():\n    return 1\n").unwrap();

    let db = dir.path().join("g.db");
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, dir.path()).unwrap();
    conn.execute(
        "UPDATE meta SET value = '2' WHERE key = 'schema_version'",
        [],
    )
    .unwrap();
    conn.execute("ALTER TABLE symbols DROP COLUMN body_hash", [])
        .unwrap();

    let summary = sync_repo(&conn, dir.path()).unwrap();

    assert!(!summary.unchanged, "upgrade must force a reindex pass");
    assert_eq!(
        meta::read(&conn, "schema_version").unwrap().as_deref(),
        Some("3")
    );
    let body_hash: Option<String> = conn
        .query_row(
            "SELECT body_hash FROM symbols WHERE qualified_name = 'value'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert!(
        body_hash.as_deref().is_some_and(|hash| hash.len() == 64),
        "body_hash was not populated: {body_hash:?}"
    );
}

#[test]
fn diff_reads_old_schema_without_body_hash_column() {
    fn old_db() -> rusqlite::Connection {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        conn.execute_batch(
            r#"
            CREATE TABLE files (
                path TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                size INTEGER NOT NULL,
                modified_at INTEGER NOT NULL,
                indexed_at INTEGER NOT NULL,
                language TEXT NOT NULL
            );
            CREATE TABLE symbols (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                signature TEXT NOT NULL,
                container TEXT,
                content_hash TEXT NOT NULL
            );
            CREATE TABLE edges (
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                kind TEXT NOT NULL,
                line INTEGER,
                PRIMARY KEY(source, target, kind, line)
            );
            "#,
        )
        .unwrap();
        conn.execute(
            "INSERT INTO files(path, content_hash, size, modified_at, indexed_at, language)
             VALUES ('mod.py', 'file', 1, 1, 1, 'python')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO symbols(id, kind, name, qualified_name, file_path, start_line, end_line, signature, content_hash)
             VALUES ('s', 'function', 'value', 'value', 'mod.py', 1, 3, 'def value():', 'file')",
            [],
        )
        .unwrap();
        conn
    }

    let before = old_db();
    let after = old_db();

    let diff = diff_graphs(&before, &after).unwrap();

    assert_eq!(diff.summary.changed_nodes, 0);
}

#[test]
fn diff_mixed_v2_v3_falls_back_to_signature_and_span_when_body_hash_missing() {
    fn old_db() -> rusqlite::Connection {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        conn.execute_batch(
            r#"
            CREATE TABLE files (
                path TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                size INTEGER NOT NULL,
                modified_at INTEGER NOT NULL,
                indexed_at INTEGER NOT NULL,
                language TEXT NOT NULL
            );
            CREATE TABLE symbols (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                signature TEXT NOT NULL,
                container TEXT,
                content_hash TEXT NOT NULL
            );
            CREATE TABLE edges (
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                kind TEXT NOT NULL,
                line INTEGER,
                PRIMARY KEY(source, target, kind, line)
            );
            "#,
        )
        .unwrap();
        conn.execute(
            "INSERT INTO files(path, content_hash, size, modified_at, indexed_at, language)
             VALUES ('mod.py', 'file', 1, 1, 1, 'python')",
            [],
        )
        .unwrap();
        for (id, name, start_line) in [("a", "alpha", 1), ("b", "beta", 5)] {
            conn.execute(
                "INSERT INTO symbols(id, kind, name, qualified_name, file_path, start_line, end_line, signature, content_hash)
                 VALUES (?1, 'function', ?2, ?2, 'mod.py', ?3, ?4, ?5, 'file')",
                rusqlite::params![
                    id,
                    name,
                    start_line,
                    start_line + 2,
                    format!("def {name}():")
                ],
            )
            .unwrap();
        }
        conn
    }

    let new_source = "\
def alpha():
    value = 20
    return value

def beta():
    value = 40
    return value
";
    let new_dir = tempfile::tempdir().unwrap();
    let new_conn = index(new_dir.path(), new_source);
    let old_before = old_db();
    let old_after = old_db();

    let old_to_new = diff_graphs(&old_before, &new_conn).unwrap();
    let new_to_old = diff_graphs(&new_conn, &old_after).unwrap();

    assert_eq!(old_to_new.summary.changed_nodes, 0);
    assert_eq!(new_to_old.summary.changed_nodes, 0);
    assert!(old_to_new.changed_nodes.is_empty());
    assert!(new_to_old.changed_nodes.is_empty());
}
