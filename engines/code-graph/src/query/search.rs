//! Symbol search: FTS5 with a LIKE fallback.

use anyhow::Result;
use rusqlite::{Connection, params};

use crate::model::SearchRow;

pub fn search_symbols(conn: &Connection, query: &str, limit: usize) -> Result<Vec<SearchRow>> {
    search_symbols_with_path(conn, query, None, limit)
}

pub fn search_symbols_with_path(
    conn: &Connection,
    query: &str,
    path_filter: Option<&str>,
    limit: usize,
) -> Result<Vec<SearchRow>> {
    let fts = fts_query(query);
    let path = path_filter.map(normalize_path_filter);
    let path_prefix = path
        .as_ref()
        .map(|p| format!("{}/%", p.trim_end_matches('/')));
    let path_contains = path.as_ref().map(|p| format!("%{p}%"));
    let mut rows = Vec::new();
    if !fts.is_empty() {
        let sql = r#"
            SELECT s.id, s.kind, s.name, s.qualified_name, s.file_path, s.start_line,
                   s.end_line, s.signature, bm25(symbols_fts) AS rank
            FROM symbols_fts
            JOIN symbols s ON s.id = symbols_fts.symbol_id
            WHERE symbols_fts MATCH ?1
              AND (?2 IS NULL OR s.file_path = ?2 OR s.file_path LIKE ?3 OR s.file_path LIKE ?4)
            ORDER BY bm25(symbols_fts), s.file_path, s.start_line
            LIMIT ?5
        "#;
        let mut stmt = conn.prepare(sql)?;
        let mapped = stmt.query_map(
            params![fts, path, path_prefix, path_contains, sqlite_limit(limit)],
            search_row_from_sql,
        )?;
        for row in mapped {
            rows.push(row?);
        }
    }

    if rows.is_empty() {
        let like = format!("%{query}%");
        let mut stmt = conn.prepare(
            r#"
            SELECT id, kind, name, qualified_name, file_path, start_line,
                   end_line, signature, 1.0 AS rank
            FROM symbols
            WHERE (name LIKE ?1 OR qualified_name LIKE ?1 OR signature LIKE ?1)
              AND (?2 IS NULL OR file_path = ?2 OR file_path LIKE ?3 OR file_path LIKE ?4)
            ORDER BY file_path, start_line
            LIMIT ?5
            "#,
        )?;
        let mapped = stmt.query_map(
            params![like, path, path_prefix, path_contains, sqlite_limit(limit)],
            search_row_from_sql,
        )?;
        for row in mapped {
            rows.push(row?);
        }
    }
    Ok(rows)
}

fn normalize_path_filter(path: &str) -> String {
    path.trim_matches('/')
        .trim_start_matches("./")
        .replace('\\', "/")
}

fn search_row_from_sql(row: &rusqlite::Row<'_>) -> rusqlite::Result<SearchRow> {
    let rank: f64 = row.get(8)?;
    Ok(SearchRow {
        id: row.get(0)?,
        kind: row.get(1)?,
        name: row.get(2)?,
        qualified_name: row.get(3)?,
        file_path: row.get(4)?,
        start_line: row.get::<_, i64>(5)? as usize,
        end_line: row.get::<_, i64>(6)? as usize,
        signature: row.get(7)?,
        score: if rank < 0.0 { -rank } else { rank },
    })
}

fn sqlite_limit(limit: usize) -> i64 {
    i64::try_from(limit).unwrap_or(i64::MAX)
}

pub fn fts_query(query: &str) -> String {
    query
        .split_whitespace()
        .filter_map(|term| {
            let clean: String = term
                .chars()
                .filter(|c| c.is_alphanumeric() || *c == '_' || *c == '$')
                .collect();
            if clean.is_empty() {
                None
            } else {
                Some(format!("\"{clean}\"*"))
            }
        })
        .collect::<Vec<_>>()
        .join(" OR ")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::init_schema;
    use rusqlite::params;

    #[test]
    fn fts_query_quotes_terms_and_strips_punctuation() {
        assert_eq!(fts_query("handoff lint!"), "\"handoff\"* OR \"lint\"*");
    }

    #[test]
    fn sqlite_limit_clamps_before_integer_cast() {
        assert_eq!(sqlite_limit(usize::MAX), i64::MAX);
    }

    #[test]
    fn search_symbols_can_filter_by_file_path_in_sql() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        for (id, name, file_path) in [
            ("a", "helper", "src/app.py"),
            ("b", "helper", "tests/app_test.py"),
            ("c", "runner", "src/run.py"),
        ] {
            conn.execute(
                "INSERT INTO files(path, content_hash, size, modified_at, indexed_at, language)
                 VALUES (?1, 'hash', 1, 1, 1, 'python')",
                params![file_path],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO symbols(id, kind, name, qualified_name, file_path, start_line, end_line, signature, content_hash)
                 VALUES (?1, 'function', ?2, ?2, ?3, 1, 2, ?2, 'hash')",
                params![id, name, file_path],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO symbols_fts(symbol_id, name, qualified_name, signature, file_path)
                 VALUES (?1, ?2, ?2, ?2, ?3)",
                params![id, name, file_path],
            )
            .unwrap();
        }

        let rows = search_symbols_with_path(&conn, "helper", Some("src"), 20).unwrap();

        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].file_path, "src/app.py");
    }
}
