//! Exact per-file symbol overview, ordered by source position.

use anyhow::Result;
use rusqlite::{Connection, params};

use crate::model::{OutlineRow, SymbolCandidate};

pub fn outline(conn: &Connection, path: &str) -> Result<Vec<OutlineRow>> {
    let path = path.trim().replace('\\', "/");
    let path = path.trim_start_matches("./").trim_matches('/');
    // Symbols are inserted in extractor preorder; rowid preserves that order
    // when parents or siblings begin on the same line.
    let mut stmt = conn.prepare(
        "SELECT id, kind, name, qualified_name, file_path, start_line, end_line, signature
         FROM symbols WHERE file_path = ?1 ORDER BY start_line, rowid",
    )?;
    let rows = stmt.query_map(params![path], |row| {
        let signature: String = row.get(7)?;
        Ok(OutlineRow {
            symbol: SymbolCandidate {
                id: row.get(0)?,
                kind: row.get(1)?,
                name: row.get(2)?,
                qualified_name: row.get(3)?,
                file_path: row.get(4)?,
                start_line: row.get::<_, i64>(5)? as usize,
                end_line: row.get::<_, i64>(6)? as usize,
            },
            signature: signature.lines().next().unwrap_or_default().to_string(),
        })
    })?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}
