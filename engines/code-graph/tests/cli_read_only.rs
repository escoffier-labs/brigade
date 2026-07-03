//! CLI query commands must open the graph db read-only.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use graphtrail::store::{init_schema, open_db, sync_repo};
use sha2::{Digest, Sha256};

fn graphtrail() -> &'static str {
    env!("CARGO_BIN_EXE_graphtrail")
}

fn command_args<'a>(db: &'a Path, command: &'a str) -> Vec<String> {
    let db = db.display().to_string();
    match command {
        "search" => vec!["--db".into(), db, "search".into(), "helper".into()],
        "callers" => vec!["--db".into(), db, "callers".into(), "helper".into()],
        "callees" => vec!["--db".into(), db, "callees".into(), "run".into()],
        "impact" => vec!["--db".into(), db, "impact".into(), "helper".into()],
        "context" => vec!["--db".into(), db, "context".into(), "helper".into()],
        "neighbors" => vec!["--db".into(), db, "neighbors".into(), "app.py".into()],
        "stats" => vec!["--db".into(), db, "stats".into()],
        other => panic!("unknown query command: {other}"),
    }
}

fn build_db(root: &Path) -> PathBuf {
    fs::write(
        root.join("app.py"),
        r#"
def helper():
    return 1

def run():
    return helper()
"#,
    )
    .unwrap();
    let db = root.join(".graphtrail").join("graphtrail.db");
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();
    conn.pragma_update(None, "wal_checkpoint", "TRUNCATE")
        .unwrap();
    drop(conn);
    db
}

/// Snapshot every file under `root` except SQLite's own `-wal`/`-shm` sidecars: a read-only
/// WAL connection may (re)create empty sidecars, which is standard SQLite operation, not a
/// mutation of graph state.
fn snapshot_tree(root: &Path) -> BTreeMap<PathBuf, (usize, String)> {
    let mut out = BTreeMap::new();
    if !root.exists() {
        return out;
    }
    let mut stack = vec![root.to_path_buf()];
    while let Some(path) = stack.pop() {
        for entry in fs::read_dir(path).unwrap() {
            let entry = entry.unwrap();
            let path = entry.path();
            let name = path.to_string_lossy().to_string();
            if name.ends_with("-wal") || name.ends_with("-shm") {
                continue;
            }
            if path.is_dir() {
                stack.push(path);
            } else {
                let bytes = fs::read(&path).unwrap();
                let digest = Sha256::digest(&bytes);
                out.insert(
                    path.strip_prefix(root).unwrap().to_path_buf(),
                    (bytes.len(), format!("{digest:x}")),
                );
            }
        }
    }
    out
}

#[test]
fn query_commands_do_not_create_default_db_state_when_missing() {
    for command in [
        "search",
        "callers",
        "callees",
        "impact",
        "context",
        "neighbors",
        "stats",
    ] {
        let dir = tempfile::tempdir().unwrap();
        let mut cmd = Command::new(graphtrail());
        cmd.current_dir(dir.path());
        match command {
            "search" => cmd.args(["search", "helper"]),
            "callers" => cmd.args(["callers", "helper"]),
            "callees" => cmd.args(["callees", "run"]),
            "impact" => cmd.args(["impact", "helper"]),
            "context" => cmd.args(["context", "helper"]),
            "neighbors" => cmd.args(["neighbors", "app.py"]),
            "stats" => cmd.arg("stats"),
            other => panic!("unknown query command: {other}"),
        };

        let output = cmd.output().unwrap();

        assert!(
            !output.status.success(),
            "{command} unexpectedly succeeded without a db"
        );
        assert!(
            !dir.path().join(".graphtrail").exists(),
            "{command} created default graph db state"
        );
    }
}

#[test]
fn query_commands_do_not_mutate_existing_db_state() {
    for command in [
        "search",
        "callers",
        "callees",
        "impact",
        "context",
        "neighbors",
        "stats",
    ] {
        let dir = tempfile::tempdir().unwrap();
        let db = build_db(dir.path());
        let graph_dir = db.parent().unwrap();
        let before = snapshot_tree(graph_dir);

        let output = Command::new(graphtrail())
            .current_dir(dir.path())
            .args(command_args(&db, command))
            .output()
            .unwrap();

        assert!(output.status.success(), "{command} failed: {output:?}");
        assert_eq!(
            before,
            snapshot_tree(graph_dir),
            "{command} mutated graph db state"
        );
    }
}
