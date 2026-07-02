//! Integration tests for incremental sync: no-op when unchanged, rebuild on change, purge on delete.

use std::fs;
use std::thread::sleep;
use std::time::Duration;

use graphtrail::store::{init_schema, meta, open_db, sync_repo};

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
