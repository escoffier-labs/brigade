//! Structural diff of two code graphs.
//!
//! ActiveGraph-inspired: GraphTrail's code graph is a projection of a repo, so a
//! before/after diff of two indexed DBs answers "what did this change do to the
//! call graph?" in a compact, receipt-attachable shape. Pure read/compare, both
//! connections opened read-only by the caller. See `docs/design/activegraph-inspiration.md`.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::Result;
use rusqlite::Connection;

use crate::model::{DiffEdge, DiffNode, DiffNodePrevious, GraphDiff, GraphDiffCounts};
use crate::store::schema::table_has_column;

const GRAPH_DIFF_SCHEMA_VERSION: u32 = 3;

/// A symbol as loaded for diffing (a subset of the `symbols` row).
struct SymRow {
    kind: String,
    qualified_name: String,
    file_path: String,
    start_line: usize,
    end_line: usize,
    signature: String,
    body_hash: Option<String>,
}

/// Line-independent node identity: `(file_path, qualified_name, kind)`.
type NodeKey = (String, String, String);

/// Canonical call edge: `(source_file, source, line, target_file, target)`. This
/// matches the golden-corpus canonical row and is stable across re-index because
/// it uses symbol names, not the line-baked symbol ids.
type CanonEdge = (String, String, usize, String, String);
type LinelessEdge = (String, String, String, String);

fn load_symbols(conn: &Connection) -> Result<BTreeMap<NodeKey, Vec<SymRow>>> {
    let body_hash_expr = if table_has_column(conn, "symbols", "body_hash")? {
        "body_hash"
    } else {
        "NULL"
    };
    let mut stmt = conn.prepare(&format!(
        "SELECT file_path, qualified_name, kind, start_line, end_line, signature, {body_hash_expr} FROM symbols"
    ))?;
    let rows = stmt.query_map([], |r| {
        Ok(SymRow {
            file_path: r.get(0)?,
            qualified_name: r.get(1)?,
            kind: r.get(2)?,
            start_line: r.get::<_, i64>(3)? as usize,
            end_line: r.get::<_, i64>(4)? as usize,
            signature: r.get(5)?,
            body_hash: r.get(6)?,
        })
    })?;

    let mut map: BTreeMap<NodeKey, Vec<SymRow>> = BTreeMap::new();
    for row in rows {
        let row = row?;
        let key = (
            row.file_path.clone(),
            row.qualified_name.clone(),
            row.kind.clone(),
        );
        map.entry(key).or_default().push(row);
    }
    Ok(map)
}

fn load_call_edges(conn: &Connection) -> Result<BTreeSet<CanonEdge>> {
    let mut stmt = conn.prepare(
        "SELECT src.file_path, src.qualified_name, e.line, dst.file_path, dst.qualified_name \
         FROM edges e \
         JOIN symbols src ON e.source = src.id \
         JOIN symbols dst ON e.target = dst.id \
         WHERE e.kind = 'calls'",
    )?;
    let rows = stmt.query_map([], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, i64>(2)? as usize,
            r.get::<_, String>(3)?,
            r.get::<_, String>(4)?,
        ))
    })?;

    let mut set = BTreeSet::new();
    for row in rows {
        set.insert(row?);
    }
    Ok(set)
}

fn span_len(row: &SymRow) -> usize {
    row.end_line.saturating_sub(row.start_line)
}

fn rows_equivalent(before: &SymRow, after: &SymRow) -> bool {
    before.signature == after.signature
        && span_len(before) == span_len(after)
        && match (&before.body_hash, &after.body_hash) {
            (Some(before_hash), Some(after_hash)) => before_hash == after_hash,
            _ => true,
        }
}

fn rows_unchanged(before_rows: &[SymRow], after_rows: &[SymRow]) -> bool {
    if before_rows.len() != after_rows.len() {
        return false;
    }

    let mut used = vec![false; before_rows.len()];
    for after in after_rows {
        let Some((idx, _)) = before_rows
            .iter()
            .enumerate()
            .find(|(idx, before)| !used[*idx] && rows_equivalent(before, after))
        else {
            return false;
        };
        used[idx] = true;
    }
    true
}

fn previous_for_rows(
    before_rows: &[SymRow],
    after_rows: &[SymRow],
) -> Vec<Option<DiffNodePrevious>> {
    let mut used = vec![false; before_rows.len()];
    after_rows
        .iter()
        .map(|after| {
            let idx = before_rows
                .iter()
                .enumerate()
                .filter(|(idx, _)| !used[*idx])
                .min_by_key(|(_, before)| {
                    (
                        before.start_line != after.start_line,
                        before.signature != after.signature,
                        before.qualified_name != after.qualified_name,
                    )
                })
                .map(|(idx, _)| idx)?;
            used[idx] = true;
            Some(DiffNodePrevious {
                start_line: before_rows[idx].start_line,
                signature: before_rows[idx].signature.clone(),
            })
        })
        .collect()
}

fn to_node(row: &SymRow, previous: Option<DiffNodePrevious>) -> DiffNode {
    DiffNode {
        kind: row.kind.clone(),
        qualified_name: row.qualified_name.clone(),
        file_path: row.file_path.clone(),
        start_line: row.start_line,
        signature: row.signature.clone(),
        previous,
    }
}

fn to_edge(edge: &CanonEdge) -> DiffEdge {
    DiffEdge {
        source_file: edge.0.clone(),
        source: edge.1.clone(),
        line: edge.2,
        target_file: edge.3.clone(),
        target: edge.4.clone(),
    }
}

fn lineless_edge(edge: &DiffEdge) -> LinelessEdge {
    (
        edge.source_file.clone(),
        edge.source.clone(),
        edge.target_file.clone(),
        edge.target.clone(),
    )
}

fn line_insensitive_edge_counts(
    added_edges: &[DiffEdge],
    removed_edges: &[DiffEdge],
) -> (usize, usize) {
    let count_edges = |edges: &[DiffEdge]| {
        let mut counts: BTreeMap<LinelessEdge, usize> = BTreeMap::new();
        for edge in edges {
            *counts.entry(lineless_edge(edge)).or_default() += 1;
        }
        counts
    };

    let mut added = count_edges(added_edges);
    let mut removed = count_edges(removed_edges);
    for (edge, added_count) in added.iter_mut() {
        if let Some(removed_count) = removed.get_mut(edge) {
            let canceled = (*added_count).min(*removed_count);
            *added_count -= canceled;
            *removed_count -= canceled;
        }
    }
    (
        added.values().copied().sum(),
        removed.values().copied().sum(),
    )
}

fn sort_nodes(nodes: &mut [DiffNode]) {
    nodes.sort_by(|a, b| {
        (&a.file_path, &a.qualified_name, a.start_line).cmp(&(
            &b.file_path,
            &b.qualified_name,
            b.start_line,
        ))
    });
}

/// Diff two indexed graph DBs into added / removed / changed nodes and edges.
pub fn diff_graphs(before: &Connection, after: &Connection) -> Result<GraphDiff> {
    let before_syms = load_symbols(before)?;
    let after_syms = load_symbols(after)?;

    let mut added_nodes = Vec::new();
    let mut removed_nodes = Vec::new();
    let mut changed_nodes = Vec::new();

    for (key, after_rows) in &after_syms {
        match before_syms.get(key) {
            None => added_nodes.extend(after_rows.iter().map(|row| to_node(row, None))),
            Some(before_rows) => {
                if !rows_unchanged(before_rows, after_rows) {
                    let previous = previous_for_rows(before_rows, after_rows);
                    changed_nodes.extend(
                        after_rows
                            .iter()
                            .zip(previous)
                            .map(|(row, previous)| to_node(row, previous)),
                    );
                }
            }
        }
    }
    for (key, before_rows) in &before_syms {
        if !after_syms.contains_key(key) {
            removed_nodes.extend(before_rows.iter().map(|row| to_node(row, None)));
        }
    }

    sort_nodes(&mut added_nodes);
    sort_nodes(&mut removed_nodes);
    sort_nodes(&mut changed_nodes);

    let before_edges = load_call_edges(before)?;
    let after_edges = load_call_edges(after)?;
    // BTreeSet::difference yields sorted output, so edges are already deterministic.
    let added_edges: Vec<DiffEdge> = after_edges.difference(&before_edges).map(to_edge).collect();
    let removed_edges: Vec<DiffEdge> = before_edges.difference(&after_edges).map(to_edge).collect();
    let (summary_added_edges, summary_removed_edges) =
        line_insensitive_edge_counts(&added_edges, &removed_edges);

    let summary = GraphDiffCounts {
        added_nodes: added_nodes.len(),
        removed_nodes: removed_nodes.len(),
        changed_nodes: changed_nodes.len(),
        added_edges: added_edges.len(),
        removed_edges: removed_edges.len(),
        added_edges_line_insensitive: summary_added_edges,
        removed_edges_line_insensitive: summary_removed_edges,
    };

    Ok(GraphDiff {
        schema_version: GRAPH_DIFF_SCHEMA_VERSION,
        summary,
        added_nodes,
        removed_nodes,
        changed_nodes,
        added_edges,
        removed_edges,
    })
}
