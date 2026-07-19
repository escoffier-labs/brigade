//! Integration tests for the CocoIndex round 3 surfaces: explain and export.

use std::fs;
use std::path::Path;

use graphtrail::query::{ExportFormat, ExportScope, export_graph};
use graphtrail::store::{explain_calls, init_schema, open_db, sync_repo};
use rusqlite::Connection;

/// app.py imports helper from lib.py and calls it; lib.py also calls a name
/// nothing defines (`missing`) and json.dumps (external import).
fn fixture() -> (tempfile::TempDir, Connection) {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write(
        root.join("lib.py"),
        "import json\n\ndef helper():\n    missing()\n    return json.dumps({})\n",
    );
    write(
        root.join("app.py"),
        "from lib import helper\n\ndef run():\n    return helper()\n",
    );

    let conn = open_db(&root.join("g.db")).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();
    (dir, conn)
}

fn write(path: std::path::PathBuf, content: &str) {
    if let Some(parent) = Path::new(&path).parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, content).unwrap();
}

#[test]
fn explain_reports_import_strict_with_the_matched_import() {
    let (_dir, conn) = fixture();

    let rows = explain_calls(&conn, "run", "helper").unwrap();

    assert_eq!(rows.len(), 1);
    let row = &rows[0];
    assert_eq!(row.resolution, "import-strict");
    assert_eq!(row.source_file, "app.py");
    let import = row.matched_import.as_ref().expect("import must be named");
    assert_eq!(import.module, "lib");
    assert_eq!(row.targets.len(), 1);
    assert_eq!(row.targets[0].file_path, "lib.py");
    assert_eq!(row.targets[0].confidence, 0.9);
}

#[test]
fn explain_reports_unresolved_calls_instead_of_hiding_them() {
    let (_dir, conn) = fixture();

    let rows = explain_calls(&conn, "helper", "missing").unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].resolution, "no-candidates");
    assert!(rows[0].targets.is_empty());

    let external = explain_calls(&conn, "helper", "dumps").unwrap();
    assert_eq!(external.len(), 1);
    assert_eq!(external[0].resolution, "unresolved-external");
    assert_eq!(external[0].matched_import.as_ref().unwrap().module, "json");
}

#[test]
fn explain_matches_source_exactly_not_fuzzily() {
    let (_dir, conn) = fixture();

    // `run` has the call; a prefix of it must not match.
    assert!(explain_calls(&conn, "ru", "helper").unwrap().is_empty());
}

#[test]
fn export_dot_files_scope_aggregates_cross_file_calls() {
    let (_dir, conn) = fixture();

    let dot = export_graph(&conn, ExportFormat::Dot, ExportScope::Files).unwrap();

    assert!(dot.starts_with("digraph graphtrail {"));
    assert!(dot.contains("\"app.py\" -> \"lib.py\" [label=\"1\"];"));
    assert!(dot.trim_end().ends_with('}'));
}

#[test]
fn export_graphml_symbols_scope_is_well_formed_and_carries_confidence() {
    let (_dir, conn) = fixture();

    let xml = export_graph(&conn, ExportFormat::Graphml, ExportScope::Symbols).unwrap();

    assert!(xml.starts_with("<?xml version=\"1.0\""));
    assert!(xml.contains("<data key=\"qualified_name\">run</data>"));
    assert!(xml.contains("<data key=\"confidence\">0.9</data>"));
    assert_eq!(xml.matches("<graphml").count(), 1);
    assert_eq!(xml.matches("</graphml>").count(), 1);
}

#[test]
fn export_jsonl_emits_one_valid_json_value_per_line() {
    let (_dir, conn) = fixture();

    let jsonl = export_graph(&conn, ExportFormat::Jsonl, ExportScope::Symbols).unwrap();

    let mut nodes = 0;
    let mut edges = 0;
    for line in jsonl.lines() {
        let value: serde_json::Value = serde_json::from_str(line).unwrap();
        match value["type"].as_str().unwrap() {
            "node" => nodes += 1,
            "edge" => edges += 1,
            other => panic!("unexpected row type {other}"),
        }
    }
    assert_eq!(nodes, 2, "run and helper");
    assert_eq!(edges, 1, "run -> helper");
}
