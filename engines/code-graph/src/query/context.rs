//! Context packs: entry points for a task plus their caller/callee neighborhoods.

use std::collections::HashSet;

use anyhow::Result;
use rusqlite::Connection;

use crate::model::{ContextPack, Direction, EdgeRow, SearchRow};
use crate::query::graph::edges_for_symbol_id;
use crate::query::search::search_symbols;
use crate::store::SCHEMA_VERSION;

pub fn build_context_pack(conn: &Connection, task: String, limit: usize) -> Result<ContextPack> {
    let entry_points = search_symbols(conn, &task, limit)?;
    let mut callers = Vec::new();
    let mut callees = Vec::new();
    let mut files = HashSet::new();
    for row in &entry_points {
        files.insert(row.file_path.clone());
        callers.extend(edges_for_symbol_id(conn, &row.id, Direction::Incoming)?);
        callees.extend(edges_for_symbol_id(conn, &row.id, Direction::Outgoing)?);
    }
    for edge in callers.iter().chain(callees.iter()) {
        files.insert(edge.source_file.clone());
        files.insert(edge.target_file.clone());
    }
    let mut related_files: Vec<String> = files.into_iter().collect();
    related_files.sort();
    Ok(ContextPack {
        schema_version: SCHEMA_VERSION,
        task,
        entry_points,
        callers,
        callees,
        related_files,
    })
}

/// Render a context pack as a Brigade-friendly markdown document (droppable into a handoff's
/// evidence/context section, or readable directly by an agent).
pub fn render_markdown(pack: &ContextPack) -> String {
    use std::fmt::Write;
    let mut md = String::new();
    let _ = writeln!(md, "# Context Pack: {}\n", pack.task);
    let _ = writeln!(
        md,
        "_schema v{} - {} entry points - {} callers - {} callees - {} related files_\n",
        pack.schema_version,
        pack.entry_points.len(),
        pack.callers.len(),
        pack.callees.len(),
        pack.related_files.len()
    );

    let _ = writeln!(md, "## Entry points\n");
    if pack.entry_points.is_empty() {
        let _ = writeln!(md, "_none_\n");
    } else {
        for row in &pack.entry_points {
            let _ = writeln!(
                md,
                "- `{}` ({}) - {}",
                row.qualified_name,
                row.kind,
                symbol_location(row)
            );
        }
        md.push('\n');
    }

    let _ = writeln!(md, "## Callers\n");
    if pack.callers.is_empty() {
        let _ = writeln!(md, "_none_\n");
    } else {
        for edge in &pack.callers {
            let _ = writeln!(
                md,
                "- `{}` -> `{}` - {}",
                edge.source,
                edge.target,
                edge_location(edge)
            );
        }
        md.push('\n');
    }

    let _ = writeln!(md, "## Callees\n");
    if pack.callees.is_empty() {
        let _ = writeln!(md, "_none_\n");
    } else {
        for edge in &pack.callees {
            let _ = writeln!(
                md,
                "- `{}` -> `{}` - {}",
                edge.source,
                edge.target,
                edge_location(edge)
            );
        }
        md.push('\n');
    }

    let _ = writeln!(md, "## Related files\n");
    if pack.related_files.is_empty() {
        let _ = writeln!(md, "_none_");
    } else {
        for file in &pack.related_files {
            let _ = writeln!(md, "- {file}");
        }
    }

    md
}

pub(crate) fn symbol_location(row: &SearchRow) -> String {
    format!(
        "{}:{}",
        row.file_path,
        line_range(row.start_line, row.end_line)
    )
}

pub(crate) fn edge_location(edge: &EdgeRow) -> String {
    match edge.line {
        Some(line) => format!("{}:{} -> {}", edge.source_file, line, edge.target_file),
        None => format!("{}:? -> {}", edge.source_file, edge.target_file),
    }
}

fn line_range(start: usize, end: usize) -> String {
    if start == end {
        start.to_string()
    } else {
        format!("{start}-{end}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{EdgeRow, SearchRow};

    #[test]
    fn markdown_context_renders_symbol_ranges_and_edge_locations() {
        let pack = ContextPack {
            schema_version: SCHEMA_VERSION,
            task: "wire context".to_string(),
            entry_points: vec![SearchRow {
                id: "sym-run".to_string(),
                kind: "function".to_string(),
                name: "run".to_string(),
                qualified_name: "run".to_string(),
                file_path: "app.py".to_string(),
                start_line: 5,
                end_line: 7,
                signature: "def run():".to_string(),
                score: 1.0,
            }],
            callers: vec![EdgeRow {
                source_id: "sym-main".to_string(),
                source: "main".to_string(),
                target_id: "sym-run".to_string(),
                target: "run".to_string(),
                kind: "call".to_string(),
                line: Some(12),
                source_file: "cli.py".to_string(),
                target_file: "app.py".to_string(),
            }],
            callees: vec![EdgeRow {
                source_id: "sym-run".to_string(),
                source: "run".to_string(),
                target_id: "sym-helper".to_string(),
                target: "helper".to_string(),
                kind: "call".to_string(),
                line: Some(6),
                source_file: "app.py".to_string(),
                target_file: "lib.py".to_string(),
            }],
            related_files: vec![
                "app.py".to_string(),
                "cli.py".to_string(),
                "lib.py".to_string(),
            ],
        };

        let md = render_markdown(&pack);

        assert!(md.contains("- `run` (function) - app.py:5-7"));
        assert!(md.contains("- `main` -> `run` - cli.py:12 -> app.py"));
        assert!(md.contains("- `run` -> `helper` - app.py:6 -> lib.py"));
    }
}
