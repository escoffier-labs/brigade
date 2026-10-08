use std::collections::BTreeSet;
use std::fmt::Write;
use std::path::Path;

use graphtrail::store::{init_schema, open_db, sync_repo};
use rusqlite::Connection;

#[derive(Debug, Clone, Eq, Ord, PartialEq, PartialOrd)]
struct Edge {
    source_file: String,
    source: String,
    line: usize,
    target_file: String,
    target: String,
}

impl Edge {
    fn from_tsv(line: &str) -> Self {
        let fields: Vec<&str> = line.split('\t').collect();
        assert_eq!(fields.len(), 5, "expected 5 TSV fields in {line:?}");
        Self {
            source_file: fields[0].to_string(),
            source: fields[1].to_string(),
            line: fields[2]
                .parse()
                .unwrap_or_else(|_| panic!("invalid line number in {line:?}")),
            target_file: fields[3].to_string(),
            target: fields[4].to_string(),
        }
    }
}

impl std::fmt::Display for Edge {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "{}\t{}\t{}\t{}\t{}",
            self.source_file, self.source, self.line, self.target_file, self.target
        )
    }
}

#[test]
fn mixed_language_fixture_edges_match_golden_corpus() {
    let fixture = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/golden/mixed");
    let temp = tempfile::tempdir().unwrap();
    let conn = open_db(&temp.path().join("graphtrail.db")).unwrap();
    init_schema(&conn).unwrap();

    sync_repo(&conn, &fixture).unwrap();

    let expected = expected_edges(include_str!("fixtures/golden/mixed/expected_edges.tsv"));
    let actual = actual_edges(&conn);

    assert_sets_match(&expected, &actual);
}

fn expected_edges(tsv: &str) -> BTreeSet<Edge> {
    tsv.lines()
        .map(str::trim)
        .filter(|line| !line.is_empty() && !line.starts_with('#'))
        .map(Edge::from_tsv)
        .collect()
}

fn actual_edges(conn: &Connection) -> BTreeSet<Edge> {
    let mut stmt = conn
        .prepare(
            r#"
            SELECT src.file_path, src.qualified_name, e.line, dst.file_path, dst.qualified_name
            FROM edges e
            JOIN symbols src ON src.id = e.source
            JOIN symbols dst ON dst.id = e.target
            WHERE e.kind = 'calls'
            ORDER BY src.file_path, src.qualified_name, e.line, dst.file_path, dst.qualified_name
            "#,
        )
        .unwrap();
    stmt.query_map([], |row| {
        Ok(Edge {
            source_file: row.get(0)?,
            source: row.get(1)?,
            line: row.get::<_, i64>(2)? as usize,
            target_file: row.get(3)?,
            target: row.get(4)?,
        })
    })
    .unwrap()
    .map(|row| row.unwrap())
    .collect()
}

fn assert_sets_match(expected: &BTreeSet<Edge>, actual: &BTreeSet<Edge>) {
    let missing: Vec<&Edge> = expected.difference(actual).collect();
    let unexpected: Vec<&Edge> = actual.difference(expected).collect();
    if missing.is_empty() && unexpected.is_empty() {
        return;
    }

    let mut message = String::new();
    if !missing.is_empty() {
        let _ = writeln!(message, "missing expected edges:");
        for edge in missing {
            let _ = writeln!(message, "- {edge}");
        }
    }
    if !unexpected.is_empty() {
        let _ = writeln!(message, "unexpected edges:");
        for edge in unexpected {
            let _ = writeln!(message, "- {edge}");
        }
    }

    panic!("{message}");
}

/// Language family of an indexed path. Kept independent of the resolver's own
/// mapping so a bug there cannot hide itself. An extension missing here fails
/// the test below, which forces a new language to be classified deliberately.
fn family(path: &str) -> &'static str {
    match path.rsplit_once('.').map(|(_, ext)| ext) {
        Some("py") => "python",
        Some("js" | "jsx" | "ts" | "tsx" | "astro") => "js/ts",
        Some("rs") => "rust",
        Some("go") => "go",
        other => panic!("unclassified language for {path:?} (extension {other:?})"),
    }
}

type EdgeRow = (String, String, String, i64, String, String);

fn query_pairs(conn: &Connection, sql: &str) -> Vec<(String, String)> {
    let mut stmt = conn.prepare(sql).unwrap();
    stmt.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))
        .unwrap()
        .map(|row| row.unwrap())
        .collect()
}

#[test]
fn no_resolved_edge_crosses_a_language_family_in_any_golden_corpus() {
    let golden = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/golden");
    let mut corpora: Vec<_> = std::fs::read_dir(&golden)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.is_dir())
        .collect();
    corpora.sort();
    assert!(!corpora.is_empty(), "no golden corpora under {golden:?}");

    let mut violations = String::new();
    let mut total_edges = 0;
    let mut cross_family_name_matches = 0;
    for corpus in &corpora {
        let temp = tempfile::tempdir().unwrap();
        let conn = open_db(&temp.path().join("graphtrail.db")).unwrap();
        init_schema(&conn).unwrap();
        sync_repo(&conn, corpus).unwrap();

        let mut stmt = conn
            .prepare(
                "SELECT src.file_path, src.qualified_name, e.kind, e.line, dst.file_path, dst.qualified_name
                 FROM edges e
                 JOIN symbols src ON src.id = e.source
                 JOIN symbols dst ON dst.id = e.target
                 ORDER BY src.file_path, e.line, dst.file_path",
            )
            .unwrap();
        let edges: Vec<EdgeRow> = stmt
            .query_map([], |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                ))
            })
            .unwrap()
            .map(|row| row.unwrap())
            .collect();
        total_edges += edges.len();
        let name = corpus.file_name().unwrap().to_string_lossy();
        for (source_file, source, kind, line, target_file, target) in edges {
            if family(&source_file) != family(&target_file) {
                let _ = writeln!(
                    violations,
                    "- {name}: {kind} {source_file}:{line} {source} -> {target_file} {target}"
                );
            }
        }

        // Count calls whose bare name also names a symbol in another family, so the
        // assertion above is known to have had something to reject.
        cross_family_name_matches += query_pairs(
            &conn,
            "SELECT p.file_path, s.file_path FROM pending_calls p
             JOIN symbols s ON s.name = p.target_name",
        )
        .iter()
        .filter(|(caller, callee)| family(caller) != family(callee))
        .count();
    }

    assert!(
        violations.is_empty(),
        "resolved edges cross language families:\n{violations}"
    );
    assert!(total_edges > 0, "golden corpora produced no edges");
    assert!(
        cross_family_name_matches > 0,
        "no golden call shares a name with a symbol in another language family, so this guard is vacuous"
    );
}
