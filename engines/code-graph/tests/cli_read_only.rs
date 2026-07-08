//! CLI query commands must open the graph db read-only.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::SystemTime;

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

fn snapshot_file(path: &Path) -> (Vec<u8>, SystemTime) {
    let bytes = fs::read(path).unwrap();
    let modified = fs::metadata(path).unwrap().modified().unwrap();
    (bytes, modified)
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
fn diff_reports_missing_input_db_errors() {
    let dir = tempfile::tempdir().unwrap();
    let existing_db = build_db(dir.path());

    for (missing_flag, before, after) in [
        (
            "--before",
            dir.path().join("missing-before.db"),
            existing_db.clone(),
        ),
        (
            "--after",
            existing_db.clone(),
            dir.path().join("missing-after.db"),
        ),
    ] {
        let output = Command::new(graphtrail())
            .args([
                "diff",
                "--before",
                &before.display().to_string(),
                "--after",
                &after.display().to_string(),
                "--json",
            ])
            .output()
            .unwrap();

        assert!(
            !output.status.success(),
            "diff unexpectedly succeeded with missing {missing_flag}: {output:?}"
        );
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            stderr.contains("failed to open")
                && stderr.contains("read-only")
                && stderr.contains(missing_flag.trim_start_matches("--")),
            "diff missing {missing_flag} error was not clear: {stderr:?}"
        );
    }
}

#[test]
fn diff_does_not_mutate_input_db_files() {
    let before_dir = tempfile::tempdir().unwrap();
    let after_dir = tempfile::tempdir().unwrap();
    let before_db = build_db(before_dir.path());
    let after_db = build_db(after_dir.path());
    let before_snapshot = snapshot_file(&before_db);
    let after_snapshot = snapshot_file(&after_db);

    let output = Command::new(graphtrail())
        .args([
            "diff",
            "--before",
            &before_db.display().to_string(),
            "--after",
            &after_db.display().to_string(),
            "--json",
        ])
        .output()
        .unwrap();

    assert!(output.status.success(), "diff failed: {output:?}");
    assert_eq!(
        before_snapshot,
        snapshot_file(&before_db),
        "diff mutated the before db file"
    );
    assert_eq!(
        after_snapshot,
        snapshot_file(&after_db),
        "diff mutated the after db file"
    );
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

#[cfg(all(feature = "codesearch", feature = "miseledger"))]
#[test]
fn context_help_lists_join_layer_flags() {
    let output = Command::new(graphtrail())
        .args(["context", "--help"])
        .output()
        .unwrap();

    assert!(output.status.success(), "{output:?}");
    let help = String::from_utf8(output.stdout).unwrap();
    assert!(help.contains("--blend-code-search"));
    assert!(help.contains("--evidence"));
}
