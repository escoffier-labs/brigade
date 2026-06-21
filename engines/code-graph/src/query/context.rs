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
