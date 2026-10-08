//! The relevance floor is opt-in on `graphtrail context` (brigade#1648).

use std::fs;
use std::path::Path;
use std::process::Command;

use graphtrail::store::{init_schema, open_db, sync_repo};
use serde_json::Value;

const TASK: &str = "Update the README install section to mention pipx";

fn indexed_repo(root: &Path) -> std::path::PathBuf {
    fs::write(
        root.join("accept.py"),
        "def _is_transient_pipx_install_error(error):\n    return False\n\n\
         def install_cli():\n    return _is_transient_pipx_install_error(None)\n",
    )
    .unwrap();
    let db = root.join("graph.db");
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();
    conn.pragma_update(None, "wal_checkpoint", "TRUNCATE")
        .unwrap();
    db
}

fn context(db: &Path, extra: &[&str]) -> String {
    let output = Command::new(env!("CARGO_BIN_EXE_graphtrail"))
        .arg("--db")
        .arg(db)
        .args(["context", TASK])
        .args(extra)
        .output()
        .unwrap();
    assert!(output.status.success(), "context failed: {output:?}");
    String::from_utf8(output.stdout).unwrap()
}

#[test]
fn context_without_the_flag_keeps_unfloored_keyword_hits() {
    let dir = tempfile::tempdir().unwrap();
    let db = indexed_repo(dir.path());

    let pack: Value = serde_json::from_str(&context(&db, &["--json"])).unwrap();
    assert!(!pack["entry_points"].as_array().unwrap().is_empty());
    assert_eq!(pack["confident"], Value::Bool(true));
    assert!(pack.get("relevance_floor").is_none());

    let markdown = context(&db, &["--markdown"]);
    assert!(!markdown.contains("relevance floor"), "{markdown}");
    assert!(
        markdown.contains("_is_transient_pipx_install_error"),
        "{markdown}"
    );
}

#[test]
fn context_with_the_flag_applies_the_floor() {
    let dir = tempfile::tempdir().unwrap();
    let db = indexed_repo(dir.path());

    let pack: Value =
        serde_json::from_str(&context(&db, &["--json", "--relevance-floor"])).unwrap();
    assert!(pack["entry_points"].as_array().unwrap().is_empty());
    assert_eq!(pack["confident"], Value::Bool(false));
    assert!(pack["relevance_floor"]["rule"].is_string());

    let markdown = context(&db, &["--markdown", "--relevance-floor"]);
    assert!(
        markdown.contains("No confident code context for this task."),
        "{markdown}"
    );
    assert!(markdown.contains("relevance floor"), "{markdown}");
}
