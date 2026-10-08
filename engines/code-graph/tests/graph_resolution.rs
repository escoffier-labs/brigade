//! Graph verbs (callers, callees, impact) seed from an exact symbol match, never
//! a prefix match, and say so when the name is ambiguous or falls back to fuzzy
//! search (issue #1646).

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use graphtrail::mcp::handle_request;
use graphtrail::store::{init_schema, open_db, sync_repo};
use serde_json::{Value, json};
use tempfile::TempDir;

fn graphtrail() -> &'static str {
    env!("CARGO_BIN_EXE_graphtrail")
}

/// A repo where `run`, `run_journal`, `runguard`, `evaluate` and `evaluated`
/// share prefixes, `helper` is defined in two files, and `execute` only exists
/// as a method (so it matches by bare name, not qualified name).
fn fixture() -> (TempDir, PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    fs::write(
        root.join("lib.py"),
        r#"
def run():
    return 1

def run_journal():
    return 2

def runguard():
    return 3

def evaluate():
    return 4

def evaluated():
    return 5

class Job:
    def execute(self):
        return run()
"#,
    )
    .unwrap();
    fs::write(
        root.join("app.py"),
        r#"
from lib import run, run_journal, runguard, evaluate, evaluated, Job

def call_run():
    return run()

def call_journal():
    return run_journal()

def call_guard():
    return runguard()

def call_evaluate():
    return evaluate()

def call_evaluated():
    return evaluated()

def call_execute(job):
    return job.execute()
"#,
    )
    .unwrap();
    fs::create_dir_all(root.join("pkg")).unwrap();
    fs::write(
        root.join("pkg/one.py"),
        r#"
def helper():
    return 1

def use_one():
    return helper()
"#,
    )
    .unwrap();
    fs::write(
        root.join("pkg/two.py"),
        r#"
def helper():
    return 2

def use_two():
    return helper()
"#,
    )
    .unwrap();
    let db = root.join("graphtrail.db");
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();
    conn.pragma_update(None, "wal_checkpoint", "TRUNCATE")
        .unwrap();
    (dir, db)
}

fn run_cli(db: &Path, args: &[&str]) -> std::process::Output {
    let output = Command::new(graphtrail())
        .arg("--db")
        .arg(db)
        .args(args)
        .output()
        .unwrap();
    assert!(output.status.success(), "{args:?} failed: {output:?}");
    output
}

fn json_cli(db: &Path, args: &[&str]) -> Value {
    let mut full = args.to_vec();
    full.push("--json");
    let output = run_cli(db, &full);
    serde_json::from_slice(&output.stdout).unwrap_or_else(|err| {
        panic!(
            "{args:?} did not print JSON ({err}): {}",
            String::from_utf8_lossy(&output.stdout)
        )
    })
}

fn edge_pairs(value: &Value) -> Vec<(String, String)> {
    let mut pairs: Vec<(String, String)> = value["edges"]
        .as_array()
        .unwrap_or_else(|| panic!("edges must be an array: {value}"))
        .iter()
        .map(|edge| {
            (
                edge["source"].as_str().unwrap().to_string(),
                edge["target"].as_str().unwrap().to_string(),
            )
        })
        .collect();
    pairs.sort();
    pairs
}

fn candidate_files(value: &Value) -> Vec<String> {
    let mut files: Vec<String> = value["candidates"]
        .as_array()
        .unwrap()
        .iter()
        .map(|candidate| candidate["file_path"].as_str().unwrap().to_string())
        .collect();
    files.sort();
    files
}

#[test]
fn callers_of_run_ignore_prefix_neighbors() {
    let (_dir, db) = fixture();

    let value = json_cli(&db, &["callers", "run"]);

    assert_eq!(value["query"], "run");
    assert_eq!(value["resolution"], "qualified_name");
    assert_eq!(value["fuzzy"], false);
    assert_eq!(value["ambiguous"], false);
    assert_eq!(value["selected"].as_array().unwrap().len(), 1);
    assert_eq!(value["selected"][0]["qualified_name"], "run");
    assert_eq!(value["selected"][0]["file_path"], "lib.py");
    assert_eq!(
        edge_pairs(&value),
        vec![
            ("Job.execute".to_string(), "run".to_string()),
            ("call_run".to_string(), "run".to_string()),
        ]
    );
}

#[test]
fn callers_of_evaluate_exclude_evaluated() {
    let (_dir, db) = fixture();

    let value = json_cli(&db, &["callers", "evaluate"]);

    assert_eq!(
        edge_pairs(&value),
        vec![("call_evaluate".to_string(), "evaluate".to_string())]
    );
    let text = String::from_utf8(run_cli(&db, &["callers", "evaluate"]).stdout).unwrap();
    assert!(!text.contains("evaluated"), "{text}");
}

#[test]
fn ambiguous_exact_name_restricts_to_candidates_and_lists_them() {
    let (_dir, db) = fixture();

    let value = json_cli(&db, &["callers", "helper"]);

    assert_eq!(value["ambiguous"], true);
    assert_eq!(value["fuzzy"], false);
    assert_eq!(
        candidate_files(&value),
        vec!["pkg/one.py".to_string(), "pkg/two.py".to_string()]
    );
    for candidate in value["candidates"].as_array().unwrap() {
        assert_eq!(candidate["kind"], "function");
        assert_eq!(candidate["qualified_name"], "helper");
        assert!(candidate["start_line"].as_u64().unwrap() > 0);
    }
    assert_eq!(value["selected"], value["candidates"]);
    assert_eq!(
        edge_pairs(&value),
        vec![
            ("use_one".to_string(), "helper".to_string()),
            ("use_two".to_string(), "helper".to_string()),
        ]
    );

    let text = String::from_utf8(run_cli(&db, &["callers", "helper"]).stdout).unwrap();
    assert!(text.contains("ambiguous"), "{text}");
    assert!(text.contains("candidates:"), "{text}");
    assert!(text.contains("pkg/one.py:2"), "{text}");
    assert!(text.contains("pkg/two.py:2"), "{text}");
}

#[test]
fn name_path_selects_exactly_one_symbol() {
    let (_dir, db) = fixture();

    for query in [
        "pkg/one.py::helper",
        "./pkg/one.py::helper",
        "pkg/one.py:helper",
    ] {
        let value = json_cli(&db, &["callers", query]);

        assert_eq!(value["resolution"], "name_path", "{query}");
        assert_eq!(value["ambiguous"], false, "{query}");
        assert_eq!(value["selected"][0]["file_path"], "pkg/one.py", "{query}");
        assert_eq!(
            edge_pairs(&value),
            vec![("use_one".to_string(), "helper".to_string())],
            "{query}"
        );
    }
}

#[test]
fn qualified_method_and_bare_method_name_resolve_exactly() {
    let (_dir, db) = fixture();

    let qualified = json_cli(&db, &["callees", "Job.execute"]);
    assert_eq!(qualified["resolution"], "qualified_name");
    assert_eq!(
        edge_pairs(&qualified),
        vec![("Job.execute".to_string(), "run".to_string())]
    );

    let bare = json_cli(&db, &["callees", "execute"]);
    assert_eq!(bare["resolution"], "name");
    assert_eq!(bare["selected"][0]["qualified_name"], "Job.execute");
    assert_eq!(edge_pairs(&bare), edge_pairs(&qualified));
}

#[test]
fn symbol_id_resolves_exactly() {
    let (_dir, db) = fixture();
    let first = json_cli(&db, &["callers", "pkg/two.py::helper"]);
    let id = first["selected"][0]["id"].as_str().unwrap().to_string();

    let value = json_cli(&db, &["callers", &id]);

    assert_eq!(value["resolution"], "id");
    assert_eq!(
        edge_pairs(&value),
        vec![("use_two".to_string(), "helper".to_string())]
    );
}

#[test]
fn impact_uses_the_same_exact_seed() {
    let (_dir, db) = fixture();

    let value = json_cli(&db, &["impact", "run"]);

    assert_eq!(value["resolution"], "qualified_name");
    assert_eq!(value["fuzzy"], false);
    assert_eq!(
        edge_pairs(&value),
        vec![
            ("Job.execute".to_string(), "run".to_string()),
            ("call_run".to_string(), "run".to_string()),
        ]
    );
}

#[test]
fn missing_exact_symbol_falls_back_to_fuzzy_and_says_so() {
    let (_dir, db) = fixture();

    let value = json_cli(&db, &["callers", "run_jour"]);

    assert_eq!(value["resolution"], "fuzzy");
    assert_eq!(value["fuzzy"], true);
    assert_eq!(
        edge_pairs(&value),
        vec![("call_journal".to_string(), "run_journal".to_string())]
    );
    let text = String::from_utf8(run_cli(&db, &["callers", "run_jour"]).stdout).unwrap();
    assert!(text.contains("fuzzy"), "{text}");
    assert!(text.contains("call_journal"), "{text}");
}

#[test]
fn unknown_symbol_keeps_text_stdout_empty() {
    let (_dir, db) = fixture();

    let output = run_cli(&db, &["impact", "zzz_not_a_symbol"]);

    assert!(output.stdout.is_empty(), "{output:?}");
    let value = json_cli(&db, &["impact", "zzz_not_a_symbol"]);
    assert_eq!(value["resolution"], "none");
    assert_eq!(value["edges"], json!([]));
}

#[test]
fn single_exact_match_text_output_has_no_resolution_header() {
    let (_dir, db) = fixture();

    let text = String::from_utf8(run_cli(&db, &["callers", "evaluate"]).stdout).unwrap();

    assert_eq!(text.lines().count(), 1, "{text}");
    assert!(text.starts_with("call_evaluate --calls@"), "{text}");
}

fn mcp_call(db: &Path, name: &str, arguments: Value) -> Value {
    let resp = handle_request(
        db,
        &json!({"jsonrpc":"2.0","id":7,"method":"tools/call",
                "params":{"name":name,"arguments":arguments}}),
    )
    .unwrap();
    assert_eq!(resp["result"]["isError"], false, "{resp}");
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    serde_json::from_str(text).unwrap()
}

#[test]
fn mcp_graph_tools_report_resolution_and_accept_name_paths() {
    let (_dir, db) = fixture();

    let callers = mcp_call(&db, "callers", json!({"symbol": "helper"}));
    assert_eq!(callers["ambiguous"], true);
    assert_eq!(
        candidate_files(&callers),
        vec!["pkg/one.py".to_string(), "pkg/two.py".to_string()]
    );

    let narrowed = mcp_call(&db, "callers", json!({"symbol": "pkg/two.py::helper"}));
    assert_eq!(narrowed["resolution"], "name_path");
    assert_eq!(
        edge_pairs(&narrowed),
        vec![("use_two".to_string(), "helper".to_string())]
    );

    let callees = mcp_call(&db, "callees", json!({"symbol": "execute"}));
    assert_eq!(callees["resolution"], "name");

    let impact = mcp_call(&db, "impact", json!({"symbol": "evaluate"}));
    assert_eq!(
        edge_pairs(&impact),
        vec![("call_evaluate".to_string(), "evaluate".to_string())]
    );
}
