//! Content changes must invalidate the index even when file stats collide.

use std::fs;
use std::path::Path;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use graphtrail::query::{affected, diff_graphs, doctor, freshness, outline};
use graphtrail::store::{init_schema, open_db, open_read_only, sync_repo};
use rusqlite::Connection;

const BEFORE: &str = "def alpha():\n    return 1\n";
const AFTER: &str = "def bravo():\n    return 2\n";

fn set_modified(path: &Path, modified: SystemTime) {
    fs::File::options()
        .write(true)
        .open(path)
        .unwrap()
        .set_modified(modified)
        .unwrap();
}

fn fixture() -> (tempfile::TempDir, Connection) {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let source = root.join("sample.py");
    fs::write(&source, BEFORE).unwrap();
    set_modified(
        &source,
        UNIX_EPOCH + Duration::new(1_700_000_000, 100_000_000),
    );
    fs::create_dir(root.join("tests")).unwrap();
    fs::write(
        root.join("tests/test_sample.py"),
        "from sample import bravo\n\ndef test_bravo():\n    assert bravo() == 2\n",
    )
    .unwrap();
    let conn = open_db(&root.join(".graphtrail/graphtrail.db")).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();
    (dir, conn)
}

fn edit_in_same_second(root: &Path, content: &[u8]) {
    let source = root.join("sample.py");
    let before = fs::metadata(&source).unwrap();
    fs::write(&source, content).unwrap();
    set_modified(
        &source,
        UNIX_EPOCH + Duration::new(1_700_000_000, 900_000_000),
    );
    let after = fs::metadata(&source).unwrap();
    assert_eq!(before.len(), after.len(), "fixture must preserve file size");
    let seconds = |metadata: &fs::Metadata| {
        metadata
            .modified()
            .unwrap()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs()
    };
    assert_eq!(seconds(&before), seconds(&after));
}

fn symbol_names(conn: &Connection) -> Vec<String> {
    outline(conn, "sample.py")
        .unwrap()
        .into_iter()
        .map(|row| row.symbol.name)
        .collect()
}

#[test]
fn same_second_edit_is_stale_on_a_read_only_connection() {
    let (dir, _conn) = fixture();
    let root = dir.path();
    edit_in_same_second(root, AFTER.as_bytes());
    let db = root.join(".graphtrail/graphtrail.db");
    let read_only = open_read_only(&db).unwrap();
    read_only.pragma_update(None, "query_only", true).unwrap();

    let report = doctor(&read_only, root, &db).unwrap();
    assert_eq!(report.verdict, "STALE");
    assert_eq!(report.pending.changed_files, 1);
    let compact = freshness(&read_only, root).unwrap();
    assert_eq!(compact.verdict, "STALE");
    assert_eq!(compact.pending_files, 1);
    assert_eq!(symbol_names(&read_only), ["alpha"]);
}

#[test]
fn same_second_edit_rebuilds_symbols_and_affected_test_edges() {
    let (dir, conn) = fixture();
    let root = dir.path();
    edit_in_same_second(root, AFTER.as_bytes());

    let summary = sync_repo(&conn, root).unwrap();
    assert!(!summary.unchanged, "changed bytes must trigger a rebuild");
    assert_eq!(symbol_names(&conn), ["bravo"]);
    let report = affected(&conn, &["sample.py".to_string()], 3).unwrap();
    assert_eq!(report.affected_tests.len(), 1);
    assert_eq!(report.affected_tests[0].file_path, "tests/test_sample.py");
    assert_eq!(report.affected_tests[0].min_hops, 1);

    let cold = open_db(&root.join(".graphtrail/cold.db")).unwrap();
    init_schema(&cold).unwrap();
    sync_repo(&cold, root).unwrap();
    let diff = diff_graphs(&conn, &cold).unwrap();
    assert!(diff.added_nodes.is_empty(), "{diff:?}");
    assert!(diff.removed_nodes.is_empty(), "{diff:?}");
    assert!(diff.changed_nodes.is_empty(), "{diff:?}");
    assert!(diff.added_edges.is_empty(), "{diff:?}");
    assert!(diff.removed_edges.is_empty(), "{diff:?}");
    let compact = freshness(&conn, root).unwrap();
    assert_eq!(compact.verdict, "FRESH");
    assert_eq!(compact.pending_files, 0);
    assert!(sync_repo(&conn, root).unwrap().unchanged);
}

#[test]
fn equal_size_edit_with_the_exact_timestamp_restored_is_detected() {
    let (dir, conn) = fixture();
    let root = dir.path();
    let source = root.join("sample.py");
    let before = fs::metadata(&source).unwrap();
    fs::write(&source, AFTER).unwrap();
    set_modified(&source, before.modified().unwrap());
    let after = fs::metadata(&source).unwrap();
    assert_eq!(before.len(), after.len());
    assert_eq!(before.modified().unwrap(), after.modified().unwrap());

    let compact = freshness(&conn, root).unwrap();
    assert_eq!(compact.verdict, "STALE");
    assert_eq!(compact.pending_files, 1);
    assert!(!sync_repo(&conn, root).unwrap().unchanged);
    assert_eq!(symbol_names(&conn), ["bravo"]);
}

#[test]
fn same_second_invalid_utf8_replaces_the_old_graph_with_a_skip() {
    let (dir, conn) = fixture();
    let root = dir.path();
    let mut invalid = BEFORE.as_bytes().to_vec();
    invalid[0] = 0xff;
    edit_in_same_second(root, &invalid);

    assert_eq!(freshness(&conn, root).unwrap().pending_files, 1);
    let summary = sync_repo(&conn, root).unwrap();
    assert!(!summary.unchanged);
    assert_eq!(summary.skipped, 1);
    assert_eq!(summary.skipped_by_reason["unreadable_utf8"], 1);
    assert!(symbol_names(&conn).is_empty());
}

#[test]
fn timestamp_change_without_a_content_change_remains_a_noop() {
    let (dir, conn) = fixture();
    let root = dir.path();
    set_modified(
        &root.join("sample.py"),
        UNIX_EPOCH + Duration::from_secs(1_700_000_010),
    );

    let compact = freshness(&conn, root).unwrap();
    assert_eq!(compact.verdict, "FRESH");
    assert_eq!(compact.pending_files, 0);
    assert!(sync_repo(&conn, root).unwrap().unchanged);
    assert_eq!(symbol_names(&conn), ["alpha"]);
}
