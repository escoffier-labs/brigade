//! CLI query commands must open the graph db read-only.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::SystemTime;

use graphtrail::store::{init_schema, open_db, sync_repo};
use rusqlite::params;
use sha2::{Digest, Sha256};

#[cfg(feature = "miseledger")]
const MISELEDGER_DEPRECATION_WARNING: &str = "warning: GraphTrail's direct MiseLedger adapter is deprecated. Use `brigade code sync`, `brigade code context`, `brigade code impact`, `brigade evidence crawl`, `brigade evidence search`, and `brigade evidence doctor` instead. The adapter remains functional for at least two minor GraphTrail releases or 90 days after the first GraphTrail release containing this deprecation, whichever is longer. It will not be removed before that compatibility policy is satisfied.";

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
        "doctor" => vec!["--db".into(), db, "doctor".into()],
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

fn build_map_db(root: &Path) -> PathBuf {
    let db = root.join(".graphtrail").join("graphtrail.db");
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();

    for path in [
        "src/caller.rs",
        "src/focus.rs",
        "src/callee.rs",
        "src/neighbor.rs",
    ] {
        conn.execute(
            "INSERT INTO files (path, content_hash, size, modified_at, indexed_at, language) \
             VALUES (?1, 'fixture', 0, 0, 0, 'rust')",
            [path],
        )
        .unwrap();
    }
    for (id, name, qualified_name, file_path, start_line) in [
        ("caller", "caller", "caller::entry", "src/caller.rs", 8),
        ("focus_a", "focus_a", "focus::alpha", "src/focus.rs", 4),
        ("focus_b", "focus_b", "focus::beta", "src/focus.rs", 18),
        ("callee", "callee", "callee::work", "src/callee.rs", 12),
        ("neighbor", "neighbor", "neighbor::deep", "src/neighbor.rs", 3),
    ] {
        conn.execute(
            "INSERT INTO symbols \
             (id, kind, name, qualified_name, file_path, start_line, end_line, signature, content_hash) \
             VALUES (?1, 'function', ?2, ?3, ?4, ?5, ?5, '', 'fixture')",
            params![id, name, qualified_name, file_path, start_line],
        )
        .unwrap();
    }
    for (source, target, line) in [
        ("caller", "focus_a", 21),
        ("focus_a", "callee", 9),
        ("focus_b", "callee", 22),
        ("callee", "neighbor", 16),
    ] {
        conn.execute(
            "INSERT INTO edges (source, target, kind, line, confidence) VALUES (?1, ?2, 'call', ?3, 1.0)",
            params![source, target, line],
        )
        .unwrap();
    }
    conn.pragma_update(None, "wal_checkpoint", "TRUNCATE")
        .unwrap();
    drop(conn);
    db
}

#[cfg(feature = "miseledger")]
fn build_miseledger_db(root: &Path) -> PathBuf {
    let db = root.join("miseledger.db");
    let conn = rusqlite::Connection::open(&db).unwrap();
    conn.execute_batch(
        "CREATE VIRTUAL TABLE item_fts USING fts5(item_id, source_kind, body);\
         INSERT INTO item_fts (item_id, source_kind, body) VALUES\
             ('evidence-helper', 'session', 'helper helper');",
    )
    .unwrap();
    conn.pragma_update(None, "wal_checkpoint", "TRUNCATE")
        .unwrap();
    db
}

#[cfg(feature = "miseledger")]
fn build_evidence_context_miseledger_db(root: &Path) -> PathBuf {
    let db = root.join("miseledger.db");
    let conn = rusqlite::Connection::open(&db).unwrap();
    conn.execute_batch(
        "CREATE VIRTUAL TABLE item_fts USING fts5(item_id, source_kind, body);\
         INSERT INTO item_fts (item_id, source_kind, body) VALUES\
             ('evidence-task', 'session', 'evidence'),\
             ('evidence-alpha', 'session', 'evidence_alpha'),\
             ('evidence-beta', 'session', 'evidence_beta');",
    )
    .unwrap();
    conn.pragma_update(None, "wal_checkpoint", "TRUNCATE")
        .unwrap();
    db
}

#[cfg(feature = "miseledger")]
fn build_evidence_context_db(root: &Path) -> PathBuf {
    fs::write(
        root.join("evidence.py"),
        r#"
def evidence_alpha():
    return 1

def evidence_beta():
    return evidence_alpha()
"#,
    )
    .unwrap();
    let db = root.join(".graphtrail").join("graphtrail.db");
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();
    conn.pragma_update(None, "wal_checkpoint", "TRUNCATE")
        .unwrap();
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

fn snapshot_graph_state(root: &Path) -> BTreeMap<PathBuf, (usize, String)> {
    let mut out = BTreeMap::new();
    if !root.exists() {
        return out;
    }
    for entry in fs::read_dir(root).unwrap() {
        let entry = entry.unwrap();
        let path = entry.path();
        if path.is_file() && !path.to_string_lossy().ends_with("-shm") {
            let bytes = fs::read(&path).unwrap();
            out.insert(
                path.file_name().unwrap().into(),
                (bytes.len(), format!("{:x}", Sha256::digest(&bytes))),
            );
        }
    }
    out
}

fn keep_wal_sidecars(db: &Path) -> rusqlite::Connection {
    let conn = open_db(db).unwrap();
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('map_cli_sidecar_fixture', '1')",
        [],
    )
    .unwrap();
    conn
}

fn map_data(html: &str) -> serde_json::Value {
    const OPEN: &str = r#"<script id="graphtrail-map-data" type="application/json">"#;
    const CLOSE: &str = "</script>";

    let start = html.find(OPEN).unwrap() + OPEN.len();
    let end = start + html[start..].find(CLOSE).unwrap();
    serde_json::from_str(&html[start..end]).unwrap()
}

#[test]
fn map_cli_writes_default_html_and_preserves_graph_state() {
    let dir = tempfile::tempdir().unwrap();
    let db = build_map_db(dir.path());
    let graph_dir = db.parent().unwrap();
    let output_path = dir.path().join("focus-map.html");
    let _sidecars = keep_wal_sidecars(&db);
    let before = snapshot_graph_state(graph_dir);

    let output = Command::new(graphtrail())
        .current_dir(dir.path())
        .args([
            "--db",
            &db.display().to_string(),
            "map",
            "src/focus.rs",
            "--out",
            &output_path.display().to_string(),
        ])
        .output()
        .unwrap();

    assert!(output.status.success(), "map failed: {output:?}");
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        format!("wrote {}\n", output_path.display())
    );
    let html = fs::read_to_string(&output_path).unwrap();
    let data = map_data(&html);
    assert_eq!(data["focus_path"], "src/focus.rs");
    assert_eq!(data["direction"], "neighbors");
    assert_eq!(data["depth"], 1);
    assert_eq!(data["status"]["rendered_nodes"], 4);
    assert_eq!(data["status"]["rendered_edges"], 3);
    assert_eq!(before, snapshot_graph_state(graph_dir));
}

#[test]
fn map_cli_embeds_custom_options_in_output_json() {
    let dir = tempfile::tempdir().unwrap();
    let db = build_map_db(dir.path());
    let output_path = dir.path().join("custom-map.html");

    let output = Command::new(graphtrail())
        .args([
            "--db",
            &db.display().to_string(),
            "map",
            "src/focus.rs",
            "--out",
            &output_path.display().to_string(),
            "--direction",
            "callees",
            "--depth",
            "2",
            "--max-nodes",
            "3",
            "--max-edges",
            "1",
        ])
        .output()
        .unwrap();

    assert!(output.status.success(), "map failed: {output:?}");
    let data = map_data(&fs::read_to_string(output_path).unwrap());
    assert_eq!(data["direction"], "callees");
    assert_eq!(data["depth"], 2);
    assert_eq!(data["nodes"].as_array().unwrap().len(), 3);
    assert_eq!(data["edges"].as_array().unwrap().len(), 1);
}

#[test]
fn map_cli_requires_out() {
    let dir = tempfile::tempdir().unwrap();
    let db = build_map_db(dir.path());

    let output = Command::new(graphtrail())
        .args(["--db", &db.display().to_string(), "map", "src/focus.rs"])
        .output()
        .unwrap();

    assert!(!output.status.success(), "map unexpectedly succeeded");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("--out <FILE>"),
        "missing --out diagnostic: {output:?}"
    );
}

#[test]
fn map_cli_reports_missing_indexed_focus_without_writing_or_mutating() {
    let dir = tempfile::tempdir().unwrap();
    let db = build_map_db(dir.path());
    let graph_dir = db.parent().unwrap();
    let output_path = dir.path().join("missing-map.html");
    let _sidecars = keep_wal_sidecars(&db);
    let before = snapshot_graph_state(graph_dir);

    let output = Command::new(graphtrail())
        .args([
            "--db",
            &db.display().to_string(),
            "map",
            "src/missing.rs",
            "--out",
            &output_path.display().to_string(),
        ])
        .output()
        .unwrap();

    assert!(!output.status.success(), "map unexpectedly succeeded");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("src/missing.rs is not an indexed focus file"),
        "missing focus diagnostic: {output:?}"
    );
    assert!(!output_path.exists(), "map wrote output after query failure");
    assert_eq!(before, snapshot_graph_state(graph_dir));
}

#[test]
fn map_cli_rejects_invalid_bounds_without_mutating_graph_state() {
    let dir = tempfile::tempdir().unwrap();
    let db = build_map_db(dir.path());
    let graph_dir = db.parent().unwrap();
    let before = snapshot_graph_state(graph_dir);

    for (option, value, expected) in [
        ("--depth", "0", "depth must be between 1 and 5"),
        ("--depth", "6", "depth must be between 1 and 5"),
        ("--max-nodes", "0", "max_nodes must be between 1 and 250"),
        ("--max-nodes", "251", "max_nodes must be between 1 and 250"),
        ("--max-edges", "0", "max_edges must be between 1 and 500"),
        ("--max-edges", "501", "max_edges must be between 1 and 500"),
    ] {
        let output_path = dir.path().join(format!("invalid-{value}.html"));
        let output = Command::new(graphtrail())
            .args([
                "--db",
                &db.display().to_string(),
                "map",
                "src/focus.rs",
                "--out",
                &output_path.display().to_string(),
                option,
                value,
            ])
            .output()
            .unwrap();

        assert!(!output.status.success(), "{option}={value} unexpectedly succeeded");
        assert!(
            String::from_utf8_lossy(&output.stderr).contains(expected),
            "invalid {option}={value} diagnostic: {output:?}"
        );
        assert!(!output_path.exists(), "map wrote output for {option}={value}");
        assert_eq!(before, snapshot_graph_state(graph_dir));
    }
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
        "doctor",
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
            "doctor" => cmd.arg("doctor"),
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
        "doctor",
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

#[test]
fn dead_code_plain_text_includes_confidence_and_reason() {
    let dir = tempfile::tempdir().unwrap();
    let db = build_db(dir.path());

    let output = Command::new(graphtrail())
        .current_dir(dir.path())
        .args(["--db", &db.display().to_string(), "dead-code"])
        .output()
        .unwrap();

    assert!(output.status.success(), "dead-code failed: {output:?}");
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(
        stdout.contains(
            "low function run app.py:5 def run(): - public/exported entry point may be called outside the indexed graph"
        ),
        "stdout: {stdout:?}"
    );
}

#[test]
fn doctor_fresh_synced_repo_reports_fresh() {
    let dir = tempfile::tempdir().unwrap();
    let db = build_db(dir.path());

    let output = Command::new(graphtrail())
        .current_dir(dir.path())
        .args(["--db", &db.display().to_string(), "doctor", "--json"])
        .output()
        .unwrap();

    assert!(output.status.success(), "doctor failed: {output:?}");
    let value: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(value["verdict"], "FRESH");
    assert_eq!(value["pending"]["new_files"], 0);
    assert_eq!(value["pending"]["changed_files"], 0);
    assert_eq!(value["pending"]["deleted_files"], 0);
    assert_eq!(value["pending"]["fingerprint_stale"], 0);
}

#[test]
fn doctor_new_file_reports_stale_exit_1() {
    let dir = tempfile::tempdir().unwrap();
    let db = build_db(dir.path());
    fs::write(dir.path().join("late.py"), "def late():\n    return 2\n").unwrap();

    let output = Command::new(graphtrail())
        .current_dir(dir.path())
        .args(["--db", &db.display().to_string(), "doctor", "--json"])
        .output()
        .unwrap();

    assert_eq!(output.status.code(), Some(1), "doctor output: {output:?}");
    let value: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(value["verdict"], "STALE");
    assert_eq!(value["pending"]["new_files"], 1);
    assert_eq!(value["pending"]["changed_files"], 0);
}

#[test]
fn doctor_null_fingerprint_reports_stale_fingerprint() {
    let dir = tempfile::tempdir().unwrap();
    let db = build_db(dir.path());
    {
        let conn = open_db(&db).unwrap();
        conn.execute(
            "UPDATE files SET extractor_fingerprint = NULL WHERE path = 'app.py'",
            [],
        )
        .unwrap();
        conn.pragma_update(None, "wal_checkpoint", "TRUNCATE")
            .unwrap();
    }

    let output = Command::new(graphtrail())
        .current_dir(dir.path())
        .args(["--db", &db.display().to_string(), "doctor", "--json"])
        .output()
        .unwrap();

    assert_eq!(output.status.code(), Some(1), "doctor output: {output:?}");
    let value: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(value["verdict"], "STALE");
    assert_eq!(value["pending"]["fingerprint_stale"], 1);
    assert_eq!(value["pending"]["new_files"], 0);
    assert_eq!(value["pending"]["changed_files"], 0);
}

#[test]
fn doctor_missing_db_exits_2_without_creating_state() {
    let dir = tempfile::tempdir().unwrap();

    let output = Command::new(graphtrail())
        .current_dir(dir.path())
        .args(["doctor", "--json"])
        .output()
        .unwrap();

    assert_eq!(output.status.code(), Some(2), "doctor output: {output:?}");
    assert!(
        !dir.path().join(".graphtrail").exists(),
        "doctor created default graph db state"
    );
}

#[test]
fn doctor_does_not_mutate_db_or_tree() {
    let dir = tempfile::tempdir().unwrap();
    let db = build_db(dir.path());
    let graph_dir = db.parent().unwrap();
    let before_db = snapshot_file(&db);
    let before_tree = snapshot_tree(dir.path());

    let output = Command::new(graphtrail())
        .current_dir(dir.path())
        .args(["--db", &db.display().to_string(), "doctor", "--json"])
        .output()
        .unwrap();

    assert!(output.status.success(), "doctor failed: {output:?}");
    assert_eq!(before_db, snapshot_file(&db), "doctor mutated the db file");
    assert_eq!(
        before_tree,
        snapshot_tree(dir.path()),
        "doctor mutated the repo tree"
    );
    assert_eq!(before_tree, snapshot_tree(graph_dir.parent().unwrap()));
}

#[cfg(feature = "miseledger")]
#[test]
fn deprecated_miseledger_commands_warn_once_without_changing_json_or_databases() {
    let graph_dir = tempfile::tempdir().unwrap();
    let evidence_dir = tempfile::tempdir().unwrap();
    let links_evidence_dir = tempfile::tempdir().unwrap();
    let graph_db = build_evidence_context_db(graph_dir.path());
    let evidence_db = build_evidence_context_miseledger_db(evidence_dir.path());
    let links_evidence_db = build_miseledger_db(links_evidence_dir.path());
    let before_graph = snapshot_tree(graph_dir.path());
    let before_evidence = snapshot_tree(evidence_dir.path());
    let before_links_evidence = snapshot_tree(links_evidence_dir.path());

    let context_without_evidence = Command::new(graphtrail())
        .current_dir(graph_dir.path())
        .args([
            "--db",
            &graph_db.display().to_string(),
            "context",
            "evidence",
            "--json",
        ])
        .env("MISELEDGER_DB", &evidence_db)
        .output()
        .unwrap();
    assert!(
        context_without_evidence.status.success(),
        "context without --evidence failed: {context_without_evidence:?}"
    );
    assert!(
        context_without_evidence.stderr.is_empty(),
        "context without --evidence warned: {:?}",
        String::from_utf8_lossy(&context_without_evidence.stderr)
    );

    let context_with_evidence = Command::new(graphtrail())
        .current_dir(graph_dir.path())
        .args([
            "--db",
            &graph_db.display().to_string(),
            "context",
            "evidence",
            "--json",
            "--evidence",
        ])
        .env("MISELEDGER_DB", &evidence_db)
        .output()
        .unwrap();
    assert!(
        context_with_evidence.status.success(),
        "context with --evidence failed: {context_with_evidence:?}"
    );
    assert_eq!(
        String::from_utf8(context_with_evidence.stderr).unwrap(),
        format!("{MISELEDGER_DEPRECATION_WARNING}\n")
    );
    assert_eq!(
        context_with_evidence.stdout, context_without_evidence.stdout,
        "--evidence must not change context JSON output"
    );
    let context_json: serde_json::Value =
        serde_json::from_slice(&context_with_evidence.stdout).unwrap();
    assert_eq!(context_json["task"], "evidence");

    let context_markdown_with_evidence = Command::new(graphtrail())
        .current_dir(graph_dir.path())
        .args([
            "--db",
            &graph_db.display().to_string(),
            "context",
            "evidence",
            "--markdown",
            "--evidence",
        ])
        .env("MISELEDGER_DB", &evidence_db)
        .output()
        .unwrap();
    assert!(
        context_markdown_with_evidence.status.success(),
        "markdown context with --evidence failed: {context_markdown_with_evidence:?}"
    );
    assert_eq!(
        String::from_utf8(context_markdown_with_evidence.stderr).unwrap(),
        format!("{MISELEDGER_DEPRECATION_WARNING}\n")
    );
    let markdown = String::from_utf8(context_markdown_with_evidence.stdout).unwrap();
    assert_eq!(markdown.matches("### `").count(), 3, "{markdown}");
    for expected_link in [
        "### `evidence`",
        "- [session] `evidence-task` - [evidence]",
        "### `evidence_alpha`",
        "- [session] `evidence-alpha` - [evidence-alpha]",
        "### `evidence_beta`",
        "- [session] `evidence-beta` - [evidence-beta]",
    ] {
        assert!(markdown.contains(expected_link), "{markdown}");
    }

    let links = Command::new(graphtrail())
        .args(["links", "helper", "--json"])
        .env("MISELEDGER_DB", &links_evidence_db)
        .output()
        .unwrap();
    assert!(links.status.success(), "links failed: {links:?}");
    assert_eq!(
        String::from_utf8(links.stderr).unwrap(),
        format!("{MISELEDGER_DEPRECATION_WARNING}\n")
    );
    let links_json: serde_json::Value = serde_json::from_slice(&links.stdout).unwrap();
    assert_eq!(links_json[0]["item_id"], "evidence-helper");

    assert_eq!(before_graph, snapshot_tree(graph_dir.path()));
    assert_eq!(before_evidence, snapshot_tree(evidence_dir.path()));
    assert_eq!(
        before_links_evidence,
        snapshot_tree(links_evidence_dir.path())
    );
}

#[cfg(not(feature = "miseledger"))]
#[test]
fn miseledger_commands_are_unavailable_without_the_feature_and_do_not_warn() {
    let context_help = Command::new(graphtrail())
        .args(["context", "--help"])
        .output()
        .unwrap();
    assert!(context_help.status.success(), "{context_help:?}");
    assert!(
        !String::from_utf8_lossy(&context_help.stdout).contains("--evidence"),
        "{context_help:?}"
    );
    assert!(context_help.stderr.is_empty(), "{context_help:?}");

    let links = Command::new(graphtrail()).arg("links").output().unwrap();
    assert!(!links.status.success(), "links unexpectedly succeeded");
    assert!(
        !String::from_utf8_lossy(&links.stderr).contains("direct MiseLedger adapter is deprecated"),
        "miseledger-free build warned: {links:?}"
    );
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
