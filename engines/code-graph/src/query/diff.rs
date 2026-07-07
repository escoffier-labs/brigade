//! Structural diff of two code graphs.
//!
//! ActiveGraph-inspired: GraphTrail's code graph is a projection of a repo, so a
//! before/after diff of two indexed DBs answers "what did this change do to the
//! call graph?" in a compact, receipt-attachable shape. Pure read/compare, both
//! connections opened read-only by the caller. See `docs/design/activegraph-inspiration.md`.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::Result;
use rusqlite::Connection;

use crate::model::{DiffEdge, DiffNode, GraphDiff, GraphDiffCounts};
use crate::store::SCHEMA_VERSION;

/// A symbol as loaded for diffing (a subset of the `symbols` row).
struct SymRow {
    kind: String,
    qualified_name: String,
    file_path: String,
    start_line: usize,
    end_line: usize,
    signature: String,
}

/// Line-independent node identity: `(file_path, qualified_name, kind)`.
type NodeKey = (String, String, String);

/// Canonical call edge: `(source_file, source, line, target_file, target)`. This
/// matches the golden-corpus canonical row and is stable across re-index because
/// it uses symbol names, not the line-baked symbol ids.
type CanonEdge = (String, String, usize, String, String);

fn load_symbols(conn: &Connection) -> Result<BTreeMap<NodeKey, Vec<SymRow>>> {
    let mut stmt = conn.prepare(
        "SELECT file_path, qualified_name, kind, start_line, end_line, signature FROM symbols",
    )?;
    let rows = stmt.query_map([], |r| {
        Ok(SymRow {
            file_path: r.get(0)?,
            qualified_name: r.get(1)?,
            kind: r.get(2)?,
            start_line: r.get::<_, i64>(3)? as usize,
            end_line: r.get::<_, i64>(4)? as usize,
            signature: r.get(5)?,
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

/// Identity of a symbol's body, independent of where it sits in the file: its
/// signature plus its line span. Two symbols with the same key and fingerprint
/// are treated as unchanged even if they moved. A sorted `Vec` (not a set) so
/// multiplicity is preserved: going from one `def hook()` to two identical ones
/// under the same key is a change, not a silent no-op.
fn fingerprint(rows: &[SymRow]) -> Vec<(String, usize)> {
    let mut prints: Vec<(String, usize)> = rows
        .iter()
        .map(|r| (r.signature.clone(), r.end_line.saturating_sub(r.start_line)))
        .collect();
    prints.sort();
    prints
}

fn to_node(row: &SymRow) -> DiffNode {
    DiffNode {
        kind: row.kind.clone(),
        qualified_name: row.qualified_name.clone(),
        file_path: row.file_path.clone(),
        start_line: row.start_line,
        signature: row.signature.clone(),
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
            None => added_nodes.extend(after_rows.iter().map(to_node)),
            Some(before_rows) => {
                if fingerprint(before_rows) != fingerprint(after_rows) {
                    changed_nodes.extend(after_rows.iter().map(to_node));
                }
            }
        }
    }
    for (key, before_rows) in &before_syms {
        if !after_syms.contains_key(key) {
            removed_nodes.extend(before_rows.iter().map(to_node));
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

    let summary = GraphDiffCounts {
        added_nodes: added_nodes.len(),
        removed_nodes: removed_nodes.len(),
        changed_nodes: changed_nodes.len(),
        added_edges: added_edges.len(),
        removed_edges: removed_edges.len(),
    };

    Ok(GraphDiff {
        schema_version: SCHEMA_VERSION,
        summary,
        added_nodes,
        removed_nodes,
        changed_nodes,
        added_edges,
        removed_edges,
    })
}
