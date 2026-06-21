//! Call-graph edge queries shared by callers/callees/impact.

use std::collections::HashSet;

use anyhow::Result;
use rusqlite::{Connection, params};

use crate::model::{Direction, EdgeRow};
use crate::query::search::search_symbols;

pub fn graph_edges(
    conn: &Connection,
    symbol_query: &str,
    direction: Direction,
) -> Result<Vec<EdgeRow>> {
    let symbols = search_symbols(conn, symbol_query, 20)?;
    let mut edges = Vec::new();
    for symbol in symbols {
        edges.extend(edges_for_symbol_id(conn, &symbol.id, direction)?);
    }
    dedupe_edges(edges)
}

pub fn edges_for_symbol_id(
    conn: &Connection,
    symbol_id: &str,
    direction: Direction,
) -> Result<Vec<EdgeRow>> {
    let (where_clause, order) = match direction {
        Direction::Incoming => ("e.target = ?1", "src.file_path, src.start_line"),
        Direction::Outgoing => ("e.source = ?1", "dst.file_path, dst.start_line"),
    };
    let sql = format!(
        r#"
        SELECT e.source, src.qualified_name, e.target, dst.qualified_name, e.kind, e.line,
               src.file_path, dst.file_path
        FROM edges e
        JOIN symbols src ON src.id = e.source
        JOIN symbols dst ON dst.id = e.target
        WHERE {where_clause}
        ORDER BY {order}
        "#
    );
    let mut stmt = conn.prepare(&sql)?;
    let mapped = stmt.query_map(params![symbol_id], |row| {
        Ok(EdgeRow {
            source_id: row.get(0)?,
            source: row.get(1)?,
            target_id: row.get(2)?,
            target: row.get(3)?,
            kind: row.get(4)?,
            line: row.get::<_, Option<i64>>(5)?.map(|v| v as usize),
            source_file: row.get(6)?,
            target_file: row.get(7)?,
        })
    })?;
    let mut rows = Vec::new();
    for row in mapped {
        rows.push(row?);
    }
    Ok(rows)
}

pub fn dedupe_edges(edges: Vec<EdgeRow>) -> Result<Vec<EdgeRow>> {
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for edge in edges {
        let key = format!(
            "{}:{}:{}:{}",
            edge.source_id,
            edge.target_id,
            edge.kind,
            edge.line.unwrap_or_default()
        );
        if seen.insert(key) {
            out.push(edge);
        }
    }
    Ok(out)
}
