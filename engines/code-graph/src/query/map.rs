//! Bounded, file-rooted call-map selection for the HTML renderer.

use std::collections::{HashMap, HashSet};

use anyhow::{bail, Result};
use clap::ValueEnum;
use rusqlite::{params, Connection};
use serde::Serialize;

pub const MAX_MAP_DEPTH: u8 = 5;
pub const MAX_MAP_NODES: usize = 250;
pub const MAX_MAP_EDGES: usize = 500;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, ValueEnum)]
#[serde(rename_all = "lowercase")]
pub enum MapDirection {
    Callers,
    Callees,
    Neighbors,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MapOptions {
    pub direction: MapDirection,
    pub depth: u8,
    pub max_nodes: usize,
    pub max_edges: usize,
}

impl Default for MapOptions {
    fn default() -> Self {
        Self {
            direction: MapDirection::Neighbors,
            depth: 1,
            max_nodes: 100,
            max_edges: 250,
        }
    }
}

impl MapOptions {
    pub fn validate(self) -> Result<()> {
        if self.depth == 0 || self.depth > MAX_MAP_DEPTH {
            bail!("depth must be between 1 and {MAX_MAP_DEPTH}");
        }
        if self.max_nodes == 0 || self.max_nodes > MAX_MAP_NODES {
            bail!("max_nodes must be between 1 and {MAX_MAP_NODES}");
        }
        if self.max_edges == 0 || self.max_edges > MAX_MAP_EDGES {
            bail!("max_edges must be between 1 and {MAX_MAP_EDGES}");
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct MapGraph {
    pub focus_path: String,
    pub direction: MapDirection,
    pub depth: u8,
    pub nodes: Vec<MapNode>,
    pub edges: Vec<MapEdge>,
    pub status: MapStatus,
    pub empty: bool,
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct MapNode {
    pub id: String,
    pub qualified_name: String,
    pub kind: String,
    pub file_path: String,
    pub start_line: usize,
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct MapEdge {
    pub source: String,
    pub target: String,
    pub kind: String,
    pub line: Option<usize>,
    pub confidence: Option<f64>,
}

#[derive(Debug, Clone, Copy, Serialize)]
pub(crate) struct MapStatus {
    pub rendered_nodes: usize,
    pub omitted_nodes: usize,
    pub rendered_edges: usize,
    pub omitted_edges: usize,
}

#[derive(Debug, Clone)]
struct TraversalNode {
    node: MapNode,
    hop: u8,
}

#[derive(Debug, Clone)]
struct TraversalEdge {
    edge: MapEdge,
    hop: u8,
}

pub(crate) fn build_file_map(
    conn: &Connection,
    focus_path: &str,
    options: MapOptions,
) -> Result<MapGraph> {
    options.validate()?;
    if !indexed_file_exists(conn, focus_path)? {
        bail!("{focus_path} is not an indexed focus file");
    }

    let focus_nodes = nodes_for_file(conn, focus_path)?;
    let mut nodes = HashMap::new();
    let mut frontier = Vec::new();
    for node in focus_nodes {
        nodes.insert(
            node.id.clone(),
            TraversalNode {
                node: node.clone(),
                hop: 0,
            },
        );
        frontier.push(node);
    }
    sort_nodes(&mut frontier, &nodes);

    let mut edges = Vec::new();
    let mut seen_edges = HashSet::new();
    for current_hop in 0..options.depth {
        let mut next_frontier = Vec::new();
        for current_node in frontier {
            let current_id = &current_node.id;
            for edge in direct_edges(conn, current_id, options.direction)? {
                let edge_key = (
                    edge.edge.source.clone(),
                    edge.edge.target.clone(),
                    edge.edge.kind.clone(),
                    edge.edge.line,
                );
                if seen_edges.insert(edge_key) {
                    edges.push(TraversalEdge {
                        edge: edge.edge.clone(),
                        hop: current_hop + 1,
                    });
                }

                let next_id = next_symbol_id(current_id, &edge, options.direction);
                if !nodes.contains_key(next_id) {
                    let node = if next_id == edge.source_node.id {
                        edge.source_node.clone()
                    } else {
                        edge.target_node.clone()
                    };
                    nodes.insert(
                        next_id.to_string(),
                        TraversalNode {
                            node: node.clone(),
                            hop: current_hop + 1,
                        },
                    );
                    next_frontier.push(node);
                }
            }
        }
        sort_nodes(&mut next_frontier, &nodes);
        frontier = next_frontier;
    }

    let mut all_nodes: Vec<_> = nodes.into_values().collect();
    all_nodes.sort_by(compare_traversal_nodes);
    let total_nodes = all_nodes.len();
    let selected_nodes: Vec<_> = all_nodes
        .into_iter()
        .take(options.max_nodes)
        .map(|entry| entry.node)
        .collect();
    let selected_ids: HashSet<_> = selected_nodes.iter().map(|node| node.id.as_str()).collect();

    edges.sort_by(|left, right| {
        left.hop
            .cmp(&right.hop)
            .then_with(|| compare_map_edges(&left.edge, &right.edge))
    });
    let total_edges = edges.len();
    let mut selected_edges: Vec<_> = edges
        .into_iter()
        .filter(|entry| {
            selected_ids.contains(entry.edge.source.as_str())
                && selected_ids.contains(entry.edge.target.as_str())
        })
        .map(|entry| entry.edge)
        .collect();
    selected_edges.sort_by(compare_map_edges);
    selected_edges.truncate(options.max_edges);

    Ok(MapGraph {
        focus_path: focus_path.to_string(),
        direction: options.direction,
        depth: options.depth,
        empty: selected_nodes.is_empty(),
        status: MapStatus {
            rendered_nodes: selected_nodes.len(),
            omitted_nodes: total_nodes.saturating_sub(selected_nodes.len()),
            rendered_edges: selected_edges.len(),
            omitted_edges: total_edges.saturating_sub(selected_edges.len()),
        },
        nodes: selected_nodes,
        edges: selected_edges,
    })
}

fn indexed_file_exists(conn: &Connection, focus_path: &str) -> Result<bool> {
    conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM files WHERE path = ?1)",
        [focus_path],
        |row| row.get(0),
    )
    .map_err(Into::into)
}

fn nodes_for_file(conn: &Connection, file_path: &str) -> Result<Vec<MapNode>> {
    let mut statement = conn.prepare(
        "SELECT id, qualified_name, kind, file_path, start_line FROM symbols \
         WHERE file_path = ?1 ORDER BY file_path, start_line, qualified_name, id",
    )?;
    let rows = statement.query_map([file_path], map_node_from_row)?;
    rows.collect::<rusqlite::Result<Vec<_>>>()
        .map_err(Into::into)
}

struct DirectEdge {
    edge: MapEdge,
    source_node: MapNode,
    target_node: MapNode,
}

fn direct_edges(
    conn: &Connection,
    symbol_id: &str,
    direction: MapDirection,
) -> Result<Vec<DirectEdge>> {
    let where_clause = match direction {
        MapDirection::Callers => "e.target = ?1",
        MapDirection::Callees => "e.source = ?1",
        MapDirection::Neighbors => "e.source = ?1 OR e.target = ?1",
    };
    let confidence_column = if crate::store::schema::table_has_column(conn, "edges", "confidence")?
    {
        "e.confidence"
    } else {
        "NULL AS confidence"
    };
    let sql = format!(
        "SELECT e.source, e.target, e.kind, e.line, {confidence_column}, \
                src.id, src.qualified_name, src.kind, src.file_path, src.start_line, \
                dst.id, dst.qualified_name, dst.kind, dst.file_path, dst.start_line \
         FROM edges e \
         JOIN symbols src ON src.id = e.source \
         JOIN symbols dst ON dst.id = e.target \
         WHERE {where_clause} \
         ORDER BY e.source, e.target, e.line, e.kind"
    );
    let mut statement = conn.prepare(&sql)?;
    let rows = statement.query_map(params![symbol_id], |row| {
        Ok(DirectEdge {
            edge: MapEdge {
                source: row.get(0)?,
                target: row.get(1)?,
                kind: row.get(2)?,
                line: row.get(3)?,
                confidence: row.get(4)?,
            },
            source_node: MapNode {
                id: row.get(5)?,
                qualified_name: row.get(6)?,
                kind: row.get(7)?,
                file_path: row.get(8)?,
                start_line: row.get(9)?,
            },
            target_node: MapNode {
                id: row.get(10)?,
                qualified_name: row.get(11)?,
                kind: row.get(12)?,
                file_path: row.get(13)?,
                start_line: row.get(14)?,
            },
        })
    })?;
    rows.collect::<rusqlite::Result<Vec<_>>>()
        .map_err(Into::into)
}

fn map_node_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<MapNode> {
    Ok(MapNode {
        id: row.get(0)?,
        qualified_name: row.get(1)?,
        kind: row.get(2)?,
        file_path: row.get(3)?,
        start_line: row.get(4)?,
    })
}

fn next_symbol_id<'a>(current_id: &str, edge: &'a DirectEdge, direction: MapDirection) -> &'a str {
    match direction {
        MapDirection::Callers => &edge.edge.source,
        MapDirection::Callees => &edge.edge.target,
        MapDirection::Neighbors if edge.edge.source == current_id => &edge.edge.target,
        MapDirection::Neighbors => &edge.edge.source,
    }
}

fn sort_nodes(nodes: &mut [MapNode], details: &HashMap<String, TraversalNode>) {
    nodes.sort_by(|left, right| {
        compare_traversal_nodes(
            details
                .get(&left.id)
                .expect("frontier node must be recorded"),
            details
                .get(&right.id)
                .expect("frontier node must be recorded"),
        )
    });
}

fn compare_traversal_nodes(left: &TraversalNode, right: &TraversalNode) -> std::cmp::Ordering {
    left.hop
        .cmp(&right.hop)
        .then_with(|| left.node.file_path.cmp(&right.node.file_path))
        .then_with(|| left.node.start_line.cmp(&right.node.start_line))
        .then_with(|| left.node.qualified_name.cmp(&right.node.qualified_name))
        .then_with(|| left.node.id.cmp(&right.node.id))
}

fn compare_map_edges(left: &MapEdge, right: &MapEdge) -> std::cmp::Ordering {
    left.source
        .cmp(&right.source)
        .then_with(|| left.target.cmp(&right.target))
        .then_with(|| left.line.cmp(&right.line))
        .then_with(|| left.kind.cmp(&right.kind))
}

#[cfg(test)]
mod tests {
    use anyhow::Result;
    use rusqlite::{params, Connection};

    use super::{build_file_map, MapDirection, MapOptions};
    use crate::store::init_schema;

    fn fixture(reverse_insert_order: bool) -> Result<Connection> {
        let conn = Connection::open_in_memory()?;
        init_schema(&conn)?;
        let mut files = vec![
            "src/caller.rs",
            "src/focus.rs",
            "src/callee.rs",
            "src/deep.rs",
        ];
        let mut symbols = vec![
            ("caller", "caller::entry", "src/caller.rs", 8),
            ("focus_a", "focus::alpha", "src/focus.rs", 4),
            ("focus_b", "focus::beta", "src/focus.rs", 18),
            ("callee", "callee::work", "src/callee.rs", 12),
            ("deep", "deep::work", "src/deep.rs", 3),
        ];
        let mut edges = vec![
            ("caller", "focus_a", 21),
            ("focus_a", "callee", 9),
            ("focus_b", "callee", 22),
            ("callee", "deep", 16),
        ];
        if reverse_insert_order {
            files.reverse();
            symbols.reverse();
            edges.reverse();
        }
        for path in files {
            conn.execute(
                "INSERT INTO files (path, content_hash, size, modified_at, indexed_at, language) \
                 VALUES (?1, 'fixture', 0, 0, 0, 'rust')",
                [path],
            )?;
        }
        for (id, qualified_name, file_path, start_line) in symbols {
            conn.execute(
                "INSERT INTO symbols \
                 (id, kind, name, qualified_name, file_path, start_line, end_line, signature, content_hash) \
                 VALUES (?1, 'function', ?2, ?2, ?3, ?4, ?4, '', 'fixture')",
                params![id, qualified_name, file_path, start_line],
            )?;
        }
        for (source, target, line) in edges {
            conn.execute(
                "INSERT INTO edges (source, target, kind, line, confidence) \
                 VALUES (?1, ?2, 'call', ?3, 1.0)",
                params![source, target, line],
            )?;
        }
        Ok(conn)
    }

    #[test]
    fn options_reject_zero_and_hard_maximum_overflow() {
        for options in [
            MapOptions {
                depth: 0,
                ..MapOptions::default()
            },
            MapOptions {
                depth: 6,
                ..MapOptions::default()
            },
            MapOptions {
                max_nodes: 0,
                ..MapOptions::default()
            },
            MapOptions {
                max_nodes: 251,
                ..MapOptions::default()
            },
            MapOptions {
                max_edges: 0,
                ..MapOptions::default()
            },
            MapOptions {
                max_edges: 501,
                ..MapOptions::default()
            },
        ] {
            assert!(options.validate().is_err());
        }
    }

    #[test]
    fn empty_indexed_focus_file_has_an_empty_map() -> Result<()> {
        let conn = fixture(false)?;
        conn.execute(
            "INSERT INTO files (path, content_hash, size, modified_at, indexed_at, language) \
             VALUES ('src/empty.rs', 'fixture', 0, 0, 0, 'rust')",
            [],
        )?;

        let map = build_file_map(&conn, "src/empty.rs", MapOptions::default())?;

        assert!(map.empty);
        assert!(map.nodes.is_empty());
        assert!(map.edges.is_empty());
        Ok(())
    }

    #[test]
    fn direction_and_depth_bound_breadth_first_selection() -> Result<()> {
        let conn = fixture(false)?;
        let callers = build_file_map(
            &conn,
            "src/focus.rs",
            MapOptions {
                direction: MapDirection::Callers,
                ..MapOptions::default()
            },
        )?;
        assert_eq!(
            callers
                .nodes
                .iter()
                .map(|node| node.id.as_str())
                .collect::<Vec<_>>(),
            ["focus_a", "focus_b", "caller"]
        );

        let callees = build_file_map(
            &conn,
            "src/focus.rs",
            MapOptions {
                direction: MapDirection::Callees,
                depth: 2,
                ..MapOptions::default()
            },
        )?;
        assert_eq!(
            callees
                .nodes
                .iter()
                .map(|node| node.id.as_str())
                .collect::<Vec<_>>(),
            ["focus_a", "focus_b", "callee", "deep"]
        );
        Ok(())
    }

    #[test]
    fn map_order_and_caps_are_deterministic_and_keep_edges_connected() -> Result<()> {
        let conn = fixture(false)?;
        let map = build_file_map(
            &conn,
            "src/focus.rs",
            MapOptions {
                depth: 2,
                max_nodes: 2,
                max_edges: 1,
                ..MapOptions::default()
            },
        )?;

        assert_eq!(
            map.nodes
                .iter()
                .map(|node| node.id.as_str())
                .collect::<Vec<_>>(),
            ["focus_a", "focus_b"]
        );
        assert!(map.edges.iter().all(|edge| {
            map.nodes.iter().any(|node| node.id == edge.source)
                && map.nodes.iter().any(|node| node.id == edge.target)
        }));
        assert_eq!(map.status.rendered_nodes, 2);
        assert_eq!(map.status.omitted_nodes, 3);
        assert_eq!(map.status.rendered_edges, 0);
        assert_eq!(map.status.omitted_edges, 4);

        let same_graph_different_insert_order = build_file_map(
            &fixture(true)?,
            "src/focus.rs",
            MapOptions {
                depth: 2,
                max_nodes: 2,
                max_edges: 1,
                ..MapOptions::default()
            },
        )?;
        assert_eq!(
            serde_json::to_string(&map)?,
            serde_json::to_string(&same_graph_different_insert_order)?,
        );
        Ok(())
    }
}
