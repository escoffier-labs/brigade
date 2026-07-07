//! End-to-end test for `graphtrail diff`: build two indexed DBs from a before/after
//! version of a small repo and assert the structural diff.

use std::fs;

use graphtrail::query::diff_graphs;
use graphtrail::store::{init_schema, open_db, sync_repo};

/// Sync `source` (a single mod.py) into a fresh DB under `dir` and return the connection.
fn index(dir: &std::path::Path, source: &str) -> rusqlite::Connection {
    fs::write(dir.join("mod.py"), source).unwrap();
    let conn = open_db(&dir.join("g.db")).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, dir).unwrap();
    conn
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
