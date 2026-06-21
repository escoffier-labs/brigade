//! Context packs: entry points for a task plus their caller/callee neighborhoods.

use std::collections::HashSet;

use anyhow::Result;
use rusqlite::Connection;

use crate::model::{ContextPack, Direction};
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
        "_schema v{} · {} entry points · {} callers · {} callees · {} related files_\n",
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
                "- `{}` ({}) — {}:{}",
                row.qualified_name, row.kind, row.file_path, row.start_line
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
                "- `{}` → `{}` ({})",
                edge.source, edge.target, edge.source_file
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
                "- `{}` → `{}` ({})",
                edge.source, edge.target, edge.target_file
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
