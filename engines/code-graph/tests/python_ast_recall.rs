//! Recall guard: every definition and call that Python's own `ast` module finds in a
//! Python corpus must also exist in the graph.
//!
//! The graph may hold more than `ast` finds, never less. A miss means the extractor
//! silently dropped a construct, which no resolution test would notice.

use std::collections::BTreeSet;
use std::fmt::Write;
use std::path::{Path, PathBuf};
use std::process::Command;

use graphtrail::store::{init_schema, open_db, sync_repo};
use rusqlite::Connection;

/// (path relative to the corpus root, line, name)
type Item = (String, i64, String);

#[derive(Default)]
struct AstFacts {
    defs: BTreeSet<Item>,
    function_calls: BTreeSet<Item>,
    scope_calls: BTreeSet<Item>,
}

#[test]
fn python_extractor_recalls_everything_ast_finds_in_the_fixture_corpus() {
    let fixtures = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures");
    for corpus in [
        fixtures.join("python_recall"),
        fixtures.join("golden/mixed/python"),
    ] {
        assert_recall(&corpus);
    }
}

/// Brigade's own `src/` tree: 100% recall on 31,449 definitions and 98,542 calls inside
/// functions at the 2026-10-07 measurement. Slow in debug builds and tied to the live
/// tree, so it runs on demand: `cargo test --test python_ast_recall -- --ignored`.
#[test]
#[ignore = "indexes the live Brigade src/ tree, slow in debug builds"]
fn python_extractor_recalls_everything_ast_finds_in_brigade_src() {
    let src = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src");
    if !src.is_dir() {
        eprintln!("skipping: {src:?} is not part of this checkout");
        return;
    }
    assert_recall(&src);
}

#[test]
fn recall_fixture_exercises_the_tricky_constructs() {
    // Keeps the corpus honest: a refactor that drops a construct would otherwise
    // let the recall test pass over an easier fixture.
    let corpus = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/python_recall");
    let facts = ast_facts(&corpus);
    let def_names: BTreeSet<&str> = facts.defs.iter().map(|d| d.2.as_str()).collect();
    for name in [
        "decorated",
        "innermost",
        "fetch",
        "nested",
        "static",
        "make",
        "prop",
        "deep",
        "impl",
        "Child",
        "Inner",
    ] {
        assert!(def_names.contains(name), "fixture lost definition {name}");
    }
    assert!(
        facts.function_calls.len() >= 20,
        "{:?}",
        facts.function_calls
    );
    assert!(facts.scope_calls.len() >= 6, "{:?}", facts.scope_calls);
}

fn assert_recall(corpus: &Path) {
    let facts = ast_facts(corpus);
    assert!(!facts.defs.is_empty(), "no definitions under {corpus:?}");

    let temp = tempfile::tempdir().unwrap();
    let conn = open_db(&temp.path().join("graphtrail.db")).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, corpus).unwrap();

    let graph_defs = graph_items(
        &conn,
        "SELECT file_path, start_line, name FROM symbols WHERE kind != 'module'",
    );
    let graph_calls = graph_items(
        &conn,
        "SELECT file_path, line, target_name FROM pending_calls",
    );

    let mut report = String::new();
    report_missing(&mut report, "definitions", &facts.defs, &graph_defs);
    report_missing(
        &mut report,
        "calls inside functions",
        &facts.function_calls,
        &graph_calls,
    );
    report_missing(
        &mut report,
        "module and class level calls",
        &facts.scope_calls,
        &graph_calls,
    );
    assert!(
        report.is_empty(),
        "graph is missing items that ast finds under {corpus:?}:\n{report}"
    );
}

fn report_missing(
    report: &mut String,
    label: &str,
    expected: &BTreeSet<Item>,
    graph: &BTreeSet<Item>,
) {
    let missing: Vec<&Item> = expected.difference(graph).collect();
    if missing.is_empty() {
        return;
    }
    let _ = writeln!(
        report,
        "{label}: {} of {} missing",
        missing.len(),
        expected.len()
    );
    for (path, line, name) in missing {
        let _ = writeln!(report, "- {path}:{line} {name}");
    }
}

fn graph_items(conn: &Connection, sql: &str) -> BTreeSet<Item> {
    let mut statement = conn.prepare(sql).unwrap();
    statement
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))
        .unwrap()
        .map(|row| row.unwrap())
        .collect()
}

fn ast_facts(corpus: &Path) -> AstFacts {
    let script = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/python_ast_recall.py");
    let python = find_python();
    let output = Command::new(&python)
        .arg("-I")
        .arg(&script)
        .arg(corpus)
        .output()
        .unwrap_or_else(|error| panic!("failed to run {python:?}: {error}"));
    assert!(
        output.status.success(),
        "ast scan failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let json: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    let items = |key: &str| -> BTreeSet<Item> {
        json[key]
            .as_array()
            .unwrap_or_else(|| panic!("ast scan output lacks {key}"))
            .iter()
            .map(|item| {
                (
                    item[0].as_str().unwrap().to_string(),
                    item[1].as_i64().unwrap(),
                    item[2].as_str().unwrap().to_string(),
                )
            })
            .collect()
    };
    AstFacts {
        defs: items("defs"),
        function_calls: items("function_calls"),
        scope_calls: items("scope_calls"),
    }
}

fn find_python() -> PathBuf {
    for candidate in ["python3", "python"] {
        let probe = Command::new(candidate)
            .args(["-I", "-c", "import ast"])
            .output();
        if probe.is_ok_and(|output| output.status.success()) {
            return PathBuf::from(candidate);
        }
    }
    panic!("the Python recall guard needs python3 on PATH");
}
