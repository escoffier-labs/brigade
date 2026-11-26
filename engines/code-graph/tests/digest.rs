use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::time::{Duration, SystemTime};

use brigade_code_graph::query::{diff_graphs, digest::graph_digest};
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
    let wal = db.with_extension("db-wal");
    let before_files = [file_state(&db), file_state(&wal)];
    // A read-only digest succeeds while the advisory sync lock is held.
    assert_eq!(digest(&db, dir.path()), before);
    assert_eq!([file_state(&db), file_state(&wal)], before_files);
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

fn file_state(path: &Path) -> (Vec<u8>, SystemTime) {
    (
        fs::read(path).unwrap(),
        fs::metadata(path).unwrap().modified().unwrap(),
    )
}

fn graph_commands(db: &Path) -> Vec<Vec<&str>> {
    use clap::CommandFactory;
    let path = db.to_str().unwrap();
    let mut commands = vec![
        vec!["affected", "app.py"],
        vec!["callees", "target"],
        vec!["callers", "target"],
        vec!["context", "target"],
        vec!["cycles"],
        vec!["dead-code"],
        vec!["digest", "--json"],
        vec!["diff", "--before", path, "--after", path],
        vec!["doctor"],
        vec!["explain", "caller", "target"],
        vec!["export"],
        vec!["impact", "target"],
        vec!["init"],
        vec!["neighbors", "app.py"],
        vec!["outline", "app.py"],
        vec!["search", "target"],
        vec!["stats"],
        vec!["sync"],
    ];
    let mut non_graph = vec!["evaluate", "help"];
    if cfg!(feature = "watch") {
        commands.push(vec!["watch"]);
    }
    if cfg!(feature = "codesearch") {
        commands.push(vec!["blend", "target"]);
    }
    if cfg!(feature = "miseledger") {
        non_graph.push("links"); // Reads the evidence database instead.
    }
    let cli = brigade_code_graph::cli::Cli::command();
    let expected: std::collections::BTreeSet<_> = cli
        .get_subcommands()
        .map(clap::Command::get_name)
        .filter(|name| !non_graph.contains(name))
        .collect();
    assert_eq!(
        commands
            .iter()
            .map(|argv| argv[0])
            .collect::<std::collections::BTreeSet<_>>(),
        expected
    );
    commands
}

#[test]
fn invalid_or_newer_schema_is_rejected_at_every_graph_entry_point() {
    use brigade_code_graph::mcp::handle_request;
    for (marker, message) in [
        (
            (SCHEMA_VERSION + 1).to_string(),
            "database schema is newer than this engine",
        ),
        ("10.0".into(), "invalid database schema marker"),
        ("v10".into(), "invalid database schema marker"),
        (
            "4294967296".into(),
            "database schema is newer than this engine",
        ),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("graph.db");
        let conn = index(&db, dir.path());
        conn.set_db_config(DbConfig::SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, true)
            .unwrap();
        meta::upsert(&conn, "schema_version", &marker).unwrap();
        conn.execute("DROP TABLE module_assignments", []).unwrap();
        assert!(
            init_schema(&conn)
                .unwrap_err()
                .to_string()
                .contains(message)
        );
        drop(conn);

        // The changed marker lives in the WAL. No refusal may checkpoint it.
        let wal = db.with_extension("db-wal");
        let before = [file_state(&db), file_state(&wal)];
        assert!(!before[1].0.is_empty());
        for args in graph_commands(&db) {
            assert_failure(command(&db, dir.path(), &args).output().unwrap(), message);
            assert_eq!(
                [file_state(&db), file_state(&wal)],
                before,
                "{args:?}, {marker}"
            );
        }
        for refresh in [false, true] {
            let response = handle_request(&db, &json!({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "search", "arguments": {"query": "target", "refresh": refresh}},
            })).unwrap();
            assert!(response.to_string().contains(message), "{response}");
            assert_eq!([file_state(&db), file_state(&wal)], before);
        }
    }
}

#[test]
fn refusal_after_open_preserves_a_newer_database_and_wal_on_close() {
    for initialize in [false, true] {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("graph.db");
        drop(index(&db, dir.path()));
        let opened = open_db(&db).unwrap();
        let writer = Connection::open(&db).unwrap();
        writer
            .set_db_config(DbConfig::SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, true)
            .unwrap();
        meta::upsert(&writer, "schema_version", &(SCHEMA_VERSION + 1).to_string()).unwrap();
        drop(writer);

        let wal = db.with_extension("db-wal");
        let before = [file_state(&db), file_state(&wal)];
        assert!(!before[1].0.is_empty());
        let error = if initialize {
            init_schema(&opened).unwrap_err()
        } else {
            sync_repo(&opened, dir.path()).unwrap_err()
        };
        assert!(
            error
                .to_string()
                .contains("database schema is newer than this engine")
        );
        drop(opened);
        assert_eq!([file_state(&db), file_state(&wal)], before);
    }
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
    let output = command(&db, dir.path(), &["init"]).output().unwrap();
    assert_failure(output, "another writer holds this database");
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
        let snapshot = dir.path().join("before.db");
        conn.execute("VACUUM INTO ?1", [snapshot.to_str().unwrap()])
            .unwrap();
        conn.pragma_update(None, "foreign_keys", false).unwrap();
        assert!(conn.execute(sql, []).unwrap() > 0, "{sql}");
        assert_ne!(graph_digest(&conn).unwrap(), before, "{sql}");
        let changes = diff_graphs(&Connection::open(snapshot).unwrap(), &conn).unwrap();
        assert!(
            serde_json::to_value(changes).unwrap()["summary"]
                .as_object()
                .unwrap()
                .values()
                .any(|count| count != 0),
            "{sql}"
        );
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

#[test]
fn digest_is_stable_across_an_unchanged_sync() {
    let dir = tempfile::tempdir().unwrap();
    let conn = index(&dir.path().join("graph.db"), dir.path());
    let before = graph_digest(&conn).unwrap();
    std::thread::sleep(Duration::from_millis(1100));

    assert!(sync_repo(&conn, dir.path()).unwrap().unchanged);
    assert_eq!(graph_digest(&conn).unwrap(), before);
}

#[test]
fn forced_sync_can_change_the_digest_with_an_empty_diff() {
    let dir = tempfile::tempdir().unwrap();
    let conn = index(&dir.path().join("graph.db"), dir.path());
    let before = graph_digest(&conn).unwrap();
    let snapshot = dir.path().join("before.db");
    conn.execute("VACUUM INTO ?1", [snapshot.to_str().unwrap()])
        .unwrap();
    std::thread::sleep(Duration::from_millis(1100));

    brigade_code_graph::store::sync_repo_force(&conn, dir.path(), true).unwrap();
    assert_ne!(graph_digest(&conn).unwrap(), before);
    let diff = diff_graphs(&Connection::open(snapshot).unwrap(), &conn).unwrap();
    let diff = serde_json::to_value(diff).unwrap();
    assert!(
        diff["summary"]
            .as_object()
            .unwrap()
            .values()
            .all(|count| count == 0)
    );
}

#[test]
fn digest_reports_hot_rollback_journal_without_modifying_it() {
    const CHILD_DB: &str = "BRIGADE_TEST_ROLLBACK_CHILD_DB";
    if let Some(path) = std::env::var_os(CHILD_DB) {
        let writer = Connection::open(PathBuf::from(path)).unwrap();
        writer
            .execute_batch(
                "PRAGMA journal_mode=DELETE;
             PRAGMA synchronous=FULL;
             PRAGMA cache_size=1;
             BEGIN IMMEDIATE;
             UPDATE files SET content_hash=hex(zeroblob(131072));",
            )
            .unwrap();
        // Leave the transaction for SQLite's recovery on the next open.
        std::process::exit(0);
    }
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("graph.db");
    drop(index(&db, dir.path()));
    let child = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "digest_reports_hot_rollback_journal_without_modifying_it",
        ])
        .env(CHILD_DB, &db)
        .output()
        .unwrap();
    assert!(child.status.success(), "{child:?}");

    let journal = db.with_extension("db-journal");
    let expected = [file_state(&db), file_state(&journal)];
    assert_ne!(
        expected[1].0[0], 0,
        "journal header must be initialized for recovery"
    );
    let output = command(&db, dir.path(), &["digest", "--json"])
        .output()
        .unwrap();
    assert!(
        !String::from_utf8_lossy(&output.stderr).contains("WAL sidecars"),
        "{output:?}"
    );
    assert_failure(output, "rollback journal requires recovery");
    assert_eq!([file_state(&db), file_state(&journal)], expected);
}
