use std::fs;
use std::path::Path;
use std::process::Command;

use brigade_code_graph::store::schema::graph_revision;
use brigade_code_graph::store::{init_schema, open_db, open_read_only};
use rusqlite::Connection;
use serde_json::Value;
use tempfile::TempDir;

fn engine(db: &Path, root: &Path, args: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_brigade-code-graph"))
        .arg("--db")
        .arg(db)
        .args(args)
        .current_dir(root)
        .output()
        .unwrap()
}

fn json(db: &Path, root: &Path, args: &[&str]) -> Value {
    let result = engine(db, root, args);
    assert!(
        result.status.success(),
        "{args:?}: {}",
        String::from_utf8_lossy(&result.stderr)
    );
    serde_json::from_slice(&result.stdout).unwrap()
}

#[derive(Debug, PartialEq, Eq)]
struct Revision {
    generation: String,
    counter: u64,
    id: String,
}

impl Revision {
    fn read(value: &Value) -> Self {
        Self {
            generation: value["graph_generation"].as_str().unwrap().into(),
            counter: value["graph_revision"].as_u64().unwrap(),
            id: value["graph_revision_id"].as_str().unwrap().into(),
        }
    }

    fn advanced_from(&self, before: &Self) {
        assert_eq!(self.generation, before.generation);
        assert_eq!(self.counter, before.counter + 1);
        assert_ne!(self.id, before.id);
    }
}

fn sync(db: &Path, root: &Path) -> Value {
    json(db, root, &["sync", "--json"])
}

fn revision(db: &Path, root: &Path) -> Revision {
    Revision::read(&json(db, root, &["revision", "--json"]))
}

fn git(root: &Path, args: &[&str]) -> Vec<u8> {
    let result = Command::new("git")
        .current_dir(root)
        .args(args)
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{args:?}: {}",
        String::from_utf8_lossy(&result.stderr)
    );
    result.stdout
}

#[test]
fn revision_sync_lifecycle_and_git_exclude() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    let db = root.join("index.db");
    git(root, &["init", "-q"]);
    let initial = sync(&db, root);
    let mut previous = Revision::read(&initial);
    assert_eq!(previous, revision(&db, root));

    let file = root.join("app.py");
    // Keep this inventory aligned with graph-changing sync paths.
    for (operation, source, files, skipped) in [
        ("add", Some(&b"def added(): pass\n"[..]), 1, 0),
        ("modify", Some(&b"def modified(): pass\n"[..]), 1, 0),
        ("delete", None, 0, 0),
        ("skip", Some(&b"\xff\n"[..]), 0, 1),
        ("prune skipped", None, 0, 0),
        ("re-add", Some(&b"def added(): pass\n"[..]), 1, 0),
    ] {
        match source {
            Some(source) => fs::write(&file, source).unwrap(),
            None => fs::remove_file(&file).unwrap(),
        }
        let summary = sync(&db, root);
        assert_eq!(summary["files"], files, "{operation}");
        assert_eq!(summary["skipped"], skipped, "{operation}");
        if operation == "prune skipped" {
            assert_eq!(summary["unchanged"], true);
        }
        let next = Revision::read(&summary);
        next.advanced_from(&previous);
        let unchanged = sync(&db, root);
        assert_eq!(unchanged["unchanged"], true);
        assert_eq!(Revision::read(&unchanged), next);
        previous = next;
    }

    git(root, &["add", "app.py"]);
    let tree = git(root, &["write-tree"]);
    fs::write(root.join(".git/info/exclude"), "app.py\n").unwrap();
    assert_eq!(git(root, &["write-tree"]), tree);
    let excluded = sync(&db, root);
    assert_eq!(excluded["deleted"], 1);
    assert_eq!(excluded["files"], 0);
    Revision::read(&excluded).advanced_from(&previous);
}

fn symbol_count(conn: &Connection) -> i64 {
    conn.query_row("SELECT count(*) FROM symbols", [], |row| row.get(0))
        .unwrap()
}

#[test]
fn revision_status_reads_committed_wal_snapshot_without_mutating_main_file() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    let db = root.join("index.db");
    let missing = engine(&db, root, &["revision", "--json"]);
    assert!(!missing.status.success());
    assert!(!db.exists());

    let initial = Revision::read(&sync(&db, root));
    let reader = open_read_only(&db).unwrap();
    let snapshot = reader.unchecked_transaction().unwrap();
    assert_eq!(symbol_count(&snapshot), 0);
    assert_eq!(
        graph_revision(&snapshot).unwrap().graph_revision_id,
        initial.id
    );

    fs::write(root.join("app.py"), "def added(): pass\n").unwrap();
    let next = Revision::read(&sync(&db, root));
    next.advanced_from(&initial);
    assert_eq!(symbol_count(&snapshot), 0);
    assert_eq!(
        graph_revision(&snapshot).unwrap().graph_revision_id,
        initial.id
    );
    snapshot.commit().unwrap();
    assert_eq!(symbol_count(&reader), 1);

    let writer = open_db(&db).unwrap();
    let tx = writer.unchecked_transaction().unwrap();
    tx.execute("DELETE FROM symbols", []).unwrap();
    tx.execute("UPDATE graph_revision SET counter = counter + 1", [])
        .unwrap();
    // A competing sync lock plus an actual SQLite writer must not block status.
    let _lock = brigade_code_graph::store::lock::SyncLock::acquire(&db).unwrap();
    let bytes = fs::read(&db).unwrap();
    let mtime = fs::metadata(&db).unwrap().modified().unwrap();
    assert_eq!(revision(&db, root), next);
    assert_eq!(fs::read(&db).unwrap(), bytes);
    assert_eq!(fs::metadata(&db).unwrap().modified().unwrap(), mtime);
    assert_eq!(symbol_count(&reader), 1);
    tx.rollback().unwrap();
}

// The fixture can use a raw file copy only after all connections close.
fn copy_quiescent_database(source: &Path, destination: &Path) {
    for db in [source, destination] {
        assert!(!db.with_extension("db-wal").exists());
        assert!(!db.with_extension("db-shm").exists());
    }
    fs::copy(source, destination).unwrap();
}

// A restored backup can report the original revision for that exact snapshot.
// A subsequent write on the restored history must get a different identity.
#[test]
fn revision_restore_divergence_and_rebuild() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    let db = root.join("index.db");
    let backup = root.join("backup.db");
    let file = root.join("app.py");
    fs::write(&file, "def baseline(): pass\n").unwrap();
    let baseline = Revision::read(&sync(&db, root));
    copy_quiescent_database(&db, &backup);

    fs::write(&file, "def first_history(): pass\n").unwrap();
    sync(&db, root);
    let first = graph_revision(&open_db(&db).unwrap()).unwrap();
    copy_quiescent_database(&backup, &db);
    assert_eq!(revision(&db, root), baseline);

    fs::write(&file, "def second_history(): pass\n").unwrap();
    sync(&db, root);
    let second = graph_revision(&open_read_only(&db).unwrap()).unwrap();
    assert_eq!(first.graph_revision, second.graph_revision);
    assert_eq!(first.graph_generation, second.graph_generation);
    assert_ne!(first.graph_revision_id, second.graph_revision_id);
    assert!(first.same_snapshot(&first));
    assert!(!first.same_snapshot(&second));
    assert_ne!(first.token(), second.token());

    let forced = Revision::read(&json(&db, root, &["sync", "--force", "--json"]));
    assert_ne!(forced.generation, second.graph_generation);
    fs::remove_file(&db).unwrap();
    let rebuilt = Revision::read(&sync(&db, root));
    assert_ne!(rebuilt.generation, forced.generation);
}

#[test]
fn revision_migration_and_resolver_repair() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    let db = root.join("index.db");
    fs::write(root.join("app.py"), "def added(): pass\n").unwrap();
    let initial = Revision::read(&sync(&db, root));
    let conn = open_db(&db).unwrap();

    // A pre-revision v9 graph retains its indexed data during migration.
    conn.execute_batch(
        "DROP TABLE graph_revision;
         UPDATE meta SET value = '9' WHERE key = 'schema_version';",
    )
    .unwrap();
    assert!(!engine(&db, root, &["revision", "--json"]).status.success());
    assert_eq!(symbol_count(&conn), 1);
    let summary = sync(&db, root);
    assert_eq!(summary["unchanged"], true);
    assert_eq!(symbol_count(&conn), 1);
    let migrated = Revision::read(&summary);
    assert_ne!(initial.generation, migrated.generation);
    assert_eq!(revision(&db, root), migrated);

    conn.execute("DELETE FROM meta WHERE key = 'resolver_version'", [])
        .unwrap();
    let repaired = Revision::read(&sync(&db, root));
    repaired.advanced_from(&migrated);

    // A pre-v10 binary writes the older schema marker with its graph changes.
    conn.execute_batch(
        "BEGIN;
         DELETE FROM symbols;
         UPDATE meta SET value = '9' WHERE key = 'schema_version';
         COMMIT;",
    )
    .unwrap();
    assert!(!engine(&db, root, &["revision", "--json"]).status.success());
    let after_old_writer = Revision::read(&sync(&db, root));
    assert_ne!(after_old_writer.generation, repaired.generation);

    // Exercise the pre-v9 parse-census upgrade against the same fixture shape.
    conn.execute_batch(
        "ALTER TABLE files DROP COLUMN parse_errors;
         DROP TABLE skipped_files;
         UPDATE meta SET value = '7' WHERE key = 'schema_version';",
    )
    .unwrap();
    let summary = sync(&db, root);
    assert_eq!(summary["unchanged"], false);
    assert_eq!(symbol_count(&conn), 1);
    let upgraded = Revision::read(&summary);
    assert_ne!(upgraded.generation, after_old_writer.generation);

    // Schema init itself is idempotent on a current database.
    init_schema(&conn).unwrap();
    assert_eq!(revision(&db, root), upgraded);
}

#[test]
fn revision_rolls_back_with_graph_and_covers_schema_repair() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    let db = root.join("index.db");
    let initial = Revision::read(&sync(&db, root));
    let conn = open_db(&db).unwrap();

    // Schema initialization is a writer if it repairs a missing graph table.
    conn.execute("DROP TABLE module_assignments", []).unwrap();
    init_schema(&conn).unwrap();
    let repaired = revision(&db, root);
    repaired.advanced_from(&initial);

    // Simulate a sync failure after graph rows and revision have been updated.
    conn.execute_batch(
        "CREATE TRIGGER fail_sync_meta BEFORE UPDATE ON meta
         WHEN NEW.key = 'synced_at'
         BEGIN SELECT RAISE(ABORT, 'simulated provenance failure'); END;",
    )
    .unwrap();
    fs::write(root.join("app.py"), "def added(): pass\n").unwrap();
    assert!(!engine(&db, root, &["sync", "--json"]).status.success());
    assert_eq!(symbol_count(&conn), 0);
    assert_eq!(revision(&db, root), repaired);

    conn.execute("DROP TRIGGER fail_sync_meta", []).unwrap();
    Revision::read(&sync(&db, root)).advanced_from(&repaired);
    assert_eq!(symbol_count(&conn), 1);
}

#[test]
fn revision_mcp_refresh_advances_only_when_requested() {
    use brigade_code_graph::mcp::handle_request;
    use serde_json::json;

    let dir = TempDir::new().unwrap();
    let root = dir.path();
    git(root, &["init", "-q"]);
    let db = root.join(".brigade/code-graph/code-graph.db");
    // A fresh sync also writes a .gitignore notice. Its JSON stays parseable.
    let initial = Revision::read(&sync(&db, root));
    fs::write(root.join("app.py"), "def added(): pass\n").unwrap();
    for refresh in [false, true] {
        let response = handle_request(
            &db,
            &json!({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "search", "arguments": {
                    "query": "added", "repo": root, "refresh": refresh
                }}
            }),
        )
        .unwrap();
        assert_eq!(response["result"]["isError"], false, "{response}");
        let current = revision(&db, root);
        if refresh {
            current.advanced_from(&initial);
        } else {
            assert_eq!(current, initial);
        }
    }
}

#[cfg(feature = "watch")]
#[test]
fn revision_watch_initial_sync_advances() {
    use std::io::{BufRead, BufReader};
    use std::process::Stdio;

    let dir = TempDir::new().unwrap();
    let root = dir.path();
    let db = root.join("index.db");
    let initial = Revision::read(&sync(&db, root));
    fs::write(root.join("app.py"), "def added(): pass\n").unwrap();

    let mut child = Command::new(env!("CARGO_BIN_EXE_brigade-code-graph"))
        .arg("--db")
        .arg(&db)
        .arg("watch")
        .current_dir(root)
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut line = String::new();
    let result = BufReader::new(child.stdout.take().unwrap()).read_line(&mut line);
    let _ = child.kill();
    child.wait().unwrap();
    result.unwrap();
    assert!(line.starts_with("watching "), "{line}");
    revision(&db, root).advanced_from(&initial);
}

fn assert_cli_error(output: &std::process::Output, message: &str) {
    assert!(!output.status.success(), "{output:?}");
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains(message), "{stderr}");
}

#[test]
fn revision_requires_a_current_sync_marker() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    let db = root.join("index.db");
    assert!(engine(&db, root, &["init"]).status.success());
    assert_cli_error(
        &engine(&db, root, &["revision", "--json"]),
        "graph revision unavailable",
    );

    let mut previous = Revision::read(&sync(&db, root));
    let conn = open_db(&db).unwrap();
    for marker in [None, Some("9-dev"), Some("9"), Some("010")] {
        conn.execute("DELETE FROM meta WHERE key = 'schema_version'", [])
            .unwrap();
        if let Some(marker) = marker {
            conn.execute("INSERT INTO meta VALUES ('schema_version', ?1)", [marker])
                .unwrap();
        }
        assert_cli_error(
            &engine(&db, root, &["revision", "--json"]),
            "graph revision unavailable",
        );
        let current = Revision::read(&sync(&db, root));
        assert_ne!(current.generation, previous.generation, "{marker:?}");
        assert_eq!(revision(&db, root), current);
        previous = current;
    }
}

#[test]
fn newer_schema_is_refused_without_modifying_the_database() {
    use brigade_code_graph::mcp::handle_request;
    use serde_json::json;

    let dir = TempDir::new().unwrap();
    let root = dir.path();
    let db = root.join(".brigade/code-graph/code-graph.db");
    fs::write(root.join("app.py"), "def added(): pass\n").unwrap();
    sync(&db, root);
    let conn = Connection::open(&db).unwrap();
    // A database newer than this build need not use WAL or today's tables.
    conn.pragma_update(None, "journal_mode", "DELETE").unwrap();
    conn.execute_batch(
        "UPDATE meta SET value = '11' WHERE key = 'schema_version';
         DROP TABLE module_assignments;",
    )
    .unwrap();
    let bytes = fs::read(&db).unwrap();
    let mtime = fs::metadata(&db).unwrap().modified().unwrap();
    let message = "database schema is newer than this engine";
    let error = init_schema(&conn).unwrap_err();
    assert!(error.to_string().contains(message), "{error:#}");
    drop(conn);

    for args in [&["init"][..], &["sync", "--json"], &["revision", "--json"]] {
        assert_cli_error(&engine(&db, root, args), message);
        assert_eq!(fs::read(&db).unwrap(), bytes, "{args:?}");
        assert_eq!(fs::metadata(&db).unwrap().modified().unwrap(), mtime);
    }

    let response = handle_request(
        &db,
        &json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "search", "arguments": {"query": "added", "refresh": true}}}),
    )
    .unwrap();
    // The refresh fails, while the existing MCP query contract can still answer.
    let content = response["result"]["content"][0]["text"].as_str().unwrap();
    assert!(content.contains("refresh_error:"), "{response}");
    assert!(content.contains(message), "{response}");
    assert_eq!(fs::read(&db).unwrap(), bytes);
    assert_eq!(fs::metadata(&db).unwrap().modified().unwrap(), mtime);

    #[cfg(feature = "watch")]
    {
        assert_cli_error(&engine(&db, root, &["watch"]), message);
        assert_eq!(fs::read(&db).unwrap(), bytes);
        assert_eq!(fs::metadata(&db).unwrap().modified().unwrap(), mtime);
    }
}

#[cfg(unix)]
#[test]
fn revision_reports_when_wal_sidecars_are_inaccessible() {
    use std::os::unix::fs::PermissionsExt;

    let dir = TempDir::new().unwrap();
    let root = dir.path();
    let db = root.join("index.db");
    sync(&db, root);
    assert!(!db.with_extension("db-wal").exists());
    assert!(!db.with_extension("db-shm").exists());
    let original = fs::metadata(root).unwrap().permissions();
    fs::set_permissions(root, fs::Permissions::from_mode(0o500)).unwrap();
    let output = engine(&db, root, &["revision", "--json"]);
    fs::set_permissions(root, original).unwrap();

    // Root can create sidecars even without directory write permission.
    if unsafe { libc::geteuid() } != 0 {
        assert_cli_error(&output, "SQLite WAL sidecars");
        assert!(!String::from_utf8_lossy(&output.stderr).contains("run sync"));
    }
}

#[test]
fn revision_text_reports_the_same_fields_as_json() {
    let dir = TempDir::new().unwrap();
    let root = dir.path();
    let db = root.join("index.db");
    sync(&db, root);
    let fields = json(&db, root, &["revision", "--json"]);
    let output = engine(&db, root, &["revision"]);
    assert!(output.status.success(), "{output:?}");
    let stdout = String::from_utf8(output.stdout).unwrap();
    let lines: Vec<_> = stdout.lines().collect();
    assert_eq!(lines.len(), 4);
    for (key, value) in fields.as_object().unwrap() {
        let rendered = value
            .as_str()
            .map_or_else(|| value.to_string(), str::to_owned);
        assert!(lines.contains(&format!("{key}: {rendered}").as_str()));
    }
}

#[test]
fn refusing_a_newer_schema_does_not_checkpoint_its_wal() {
    use rusqlite::config::DbConfig;

    let dir = TempDir::new().unwrap();
    let root = dir.path();
    let db = root.join("index.db");
    sync(&db, root);
    let writer = Connection::open(&db).unwrap();
    writer
        .set_db_config(DbConfig::SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, true)
        .unwrap();
    writer
        .execute(
            "UPDATE meta SET value = '11' WHERE key = 'schema_version'",
            [],
        )
        .unwrap();
    drop(writer);
    let wal = db.with_extension("db-wal");
    let main_bytes = fs::read(&db).unwrap();
    let wal_bytes = fs::read(&wal).unwrap();
    assert!(!wal_bytes.is_empty());

    for args in [&["init"][..], &["sync", "--json"], &["revision", "--json"]] {
        assert_cli_error(&engine(&db, root, args), "database schema is newer");
        assert_eq!(fs::read(&db).unwrap(), main_bytes, "{args:?}");
        assert_eq!(fs::read(&wal).unwrap(), wal_bytes, "{args:?}");
    }
}
