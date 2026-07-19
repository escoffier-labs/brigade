//! Integration tests for dead_code, cycles, affected, and edge confidence.

use std::collections::HashMap;
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

/// Low-confidence candidates sort before the private function by file name,
/// so the report must use confidence rather than the SQL's file ordering.
fn dead_code_confidence_fixture() -> (tempfile::TempDir, Connection) {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write(
        root.join("a_trait.rs"),
        "trait Handler {\n    fn handle(&self) {}\n}\n",
    );
    write(root.join("b_public.rs"), "pub fn exported_entry() {}\n");
    write(root.join("c_callback.rs"), "fn on_event() {}\n");
    write(
        root.join("d_export_list.js"),
        "function listedApi() {}\n\nexport { listedApi };\n",
    );
    write(
        root.join("z_private.rs"),
        "fn local_helper() {}\nfn pending_hint() {}\nfn import_hint() {}\n",
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
fn dead_code_ranks_private_candidates_ahead_of_uncertain_ones() {
    let (_dir, conn) = dead_code_confidence_fixture();
    let report = dead_code(&conn, 100).unwrap();
    let by_name: HashMap<&str, _> = report
        .symbols
        .iter()
        .map(|symbol| (symbol.name.as_str(), symbol))
        .collect();

    let private = by_name.get("local_helper").expect("private candidate");
    assert_eq!(private.confidence, "high");
    assert!(
        private.reason.contains("private/local"),
        "{}",
        private.reason
    );

    for uncertain in ["handle", "exported_entry", "on_event", "listedApi"] {
        let symbol = by_name
            .get(uncertain)
            .expect("uncertain candidate retained");
        assert_eq!(symbol.confidence, "low", "{uncertain}: {symbol:?}");
        assert!(!symbol.reason.is_empty(), "{uncertain} needs a reason");
    }

    assert!(by_name["handle"].reason.contains("dynamic dispatch"));
    assert!(by_name["exported_entry"].reason.contains("public/exported"));
    assert!(by_name["on_event"].reason.contains("callback-style"));
    assert!(by_name["listedApi"].reason.contains("module visibility"));

    let private_position = report
        .symbols
        .iter()
        .position(|symbol| symbol.name == "local_helper")
        .unwrap();
    let first_uncertain_position = report
        .symbols
        .iter()
        .position(|symbol| {
            matches!(
                symbol.name.as_str(),
                "handle" | "exported_entry" | "on_event" | "listedApi"
            )
        })
        .unwrap();
    assert!(
        private_position < first_uncertain_position,
        "symbols: {:?}",
        report.symbols
    );

    let limited = dead_code(&conn, 1).unwrap();
    assert_eq!(limited.total, report.total);
    assert_eq!(limited.symbols[0].name, "local_helper");
    assert_eq!(limited.symbols[0].confidence, "high");

    let serialized = serde_json::to_value(&report).unwrap();
    for symbol in serialized["symbols"].as_array().unwrap() {
        assert!(matches!(
            symbol["confidence"].as_str(),
            Some("high" | "low")
        ));
        assert!(
            symbol["reason"]
                .as_str()
                .is_some_and(|reason| !reason.is_empty())
        );
    }
}

#[test]
fn dead_code_downgrades_stored_reference_hints() {
    let (_dir, conn) = dead_code_confidence_fixture();
    conn.execute(
        "INSERT INTO pending_calls(source_id, file_path, target_name, kind, qualifier, line)
         VALUES ('external-source', 'consumer.rs', 'pending_hint', 'bare', NULL, 1)",
        [],
    )
    .unwrap();
    conn.execute(
        "INSERT INTO imports(file_path, module, local_name, imported_name, alias, line)
         VALUES ('consumer.rs', 'crate::z_private', 'renamed', 'import_hint', 'renamed', 1)",
        [],
    )
    .unwrap();

    let report = dead_code(&conn, 100).unwrap();
    let by_name: HashMap<&str, _> = report
        .symbols
        .iter()
        .map(|symbol| (symbol.name.as_str(), symbol))
        .collect();

    assert_eq!(by_name["pending_hint"].confidence, "low");
    assert!(by_name["pending_hint"].reason.contains("unresolved call"));
    assert_eq!(by_name["import_hint"].confidence, "low");
    assert!(by_name["import_hint"].reason.contains("import evidence"));
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
