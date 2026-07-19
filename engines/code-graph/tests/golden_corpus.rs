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
