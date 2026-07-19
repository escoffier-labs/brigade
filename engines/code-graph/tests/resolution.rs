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
