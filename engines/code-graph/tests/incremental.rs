//! Integration tests for incremental sync: no-op when unchanged, rebuild on change, purge on delete.

use std::collections::BTreeSet;
use std::fs;
use std::path::Path;
use std::thread::sleep;
use std::time::Duration;

use graphtrail::extractors::{python, rust};
use graphtrail::store::{SCHEMA_VERSION, init_schema, meta, open_db, sync_repo};
use rusqlite::Connection;

#[test]
fn second_sync_is_noop_then_change_and_delete_are_detected() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    fs::write(root.join("a.py"), "def helper():\n    return 1\n").unwrap();
    fs::write(root.join("b.py"), "def run():\n    return 1\n").unwrap();

    let conn = open_db(&root.join("g.db")).unwrap();
    init_schema(&conn).unwrap();

    let first = sync_repo(&conn, root).unwrap();
    assert!(!first.unchanged);
    assert_eq!(first.files, 2);

    // Nothing changed -> no-op.
    let second = sync_repo(&conn, root).unwrap();
    assert!(second.unchanged, "second sync should be a no-op");
    assert_eq!(second.files, 2);

    // Modify a file (sleep so mtime advances at 1s resolution) -> rebuild.
    sleep(Duration::from_millis(1100));
    fs::write(
        root.join("a.py"),
        "def helper():\n    return 2\n\ndef extra():\n    return 9\n",
    )
    .unwrap();
    let third = sync_repo(&conn, root).unwrap();
    assert!(!third.unchanged, "modified file should trigger a rebuild");

    // Delete a file -> purge its rows and report it.
    fs::remove_file(root.join("b.py")).unwrap();
    let fourth = sync_repo(&conn, root).unwrap();
    assert!(!fourth.unchanged);
    assert_eq!(fourth.deleted, 1);
    let remaining: i64 = conn
        .query_row("SELECT COUNT(*) FROM files WHERE path = 'b.py'", [], |r| {
            r.get(0)
        })
        .unwrap();
    assert_eq!(remaining, 0, "deleted file rows should be purged");

    // And now it's a no-op again.
    let fifth = sync_repo(&conn, root).unwrap();
    assert!(fifth.unchanged);
}

#[test]
fn changed_sync_refreshes_synced_at() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    fs::write(root.join("a.py"), "def helper():\n    return 1\n").unwrap();

    let conn = open_db(&root.join("g.db")).unwrap();
    init_schema(&conn).unwrap();

    let first = sync_repo(&conn, root).unwrap();
    assert!(!first.unchanged);
    let first_synced_at: i64 = meta::read(&conn, "synced_at")
        .unwrap()
        .expect("synced_at after first sync")
        .parse()
        .unwrap();

    sleep(Duration::from_millis(1100));
    fs::write(
        root.join("a.py"),
        "def helper():\n    return 2\n\ndef extra():\n    return 9\n",
    )
    .unwrap();

    let second = sync_repo(&conn, root).unwrap();
    assert!(!second.unchanged);
    let second_synced_at: i64 = meta::read(&conn, "synced_at")
        .unwrap()
        .expect("synced_at after changed sync")
        .parse()
        .unwrap();

    assert!(second_synced_at > first_synced_at);
}

#[test]
fn unchanged_sync_refreshes_synced_at_without_reindexing_files() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    fs::write(root.join("a.py"), "def helper():\n    return 1\n").unwrap();

    let conn = open_db(&root.join("g.db")).unwrap();
    init_schema(&conn).unwrap();

    let first = sync_repo(&conn, root).unwrap();
    assert!(!first.unchanged);
    let first_synced_at: i64 = meta::read(&conn, "synced_at")
        .unwrap()
        .expect("synced_at after first sync")
        .parse()
        .unwrap();
    let first_indexed_at: i64 = conn
        .query_row(
            "SELECT indexed_at FROM files WHERE path = 'a.py'",
            [],
            |row| row.get(0),
        )
        .unwrap();

    sleep(Duration::from_millis(1100));

    let second = sync_repo(&conn, root).unwrap();
    assert!(second.unchanged);
    let second_synced_at: i64 = meta::read(&conn, "synced_at")
        .unwrap()
        .expect("synced_at after unchanged sync")
        .parse()
        .unwrap();
    let second_indexed_at: i64 = conn
        .query_row(
            "SELECT indexed_at FROM files WHERE path = 'a.py'",
            [],
            |row| row.get(0),
        )
        .unwrap();

    assert!(second_synced_at > first_synced_at);
    assert_eq!(second_indexed_at, first_indexed_at);
}

#[test]
fn old_extractor_fingerprint_reextracts_file_with_same_content() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join("a.py"), "def helper():\n    return 1\n");

    let conn = open_graph(root);
    let first = sync_repo(&conn, root).unwrap();
    assert!(!first.unchanged);
    let first_indexed_at = indexed_at(&conn, "a.py");

    conn.execute(
        "UPDATE files SET extractor_fingerprint = 'python-old' WHERE path = 'a.py'",
        [],
    )
    .unwrap();
    sleep(Duration::from_millis(1100));

    let second = sync_repo(&conn, root).unwrap();

    assert!(!second.unchanged, "old extractor fingerprint is stale");
    assert!(indexed_at(&conn, "a.py") > first_indexed_at);
    assert_eq!(
        extractor_fingerprint(&conn, "a.py").as_deref(),
        Some(python::EXTRACTOR_FINGERPRINT)
    );
}

#[test]
fn only_doctored_language_row_is_reextracted() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join("lib.rs"), "fn helper() {}\n");
    write_file(root.join("app.py"), "def helper():\n    return 1\n");

    let conn = open_graph(root);
    let first = sync_repo(&conn, root).unwrap();
    assert!(!first.unchanged);
    let rust_indexed_at = indexed_at(&conn, "lib.rs");
    let python_indexed_at = indexed_at(&conn, "app.py");

    conn.execute(
        "UPDATE files SET extractor_fingerprint = 'rust-old' WHERE path = 'lib.rs'",
        [],
    )
    .unwrap();
    sleep(Duration::from_millis(1100));

    let second = sync_repo(&conn, root).unwrap();

    assert!(!second.unchanged);
    assert!(indexed_at(&conn, "lib.rs") > rust_indexed_at);
    assert_eq!(indexed_at(&conn, "app.py"), python_indexed_at);
    assert_eq!(
        extractor_fingerprint(&conn, "lib.rs").as_deref(),
        Some(rust::EXTRACTOR_FINGERPRINT)
    );
    assert_eq!(
        extractor_fingerprint(&conn, "app.py").as_deref(),
        Some(python::EXTRACTOR_FINGERPRINT)
    );
}

#[test]
fn sync_migrates_v3_files_table_and_populates_extractor_fingerprint() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join("a.py"), "def helper():\n    return 1\n");

    let conn = open_graph(root);
    let first = sync_repo(&conn, root).unwrap();
    assert!(!first.unchanged);
    conn.execute(
        "UPDATE meta SET value = '3' WHERE key = 'schema_version'",
        [],
    )
    .unwrap();
    conn.execute("ALTER TABLE files DROP COLUMN extractor_fingerprint", [])
        .unwrap();

    let second = sync_repo(&conn, root).unwrap();

    assert!(!second.unchanged);
    assert_eq!(
        meta::read(&conn, "schema_version").unwrap().as_deref(),
        Some(SCHEMA_VERSION.to_string().as_str())
    );
    assert_eq!(
        extractor_fingerprint(&conn, "a.py").as_deref(),
        Some(python::EXTRACTOR_FINGERPRINT)
    );
}

#[test]
fn sync_honors_root_gitignore_patterns() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    make_git_repo(root);
    write_file(root.join(".gitignore"), "vendor/\n*.gen.py\n");
    write_file(root.join("app.py"), "def kept():\n    return 1\n");
    write_file(
        root.join("vendor/pkg.py"),
        "def vendored():\n    return 1\n",
    );
    write_file(
        root.join("schema.gen.py"),
        "def generated():\n    return 1\n",
    );

    let conn = open_graph(root);
    let summary = sync_repo(&conn, root).unwrap();

    assert_eq!(summary.files, 1);
    assert_eq!(indexed_paths(&conn), paths(["app.py"]));
}

#[test]
fn sync_honors_nested_gitignore_patterns() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    make_git_repo(root);
    write_file(root.join("pkg/.gitignore"), "generated/\n");
    write_file(root.join("pkg/kept.py"), "def kept():\n    return 1\n");
    write_file(
        root.join("pkg/generated/noise.py"),
        "def ignored():\n    return 1\n",
    );

    let conn = open_graph(root);
    let summary = sync_repo(&conn, root).unwrap();

    assert_eq!(summary.files, 1);
    assert_eq!(indexed_paths(&conn), paths(["pkg/kept.py"]));
}

#[test]
fn non_git_root_ignores_only_the_hardcoded_floor() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join(".gitignore"), "ignored.py\n");
    write_file(root.join("kept.py"), "def kept():\n    return 1\n");
    write_file(
        root.join("ignored.py"),
        "def still_indexed():\n    return 1\n",
    );
    write_file(
        root.join("vendor/pkg.py"),
        "def vendored():\n    return 1\n",
    );
    write_file(
        root.join("venv/lib/site.py"),
        "def skipped():\n    return 1\n",
    );
    write_file(
        root.join("node_modules/pkg/index.js"),
        "export function skipped() { return 1 }\n",
    );

    let conn = open_graph(root);
    let summary = sync_repo(&conn, root).unwrap();

    assert_eq!(summary.files, 3);
    assert_eq!(
        indexed_paths(&conn),
        paths(["ignored.py", "kept.py", "vendor/pkg.py"])
    );
}

#[test]
fn file_removed_by_new_gitignore_rule_self_cleans_from_db() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    make_git_repo(root);
    write_file(root.join("app.py"), "def kept():\n    return 1\n");
    write_file(
        root.join("vendor/pkg.py"),
        "def vendored():\n    return 1\n",
    );

    let conn = open_graph(root);
    let first = sync_repo(&conn, root).unwrap();
    assert_eq!(first.files, 2);
    assert_eq!(indexed_paths(&conn), paths(["app.py", "vendor/pkg.py"]));

    write_file(root.join(".gitignore"), "vendor/\n");
    let second = sync_repo(&conn, root).unwrap();

    assert!(!second.unchanged);
    assert_eq!(second.deleted, 1);
    assert_eq!(indexed_paths(&conn), paths(["app.py"]));
}

#[test]
fn first_graphtrail_index_in_git_repo_adds_root_gitignore_entry() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    make_git_repo(root);
    write_file(root.join("app.py"), "def kept():\n    return 1\n");

    let conn = open_graphtrail_graph(root);
    let summary = sync_repo(&conn, root).unwrap();

    assert!(!summary.unchanged);
    assert_eq!(
        fs::read_to_string(root.join(".gitignore")).unwrap(),
        ".graphtrail/\n"
    );
}

#[test]
fn second_graphtrail_sync_does_not_duplicate_gitignore_entry() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    make_git_repo(root);
    write_file(root.join("app.py"), "def kept():\n    return 1\n");

    let conn = open_graphtrail_graph(root);
    sync_repo(&conn, root).unwrap();
    sync_repo(&conn, root).unwrap();

    assert_eq!(
        fs::read_to_string(root.join(".gitignore")).unwrap(),
        ".graphtrail/\n"
    );
}

#[test]
fn first_graphtrail_index_respects_covering_gitignore_pattern() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    make_git_repo(root);
    write_file(root.join(".gitignore"), ".graphtrail*\n");
    write_file(root.join("app.py"), "def kept():\n    return 1\n");

    let conn = open_graphtrail_graph(root);
    sync_repo(&conn, root).unwrap();

    assert_eq!(
        fs::read_to_string(root.join(".gitignore")).unwrap(),
        ".graphtrail*\n"
    );
}

#[test]
fn first_graphtrail_index_in_non_git_root_does_not_write_gitignore() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join("app.py"), "def kept():\n    return 1\n");

    let conn = open_graphtrail_graph(root);
    sync_repo(&conn, root).unwrap();

    assert!(!root.join(".gitignore").exists());
}

#[test]
fn preexisting_graphtrail_dir_does_not_write_gitignore() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    make_git_repo(root);
    fs::create_dir(root.join(".graphtrail")).unwrap();
    write_file(root.join("app.py"), "def kept():\n    return 1\n");

    let conn = open_graphtrail_graph(root);
    sync_repo(&conn, root).unwrap();

    assert!(!root.join(".gitignore").exists());
}

#[test]
fn hidden_paths_are_indexed() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    make_git_repo(root);
    write_file(
        root.join(".github/workflows/check.py"),
        "def hidden_workflow():\n    return 1\n",
    );
    write_file(
        root.join(".hidden.py"),
        "def hidden_file():\n    return 1\n",
    );

    let conn = open_graph(root);
    let summary = sync_repo(&conn, root).unwrap();

    assert_eq!(summary.files, 2);
    assert_eq!(
        indexed_paths(&conn),
        paths([".github/workflows/check.py", ".hidden.py"])
    );
}

fn make_git_repo(root: &Path) {
    fs::create_dir_all(root.join(".git")).unwrap();
    fs::write(root.join(".git/HEAD"), "ref: refs/heads/main\n").unwrap();
}

fn open_graph(root: &Path) -> Connection {
    let conn = open_db(&root.join("g.db")).unwrap();
    init_schema(&conn).unwrap();
    conn
}

fn open_graphtrail_graph(root: &Path) -> Connection {
    let conn = open_db(&root.join(".graphtrail").join("graphtrail.db")).unwrap();
    init_schema(&conn).unwrap();
    conn
}

fn write_file(path: impl AsRef<Path>, content: &str) {
    let path = path.as_ref();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, content).unwrap();
}

fn indexed_paths(conn: &Connection) -> BTreeSet<String> {
    let mut stmt = conn
        .prepare("SELECT path FROM files ORDER BY path")
        .unwrap();
    stmt.query_map([], |row| row.get::<_, String>(0))
        .unwrap()
        .map(|row| row.unwrap())
        .collect()
}

fn indexed_at(conn: &Connection, path: &str) -> i64 {
    conn.query_row(
        "SELECT indexed_at FROM files WHERE path = ?1",
        [path],
        |row| row.get(0),
    )
    .unwrap()
}

fn extractor_fingerprint(conn: &Connection, path: &str) -> Option<String> {
    conn.query_row(
        "SELECT extractor_fingerprint FROM files WHERE path = ?1",
        [path],
        |row| row.get(0),
    )
    .unwrap()
}

fn paths<const N: usize>(paths: [&str; N]) -> BTreeSet<String> {
    paths.into_iter().map(str::to_owned).collect()
}
