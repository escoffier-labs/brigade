//! Integration test: call-edge resolution prefers same-file targets over cross-file homonyms.

use std::fs;

use graphtrail::store::{init_schema, open_db, sync_repo, sync_repo_force};

#[test]
fn same_file_call_resolves_to_same_file_symbol() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();

    // caller.py defines `helper` and calls it -> should link to the LOCAL helper, not other.py's.
    fs::write(
        root.join("caller.py"),
        r#"
def helper():
    return 1

def run():
    return helper()
"#,
    )
    .unwrap();
    fs::write(
        root.join("other.py"),
        r#"
def helper():
    return 2
"#,
    )
    .unwrap();

    let db = root.join("graphtrail.db");
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();

    // The edge from run -> helper must target the helper defined in caller.py.
    let target_file: String = conn
        .query_row(
            r#"
            SELECT dst.file_path
            FROM edges e
            JOIN symbols src ON src.id = e.source
            JOIN symbols dst ON dst.id = e.target
            WHERE src.name = 'run' AND dst.name = 'helper'
            "#,
            [],
            |row| row.get(0),
        )
        .expect("run -> helper edge should exist");

    assert_eq!(target_file, "caller.py");
}

#[test]
fn cross_file_fallback_edges_are_capped_in_stable_order() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();

    fs::write(
        root.join("caller.py"),
        r#"
def run():
    return target()
"#,
    )
    .unwrap();

    for i in (0..10).rev() {
        fs::write(
            root.join(format!("target_{i:02}.py")),
            r#"
def target():
    return 1
"#,
        )
        .unwrap();
    }

    let db = root.join("graphtrail.db");
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();

    let mut stmt = conn
        .prepare(
            r#"
            SELECT dst.file_path
            FROM edges e
            JOIN symbols src ON src.id = e.source
            JOIN symbols dst ON dst.id = e.target
            WHERE src.name = 'run' AND dst.name = 'target'
            ORDER BY dst.file_path
            "#,
        )
        .unwrap();
    let target_files: Vec<String> = stmt
        .query_map([], |row| row.get(0))
        .unwrap()
        .map(|row| row.unwrap())
        .collect();

    assert_eq!(
        target_files,
        vec![
            "target_00.py",
            "target_01.py",
            "target_02.py",
            "target_03.py",
            "target_04.py",
            "target_05.py",
            "target_06.py",
            "target_07.py",
        ]
    );
}

#[test]
fn imported_python_call_resolves_to_imported_file_before_global_fallback() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();

    fs::create_dir(root.join("services")).unwrap();
    fs::write(
        root.join("caller.py"),
        r#"
from services.email import send

def run():
    return send()
"#,
    )
    .unwrap();
    fs::write(
        root.join("services").join("email.py"),
        r#"
def send():
    return 1
"#,
    )
    .unwrap();
    fs::write(
        root.join("local.py"),
        r#"
def send():
    return 2
"#,
    )
    .unwrap();

    let db = root.join("graphtrail.db");
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();

    let target_file: String = conn
        .query_row(
            r#"
            SELECT dst.file_path
            FROM edges e
            JOIN symbols src ON src.id = e.source
            JOIN symbols dst ON dst.id = e.target
            WHERE src.name = 'run' AND dst.name = 'send'
            "#,
            [],
            |row| row.get(0),
        )
        .expect("run -> imported send edge should exist");

    assert_eq!(target_file, "services/email.py");
}

#[test]
fn python_relative_parent_imported_module_resolves_qualified_call() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();

    fs::create_dir_all(root.join("pkg").join("commands").join("sub")).unwrap();
    fs::write(root.join("pkg").join("__init__.py"), "").unwrap();
    fs::write(root.join("pkg").join("commands").join("__init__.py"), "").unwrap();
    fs::write(
        root.join("pkg")
            .join("commands")
            .join("sub")
            .join("caller.py"),
        r#"
from .. import handoff_cmd

def run():
    handoff_cmd.lint()
"#,
    )
    .unwrap();
    fs::write(
        root.join("pkg").join("commands").join("handoff_cmd.py"),
        r#"
def lint():
    return 1
"#,
    )
    .unwrap();
    fs::write(
        root.join("other.py"),
        r#"
def lint():
    return 2
"#,
    )
    .unwrap();

    let db = root.join("graphtrail.db");
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();

    let target_file: String = conn
        .query_row(
            r#"
            SELECT dst.file_path
            FROM edges e
            JOIN symbols src ON src.id = e.source
            JOIN symbols dst ON dst.id = e.target
            WHERE src.name = 'run' AND dst.name = 'lint'
            "#,
            [],
            |row| row.get(0),
        )
        .expect("run -> imported module lint edge should exist");

    assert_eq!(target_file, "pkg/commands/handoff_cmd.py");
}

#[test]
fn python_relative_sibling_imported_function_resolves_bare_call() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();

    fs::create_dir(root.join("pkg")).unwrap();
    fs::write(root.join("pkg").join("__init__.py"), "").unwrap();
    fs::write(
        root.join("pkg").join("caller.py"),
        r#"
from .sibling import func

def run():
    func()
"#,
    )
    .unwrap();
    fs::write(
        root.join("pkg").join("sibling.py"),
        r#"
def func():
    return 1
"#,
    )
    .unwrap();
    fs::write(
        root.join("other.py"),
        r#"
def func():
    return 2
"#,
    )
    .unwrap();

    let db = root.join("graphtrail.db");
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();

    let target_file: String = conn
        .query_row(
            r#"
            SELECT dst.file_path
            FROM edges e
            JOIN symbols src ON src.id = e.source
            JOIN symbols dst ON dst.id = e.target
            WHERE src.name = 'run' AND dst.name = 'func'
            "#,
            [],
            |row| row.get(0),
        )
        .expect("run -> imported sibling func edge should exist");

    assert_eq!(target_file, "pkg/sibling.py");
}

#[test]
fn scoped_rust_call_resolves_to_matching_impl_container_only() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();

    fs::write(
        root.join("lib.rs"),
        r#"
struct A;
struct B;

impl A {
    fn new() -> A { A }
}

impl B {
    fn new() -> B { B }
}

fn run() {
    A::new();
}
"#,
    )
    .unwrap();

    let db = root.join("graphtrail.db");
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();

    let targets: Vec<String> = {
        let mut stmt = conn
            .prepare(
                r#"
                SELECT dst.qualified_name
                FROM edges e
                JOIN symbols src ON src.id = e.source
                JOIN symbols dst ON dst.id = e.target
                WHERE src.name = 'run' AND dst.name = 'new'
                ORDER BY dst.qualified_name
                "#,
            )
            .unwrap();
        stmt.query_map([], |row| row.get(0))
            .unwrap()
            .map(|row| row.unwrap())
            .collect()
    };

    assert_eq!(targets, vec!["A.new"]);
}

#[test]
fn unresolved_import_match_suppresses_global_fallback() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();

    fs::create_dir(root.join("src")).unwrap();
    fs::write(
        root.join("src").join("caller.ts"),
        r#"
import { parse } from "../missing";

export function run() {
  parse();
}
"#,
    )
    .unwrap();
    fs::write(
        root.join("src").join("local.ts"),
        r#"
export function parse() {
  return 1;
}
"#,
    )
    .unwrap();
    fs::write(
        root.join("other.ts"),
        r#"
export function parse() {
  return 2;
}
"#,
    )
    .unwrap();

    let db = root.join("graphtrail.db");
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();

    let edge_count: i64 = conn
        .query_row(
            r#"
            SELECT COUNT(*)
            FROM edges e
            JOIN symbols src ON src.id = e.source
            JOIN symbols dst ON dst.id = e.target
            WHERE src.name = 'run' AND dst.name = 'parse'
            "#,
            [],
            |row| row.get(0),
        )
        .unwrap();

    assert_eq!(edge_count, 0);
}

#[test]
fn relative_parent_import_resolves_to_normalized_file() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();

    fs::create_dir_all(root.join("src").join("app")).unwrap();
    fs::write(
        root.join("src").join("app").join("caller.ts"),
        r#"
import { parse } from "../util";

export function run() {
  parse();
}
"#,
    )
    .unwrap();
    fs::write(
        root.join("src").join("util.ts"),
        r#"
export function parse() {
  return 1;
}
"#,
    )
    .unwrap();
    fs::write(
        root.join("src").join("app").join("util.ts"),
        r#"
export function parse() {
  return 2;
}
"#,
    )
    .unwrap();

    let db = root.join("graphtrail.db");
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();

    let target_file: String = conn
        .query_row(
            r#"
            SELECT dst.file_path
            FROM edges e
            JOIN symbols src ON src.id = e.source
            JOIN symbols dst ON dst.id = e.target
            WHERE src.name = 'run' AND dst.name = 'parse'
            "#,
            [],
            |row| row.get(0),
        )
        .expect("run -> imported parse edge should exist");

    assert_eq!(target_file, "src/util.ts");
}

#[test]
fn rust_grouped_use_alias_resolves_imported_call() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();

    fs::create_dir(root.join("src")).unwrap();
    fs::write(
        root.join("src").join("lib.rs"),
        r#"
use crate::factory::{build as make};

fn run() {
    make();
}
"#,
    )
    .unwrap();
    fs::write(
        root.join("src").join("factory.rs"),
        r#"
pub fn build() {}
"#,
    )
    .unwrap();
    fs::write(
        root.join("src").join("other.rs"),
        r#"
pub fn build() {}
"#,
    )
    .unwrap();

    let db = root.join("graphtrail.db");
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();

    let target_file: String = conn
        .query_row(
            r#"
            SELECT dst.file_path
            FROM edges e
            JOIN symbols src ON src.id = e.source
            JOIN symbols dst ON dst.id = e.target
            WHERE src.name = 'run' AND dst.name = 'build'
            "#,
            [],
            |row| row.get(0),
        )
        .expect("run -> imported build edge should exist");

    assert_eq!(target_file, "src/factory.rs");
}

#[test]
fn rust_crate_use_resolves_bare_imported_function_call() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();

    fs::create_dir(root.join("src")).unwrap();
    fs::write(
        root.join("src").join("lib.rs"),
        r#"
use crate::m::f;
use graphtrail::m::g;

fn run() {
    f();
    g();
}
"#,
    )
    .unwrap();
    fs::write(
        root.join("src").join("m.rs"),
        r#"
pub fn f() {}
pub fn g() {}
"#,
    )
    .unwrap();
    fs::write(
        root.join("src").join("other.rs"),
        r#"
pub fn f() {}
pub fn g() {}
"#,
    )
    .unwrap();

    let db = root.join("graphtrail.db");
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();

    let target_file: String = conn
        .query_row(
            r#"
            SELECT dst.file_path
            FROM edges e
            JOIN symbols src ON src.id = e.source
            JOIN symbols dst ON dst.id = e.target
            WHERE src.name = 'run' AND dst.name = 'f'
            "#,
            [],
            |row| row.get(0),
        )
        .expect("run -> imported crate function edge should exist");

    assert_eq!(target_file, "src/m.rs");

    let graphtrail_target_file: String = conn
        .query_row(
            r#"
            SELECT dst.file_path
            FROM edges e
            JOIN symbols src ON src.id = e.source
            JOIN symbols dst ON dst.id = e.target
            WHERE src.name = 'run' AND dst.name = 'g'
            "#,
            [],
            |row| row.get(0),
        )
        .expect("run -> imported graphtrail crate function edge should exist");

    assert_eq!(graphtrail_target_file, "src/m.rs");
}

#[test]
fn go_imported_package_call_resolves_without_dot_alias() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();

    fs::create_dir(root.join("pkg")).unwrap();
    fs::write(
        root.join("caller.go"),
        r#"
package main

import "pkg"

func run() {
    pkg.Func()
}
"#,
    )
    .unwrap();
    fs::write(
        root.join("pkg").join("pkg.go"),
        r#"
package pkg

func Func() {}
"#,
    )
    .unwrap();
    fs::write(
        root.join("other.go"),
        r#"
package main

func Func() {}
"#,
    )
    .unwrap();

    let db = root.join("graphtrail.db");
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();

    let target_file: String = conn
        .query_row(
            r#"
            SELECT dst.file_path
            FROM edges e
            JOIN symbols src ON src.id = e.source
            JOIN symbols dst ON dst.id = e.target
            WHERE src.name = 'run' AND dst.name = 'Func'
            "#,
            [],
            |row| row.get(0),
        )
        .expect("run -> imported go package function edge should exist");

    assert_eq!(target_file, "pkg/pkg.go");
}

#[test]
fn schema_v1_imports_upgrade_and_force_sync_are_idempotent() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();

    fs::write(
        root.join("a.py"),
        r#"
from b import helper

def run():
    helper()
"#,
    )
    .unwrap();
    fs::write(
        root.join("b.py"),
        r#"
def helper():
    return 1
"#,
    )
    .unwrap();

    let db = root.join("graphtrail.db");
    let conn = open_db(&db).unwrap();
    conn.execute_batch(
        r#"
        CREATE TABLE imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            module TEXT NOT NULL,
            line INTEGER NOT NULL
        );
        "#,
    )
    .unwrap();

    init_schema(&conn).unwrap();
    sync_repo_force(&conn, root, true).unwrap();
    sync_repo_force(&conn, root, true).unwrap();

    for column in ["local_name", "imported_name", "alias"] {
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM pragma_table_info('imports') WHERE name = ?1",
                [column],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 1, "{column} column should exist once");
    }

    let import_rows: i64 = conn
        .query_row("SELECT COUNT(*) FROM imports", [], |row| row.get(0))
        .unwrap();
    let edge_rows: i64 = conn
        .query_row("SELECT COUNT(*) FROM edges", [], |row| row.get(0))
        .unwrap();

    assert_eq!(import_rows, 1);
    assert_eq!(edge_rows, 1);
}

fn write_resolution_file(root: &std::path::Path, path: &str, content: &str) {
    let path = root.join(path);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, content).unwrap();
}

fn python_layout_fixture(root: &std::path::Path, source_root: &str) -> rusqlite::Connection {
    write_resolution_file(root, &format!("{source_root}pkg/__init__.py"), "");
    write_resolution_file(
        root,
        &format!("{source_root}pkg/a.py"),
        "def foo():\n    return 1\n",
    );
    // A nested source root serves callers under its parent, not the whole repo.
    let tests = if source_root == "lib/python/" {
        "lib/tests"
    } else {
        "tests"
    };
    write_resolution_file(
        root,
        &format!("{tests}/test_bare.py"),
        "from pkg.a import foo\n\ndef test_bare():\n    foo()\n",
    );
    write_resolution_file(
        root,
        &format!("{tests}/test_qualified.py"),
        "from pkg import a\n\ndef test_qualified():\n    a.foo()\n",
    );
    let conn = open_db(&root.join("g.db")).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();
    conn
}

fn assert_python_layout_calls(conn: &rusqlite::Connection, target_file: &str) {
    for source in ["test_bare", "test_qualified"] {
        let rows = graphtrail::store::explain_calls(conn, source, "foo").unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].resolution, "import-strict", "{rows:?}");
        assert_eq!(rows[0].targets.len(), 1);
        assert_eq!(rows[0].targets[0].file_path, target_file);
        assert_eq!(rows[0].targets[0].confidence, 0.9);
        let edge: (String, f64) = conn
            .query_row(
                "SELECT dst.file_path, e.confidence FROM edges e
             JOIN symbols src ON src.id = e.source JOIN symbols dst ON dst.id = e.target
             WHERE src.name = ?1",
                [source],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(edge, (target_file.to_string(), 0.9));
    }
}

#[test]
fn python_absolute_imports_resolve_under_inferred_source_roots() {
    for source_root in ["src/", "lib/python/"] {
        let dir = tempfile::tempdir().unwrap();
        let conn = python_layout_fixture(dir.path(), source_root);
        assert_python_layout_calls(&conn, &format!("{source_root}pkg/a.py"));
    }
}

#[test]
fn python_flat_layout_keeps_repo_root_precedence() {
    let dir = tempfile::tempdir().unwrap();
    let conn = python_layout_fixture(dir.path(), "");
    // A second package with the same absolute module must not compete with the flat one.
    write_resolution_file(dir.path(), "src/pkg/__init__.py", "");
    write_resolution_file(dir.path(), "src/pkg/a.py", "def foo():\n    return 2\n");
    sync_repo(&conn, dir.path()).unwrap();
    assert_python_layout_calls(&conn, "pkg/a.py");
}

#[test]
fn python_nested_packages_are_not_source_roots() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(root, "src/app/__init__.py", "");
    write_resolution_file(root, "src/app/cli/__init__.py", "");
    write_resolution_file(root, "src/app/cli/x.py", "def run():\n    return 1\n");
    // `cli` here is a third-party package; src/app is not on sys.path.
    write_resolution_file(
        root,
        "tools/script.py",
        "from cli.x import run\n\ndef main():\n    run()\n",
    );
    let conn = open_db(&root.join("g.db")).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();
    let rows = graphtrail::store::explain_calls(&conn, "main", "run").unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].resolution, "unresolved-external", "{rows:?}");
    assert!(rows[0].targets.is_empty());
}

#[test]
fn affected_attributes_src_layout_test_callers() {
    let dir = tempfile::tempdir().unwrap();
    let conn = python_layout_fixture(dir.path(), "src/");
    let report = graphtrail::query::affected(&conn, &["src/pkg/a.py".to_string()], 3).unwrap();
    let files: Vec<_> = report
        .affected_tests
        .iter()
        .map(|test| test.file_path.as_str())
        .collect();
    assert_eq!(
        files,
        ["tests/test_bare.py", "tests/test_qualified.py"],
        "{report:?}"
    );
    assert!(report.affected_tests.iter().all(|test| test.min_hops == 1));
}

#[test]
fn name_fallback_uses_only_the_callers_language_family() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(
        root,
        "caller.go",
        "package main\nfunc caller() { dispatch(); helper() }\n",
    );
    write_resolution_file(
        root,
        "callee.py",
        "def dispatch():\n    pass\ndef helper():\n    pass\n",
    );
    write_resolution_file(root, "callee.go", "package main\nfunc helper() {}\n");
    let conn = open_db(&root.join("g.db")).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();
    let absent = graphtrail::store::explain_calls(&conn, "caller", "dispatch").unwrap();
    assert_eq!(absent.len(), 1);
    assert_eq!(absent[0].resolution, "no-candidates", "{absent:?}");
    assert!(absent[0].targets.is_empty());
    let unique = graphtrail::store::explain_calls(&conn, "caller", "helper").unwrap();
    assert_eq!(unique[0].resolution, "unique-name", "{unique:?}");
    assert_eq!(unique[0].targets.len(), 1);
    assert_eq!(unique[0].targets[0].file_path, "callee.go");
    assert_eq!(unique[0].targets[0].confidence, 0.7);
    let edges: i64 = conn
        .query_row("SELECT COUNT(*) FROM edges", [], |row| row.get(0))
        .unwrap();
    assert_eq!(
        edges, 1,
        "the cross-family dispatch call must produce zero edges"
    );
}

#[test]
fn resolver_upgrade_rebuilds_existing_edges_without_reparsing() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let conn = python_layout_fixture(root, "src/");
    write_resolution_file(root, "caller.go", "package main\nfunc caller() { foo() }\n");
    sync_repo(&conn, root).unwrap();
    // Re-extraction writes these tables. Reject it even if timestamps would be identical.
    conn.execute_batch("CREATE TRIGGER no_file_reparse BEFORE INSERT ON files BEGIN SELECT RAISE(ABORT, 'unexpected reparse'); END;
        CREATE TRIGGER no_symbol_reparse BEFORE DELETE ON symbols BEGIN SELECT RAISE(ABORT, 'unexpected reparse'); END;").unwrap();
    for old_version in [None, Some("obsolete")] {
        conn.execute("DELETE FROM meta WHERE key = 'resolver_version'", [])
            .unwrap();
        if let Some(version) = old_version {
            graphtrail::store::meta::upsert(&conn, "resolver_version", version).unwrap();
        }
        // Simulate old derived state: missing Python edges plus an erroneous Go -> Python edge.
        conn.execute_batch(
            "DELETE FROM edges;
            INSERT INTO edges(source, target, kind, line, confidence)
            SELECT src.id, dst.id, 'calls', 2, 0.7 FROM symbols src, symbols dst
            WHERE src.name = 'caller' AND dst.name = 'foo';",
        )
        .unwrap();
        let summary = sync_repo(&conn, root).unwrap();
        assert!(
            !summary.unchanged,
            "an outdated resolver must rebuild edges"
        );
        assert_python_layout_calls(&conn, "src/pkg/a.py");
        let edges: i64 = conn
            .query_row("SELECT COUNT(*) FROM edges", [], |row| row.get(0))
            .unwrap();
        assert_eq!(edges, 2, "only the two Python imports should have edges");
        assert!(
            graphtrail::store::meta::read(&conn, "resolver_version")
                .unwrap()
                .is_some()
        );
        assert!(
            sync_repo(&conn, root).unwrap().unchanged,
            "the upgrade must run only once"
        );
    }
}

fn resolution_db(root: &std::path::Path) -> rusqlite::Connection {
    let conn = open_db(&root.join("g.db")).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();
    conn
}

#[test]
fn python_module_assignment_shadows_exported_definition() {
    for assignment in [
        "helper = 42",
        "helper: int = 42",
        "helper, other = (42, 0)",
        "(other, [helper]) = (0, [42])",
        "other = helper = 42",
        "helper += 1",
        "helper = other_helper",
    ] {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        write_resolution_file(
            root,
            "core.py",
            &format!("def helper():\n    pass\n\n{assignment}\n"),
        );
        write_resolution_file(
            root,
            "use.py",
            "from core import helper\n\ndef run():\n    return helper()\n",
        );
        let conn = resolution_db(root);
        assert_unresolved_call(&conn, "run", "helper", "unresolved-external");
    }
}

#[test]
fn python_conditional_module_assignment_keeps_uncertain_definition() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(
        root,
        "core.py",
        "def helper():\n    pass\n\nif FLAG:\n    helper = 42\n",
    );
    write_resolution_file(
        root,
        "use.py",
        "from core import helper\n\ndef run():\n    return helper()\n",
    );
    let conn = resolution_db(root);
    assert_resolves_to(&conn, "run", "helper", "unique-name", "core.py");
}

#[test]
fn python_module_assignment_preserves_later_and_nonbinding_definitions() {
    for core in [
        "helper = 42\ndef helper():\n    pass\n",
        "def helper():\n    pass\nhelper: int\n",
        "def helper():\n    pass\ndef local():\n    helper = 42\n",
        "def helper():\n    pass\nclass Local:\n    helper = 42\n",
        "def helper():\n    pass\nhelper.attribute = 42\nhelper[0] = 42\n",
        "from seed import helper\n",
    ] {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        write_resolution_file(root, "seed.py", "def helper():\n    pass\n");
        write_resolution_file(root, "core.py", core);
        write_resolution_file(
            root,
            "use.py",
            "from core import helper\n\ndef run():\n    return helper()\n",
        );
        let conn = resolution_db(root);
        let target = if core.starts_with("from seed") {
            "seed.py"
        } else {
            "core.py"
        };
        assert_resolves_to(&conn, "run", "helper", "import-strict", target);
    }
}

#[test]
fn python_module_assignment_survives_reopen_and_incremental_removal() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let original = "def helper():\n    pass\n";
    write_resolution_file(root, "core.py", original);
    write_resolution_file(root, "facade.py", "from core import helper\nhelper = 42\n");
    write_resolution_file(
        root,
        "use.py",
        "from facade import helper\n\ndef run():\n    return helper()\n",
    );
    let conn = resolution_db(root);
    assert_unresolved_call(&conn, "run", "helper", "unresolved-external");
    drop(conn);
    let conn = open_db(&root.join("g.db")).unwrap();
    // Only the caller changes: the unchanged assignment must come from storage.
    write_resolution_file(
        root,
        "use.py",
        "from facade import helper\n\ndef run():\n    return helper()\n\n# changed caller\n",
    );
    sync_repo(&conn, root).unwrap();
    assert_unresolved_call(&conn, "run", "helper", "unresolved-external");
    write_resolution_file(root, "facade.py", "from core import helper\n");
    sync_repo(&conn, root).unwrap();
    assert_resolves_to(&conn, "run", "helper", "import-strict", "core.py");
    // Emulate an old index: no assignment table and the previous fingerprint.
    write_resolution_file(root, "core.py", &format!("{original}helper = 42\n"));
    sync_repo(&conn, root).unwrap();
    conn.execute("DROP TABLE module_assignments", []).unwrap();
    conn.execute(
        "UPDATE files SET extractor_fingerprint = 'python-extractor-v6' WHERE language = 'python'",
        [],
    )
    .unwrap();
    sync_repo(&conn, root).unwrap();
    assert_unresolved_call(&conn, "run", "helper", "unresolved-external");
    fs::remove_file(root.join("core.py")).unwrap();
    sync_repo(&conn, root).unwrap();
    let assignments: i64 = conn
        .query_row("SELECT COUNT(*) FROM module_assignments", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(assignments, 0);
}

#[test]
fn python_module_assignment_obeys_same_line_import_order() {
    for (facade, callable) in [
        ("helper = 42; from seed import helper\n", true),
        ("from seed import helper; helper = 42\n", false),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        write_resolution_file(root, "seed.py", "def helper():\n    pass\n");
        write_resolution_file(root, "facade.py", facade);
        write_resolution_file(
            root,
            "use.py",
            "from facade import helper\n\ndef run():\n    return helper()\n",
        );
        let conn = resolution_db(root);
        if callable {
            assert_resolves_to(&conn, "run", "helper", "import-strict", "seed.py");
        } else {
            assert_unresolved_call(&conn, "run", "helper", "unresolved-external");
        }
    }
}

#[test]
fn python_conditional_module_assignment_keeps_reexported_alias_candidate() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(root, "seed.py", "def original():\n    pass\n");
    write_resolution_file(
        root,
        "facade.py",
        "from seed import original as helper\nif FLAG:\n    helper = 42\n",
    );
    write_resolution_file(root, "api.py", "from facade import helper\n");
    write_resolution_file(
        root,
        "use.py",
        "from api import helper\n\ndef run():\n    return helper()\n",
    );
    let conn = resolution_db(root);
    let rows = graphtrail::store::explain_calls(&conn, "run", "helper").unwrap();
    assert_eq!(rows.len(), 1, "{rows:?}");
    assert_eq!(rows[0].resolution, "import-fallback", "{rows:?}");
    assert_eq!(rows[0].targets.len(), 1, "{rows:?}");
    assert_eq!(rows[0].targets[0].qualified_name, "original");
    assert_eq!(rows[0].targets[0].file_path, "seed.py");
    assert_eq!(rows[0].targets[0].confidence, 0.55);
    let edge: (String, String, f64) = conn
        .query_row(
            "SELECT dst.qualified_name, dst.file_path, e.confidence FROM edges e
         JOIN symbols src ON src.id = e.source JOIN symbols dst ON dst.id = e.target
         WHERE src.name = 'run'",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .unwrap();
    assert_eq!(edge, ("original".to_string(), "seed.py".to_string(), 0.55));
}

fn assert_unresolved_call(conn: &rusqlite::Connection, source: &str, target: &str, path: &str) {
    let rows = graphtrail::store::explain_calls(conn, source, target).unwrap();
    assert_eq!(rows.len(), 1, "{rows:?}");
    assert_eq!(rows[0].resolution, path, "{rows:?}");
    assert!(rows[0].targets.is_empty(), "{rows:?}");
    let edges: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM edges e JOIN symbols s ON s.id = e.source WHERE s.name = ?1",
            [source],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(edges, 0);
}

#[test]
fn python_sibling_services_resolve_only_their_own_package() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    for service in ["a", "b"] {
        write_resolution_file(
            root,
            &format!("services/{service}/src/common/__init__.py"),
            "",
        );
        write_resolution_file(
            root,
            &format!("services/{service}/src/common/api.py"),
            "def work():\n    pass\n",
        );
        write_resolution_file(
            root,
            &format!("services/{service}/tests/test_api.py"),
            &format!("from common.api import work\ndef test_{service}():\n    work()\n"),
        );
    }
    let conn = resolution_db(root);
    for service in ["a", "b"] {
        let rows =
            graphtrail::store::explain_calls(&conn, &format!("test_{service}"), "work").unwrap();
        assert_eq!(rows[0].resolution, "import-strict");
        assert_eq!(rows[0].targets.len(), 1, "{rows:?}");
        assert_eq!(
            rows[0].targets[0].file_path,
            format!("services/{service}/src/common/api.py")
        );
        assert_eq!(rows[0].targets[0].confidence, 0.9);
    }
    let report =
        graphtrail::query::affected(&conn, &["services/b/src/common/api.py".into()], 3).unwrap();
    let files: Vec<_> = report
        .affected_tests
        .iter()
        .map(|test| test.file_path.as_str())
        .collect();
    assert_eq!(files, ["services/b/tests/test_api.py"], "{report:?}");
}

#[test]
fn python_fixture_copy_does_not_compete_with_src_package() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    for prefix in ["src", "tests/fixtures/sample"] {
        write_resolution_file(root, &format!("{prefix}/pkg/__init__.py"), "");
        write_resolution_file(
            root,
            &format!("{prefix}/pkg/api.py"),
            "def work():\n    pass\n",
        );
    }
    write_resolution_file(
        root,
        "tests/test_api.py",
        "from pkg.api import work\ndef test_api():\n    work()\n",
    );
    let conn = resolution_db(root);
    let rows = graphtrail::store::explain_calls(&conn, "test_api", "work").unwrap();
    assert_eq!(rows[0].resolution, "import-strict");
    assert_eq!(rows[0].targets.len(), 1, "{rows:?}");
    assert_eq!(rows[0].targets[0].file_path, "src/pkg/api.py");
}

#[test]
fn python_namespace_parent_does_not_shadow_stdlib() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(
        root,
        "src/acme/logging/__init__.py",
        "def getLogger():\n    pass\n",
    );
    write_resolution_file(
        root,
        "src/app/main.py",
        "import logging\ndef run():\n    logging.getLogger()\n",
    );
    let conn = resolution_db(root);
    assert_unresolved_call(&conn, "run", "getLogger", "unresolved-external");
}

#[test]
fn python_yaml_fixture_does_not_enable_name_fallback() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(root, "tests/fixtures/sample_project/yaml/__init__.py", "");
    write_resolution_file(root, "src/app/compat.py", "def safe_load():\n    pass\n");
    write_resolution_file(
        root,
        "src/app/main.py",
        "import yaml\ndef run():\n    yaml.safe_load()\n",
    );
    let conn = resolution_db(root);
    assert_unresolved_call(&conn, "run", "safe_load", "unresolved-external");
}

#[test]
fn python_deepest_serving_root_wins() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    for prefix in ["src", "services/a/src"] {
        write_resolution_file(root, &format!("{prefix}/pkg/__init__.py"), "");
        write_resolution_file(
            root,
            &format!("{prefix}/pkg/api.py"),
            "def work():\n    pass\n",
        );
    }
    write_resolution_file(
        root,
        "services/a/tests/test_api.py",
        "from pkg.api import work\ndef test_api():\n    work()\n",
    );
    let conn = resolution_db(root);
    let rows = graphtrail::store::explain_calls(&conn, "test_api", "work").unwrap();
    assert_eq!(rows[0].targets.len(), 1, "{rows:?}");
    assert_eq!(rows[0].targets[0].file_path, "services/a/src/pkg/api.py");
}

#[test]
fn python_tied_serving_roots_emit_no_targets() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    for prefix in ["src", "lib"] {
        write_resolution_file(root, &format!("{prefix}/pkg/__init__.py"), "");
        write_resolution_file(
            root,
            &format!("{prefix}/pkg/api.py"),
            "def work():\n    pass\n",
        );
    }
    write_resolution_file(
        root,
        "tests/test_api.py",
        "from pkg.api import work\ndef test_api():\n    work()\n",
    );
    let conn = resolution_db(root);
    assert_unresolved_call(&conn, "test_api", "work", "unresolved-external");
}

#[test]
fn go_import_directory_matches_only_direct_go_files() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(
        root,
        "main.go",
        "package main\nimport \"example.com/app/web\"\nfunc run() { web.Render() }\n",
    );
    write_resolution_file(root, "web/render.go", "package web\nfunc Render() {}\n");
    write_resolution_file(root, "web/render.ts", "export function Render() {}\n");
    write_resolution_file(
        root,
        "web/nested/render.go",
        "package nested\nfunc Render() {}\n",
    );
    let conn = resolution_db(root);
    let rows = graphtrail::store::explain_calls(&conn, "run", "Render").unwrap();
    assert_eq!(rows[0].resolution, "import-strict");
    assert_eq!(rows[0].targets.len(), 1, "{rows:?}");
    assert_eq!(rows[0].targets[0].file_path, "web/render.go");
}

#[test]
fn go_external_import_with_ts_only_suffix_is_external() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(
        root,
        "main.go",
        "package main\nimport \"example.com/app/web\"\nfunc run() { web.Render() }\n",
    );
    write_resolution_file(root, "web/tool.ts", "export function other() {}\n");
    write_resolution_file(root, "compat.go", "package main\nfunc Render() {}\n");
    let conn = resolution_db(root);
    assert_unresolved_call(&conn, "run", "Render", "unresolved-external");
}

#[test]
fn rust_import_directory_does_not_match_python() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(
        root,
        "src/lib.rs",
        "use crate::util::parse;\nfn run() { parse(); }\n",
    );
    write_resolution_file(root, "src/util/parse.py", "def parse():\n    pass\n");
    let conn = resolution_db(root);
    assert_unresolved_call(&conn, "run", "parse", "unresolved-external");
}

#[test]
fn python_import_fallback_does_not_match_typescript_names() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(
        root,
        "pkg/main.py",
        "from . import helpers\ndef run():\n    helpers.lint()\n",
    );
    write_resolution_file(root, "pkg/helpers.py", "def other():\n    pass\n");
    write_resolution_file(root, "tools.ts", "export function lint() {}\n");
    let conn = resolution_db(root);
    assert_unresolved_call(&conn, "run", "lint", "no-candidates");
}

#[test]
fn older_writer_sync_provenance_forces_edge_rebuild_without_reparse() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(
        root,
        "main.py",
        "def helper():\n    pass\ndef run():\n    helper()\n",
    );
    let conn = resolution_db(root);
    conn.execute_batch("CREATE TRIGGER no_reparse BEFORE DELETE ON symbols BEGIN SELECT RAISE(ABORT, 'unexpected reparse'); END;
        UPDATE edges SET confidence = 0.55;
        UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'synced_at';").unwrap();
    let summary = sync_repo(&conn, root).unwrap();
    assert!(
        !summary.unchanged,
        "older writer changed edges without updating the resolver marker"
    );
    let confidence: f64 = conn
        .query_row("SELECT confidence FROM edges", [], |row| row.get(0))
        .unwrap();
    assert_eq!(confidence, 0.8);
    assert_eq!(
        graphtrail::store::meta::read(&conn, "resolver_synced_at").unwrap(),
        graphtrail::store::meta::read(&conn, "synced_at").unwrap()
    );
    assert!(sync_repo(&conn, root).unwrap().unchanged);
}

/// The #1647 repro: a package `__init__` re-exports `helper` from `core`, and
/// `core` has a decorated function plus an `if __name__ == "__main__"` block.
fn reexport_fixture(root: &std::path::Path, init: &str) -> rusqlite::Connection {
    write_resolution_file(root, "pkg/__init__.py", init);
    write_resolution_file(
        root,
        "pkg/core.py",
        "import functools\n\ndef helper():\n    return 1\n\n@functools.lru_cache(maxsize=None)\ndef cached():\n    return helper()\n\ndef main():\n    return cached()\n\nif __name__ == \"__main__\":\n    main()\n",
    );
    write_resolution_file(
        root,
        "use.py",
        "from pkg import helper\n\ndef run():\n    return helper()\n",
    );
    resolution_db(root)
}

fn assert_resolves_to(
    conn: &rusqlite::Connection,
    source: &str,
    target: &str,
    path: &str,
    target_file: &str,
) {
    let rows = graphtrail::store::explain_calls(conn, source, target).unwrap();
    assert_eq!(rows.len(), 1, "{rows:?}");
    assert_eq!(rows[0].resolution, path, "{rows:?}");
    assert_eq!(rows[0].targets.len(), 1, "{rows:?}");
    assert_eq!(rows[0].targets[0].file_path, target_file, "{rows:?}");
    let edge_target: String = conn
        .query_row(
            "SELECT dst.file_path FROM edges e
             JOIN symbols src ON src.id = e.source JOIN symbols dst ON dst.id = e.target
             WHERE src.name = ?1 AND dst.name = ?2",
            [source, target],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(edge_target, target_file);
}

#[test]
fn python_explicit_package_reexport_resolves_to_defining_file() {
    let dir = tempfile::tempdir().unwrap();
    let conn = reexport_fixture(dir.path(), "from .core import helper\n");
    assert_resolves_to(&conn, "run", "helper", "import-strict", "pkg/core.py");
}

#[test]
fn python_star_package_reexport_resolves_to_defining_file() {
    let dir = tempfile::tempdir().unwrap();
    let conn = reexport_fixture(dir.path(), "from .core import *\n");
    assert_resolves_to(&conn, "run", "helper", "import-strict", "pkg/core.py");
    let star_rows: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM imports
             WHERE file_path = 'pkg/__init__.py' AND module = '.core' AND imported_name = '*'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(star_rows, 1);
}

#[test]
fn python_absolute_package_reexport_resolves_to_defining_file() {
    let dir = tempfile::tempdir().unwrap();
    let conn = reexport_fixture(dir.path(), "from pkg.core import helper as helper\n");
    assert_resolves_to(&conn, "run", "helper", "import-strict", "pkg/core.py");
}

#[test]
fn python_module_attribute_call_follows_package_reexport() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(root, "pkg/__init__.py", "from .core import helper\n");
    write_resolution_file(root, "pkg/core.py", "def helper():\n    return 1\n");
    write_resolution_file(
        root,
        "use.py",
        "import pkg\n\ndef run():\n    return pkg.helper()\n",
    );
    let conn = resolution_db(root);
    assert_resolves_to(&conn, "run", "helper", "import-strict", "pkg/core.py");
}

#[test]
fn python_reexport_cycle_terminates_and_resolves_through_the_other_branch() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(
        root,
        "a/__init__.py",
        "from b import *\nfrom .core import *\n",
    );
    write_resolution_file(root, "a/core.py", "def helper():\n    return 1\n");
    write_resolution_file(root, "b/__init__.py", "from a import *\n");
    write_resolution_file(
        root,
        "use.py",
        "from b import helper\n\ndef run():\n    return helper()\n",
    );
    let conn = resolution_db(root);
    assert_resolves_to(&conn, "run", "helper", "import-strict", "a/core.py");
}

#[test]
fn python_explicit_reexport_cycle_creates_no_edge() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(root, "a/__init__.py", "from b import helper\n");
    write_resolution_file(root, "b/__init__.py", "from a import helper\n");
    // A homonym elsewhere must not be picked up by the name fallback.
    write_resolution_file(root, "other.py", "def helper():\n    return 2\n");
    write_resolution_file(
        root,
        "use.py",
        "from a import helper\n\ndef run():\n    return helper()\n",
    );
    let conn = resolution_db(root);
    assert_unresolved_call(&conn, "run", "helper", "unresolved-external");
}

#[test]
fn python_reexport_of_missing_name_creates_no_false_edge() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(root, "pkg/__init__.py", "from .core import missing\n");
    write_resolution_file(root, "pkg/core.py", "def helper():\n    return 1\n");
    write_resolution_file(root, "other.py", "def missing():\n    return 2\n");
    write_resolution_file(
        root,
        "use.py",
        "from pkg import missing\n\ndef run():\n    return missing()\n",
    );
    let conn = resolution_db(root);
    assert_unresolved_call(&conn, "run", "missing", "unresolved-external");
}

#[test]
fn python_package_name_without_reexport_falls_back_to_name_match() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    // `pkg/__init__.py` neither defines nor re-exports `helper`, so the import
    // cannot be pinned. The unique-name fallback still finds the definition.
    write_resolution_file(root, "pkg/__init__.py", "");
    write_resolution_file(root, "pkg/core.py", "def helper():\n    return 1\n");
    write_resolution_file(
        root,
        "use.py",
        "from pkg import helper\n\ndef run():\n    return helper()\n",
    );
    let conn = resolution_db(root);
    assert_resolves_to(&conn, "run", "helper", "unique-name", "pkg/core.py");
}

#[test]
fn python_star_import_in_caller_resolves_against_imported_module() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(root, "pkg/__init__.py", "");
    write_resolution_file(root, "pkg/core.py", "def helper():\n    return 1\n");
    write_resolution_file(root, "pkg/other.py", "def helper():\n    return 2\n");
    write_resolution_file(
        root,
        "use.py",
        "from pkg.core import *\n\ndef run():\n    return helper()\n",
    );
    let conn = resolution_db(root);
    assert_resolves_to(&conn, "run", "helper", "import-fallback", "pkg/core.py");
}

#[test]
fn python_module_level_calls_reach_callers_and_dead_code() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let conn = reexport_fixture(root, "from .core import helper\n");
    let callers: Vec<(String, String, String)> = {
        let mut stmt = conn
            .prepare(
                "SELECT src.file_path, src.kind, src.qualified_name FROM edges e
                 JOIN symbols src ON src.id = e.source JOIN symbols dst ON dst.id = e.target
                 WHERE dst.name = 'main'",
            )
            .unwrap();
        stmt.query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))
            .unwrap()
            .map(Result::unwrap)
            .collect()
    };
    assert_eq!(
        callers,
        [(
            "pkg/core.py".to_string(),
            "module".to_string(),
            "<module>".to_string()
        )]
    );
    let span: (i64, i64) = conn
        .query_row(
            "SELECT start_line, end_line FROM symbols WHERE name = 'cached'",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(span, (6, 8));

    // An entry point not named `main` is only reachable from the `__main__` block.
    write_resolution_file(
        root,
        "tool.py",
        "def cli():\n    return 1\n\nif __name__ == \"__main__\":\n    cli()\n",
    );
    sync_repo(&conn, root).unwrap();
    let report = graphtrail::query::dead_code(&conn, 100).unwrap();
    let names: Vec<&str> = report.symbols.iter().map(|s| s.name.as_str()).collect();
    assert!(!names.contains(&"cli"), "{names:?}");
    assert!(!names.contains(&"<module>"), "{names:?}");
}

/// pkg/__init__.py binds `helper` twice: once through a four-hop star chain
/// that ends at shared.py, and once through a direct import of shared.py.
/// shared.py re-exports the real definition in impl.py.
fn diamond_fixture(root: &std::path::Path, init: &str) -> rusqlite::Connection {
    write_resolution_file(root, "pkg/__init__.py", init);
    write_resolution_file(root, "pkg/s1.py", "from .s2 import *\n");
    write_resolution_file(root, "pkg/s2.py", "from .s3 import *\n");
    write_resolution_file(root, "pkg/s3.py", "from .shared import *\n");
    write_resolution_file(root, "pkg/shared.py", "from .impl import helper\n");
    write_resolution_file(root, "pkg/impl.py", "def helper():\n    return 1\n");
    write_resolution_file(
        root,
        "use.py",
        "from pkg import helper\n\ndef run():\n    return helper()\n",
    );
    resolution_db(root)
}

#[test]
fn python_reexport_diamond_resolves_in_either_import_order() {
    for init in [
        "from .s1 import *\nfrom .shared import helper\n",
        "from .shared import helper\nfrom .s1 import *\n",
    ] {
        let dir = tempfile::tempdir().unwrap();
        let conn = diamond_fixture(dir.path(), init);
        assert_resolves_to(&conn, "run", "helper", "import-strict", "pkg/impl.py");
    }
}

#[test]
fn python_long_star_reexport_chain_resolves() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(root, "pkg/__init__.py", "from .m1 import *\n");
    // Deeper than any recursion cap: 80 star hops, every fifth one an explicit import.
    for hop in 1..80 {
        let next = hop + 1;
        let body = if hop % 5 == 0 {
            format!("from .m{next} import helper\n")
        } else {
            format!("from .m{next} import *\n")
        };
        write_resolution_file(root, &format!("pkg/m{hop}.py"), &body);
    }
    write_resolution_file(root, "pkg/m80.py", "def helper():\n    return 1\n");
    write_resolution_file(
        root,
        "use.py",
        "from pkg import helper\n\ndef run():\n    return helper()\n",
    );
    let conn = resolution_db(root);
    assert_resolves_to(&conn, "run", "helper", "import-strict", "pkg/m80.py");
}

/// Every module star-imports every other one, so a lookup of a name nobody
/// binds explores a dense cycle. Path enumeration is exponential in the
/// module count. The fixpoint visits each (module, name) pair a bounded
/// number of times, so this finishes in well under the time bound.
#[test]
fn python_dense_star_cycle_resolves_in_polynomial_time() {
    const MODULES: usize = 14;
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let star_all = |skip: Option<usize>| -> String {
        (0..MODULES)
            .filter(|module| Some(*module) != skip)
            .map(|module| format!("from .m{module} import *\n"))
            .collect()
    };
    write_resolution_file(root, "pkg/__init__.py", &star_all(None));
    for module in 0..MODULES {
        write_resolution_file(root, &format!("pkg/m{module}.py"), &star_all(Some(module)));
    }
    write_resolution_file(
        root,
        "pkg/m7.py",
        &format!("{}def present():\n    return 1\n", star_all(Some(7))),
    );
    write_resolution_file(
        root,
        "use.py",
        "from pkg import absent, present\n\ndef run():\n    absent()\n    present()\n",
    );
    let started = std::time::Instant::now();
    let conn = resolution_db(root);
    let rows = graphtrail::store::explain_calls(&conn, "run", "absent").unwrap();
    assert_eq!(rows.len(), 1, "{rows:?}");
    assert!(rows[0].targets.is_empty(), "{rows:?}");
    assert_resolves_to(&conn, "run", "present", "import-strict", "pkg/m7.py");
    let elapsed = started.elapsed();
    assert!(
        elapsed < std::time::Duration::from_secs(10),
        "dense star cycle took {elapsed:?}"
    );
}

#[test]
fn python_function_local_import_is_not_a_module_export() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(root, "a.py", "def helper():\n    return 1\n");
    write_resolution_file(root, "b.py", "def helper():\n    return 2\n");
    write_resolution_file(
        root,
        "facade.py",
        "from a import helper\n\ndef unrelated():\n    from b import helper\n    return helper()\n\nclass Holder:\n    from b import helper\n",
    );
    write_resolution_file(
        root,
        "use.py",
        "from facade import helper\n\ndef run():\n    return helper()\n",
    );
    // An import under a module-level `if` still binds at module scope.
    write_resolution_file(root, "guarded.py", "if FLAG:\n    from b import helper\n");
    write_resolution_file(
        root,
        "use_guarded.py",
        "from guarded import helper\n\ndef go():\n    return helper()\n",
    );
    // When it may not run, the earlier binding to a different definition
    // still counts, so neither one is claimed strictly.
    write_resolution_file(
        root,
        "either.py",
        "from a import helper\n\nif FLAG:\n    from b import helper\n",
    );
    write_resolution_file(
        root,
        "use_either.py",
        "from either import helper\n\ndef pick():\n    return helper()\n",
    );
    let conn = resolution_db(root);
    assert_resolves_to(&conn, "run", "helper", "import-strict", "a.py");
    assert_resolves_to(&conn, "go", "helper", "import-strict", "b.py");
    let rows = graphtrail::store::explain_calls(&conn, "pick", "helper").unwrap();
    assert_ne!(rows[0].resolution, "import-strict", "{rows:?}");
}

#[test]
fn python_star_import_of_aliased_reexport_resolves() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(root, "core.py", "def original():\n    return 1\n");
    write_resolution_file(root, "facade.py", "from core import original as exported\n");
    write_resolution_file(
        root,
        "use.py",
        "from facade import *\n\ndef run():\n    return exported()\n",
    );
    let conn = resolution_db(root);
    let rows = graphtrail::store::explain_calls(&conn, "run", "exported").unwrap();
    assert_eq!(rows.len(), 1, "{rows:?}");
    assert_eq!(rows[0].resolution, "import-fallback", "{rows:?}");
    assert_eq!(rows[0].targets.len(), 1, "{rows:?}");
    assert_eq!(rows[0].targets[0].file_path, "core.py");
    assert_eq!(rows[0].targets[0].qualified_name, "original");
    let edges: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM edges e JOIN symbols s ON s.id = e.source
             JOIN symbols t ON t.id = e.target WHERE s.name = 'run' AND t.name = 'original'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(edges, 1);
}

#[test]
fn python_star_reexport_respects_dunder_all_and_binding_order() {
    for all in ["__all__ = ['other']\n", "__all__ = ('other',)\n"] {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        write_resolution_file(
            root,
            "pkg/__init__.py",
            "from .a import *\nfrom .b import *\n",
        );
        write_resolution_file(root, "pkg/a.py", "def helper():\n    return 1\n");
        // b defines `helper` but does not export it, so a's binding survives.
        write_resolution_file(
            root,
            "pkg/b.py",
            &format!("{all}\ndef helper():\n    return 2\n\ndef other():\n    return 3\n"),
        );
        write_resolution_file(
            root,
            "use.py",
            "from pkg import helper\n\ndef run():\n    return helper()\n",
        );
        let conn = resolution_db(root);
        assert_resolves_to(&conn, "run", "helper", "import-strict", "pkg/a.py");
    }

    // With only the restricted module, the package does not export `helper`.
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(root, "pkg/__init__.py", "from .core import *\n");
    write_resolution_file(
        root,
        "pkg/core.py",
        "__all__ = ['other']\n\ndef helper():\n    return 1\n\ndef other():\n    return 2\n",
    );
    write_resolution_file(
        root,
        "use.py",
        "from pkg import helper\n\ndef run():\n    return helper()\n",
    );
    let conn = resolution_db(root);
    let rows = graphtrail::store::explain_calls(&conn, "run", "helper").unwrap();
    assert_ne!(rows[0].resolution, "import-strict", "{rows:?}");
}

#[test]
fn python_star_reexport_skips_private_names_unless_listed() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(
        root,
        "pkg/__init__.py",
        "from .a import *\nfrom .b import *\n",
    );
    write_resolution_file(
        root,
        "pkg/a.py",
        "__all__ = ['_hidden']\n\ndef _hidden():\n    return 1\n",
    );
    write_resolution_file(root, "pkg/b.py", "def _hidden():\n    return 2\n");
    write_resolution_file(
        root,
        "use.py",
        "from pkg import _hidden\n\ndef run():\n    return _hidden()\n",
    );
    let conn = resolution_db(root);
    assert_resolves_to(&conn, "run", "_hidden", "import-strict", "pkg/a.py");
}

#[test]
fn python_module_export_requires_a_top_level_definition() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(root, "pkg/__init__.py", "from .core import *\n");
    write_resolution_file(
        root,
        "pkg/core.py",
        "class Box:\n    def helper(self):\n        return 1\n",
    );
    write_resolution_file(
        root,
        "use.py",
        "from pkg import helper\n\ndef run():\n    return helper()\n",
    );
    let conn = resolution_db(root);
    let rows = graphtrail::store::explain_calls(&conn, "run", "helper").unwrap();
    assert_eq!(rows.len(), 1, "{rows:?}");
    assert_ne!(rows[0].resolution, "import-strict", "{rows:?}");
    let strict: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM edges WHERE confidence = 0.9",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(strict, 0);
}

#[test]
fn python_later_star_import_shadows_earlier_one() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_resolution_file(root, "a.py", "def helper():\n    return 1\n");
    write_resolution_file(root, "b.py", "def helper():\n    return 2\n");
    write_resolution_file(
        root,
        "use.py",
        "from a import *\nfrom b import *\n\ndef run():\n    return helper()\n",
    );
    write_resolution_file(
        root,
        "pkg/__init__.py",
        "from a import *\nfrom b import *\n",
    );
    write_resolution_file(
        root,
        "via_pkg.py",
        "from pkg import helper\n\ndef go():\n    return helper()\n",
    );
    let conn = resolution_db(root);
    assert_resolves_to(&conn, "run", "helper", "import-fallback", "b.py");
    assert_resolves_to(&conn, "go", "helper", "import-strict", "b.py");
}

#[test]
fn python_module_symbol_stays_out_of_search() {
    let dir = tempfile::tempdir().unwrap();
    let conn = reexport_fixture(dir.path(), "from .core import helper\n");
    for query in ["core.py", "pkg/core.py", "main"] {
        let rows = graphtrail::query::search_symbols(&conn, query, 20).unwrap();
        assert!(
            rows.iter().all(|row| row.qualified_name != "<module>"),
            "{query}: {rows:?}"
        );
    }
    let rows = graphtrail::query::search_symbols(&conn, "core.py", 20).unwrap();
    assert!(rows.iter().any(|row| row.name == "main"), "{rows:?}");
}

/// A star-import cycle (a -> b -> c -> a) that can supply two different
/// definitions of `helper`: seed's through a, and b's own. Which one Python
/// binds depends on runtime import order, so the resolver must not claim an
/// import-strict edge. Transparent wrappers between the facade and the cycle
/// must not change that.
#[test]
fn python_star_cycle_with_competing_definitions_is_not_import_strict() {
    for wrappers in 0..3 {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        write_resolution_file(root, "seed.py", "def helper():\n    return 0\n");
        write_resolution_file(root, "a.py", "from seed import *\nfrom b import *\n");
        write_resolution_file(
            root,
            "b.py",
            "def helper():\n    return 1\n\nfrom c import *\n",
        );
        write_resolution_file(root, "c.py", "from b import *\nfrom a import *\n");
        // facade -> w0 -> w1 -> ... -> c
        let mut previous = "c".to_string();
        for wrapper in (0..wrappers).rev() {
            let name = format!("w{wrapper}");
            write_resolution_file(
                root,
                &format!("{name}.py"),
                &format!("from {previous} import *\n"),
            );
            previous = name;
        }
        write_resolution_file(root, "facade.py", &format!("from {previous} import *\n"));
        write_resolution_file(
            root,
            "use.py",
            "from facade import helper\n\ndef run():\n    return helper()\n",
        );
        let conn = resolution_db(root);
        let rows = graphtrail::store::explain_calls(&conn, "run", "helper").unwrap();
        assert_eq!(rows.len(), 1, "wrappers={wrappers}: {rows:?}");
        assert_ne!(
            rows[0].resolution, "import-strict",
            "wrappers={wrappers}: {rows:?}"
        );
        let strict: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM edges e JOIN symbols s ON s.id = e.source
                 WHERE s.name = 'run' AND e.confidence = 0.9",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(strict, 0, "wrappers={wrappers}");
    }
}

/// a and b star-import each other, but a's last binding of `helper` is an
/// unconditional `from seed import helper`. That shadows the star import, so
/// Python binds seed's helper whatever the import order, and the cycle no
/// longer matters.
fn shadowed_cycle_fixture(root: &std::path::Path, a: &str) -> rusqlite::Connection {
    write_resolution_file(root, "seed.py", "def helper():\n    return 0\n");
    write_resolution_file(root, "a.py", a);
    write_resolution_file(
        root,
        "b.py",
        "from a import *\n\ndef helper():\n    return 1\n",
    );
    write_resolution_file(
        root,
        "use.py",
        "from a import helper\n\ndef run():\n    return helper()\n",
    );
    resolution_db(root)
}

#[test]
fn python_unconditional_binding_shadows_earlier_cycle_statements() {
    let dir = tempfile::tempdir().unwrap();
    let conn = shadowed_cycle_fixture(dir.path(), "from b import *\nfrom seed import helper\n");
    assert_resolves_to(&conn, "run", "helper", "import-strict", "seed.py");
}

#[test]
fn python_conditional_binding_keeps_earlier_cycle_statements() {
    // Under an `if`, the seed import may not run, so b's helper can still
    // arrive through the star-import cycle and no strict claim is safe.
    for guard in ["if FLAG:\n    ", "try:\n    "] {
        let tail = if guard.starts_with("try") {
            "\nexcept ImportError:\n    pass\n"
        } else {
            "\n"
        };
        let dir = tempfile::tempdir().unwrap();
        let conn = shadowed_cycle_fixture(
            dir.path(),
            &format!("FLAG = True\nfrom b import *\n{guard}from seed import helper{tail}"),
        );
        let rows = graphtrail::store::explain_calls(&conn, "run", "helper").unwrap();
        assert_eq!(rows.len(), 1, "{guard:?}: {rows:?}");
        assert_ne!(rows[0].resolution, "import-strict", "{guard:?}: {rows:?}");
    }
}
