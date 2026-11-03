//! CLI query commands must open the graph db read-only.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::SystemTime;

use graphtrail::store::{init_schema, open_db, sync_repo};
use sha2::{Digest, Sha256};

#[cfg(feature = "miseledger")]
const MISELEDGER_DEPRECATION_WARNING: &str = "warning: GraphTrail's direct MiseLedger adapter is deprecated. Use `brigade code sync`, `brigade code context`, `brigade code impact`, `brigade evidence crawl`, `brigade evidence search`, and `brigade evidence doctor` instead. The adapter remains functional for at least two minor GraphTrail releases or 90 days after the first GraphTrail release containing this deprecation, whichever is longer. It will not be removed before that compatibility policy is satisfied.";

/// The canonical entry point. Legacy behavior has its own tests below.
fn engine() -> &'static str {
    env!("CARGO_BIN_EXE_brigade-code-graph")
}

fn legacy_engine() -> &'static str {
    env!("CARGO_BIN_EXE_graphtrail")
}

fn index_dirs_absent(root: &Path) -> bool {
    !root.join(".graphtrail").exists() && !root.join(".brigade").exists()
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
        "outline" => vec!["--db".into(), db, "outline".into(), "app.py".into()],
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
        let output = Command::new(engine())
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

    let output = Command::new(engine())
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
        "outline",
        "stats",
        "doctor",
    ] {
        let dir = tempfile::tempdir().unwrap();
        let mut cmd = Command::new(engine());
        cmd.current_dir(dir.path());
        match command {
            "search" => cmd.args(["search", "helper"]),
            "callers" => cmd.args(["callers", "helper"]),
            "callees" => cmd.args(["callees", "run"]),
            "impact" => cmd.args(["impact", "helper"]),
            "context" => cmd.args(["context", "helper"]),
            "neighbors" => cmd.args(["neighbors", "app.py"]),
            "outline" => cmd.args(["outline", "app.py"]),
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
            index_dirs_absent(dir.path()),
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
        "outline",
        "stats",
        "doctor",
    ] {
        let dir = tempfile::tempdir().unwrap();
        let db = build_db(dir.path());
        let graph_dir = db.parent().unwrap();
        let before = snapshot_tree(graph_dir);

        let output = Command::new(engine())
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

    let output = Command::new(engine())
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

    let output = Command::new(engine())
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
fn doctor_text_reports_head_movement_and_language_coverage() {
    let dir = tempfile::tempdir().unwrap();
    let db = build_db(dir.path());

    let output = Command::new(engine())
        .current_dir(dir.path())
        .args(["--db", &db.display().to_string(), "doctor"])
        .output()
        .unwrap();

    assert!(output.status.success(), "doctor failed: {output:?}");
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(stdout.contains("head_moved=false"), "{stdout}");
    assert!(
        stdout.contains(
            "coverage.python: files=1 symbols=2 call_sites=1 resolved=1 unresolved=0 cross_file_edges=0"
        ),
        "{stdout}"
    );
}

#[test]
fn doctor_withholds_stale_resolver_coverage_until_sync() {
    let dir = tempfile::tempdir().unwrap();
    let db = build_db(dir.path());
    let conn = open_db(&db).unwrap();
    graphtrail::store::meta::upsert(&conn, "resolver_version", "0").unwrap();
    drop(conn);

    for stale in [true, false] {
        if !stale {
            let sync = Command::new(engine())
                .current_dir(dir.path())
                .args(["--db", &db.display().to_string(), "sync", "."])
                .output()
                .unwrap();
            assert!(sync.status.success(), "sync failed: {sync:?}");
        }
        let expected_exit = if stale { 1 } else { 0 };
        let expected_verdict = if stale { "STALE" } else { "FRESH" };
        let output = Command::new(engine())
            .current_dir(dir.path())
            .args(["--db", &db.display().to_string(), "doctor", "--json"])
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(expected_exit), "{output:?}");
        let value: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(value["schema"]["needs_migration"], false);
        assert_eq!(value["resolver"]["stale"], stale);
        assert_eq!(value["verdict"], expected_verdict);
        if stale {
            assert_eq!(value["resolver"]["stored"], "0");
            assert_eq!(value["coverage"], serde_json::json!({}));
        } else {
            assert_eq!(value["resolver"]["stored"], value["resolver"]["current"]);
            assert_eq!(
                value["coverage"]["python"],
                serde_json::json!({
                    "files": 1,
                    "symbols": 2,
                    "call_sites": 1,
                    "resolved_call_sites": 1,
                    "unresolved_call_sites": 0,
                    "cross_file_edges": 0,
                })
            );
        }

        let output = Command::new(engine())
            .current_dir(dir.path())
            .args(["--db", &db.display().to_string(), "doctor"])
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(expected_exit), "{output:?}");
        let text = String::from_utf8(output.stdout).unwrap();
        assert!(
            text.contains(&format!("verdict: {expected_verdict}")),
            "{text}"
        );
        if stale {
            assert!(text.contains("stale=true"), "{text}");
            assert!(!text.contains("coverage."), "{text}");
        } else {
            assert!(
                text.contains(
                    "coverage.python: files=1 symbols=2 call_sites=1 resolved=1 unresolved=0 cross_file_edges=0"
                ),
                "{text}"
            );
        }
    }
}

#[test]
fn doctor_new_file_reports_stale_exit_1() {
    let dir = tempfile::tempdir().unwrap();
    let db = build_db(dir.path());
    fs::write(dir.path().join("late.py"), "def late():\n    return 2\n").unwrap();

    let output = Command::new(engine())
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
fn sync_skips_non_utf8_file_and_reports_it() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    fs::create_dir(root.join("pkg")).unwrap();
    fs::write(root.join("pkg/core.py"), "def helper():\n    return 1\n").unwrap();
    let sync = |root: &Path| {
        Command::new(engine())
            .current_dir(root)
            .args(["sync", "."])
            .output()
            .unwrap()
    };
    assert!(sync(root).status.success());
    fs::write(root.join("pkg/new.py"), "def newfn():\n    return 2\n").unwrap();
    fs::write(root.join("pkg/bad.py"), b"x = 1\n\xff\xfe bad\n").unwrap();

    let output = sync(root);

    assert!(output.status.success(), "sync failed: {output:?}");
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        stdout.contains("skipped files=1 unreadable_utf8=1"),
        "stdout: {stdout}"
    );
    let db = root
        .join(".brigade")
        .join("code-graph")
        .join("code-graph.db");
    let doctor = Command::new(engine())
        .current_dir(root)
        .args(["--db", &db.display().to_string(), "doctor", "--json"])
        .output()
        .unwrap();
    assert!(doctor.status.success(), "doctor output: {doctor:?}");
    let value: serde_json::Value = serde_json::from_slice(&doctor.stdout).unwrap();
    assert_eq!(value["verdict"], "FRESH");
    assert_eq!(value["pending"]["new_files"], 0);
    assert_eq!(value["skipped"]["count"], 1);
    assert_eq!(value["skipped"]["sample"][0]["path"], "pkg/bad.py");
    let search = Command::new(engine())
        .current_dir(root)
        .args(["--db", &db.display().to_string(), "search", "newfn"])
        .output()
        .unwrap();
    assert!(String::from_utf8_lossy(&search.stdout).contains("newfn"));

    let text = Command::new(engine())
        .current_dir(root)
        .args(["--db", &db.display().to_string(), "doctor"])
        .output()
        .unwrap();
    let text = String::from_utf8_lossy(&text.stdout);
    assert!(
        text.contains("skipped: count=1 sample=pkg/bad.py (unreadable_utf8)"),
        "doctor text: {text}"
    );
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

    let output = Command::new(engine())
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

    let output = Command::new(engine())
        .current_dir(dir.path())
        .args(["doctor", "--json"])
        .output()
        .unwrap();

    assert_eq!(output.status.code(), Some(2), "doctor output: {output:?}");
    assert!(
        index_dirs_absent(dir.path()),
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

    let output = Command::new(engine())
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

    let context_without_evidence = Command::new(engine())
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

    let context_with_evidence = Command::new(engine())
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

    let context_markdown_with_evidence = Command::new(engine())
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

    let links = Command::new(engine())
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
    let context_help = Command::new(engine())
        .args(["context", "--help"])
        .output()
        .unwrap();
    assert!(context_help.status.success(), "{context_help:?}");
    assert!(
        !String::from_utf8_lossy(&context_help.stdout).contains("--evidence"),
        "{context_help:?}"
    );
    assert!(context_help.stderr.is_empty(), "{context_help:?}");

    let links = Command::new(engine()).arg("links").output().unwrap();
    assert!(!links.status.success(), "links unexpectedly succeeded");
    assert!(
        !String::from_utf8_lossy(&links.stderr).contains("direct MiseLedger adapter is deprecated"),
        "miseledger-free build warned: {links:?}"
    );
}

#[cfg(all(feature = "codesearch", feature = "miseledger"))]
#[test]
fn context_help_lists_join_layer_flags() {
    let output = Command::new(engine())
        .args(["context", "--help"])
        .output()
        .unwrap();

    assert!(output.status.success(), "{output:?}");
    let help = String::from_utf8(output.stdout).unwrap();
    assert!(help.contains("--blend-code-search"));
    assert!(help.contains("--evidence"));
}

/// Exercise real executables: target metadata alone cannot provide compatibility.
#[test]
fn canonical_and_legacy_cli_contracts() {
    let dir = tempfile::tempdir().unwrap();
    let db = build_db(dir.path());
    // Neither entry point writes extra stderr: the legacy deprecation line lands in R5
    // (docs/phase-engine-rename.md, "Entry point identity").
    for (binary, name) in [
        (legacy_engine(), "graphtrail"),
        (engine(), "brigade-code-graph"),
    ] {
        let invoke = |args: &[&str]| {
            Command::new(binary)
                .current_dir(dir.path())
                .env("HOME", dir.path())
                .args(args)
                .output()
                .unwrap()
        };
        let help = invoke(&["--help"]);
        assert_eq!(help.status.code(), Some(0), "{help:?}");
        let text = String::from_utf8_lossy(&help.stdout);
        assert!(text.contains("--db <PATH>"), "{text}");
        assert!(text.contains("sync"), "{text}");
        assert!(text.contains(&format!("Usage: {name} ")), "{text}");
        assert!(help.stderr.is_empty(), "{help:?}");

        let version = invoke(&["--version"]);
        assert_eq!(version.status.code(), Some(0));
        assert_eq!(
            String::from_utf8_lossy(&version.stdout),
            format!("{name} {}\n", env!("CARGO_PKG_VERSION"))
        );
        assert!(version.stderr.is_empty(), "{version:?}");

        for args in [
            vec!["--unknown-option"],
            vec!["--db"],
            vec!["unknown-command"],
        ] {
            let error = invoke(&args);
            assert_eq!(error.status.code(), Some(2), "{error:?}");
            assert!(error.stdout.is_empty(), "{error:?}");
            let stderr = String::from_utf8_lossy(&error.stderr);
            assert!(stderr.starts_with("error:"), "{error:?}");
            assert!(stderr.contains("error:"), "{error:?}");
        }
        let stats = invoke(&["--db", db.to_str().unwrap(), "stats", "--json"]);
        assert_eq!(stats.status.code(), Some(0), "{stats:?}");
        let value: serde_json::Value = serde_json::from_slice(&stats.stdout).unwrap();
        assert_eq!(value["symbols"], 2, "{value}");

        let missing = invoke(&["--db", "absent.db", "search", "helper", "--json"]);
        assert_eq!(missing.status.code(), Some(1), "{missing:?}");
        assert!(!dir.path().join("absent.db").exists());
    }
}

/// Identity comes from the basename of `argv[0]`: any other name, such as a renamed copy, takes
/// the canonical identity, and the legacy name matches with `.exe` and in any case.
#[cfg(unix)]
#[test]
fn argv0_selects_the_entry_point_identity() {
    use std::os::unix::process::CommandExt;

    for (argv0, banner) in [
        ("/opt/tools/code-graph-copy", "brigade-code-graph"),
        ("/opt/tools/GraphTrail.EXE", "graphtrail"),
    ] {
        let output = Command::new(legacy_engine())
            .arg0(argv0)
            .arg("--version")
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(0), "{output:?}");
        assert_eq!(
            String::from_utf8_lossy(&output.stdout),
            format!("{banner} {}\n", env!("CARGO_PKG_VERSION"))
        );
        assert!(output.stderr.is_empty(), "{output:?}");
    }
}

fn write_app(root: &Path) {
    fs::write(
        root.join("app.py"),
        "def helper():\n    return 1\n\ndef run():\n    return helper()\n",
    )
    .unwrap();
}

fn engine_in(root: &Path, args: &[&str]) -> std::process::Output {
    Command::new(engine())
        .current_dir(root)
        .env_remove("BRIGADE_CODE_GRAPH_DB")
        .env_remove("GRAPHTRAIL_DB")
        .args(args)
        .output()
        .unwrap()
}

/// A repository indexed before the rename answers queries with no reindex, and later syncs keep
/// writing the legacy index in place.
#[test]
fn legacy_only_repository_is_read_and_written_in_place() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    fs::create_dir(root.join(".git")).unwrap();
    fs::write(root.join(".git").join("HEAD"), "ref: refs/heads/main\n").unwrap();
    let legacy_db = build_db(root);
    let gitignore_before = fs::read_to_string(root.join(".gitignore")).ok();

    let callers = engine_in(root, &["callers", "helper"]);
    assert_eq!(callers.status.code(), Some(0), "{callers:?}");
    assert!(
        String::from_utf8_lossy(&callers.stdout).contains("run"),
        "{callers:?}"
    );

    fs::write(root.join("extra.py"), "def extra():\n    return helper()\n").unwrap();
    let sync = engine_in(root, &["sync", "."]);
    assert_eq!(sync.status.code(), Some(0), "{sync:?}");
    // Windows prints the relative path with backslashes.
    let stdout = String::from_utf8_lossy(&sync.stdout).replace('\\', "/");
    assert!(
        stdout.contains("db=./.graphtrail/graphtrail.db"),
        "{stdout}"
    );
    assert!(!root.join(".brigade").exists());
    assert_eq!(
        fs::read_to_string(root.join(".gitignore")).ok(),
        gitignore_before
    );

    let conn = rusqlite::Connection::open(&legacy_db).unwrap();
    let files: i64 = conn
        .query_row("SELECT COUNT(*) FROM files", [], |row| row.get(0))
        .unwrap();
    assert_eq!(files, 2);
}

/// A fresh repository gets the canonical index and its ignore entry.
#[test]
fn fresh_repository_gets_canonical_index_and_ignore_entry() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    fs::create_dir(root.join(".git")).unwrap();
    fs::write(root.join(".git").join("HEAD"), "ref: refs/heads/main\n").unwrap();
    write_app(root);

    let sync = engine_in(root, &["sync", "."]);
    assert_eq!(sync.status.code(), Some(0), "{sync:?}");
    // Windows prints the relative path with backslashes.
    let stdout = String::from_utf8_lossy(&sync.stdout).replace('\\', "/");
    assert!(
        stdout.contains("db=./.brigade/code-graph/code-graph.db"),
        "{stdout}"
    );
    assert!(
        root.join(".brigade/code-graph/code-graph.db").is_file(),
        "{stdout}"
    );
    assert!(!root.join(".graphtrail").exists());
    assert_eq!(
        fs::read_to_string(root.join(".gitignore")).unwrap(),
        ".brigade/code-graph/\n"
    );

    // A second sync neither reindexes into the walker's view of the index nor re-appends.
    let again = engine_in(root, &["sync", "."]);
    assert_eq!(again.status.code(), Some(0), "{again:?}");
    assert!(
        String::from_utf8_lossy(&again.stdout).starts_with("unchanged: files=1 "),
        "{again:?}"
    );
    assert_eq!(
        fs::read_to_string(root.join(".gitignore")).unwrap(),
        ".brigade/code-graph/\n"
    );
}

/// Both layouts present: the canonical index wins and the legacy one is left alone.
#[test]
fn canonical_index_wins_when_both_exist() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let legacy_db = build_db(root);
    let legacy_bytes = fs::read(&legacy_db).unwrap();
    fs::write(root.join("app.py"), "def only_canonical():\n    return 1\n").unwrap();
    let canonical = root.join(".brigade/code-graph/code-graph.db");
    let sync = engine_in(root, &["--db", canonical.to_str().unwrap(), "sync", "."]);
    assert_eq!(sync.status.code(), Some(0), "{sync:?}");

    let search = engine_in(root, &["search", "only_canonical"]);
    assert_eq!(search.status.code(), Some(0), "{search:?}");
    assert!(
        String::from_utf8_lossy(&search.stdout).contains("only_canonical"),
        "{search:?}"
    );
    assert_eq!(fs::read(&legacy_db).unwrap(), legacy_bytes);
}

#[test]
fn database_variables_dual_read_silently_and_refuse_conflicts() {
    let dir = tempfile::tempdir().unwrap();
    let db = build_db(dir.path());
    let other = tempfile::tempdir().unwrap();
    let stats = |vars: &[(&str, &str)]| {
        let mut command = Command::new(engine());
        command
            .current_dir(other.path())
            .env_remove("BRIGADE_CODE_GRAPH_DB")
            .env_remove("GRAPHTRAIL_DB")
            .args(["stats", "--json"]);
        for (key, value) in vars {
            command.env(key, value);
        }
        command.output().unwrap()
    };
    let db = db.to_str().unwrap();

    let canonical = stats(&[("BRIGADE_CODE_GRAPH_DB", db)]);
    assert_eq!(canonical.status.code(), Some(0), "{canonical:?}");
    assert!(canonical.stderr.is_empty(), "{canonical:?}");

    // The legacy variable works and prints nothing until R5 (docs/phase-engine-rename.md).
    let legacy = stats(&[("GRAPHTRAIL_DB", db)]);
    assert_eq!(legacy.status.code(), Some(0), "{legacy:?}");
    assert!(legacy.stderr.is_empty(), "{legacy:?}");
    let value: serde_json::Value = serde_json::from_slice(&legacy.stdout).unwrap();
    assert_eq!(value["symbols"], 2, "{value}");

    let same = stats(&[("BRIGADE_CODE_GRAPH_DB", db), ("GRAPHTRAIL_DB", db)]);
    assert_eq!(same.status.code(), Some(0), "{same:?}");
    assert!(same.stderr.is_empty(), "{same:?}");

    let conflict = stats(&[("BRIGADE_CODE_GRAPH_DB", db), ("GRAPHTRAIL_DB", "other.db")]);
    assert_ne!(conflict.status.code(), Some(0), "{conflict:?}");
    assert!(conflict.stdout.is_empty(), "{conflict:?}");
    assert!(
        String::from_utf8_lossy(&conflict.stderr).contains("conflicting values"),
        "{conflict:?}"
    );
    assert!(!other.path().join("other.db").exists());
}

#[test]
fn unsafe_root_variables_dual_read_silently_and_refuse_conflicts() {
    let home = tempfile::tempdir().unwrap();
    write_app(home.path());
    let sync = |vars: &[(&str, &str)]| {
        let db = home.path().join("index.db");
        let mut command = Command::new(engine());
        command
            .current_dir(home.path())
            .env("HOME", home.path())
            .env_remove("BRIGADE_CODE_GRAPH_ALLOW_UNSAFE_ROOT")
            .env_remove("GRAPHTRAIL_ALLOW_UNSAFE_ROOT")
            .args(["--db", db.to_str().unwrap(), "sync", "."]);
        for (key, value) in vars {
            command.env(key, value);
        }
        command.output().unwrap()
    };

    let refused = sync(&[]);
    assert_ne!(refused.status.code(), Some(0), "{refused:?}");
    let stderr = String::from_utf8_lossy(&refused.stderr);
    assert!(stderr.contains("root is the home directory"), "{stderr}");
    assert!(
        stderr.contains("BRIGADE_CODE_GRAPH_ALLOW_UNSAFE_ROOT=1"),
        "{stderr}"
    );

    let canonical = sync(&[("BRIGADE_CODE_GRAPH_ALLOW_UNSAFE_ROOT", "1")]);
    assert_eq!(canonical.status.code(), Some(0), "{canonical:?}");
    assert!(canonical.stderr.is_empty(), "{canonical:?}");

    // The legacy variable works and prints nothing until R5 (docs/phase-engine-rename.md).
    let legacy = sync(&[("GRAPHTRAIL_ALLOW_UNSAFE_ROOT", "1")]);
    assert_eq!(legacy.status.code(), Some(0), "{legacy:?}");
    assert!(legacy.stderr.is_empty(), "{legacy:?}");

    let conflict = sync(&[
        ("BRIGADE_CODE_GRAPH_ALLOW_UNSAFE_ROOT", "1"),
        ("GRAPHTRAIL_ALLOW_UNSAFE_ROOT", "0"),
    ]);
    assert_ne!(conflict.status.code(), Some(0), "{conflict:?}");
    assert!(
        String::from_utf8_lossy(&conflict.stderr).contains("conflicting values"),
        "{conflict:?}"
    );
}
