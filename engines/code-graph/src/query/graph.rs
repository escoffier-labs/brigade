//! Call-graph edge queries shared by callers/callees/impact.

use std::collections::{HashSet, VecDeque};

use anyhow::Result;
use rusqlite::{Connection, params};

use crate::model::{Direction, EdgeRow, FileNeighbor};
use crate::query::search::search_symbols;

pub const DEFAULT_IMPACT_DEPTH: usize = 1;
pub const MAX_IMPACT_DEPTH: usize = 5;
pub const EDGE_CAP_PER_DIRECTION: usize = 500;
pub const TRUNCATED_EDGE_KIND: &str = "truncated";

pub fn graph_edges(
    conn: &Connection,
    symbol_query: &str,
    direction: Direction,
) -> Result<Vec<EdgeRow>> {
    graph_edges_with_depth(conn, symbol_query, direction, DEFAULT_IMPACT_DEPTH)
}

pub fn graph_edges_with_depth(
    conn: &Connection,
    symbol_query: &str,
    direction: Direction,
    depth: usize,
) -> Result<Vec<EdgeRow>> {
    let symbols = search_symbols(conn, symbol_query, 20)?;
    let mut edges = Vec::new();
    for symbol in symbols {
        edges.extend(edges_for_symbol_id_with_depth(
            conn, &symbol.id, direction, depth,
        )?);
    }
    cap_direction_edges(dedupe_edges(edges)?, direction, symbol_query)
}

pub fn impact_edges(conn: &Connection, symbol_query: &str, depth: usize) -> Result<Vec<EdgeRow>> {
    let mut edges = graph_edges_with_depth(conn, symbol_query, Direction::Incoming, depth)?;
    edges.extend(graph_edges_with_depth(
        conn,
        symbol_query,
        Direction::Outgoing,
        depth,
    )?);
    sort_impact_edges(&mut edges);
    Ok(edges)
}

pub fn edges_for_symbol_id(
    conn: &Connection,
    symbol_id: &str,
    direction: Direction,
) -> Result<Vec<EdgeRow>> {
    edges_for_symbol_id_with_depth(conn, symbol_id, direction, DEFAULT_IMPACT_DEPTH)
}

pub fn edges_for_symbol_id_with_depth(
    conn: &Connection,
    symbol_id: &str,
    direction: Direction,
    depth: usize,
) -> Result<Vec<EdgeRow>> {
    let depth = normalize_depth(depth);
    // Pre-v6 databases have no confidence column; selecting it would error on
    // a read-only connection that cannot migrate. Check once per query.
    let has_confidence = crate::store::schema::table_has_column(conn, "edges", "confidence")?;
    let mut rows = Vec::new();
    let mut queue = VecDeque::from([(symbol_id.to_string(), 0usize)]);
    let mut visited_symbols = HashSet::from([symbol_id.to_string()]);

    while let Some((current_symbol, current_hops)) = queue.pop_front() {
        if current_hops >= depth {
            continue;
        }
        for mut edge in
            direct_edges_for_symbol_id(conn, &current_symbol, direction, has_confidence)?
        {
            edge.hops = current_hops + 1;
            if rows.len() == EDGE_CAP_PER_DIRECTION {
                rows.push(truncated_edge(direction, edge.hops, &current_symbol));
                return dedupe_edges(rows);
            }

            let next_symbol = match direction {
                Direction::Incoming => edge.source_id.clone(),
                Direction::Outgoing => edge.target_id.clone(),
            };
            rows.push(edge);
            if visited_symbols.insert(next_symbol.clone()) {
                queue.push_back((next_symbol, current_hops + 1));
            }
        }
    }

    dedupe_edges(rows)
}

fn direct_edges_for_symbol_id(
    conn: &Connection,
    symbol_id: &str,
    direction: Direction,
    has_confidence: bool,
) -> Result<Vec<EdgeRow>> {
    let (where_clause, order) = match direction {
        Direction::Incoming => ("e.target = ?1", "src.file_path, src.start_line"),
        Direction::Outgoing => ("e.source = ?1", "dst.file_path, dst.start_line"),
    };
    let confidence_column = if has_confidence {
        "e.confidence"
    } else {
        "NULL AS confidence"
    };
    let sql = format!(
        r#"
        SELECT e.source, src.qualified_name, e.target, dst.qualified_name, e.kind, e.line,
               src.file_path, dst.file_path, {confidence_column}
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
            hops: 1,
            confidence: row.get::<_, Option<f64>>(8)?,
        })
    })?;
    let mut rows = Vec::new();
    for row in mapped {
        rows.push(row?);
    }
    Ok(rows)
}

pub fn normalize_depth(depth: usize) -> usize {
    depth.clamp(DEFAULT_IMPACT_DEPTH, MAX_IMPACT_DEPTH)
}

pub fn sort_impact_edges(edges: &mut [EdgeRow]) {
    edges.sort_by(|a, b| {
        a.hops
            .cmp(&b.hops)
            .then_with(|| a.source_file.cmp(&b.source_file))
            .then_with(|| a.source.cmp(&b.source))
            .then_with(|| a.target.cmp(&b.target))
            .then_with(|| a.kind.cmp(&b.kind))
    });
}

fn truncated_edge(direction: Direction, hops: usize, current_symbol: &str) -> EdgeRow {
    let direction_label = match direction {
        Direction::Incoming => "incoming",
        Direction::Outgoing => "outgoing",
    };
    let marker_id = format!("__graphtrail_truncated_{direction_label}__");
    EdgeRow {
        source_id: marker_id.clone(),
        source: format!("{direction_label} traversal truncated"),
        target_id: current_symbol.to_string(),
        target: format!("more than {EDGE_CAP_PER_DIRECTION} {direction_label} edges"),
        kind: TRUNCATED_EDGE_KIND.to_string(),
        line: None,
        source_file: String::new(),
        target_file: String::new(),
        hops,
        confidence: None,
    }
}

fn cap_direction_edges(
    edges: Vec<EdgeRow>,
    direction: Direction,
    current_symbol: &str,
) -> Result<Vec<EdgeRow>> {
    let max_hops = edges.iter().map(|edge| edge.hops).max().unwrap_or(1);
    let mut rows = Vec::new();
    let mut truncated = false;
    for edge in edges {
        if edge.kind == TRUNCATED_EDGE_KIND {
            truncated = true;
            continue;
        }
        if rows.len() == EDGE_CAP_PER_DIRECTION {
            truncated = true;
            continue;
        }
        rows.push(edge);
    }
    if truncated {
        rows.push(truncated_edge(direction, max_hops, current_symbol));
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

pub fn file_neighbors(conn: &Connection, file_path: &str) -> Result<Vec<FileNeighbor>> {
    let mut stmt = conn.prepare(
        r#"
        SELECT file_path, SUM(incoming_edges) AS incoming_edges, SUM(outgoing_edges) AS outgoing_edges
        FROM (
            SELECT src.file_path AS file_path, 1 AS incoming_edges, 0 AS outgoing_edges
            FROM edges e
            JOIN symbols src ON src.id = e.source
            JOIN symbols dst ON dst.id = e.target
            WHERE dst.file_path = ?1 AND src.file_path <> ?1
            UNION ALL
            SELECT dst.file_path AS file_path, 0 AS incoming_edges, 1 AS outgoing_edges
            FROM edges e
            JOIN symbols src ON src.id = e.source
            JOIN symbols dst ON dst.id = e.target
            WHERE src.file_path = ?1 AND dst.file_path <> ?1
        )
        GROUP BY file_path
        ORDER BY file_path
        "#,
    )?;
    let mapped = stmt.query_map(params![file_path], |row| {
        Ok(FileNeighbor {
            file_path: row.get(0)?,
            incoming_edges: row.get(1)?,
            outgoing_edges: row.get(2)?,
        })
    })?;
    let mut rows = Vec::new();
    for row in mapped {
        rows.push(row?);
    }
    Ok(rows)
}
