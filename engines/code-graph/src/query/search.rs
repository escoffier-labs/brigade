//! Symbol search: FTS5 with a LIKE fallback.

use anyhow::Result;
use rusqlite::{Connection, params};

use crate::model::SearchRow;

pub fn search_symbols(conn: &Connection, query: &str, limit: usize) -> Result<Vec<SearchRow>> {
    let fts = fts_query(query);
    let mut rows = Vec::new();
    if !fts.is_empty() {
        let sql = r#"
            SELECT s.id, s.kind, s.name, s.qualified_name, s.file_path, s.start_line,
                   s.end_line, s.signature, bm25(symbols_fts) AS rank
            FROM symbols_fts
            JOIN symbols s ON s.id = symbols_fts.symbol_id
            WHERE symbols_fts MATCH ?1
            ORDER BY bm25(symbols_fts), s.file_path, s.start_line
            LIMIT ?2
        "#;
        let mut stmt = conn.prepare(sql)?;
        let mapped = stmt.query_map(params![fts, sqlite_limit(limit)], search_row_from_sql)?;
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
            WHERE name LIKE ?1 OR qualified_name LIKE ?1 OR signature LIKE ?1
            ORDER BY file_path, start_line
            LIMIT ?2
            "#,
        )?;
        let mapped = stmt.query_map(params![like, sqlite_limit(limit)], search_row_from_sql)?;
        for row in mapped {
            rows.push(row?);
        }
    }
    Ok(rows)
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

    #[test]
    fn fts_query_quotes_terms_and_strips_punctuation() {
        assert_eq!(fts_query("handoff lint!"), "\"handoff\"* OR \"lint\"*");
    }

    #[test]
    fn sqlite_limit_clamps_before_integer_cast() {
        assert_eq!(sqlite_limit(usize::MAX), i64::MAX);
    }
}
