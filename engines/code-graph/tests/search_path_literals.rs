//! Path filters retain literal metacharacters in both search branches.

use std::collections::BTreeSet;
use std::fs;
use std::process::Command;

use graphtrail::query::search_symbols_with_path;
use graphtrail::store::init_schema;
use rusqlite::{Connection, params};

fn graph(paths: &[&str]) -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    init_schema(&conn).unwrap();
    for (index, path) in paths.iter().enumerate() {
        let id = format!("symbol-{index}");
        conn.execute(
            "INSERT INTO files(path, content_hash, size, modified_at, indexed_at, language)
             VALUES (?1, 'hash', 1, 1, 1, 'python')",
            params![path],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO symbols(id, kind, name, qualified_name, file_path, start_line, end_line, signature, content_hash)
             VALUES (?1, 'function', 'helper', 'helper', ?2, 1, 2, 'def helper():', 'hash')",
            params![id, path],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO symbols_fts(symbol_id, name, qualified_name, signature, file_path)
             VALUES (?1, 'helper', 'helper', 'def helper():', ?2)",
            params![id, path],
        )
        .unwrap();
    }
    conn
}

fn paths(conn: &Connection, query: &str, filter: Option<&str>, limit: usize) -> BTreeSet<String> {
    search_symbols_with_path(conn, query, filter, limit)
        .unwrap()
        .into_iter()
        .map(|row| row.file_path)
        .collect()
}

fn assert_paths(conn: &Connection, query: &str, filter: Option<&str>, expected: &[&str]) {
    assert_eq!(
        paths(conn, query, filter, 100),
        expected.iter().map(|path| path.to_string()).collect(),
        "query={query:?}, filter={filter:?}"
    );
}

fn literal_file_filters(query: &str) {
    for (literal, decoy) in [
        ("src/one_two.py", "src/oneXtwo.py"),
        ("src/per%cent.py", "src/per!cent.py"),
        ("src/bang!name.py", "src/bangname.py"),
        ("src/mix!_%name.py", "src/mix!Xname.py"),
        ("src/caf_é.py", "src/cafXé.py"),
    ] {
        let conn = graph(&[literal, decoy]);
        assert_paths(&conn, query, Some(literal), &[literal]);
    }
}

#[test]
fn fts_file_filters_preserve_literal_metacharacters() {
    literal_file_filters("helper");
}

#[test]
fn fallback_file_filters_preserve_literal_metacharacters() {
    // One-character queries have no FTS token and exercise the LIKE fallback.
    literal_file_filters("h");
}

fn literal_directory_filters(query: &str) {
    for (literal, decoy) in [
        ("pkg_name", "pkgXname"),
        ("dir%name", "dir!name"),
        ("bang!name", "bangname"),
        ("mix!_%name", "mix!Xname"),
    ] {
        let direct = format!("src/{literal}/mod.py");
        let nested = format!("extra/src/{literal}/mod.py");
        let wrong = format!("src/{decoy}/mod.py");
        let conn = graph(&[&direct, &nested, &wrong]);
        let filter = format!("src/{literal}/");
        // Keep both the directory prefix and the existing substring match.
        assert_paths(&conn, query, Some(&filter), &[&direct, &nested]);
    }
}

#[test]
fn fts_directory_filters_preserve_literal_metacharacters() {
    literal_directory_filters("helper");
}

#[test]
fn fallback_directory_filters_preserve_literal_metacharacters() {
    literal_directory_filters("h");
}

#[test]
fn limit_cannot_select_a_wildcard_decoy() {
    for query in ["helper", "h"] {
        for (literal, decoy) in [
            ("src/one_two.py", "src/oneXtwo.py"),
            ("src/per%cent.py", "src/per!cent.py"),
        ] {
            let conn = graph(&[decoy, literal]);
            assert_eq!(
                paths(&conn, query, Some(literal), 1),
                BTreeSet::from([literal.to_string()]),
                "query={query:?}, literal={literal:?}"
            );
        }
    }
}

#[test]
fn backslash_separators_keep_literal_file_and_directory_filters() {
    let conn = graph(&[
        "src/one_two.py",
        "src/oneXtwo.py",
        "src/per%cent.py",
        "src/per!cent.py",
        "src/bang!name/mod.py",
        "src/bangname/mod.py",
    ]);
    for query in ["helper", "h"] {
        assert_paths(&conn, query, Some(r"src\one_two.py"), &["src/one_two.py"]);
        assert_paths(&conn, query, Some(r"src\per%cent.py"), &["src/per%cent.py"]);
        assert_paths(
            &conn,
            query,
            Some("src\\bang!name\\"),
            &["src/bang!name/mod.py"],
        );
    }
}

#[test]
fn plain_filters_retain_exact_prefix_substring_and_unfiltered_breadth() {
    let all = [
        "src/plain.py",
        "nested/src/plain.py",
        "src/plain.py.extra.py",
        "other/else.py",
    ];
    let conn = graph(&all);
    for query in ["helper", "h"] {
        assert_paths(&conn, query, None, &all);
        assert_paths(&conn, query, Some(""), &all);
        for filter in ["src", "src/", "plain", "src/plain.py", "./src/plain.py"] {
            assert_paths(&conn, query, Some(filter), &all[..3]);
        }
    }
}

#[test]
fn cli_path_filters_are_literal_before_limiting() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    fs::create_dir(root.join("src")).unwrap();
    for name in ["one_two.py", "oneXtwo.py", "per%cent.py", "per!cent.py"] {
        fs::write(root.join("src").join(name), "def helper():\n    return 1\n").unwrap();
    }
    let db = root.join(".graphtrail/graphtrail.db");
    let binary = env!("CARGO_BIN_EXE_graphtrail");
    let sync = Command::new(binary)
        .arg("--db")
        .arg(&db)
        .arg("sync")
        .arg(root)
        .output()
        .unwrap();
    assert!(sync.status.success(), "{sync:?}");
    for query in ["helper", "h"] {
        for (filter, expected) in [
            ("src/one_two.py", "src/one_two.py"),
            ("src/per%cent.py", "src/per%cent.py"),
            (r"src\one_two.py", "src/one_two.py"),
        ] {
            let output = Command::new(binary)
                .arg("--db")
                .arg(&db)
                .args(["search", query, "--path", filter, "--limit", "1", "--json"])
                .output()
                .unwrap();
            assert!(output.status.success(), "{output:?}");
            let rows: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
            assert_eq!(rows[0]["file_path"], expected, "{rows}");
            assert_eq!(rows.as_array().unwrap().len(), 1);
        }
    }
}
