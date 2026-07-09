//! Integration tests for dead_code, cycles, affected, and edge confidence.

use std::fs;
use std::path::Path;

use graphtrail::query::{affected, cycles, dead_code};
use graphtrail::store::{init_schema, open_db, sync_repo};
use rusqlite::Connection;

/// lib.py defines helper (imported by app.py) plus two uncalled callables;
/// cycle_a.py and cycle_b.py call each other; tests/test_app.py calls run.
fn fixture() -> (tempfile::TempDir, Connection) {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write(
        root.join("lib.py"),
        "def helper():\n    return 1\n\ndef unused_fn():\n    return 2\n\ndef caller_local():\n    return helper()\n",
    );
    write(
        root.join("app.py"),
        "from lib import helper\n\ndef run():\n    return helper()\n",
    );
    write(
        root.join("cycle_a.py"),
        "from cycle_b import beta\n\ndef alpha():\n    return beta()\n",
    );
    write(
        root.join("cycle_b.py"),
        "from cycle_a import alpha\n\ndef beta():\n    return alpha()\n",
    );
    write(
        root.join("tests/test_app.py"),
        "from app import run\n\ndef test_run():\n    assert run()\n",
    );

    let conn = open_db(&root.join("g.db")).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();
    (dir, conn)
}

fn write(path: impl AsRef<Path>, content: &str) {
    let path = path.as_ref();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, content).unwrap();
}

#[test]
fn dead_code_lists_uncalled_callables_only() {
    let (_dir, conn) = fixture();
    let report = dead_code(&conn, 100).unwrap();

    let names: Vec<&str> = report
        .symbols
        .iter()
        .map(|symbol| symbol.name.as_str())
        .collect();
    assert!(names.contains(&"unused_fn"), "names: {names:?}");
    assert!(names.contains(&"caller_local"), "names: {names:?}");
    // Called symbols, and symbols in test files, are not candidates.
    for called in ["helper", "run", "alpha", "beta", "test_run"] {
        assert!(!names.contains(&called), "{called} should not be listed");
    }
    assert_eq!(report.total, report.symbols.len());
    assert!(report.attribution.contains("not proof"));
}

#[test]
fn dead_code_limit_truncates_but_reports_total() {
    let (_dir, conn) = fixture();
    let report = dead_code(&conn, 1).unwrap();
    assert_eq!(report.symbols.len(), 1);
    assert_eq!(report.total, 2);
}

#[test]
fn cycles_reports_the_mutual_recursion_group() {
    let (_dir, conn) = fixture();
    let report = cycles(&conn).unwrap();

    assert_eq!(report.total_groups, 1, "groups: {:?}", report.groups);
    assert_eq!(
        report.groups[0],
        vec!["cycle_a.py".to_string(), "cycle_b.py".to_string()]
    );
    assert!(!report.truncated);
}

#[test]
fn affected_attributes_tests_through_call_edges() {
    let (_dir, conn) = fixture();
    let report = affected(&conn, &["lib.py".to_string()], 3).unwrap();

    assert_eq!(report.changed_files, vec!["lib.py".to_string()]);
    assert!(report.missing_files.is_empty());
    assert_eq!(report.affected_tests.len(), 1, "{report:?}");
    let test = &report.affected_tests[0];
    assert_eq!(test.file_path, "tests/test_app.py");
    assert_eq!(test.min_hops, 2);
    assert_eq!(test.via, vec!["test_run".to_string()]);

    // app.py reaches lib.py at one hop; lib.py itself is input, not a finding.
    let impacted: Vec<&str> = report
        .impacted_files
        .iter()
        .map(|row| row.file_path.as_str())
        .collect();
    assert_eq!(impacted, vec!["app.py"]);
    assert!(report.attribution.contains("lower bound"));
}

#[test]
fn affected_depth_one_stops_before_the_test() {
    let (_dir, conn) = fixture();
    let report = affected(&conn, &["lib.py".to_string()], 1).unwrap();
    assert!(report.affected_tests.is_empty(), "{report:?}");
    assert_eq!(report.impacted_files.len(), 1);
}

#[test]
fn affected_reports_unknown_inputs_as_missing() {
    let (_dir, conn) = fixture();
    let report = affected(&conn, &["nope.py".to_string()], 3).unwrap();
    assert_eq!(report.missing_files, vec!["nope.py".to_string()]);
    assert!(report.changed_files.is_empty());
    assert!(report.affected_tests.is_empty());
}

#[test]
fn edge_confidence_reflects_the_resolution_path() {
    let (_dir, conn) = fixture();
    let confidence = |source: &str, target: &str| -> f64 {
        conn.query_row(
            "SELECT e.confidence FROM edges e
             JOIN symbols src ON src.id = e.source
             JOIN symbols dst ON dst.id = e.target
             WHERE src.name = ?1 AND dst.name = ?2",
            [source, target],
            |row| row.get(0),
        )
        .unwrap()
    };

    // Import-strict resolutions score highest; same-file bare calls lower.
    assert_eq!(confidence("run", "helper"), 0.9);
    assert_eq!(confidence("test_run", "run"), 0.9);
    assert_eq!(confidence("caller_local", "helper"), 0.8);
}
