//! A symbol's stored signature is bounded, and minified or generated files are
//! never indexed (issue #1650).

use std::collections::BTreeSet;
use std::fs;
use std::path::Path;

use graphtrail::extractors::typescript::extract_typescript;
use graphtrail::query::doctor::doctor;
use graphtrail::store::{init_schema, open_db, sync_repo};
use rusqlite::Connection;

/// Longest stored signature body before the `…` marker.
const CAP: usize = 256;
const MARKER: &str = "…";

fn signatures(source: &str) -> Vec<String> {
    extract_typescript("a.ts", source, "hash")
        .unwrap()
        .symbols
        .into_iter()
        .map(|symbol| symbol.signature)
        .collect()
}

#[test]
fn one_line_signature_of_5kb_is_capped_with_a_marker() {
    let params = "alpha: string, ".repeat(340);
    let source = format!("function wide({params}last: number) {{ return 1; }}\n");
    assert!(source.len() > 5000);

    let sigs = signatures(&source);
    assert_eq!(sigs.len(), 1);
    let sig = &sigs[0];
    assert!(sig.ends_with(MARKER), "capped signature keeps a marker");
    assert!(sig.len() <= CAP + MARKER.len(), "len {}", sig.len());
    assert!(sig.starts_with("function wide(alpha: string, "));
}

#[test]
fn cap_never_splits_a_multibyte_character() {
    // "function f(" is 11 bytes. 244 ASCII bytes put a 3-byte character at
    // bytes 255..258, so a naive cut at 256 would land inside it.
    let source = format!("function f({}日本語, z) {{}}\n", "a".repeat(244));
    let sigs = signatures(&source);
    let sig = &sigs[0];
    assert!(sig.ends_with(MARKER));
    let body = sig.strip_suffix(MARKER).unwrap();
    assert_eq!(body.len(), 255, "cut backs off to the char boundary");
    assert!(!body.contains('日'));
}

#[test]
fn short_signature_is_unchanged_byte_for_byte() {
    let sigs =
        signatures("export function add(a: number, b: number): number {\n  return a + b;\n}\n");
    assert_eq!(
        sigs,
        vec!["export function add(a: number, b: number): number {"]
    );
}

#[test]
fn single_line_minified_file_is_skipped_and_counted() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let mut minified = String::new();
    for i in 0..1500 {
        minified.push_str(&format!("function f{i}(a,b){{return a+b+{i}}}"));
    }
    assert!(minified.len() > 50_000 && !minified.contains('\n'));
    write_file(root.join("assets/app.js"), &minified);
    write_file(root.join("vendor_lib.min.js"), "function x(){return 1}\n");
    write_file(root.join("out.bundle.js"), "function y(){return 1}\n");
    write_file(
        root.join("src/normal.ts"),
        "export function normal(a: number): number {\n  return a;\n}\n",
    );

    let conn = open_graph(root);
    let summary = sync_repo(&conn, root).unwrap();

    assert_eq!(summary.files, 1);
    assert_eq!(indexed_paths(&conn), paths(["src/normal.ts"]));
    let signature: String = conn
        .query_row(
            "SELECT signature FROM symbols WHERE name = 'normal'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(signature, "export function normal(a: number): number {");

    let report = doctor(&conn, root, &root.join("g.db")).unwrap();
    assert_eq!(report.ignored.minified, 3);
    assert_eq!(report.pending.new_files, 0, "skipped files are not pending");
}

#[test]
fn long_average_line_marks_a_file_as_generated() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let line = format!("const data = \"{}\";\n", "x".repeat(700));
    write_file(root.join("table.ts"), &line.repeat(10));

    let conn = open_graph(root);
    let summary = sync_repo(&conn, root).unwrap();
    assert_eq!(summary.files, 0);
}

#[test]
fn ordinary_source_with_a_few_long_lines_is_still_indexed() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let mut source = String::new();
    for i in 0..40 {
        source.push_str(&format!(
            "export function f{i}(): number {{\n  return {i};\n}}\n"
        ));
    }
    source.push_str(&format!("// {}\n", "c".repeat(900)));
    write_file(root.join("big.ts"), &source);

    let conn = open_graph(root);
    let summary = sync_repo(&conn, root).unwrap();
    assert_eq!(summary.files, 1);
    assert_eq!(summary.symbols, 40);
}

fn open_graph(root: &Path) -> Connection {
    let conn = open_db(&root.join("g.db")).unwrap();
    init_schema(&conn).unwrap();
    conn
}

fn write_file(path: impl AsRef<Path>, content: &str) {
    let path = path.as_ref();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, content).unwrap();
}

fn indexed_paths(conn: &Connection) -> BTreeSet<String> {
    let mut stmt = conn
        .prepare("SELECT path FROM files ORDER BY path")
        .unwrap();
    stmt.query_map([], |row| row.get::<_, String>(0))
        .unwrap()
        .map(|row| row.unwrap())
        .collect()
}

fn paths<const N: usize>(items: [&str; N]) -> BTreeSet<String> {
    items.into_iter().map(String::from).collect()
}
