use std::fs;
use std::io::Write;
use std::path::Path;
use std::process::{Command, Output};

use brigade_code_graph::query::digest::graph_digest;
use brigade_code_graph::store::{SCHEMA_VERSION, init_schema, meta, open_db, sync_repo};
use rusqlite::{Connection, MAIN_DB, config::DbConfig};
use serde_json::{Value, json};

fn index(path: &Path, root: &Path) -> Connection {
    fs::write(
        root.join("app.py"),
        "def target():\n    return 1\n\ndef caller():\n    return target()\n",
    )
    .unwrap();
    let conn = open_db(path).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();
    conn
}

fn command(db: &Path, root: &Path, args: &[&str]) -> Command {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_brigade-code-graph"));
    cmd.arg("--db").arg(db).args(args).current_dir(root);
    cmd
}

fn digest(db: &Path, root: &Path) -> Value {
    let output = command(db, root, &["digest", "--json"]).output().unwrap();
    assert!(output.status.success(), "{output:?}");
    serde_json::from_slice(&output.stdout).unwrap()
}

fn assert_failure(output: Output, message: &str) {
    assert!(!output.status.success(), "{output:?}");
    assert!(output.stdout.is_empty(), "{output:?}");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains(message),
        "{output:?}"
    );
}

#[test]
fn digest_reflects_blob_level_row_changes() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("graph.db");
    let conn = index(&db, dir.path());
    let before = digest(&db, dir.path());
    let (rowid, hash): (i64, String) = conn
        .query_row(
            "SELECT rowid, body_hash FROM symbols WHERE body_hash IS NOT NULL LIMIT 1",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    let first = hash.as_bytes()[0];
    let changed = if first == b'0' { b'1' } else { b'0' };
    for value in [changed, first] {
        let mut field = conn
            .blob_open(MAIN_DB, "symbols", "body_hash", rowid, false)
            .unwrap();
        field.write_all(&[value]).unwrap();
        field.close().unwrap();
        assert_eq!(digest(&db, dir.path()) == before, value == first);
    }
}

#[test]
fn digest_reads_committed_state_during_a_writer_and_changes_after_commit() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("graph.db");
    let mut writer = index(&db, dir.path());
    let before = digest(&db, dir.path());
    let tx = writer.transaction().unwrap();
    tx.execute("UPDATE symbols SET body_hash = 'pending'", [])
        .unwrap();
    let lock = brigade_code_graph::store::lock::SyncLock::acquire(&db).unwrap();
    let bytes = fs::read(&db).unwrap();
    let mtime = fs::metadata(&db).unwrap().modified().unwrap();
    // This process cannot finish successfully unless it reads through the lock
    // and sees the committed WAL snapshot before the writer commits.
    assert_eq!(digest(&db, dir.path()), before);
    assert_eq!(fs::read(&db).unwrap(), bytes);
    assert_eq!(fs::metadata(&db).unwrap().modified().unwrap(), mtime);
    drop(lock);

    tx.commit().unwrap();
    assert_ne!(digest(&db, dir.path()), before);
}

#[test]
fn digest_rejects_missing_old_and_locked_databases() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("graph.db");
    let run = || {
        command(&db, dir.path(), &["digest", "--json"])
            .output()
            .unwrap()
    };
    assert_failure(run(), "failed to open");
    assert!(!db.exists());
    let conn = Connection::open(&db).unwrap();
    init_schema(&conn).unwrap();
    assert_failure(run(), "graph digest requires a database synced");
    meta::upsert(&conn, "schema_version", &(SCHEMA_VERSION - 1).to_string()).unwrap();
    assert_failure(run(), "graph digest requires a database synced");
    conn.execute_batch("BEGIN EXCLUSIVE").unwrap();
    assert_failure(run(), "database is locked");
    conn.execute_batch("ROLLBACK").unwrap();
    fs::write(dir.path().join("unreadable.db"), "not a database").unwrap();
    let output = command(
        &dir.path().join("unreadable.db"),
        dir.path(),
        &["digest", "--json"],
    )
    .output()
    .unwrap();
    assert_failure(output, "file is not a database");
}

#[cfg(unix)]
#[test]
fn digest_reports_inaccessible_wal_sidecars() {
    use std::os::unix::{fs::PermissionsExt, process::CommandExt};

    let dir = tempfile::tempdir_in("/tmp").unwrap();
    let db = dir.path().join("graph.db");
    drop(index(&db, dir.path()));
    assert!(!db.with_extension("db-wal").exists());
    let mut cmd = command(&db, dir.path(), &["digest", "--json"]);
    if unsafe { libc::geteuid() } == 0 {
        // Establish the same permissions precondition under root-run tests.
        let executable = dir.path().join("engine");
        fs::copy(env!("CARGO_BIN_EXE_brigade-code-graph"), &executable).unwrap();
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o755)).unwrap();
        cmd = Command::new(executable);
        cmd.current_dir(dir.path())
            .arg("--db")
            .arg(&db)
            .args(["digest", "--json"]);
        cmd.uid(65534).gid(65534);
        fs::set_permissions(&db, fs::Permissions::from_mode(0o644)).unwrap();
    }
    let original = fs::metadata(dir.path()).unwrap().permissions();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o555)).unwrap();
    let output = cmd.output().unwrap();
    fs::set_permissions(dir.path(), original).unwrap();
    assert_failure(output, "SQLite WAL sidecars");
}

#[test]
fn newer_schema_is_rejected_at_read_and_write_entry_points() {
    use brigade_code_graph::mcp::handle_request;
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("graph.db");
    let conn = index(&db, dir.path());
    conn.set_db_config(DbConfig::SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, true)
        .unwrap();
    meta::upsert(&conn, "schema_version", &(SCHEMA_VERSION + 1).to_string()).unwrap();
    conn.execute("DROP TABLE module_assignments", []).unwrap();
    let message = "database schema is newer than this engine";
    assert!(
        init_schema(&conn)
            .unwrap_err()
            .to_string()
            .contains(message)
    );
    drop(conn);
    // The newer schema exists only in the WAL. Refusal must not checkpoint it.
    let wal = db.with_extension("db-wal");
    let bytes = fs::read(&db).unwrap();
    let wal_bytes = fs::read(&wal).unwrap();
    let mtime = fs::metadata(&db).unwrap().modified().unwrap();
    assert!(!wal_bytes.is_empty());
    let mut options = vec![
        vec!["init"],
        vec!["sync"],
        vec!["digest", "--json"],
        vec!["stats"],
    ];
    if cfg!(feature = "watch") {
        options.push(vec!["watch"]);
    }
    for args in options {
        assert_failure(command(&db, dir.path(), &args).output().unwrap(), message);
    }
    for refresh in [false, true] {
        let response = handle_request(
            &db,
            &json!({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "search", "arguments": {"query": "target", "refresh": refresh}},
            }),
        )
        .unwrap();
        assert!(response.to_string().contains(message), "{response}");
    }
    assert_eq!(fs::read(&db).unwrap(), bytes);
    assert_eq!(fs::read(&wal).unwrap(), wal_bytes);
    assert_eq!(fs::metadata(&db).unwrap().modified().unwrap(), mtime);
}

#[test]
fn writable_open_obeys_the_sync_lock_before_changing_journal_mode() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("graph.db");
    let conn = Connection::open(&db).unwrap();
    init_schema(&conn).unwrap();
    let bytes = fs::read(&db).unwrap();
    let lock = brigade_code_graph::store::lock::SyncLock::acquire(&db).unwrap();
    assert!(open_db(&db).unwrap_err().to_string().contains("lock"));
    assert_eq!(fs::read(&db).unwrap(), bytes);
    drop(lock);
    assert!(open_db(&db).is_ok());
}

#[test]
fn digest_distinguishes_fields_the_diff_can_observe() {
    for sql in [
        "UPDATE symbols SET kind='class' WHERE name='caller'",
        "UPDATE symbols SET qualified_name='renamed' WHERE name='caller'",
        "UPDATE symbols SET signature='changed' WHERE name='caller'",
        "UPDATE symbols SET start_line=start_line+1 WHERE name='caller'",
        "UPDATE symbols SET end_line=end_line+1 WHERE name='caller'",
        "UPDATE symbols SET body_hash='changed' WHERE name='caller'",
        "UPDATE symbols SET id='changed' WHERE name='target'",
        "UPDATE symbols SET file_path='other.py' WHERE name='caller'",
        "UPDATE edges SET line=line+1",
        "UPDATE edges SET kind='other'",
        "UPDATE edges SET source=target",
        "UPDATE edges SET target=source",
    ] {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("graph.db");
        let conn = index(&db, dir.path());
        let before = graph_digest(&conn).unwrap();
        conn.pragma_update(None, "foreign_keys", false).unwrap();
        assert!(conn.execute(sql, []).unwrap() > 0, "{sql}");
        assert_ne!(graph_digest(&conn).unwrap(), before, "{sql}");
    }
}

#[test]
fn digest_distinguishes_storage_classes_and_orders_nullable_keys() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("graph.db");
    let conn = index(&db, dir.path());
    // The BLOB affinity preserves the supplied storage class of every value.
    conn.execute("ALTER TABLE files ADD COLUMN extra BLOB", [])
        .unwrap();
    let mut seen = std::collections::BTreeSet::new();
    for sql in ["NULL", "''", "1", "'1'", "x'31'"] {
        conn.execute(&format!("UPDATE files SET extra={sql}"), [])
            .unwrap();
        assert!(seen.insert(graph_digest(&conn).unwrap().sha256));
    }

    let id: String = conn
        .query_row("SELECT id FROM symbols LIMIT 1", [], |row| row.get(0))
        .unwrap();
    let insert = |confidence| {
        conn.execute(
            "INSERT INTO edges(source,target,kind,line,confidence) VALUES(?1,?1,'other',NULL,?2)",
            rusqlite::params![id, confidence],
        )
        .unwrap();
    };
    insert(0.1);
    insert(0.2);
    let before = graph_digest(&conn).unwrap();
    conn.execute("DELETE FROM edges WHERE kind='other'", [])
        .unwrap();
    insert(0.2);
    insert(0.1);
    assert_eq!(graph_digest(&conn).unwrap(), before);
}
