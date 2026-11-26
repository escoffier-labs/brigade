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
fn revision_status_reads_committed_wal_snapshot_and_never_writes() {
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
    // A competing sync lock plus an actual SQLite writer must not block status.
    let _lock = brigade_code_graph::store::lock::SyncLock::acquire(&db).unwrap();
    let bytes = fs::read(&db).unwrap();
    assert_eq!(revision(&db, root), next);
    assert_eq!(fs::read(&db).unwrap(), bytes);
    assert_eq!(symbol_count(&reader), 1);
    tx.rollback().unwrap();
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
    fs::copy(&db, &backup).unwrap();

    fs::write(&file, "def first_history(): pass\n").unwrap();
    let first = Revision::read(&sync(&db, root));
    fs::copy(&backup, &db).unwrap();
    assert_eq!(revision(&db, root), baseline);

    fs::write(&file, "def second_history(): pass\n").unwrap();
    let second = Revision::read(&sync(&db, root));
    assert_eq!(first.counter, second.counter);
    assert_eq!(first.generation, second.generation);
    assert_ne!(first.id, second.id);

    let forced = Revision::read(&json(&db, root, &["sync", "--force", "--json"]));
    assert_ne!(forced.generation, second.generation);
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

    // Schema init itself is idempotent on a current database.
    init_schema(&conn).unwrap();
    assert_eq!(revision(&db, root), after_old_writer);
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
