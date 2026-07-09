//! Structural health queries: uncalled symbols and file dependency cycles.
//!
//! Both answer from call edges alone, so they are candidate lists, not
//! verdicts: dynamic dispatch, exported APIs, reflection, and entry points
//! are invisible to the graph. The reports say so in-band.

use std::collections::HashMap;

use anyhow::Result;
use rusqlite::Connection;
use serde::Serialize;

use crate::query::affected::is_test_file;

/// In-band honesty note for uncalled-symbol reports.
pub const UNCALLED_ATTRIBUTION: &str = "symbols with no incoming call edges in the graph; \
     not proof of dead code (dynamic dispatch, exports, entry points, and reflection are \
     invisible to call edges)";

#[derive(Debug, Serialize)]
pub struct DeadCodeReport {
    pub attribution: &'static str,
    /// Uncalled callables found before `limit` was applied.
    pub total: usize,
    pub symbols: Vec<UncalledSymbol>,
}

#[derive(Debug, Serialize)]
pub struct UncalledSymbol {
    pub id: String,
    pub kind: String,
    pub name: String,
    pub qualified_name: String,
    pub file_path: String,
    pub start_line: usize,
    pub end_line: usize,
    pub signature: String,
}

/// Callables (functions and methods) with no incoming call edges.
///
/// Excluded up front because they are uncalled by design, not by accident:
/// `main`, dunder methods, anything in a test file (tests are call-graph
/// roots), and members of a `tests` container (Rust inline `mod tests`).
pub fn dead_code(conn: &Connection, limit: usize) -> Result<DeadCodeReport> {
    let mut stmt = conn.prepare(
        r#"
        SELECT s.id, s.kind, s.name, s.qualified_name, s.file_path,
               s.start_line, s.end_line, s.signature
        FROM symbols s
        LEFT JOIN edges e ON e.target = s.id
        WHERE e.source IS NULL
          AND s.kind IN ('function', 'method')
          AND s.name != 'main'
          AND s.name NOT LIKE '\_\_%' ESCAPE '\'
          AND (s.container IS NULL OR s.container != 'tests')
        ORDER BY s.file_path, s.start_line
        "#,
    )?;
    let mapped = stmt.query_map([], |row| {
        Ok(UncalledSymbol {
            id: row.get(0)?,
            kind: row.get(1)?,
            name: row.get(2)?,
            qualified_name: row.get(3)?,
            file_path: row.get(4)?,
            start_line: row.get::<_, i64>(5)? as usize,
            end_line: row.get::<_, i64>(6)? as usize,
            signature: row.get(7)?,
        })
    })?;
    let mut symbols = Vec::new();
    for row in mapped {
        let symbol = row?;
        if !is_test_file(&symbol.file_path) {
            symbols.push(symbol);
        }
    }
    let total = symbols.len();
    symbols.truncate(limit);
    Ok(DeadCodeReport {
        attribution: UNCALLED_ATTRIBUTION,
        total,
        symbols,
    })
}

/// How many cycle groups a report lists at most.
pub const CYCLE_GROUP_CAP: usize = 50;

#[derive(Debug, Serialize)]
pub struct CycleReport {
    /// Strongly connected file groups (every file reaches every other through
    /// call edges), each sorted, largest group first.
    pub groups: Vec<Vec<String>>,
    pub total_groups: usize,
    pub truncated: bool,
}

/// File-level dependency cycles from cross-file call edges.
pub fn cycles(conn: &Connection) -> Result<CycleReport> {
    let mut stmt = conn.prepare(
        r#"
        SELECT DISTINCT src.file_path, dst.file_path
        FROM edges e
        JOIN symbols src ON src.id = e.source
        JOIN symbols dst ON dst.id = e.target
        WHERE src.file_path != dst.file_path
        "#,
    )?;
    let mapped = stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    let mut adjacency: HashMap<String, Vec<String>> = HashMap::new();
    for row in mapped {
        let (from, to) = row?;
        adjacency.entry(to.clone()).or_default();
        adjacency.entry(from).or_default().push(to);
    }

    let mut groups: Vec<Vec<String>> = strongly_connected_components(&adjacency)
        .into_iter()
        .filter(|component| component.len() > 1)
        .map(|mut component| {
            component.sort();
            component
        })
        .collect();
    groups.sort_by(|a, b| b.len().cmp(&a.len()).then_with(|| a[0].cmp(&b[0])));
    let total_groups = groups.len();
    let truncated = total_groups > CYCLE_GROUP_CAP;
    groups.truncate(CYCLE_GROUP_CAP);
    Ok(CycleReport {
        groups,
        total_groups,
        truncated,
    })
}

/// Iterative Tarjan (explicit stack, so a 10k-file chain cannot overflow).
fn strongly_connected_components(adjacency: &HashMap<String, Vec<String>>) -> Vec<Vec<String>> {
    #[derive(Default, Clone)]
    struct NodeState {
        index: Option<usize>,
        low_link: usize,
        on_stack: bool,
    }

    let mut nodes: Vec<&String> = adjacency.keys().collect();
    nodes.sort();
    let ids: HashMap<&String, usize> = nodes.iter().enumerate().map(|(i, n)| (*n, i)).collect();
    let neighbors: Vec<Vec<usize>> = nodes
        .iter()
        .map(|node| {
            adjacency[*node]
                .iter()
                .filter_map(|to| ids.get(to).copied())
                .collect()
        })
        .collect();

    let mut state = vec![NodeState::default(); nodes.len()];
    let mut next_index = 0usize;
    let mut stack: Vec<usize> = Vec::new();
    let mut components = Vec::new();

    for start in 0..nodes.len() {
        if state[start].index.is_some() {
            continue;
        }
        // (node, next neighbor position) pairs emulate the recursion frames.
        let mut work: Vec<(usize, usize)> = vec![(start, 0)];
        while let Some(&mut (node, ref mut position)) = work.last_mut() {
            if *position == 0 {
                state[node].index = Some(next_index);
                state[node].low_link = next_index;
                next_index += 1;
                stack.push(node);
                state[node].on_stack = true;
            }
            if let Some(&neighbor) = neighbors[node].get(*position) {
                *position += 1;
                match state[neighbor].index {
                    None => work.push((neighbor, 0)),
                    Some(index) if state[neighbor].on_stack => {
                        state[node].low_link = state[node].low_link.min(index);
                    }
                    Some(_) => {}
                }
                continue;
            }
            // All neighbors done: close the frame.
            work.pop();
            if let Some(&(parent, _)) = work.last() {
                state[parent].low_link = state[parent].low_link.min(state[node].low_link);
            }
            if state[node].index == Some(state[node].low_link) {
                let mut component = Vec::new();
                while let Some(member) = stack.pop() {
                    state[member].on_stack = false;
                    component.push(nodes[member].clone());
                    if member == node {
                        break;
                    }
                }
                components.push(component);
            }
        }
    }
    components
}
