//! Edge lineage: explain how (or why not) a call between two symbols resolved.
//!
//! Borrowed stance from CocoIndex's CocoInsight: every derived fact should be
//! able to name its derivation. Edges are derived from persisted pending calls
//! by the resolver, so an explanation replays exactly one call's resolution
//! against the live indexes and reports the path it took.

use anyhow::Result;
use rusqlite::Connection;
use serde::Serialize;

use crate::model::{CallKind, PendingCall};
use crate::store::resolve::{
    load_import_index, load_name_index, load_symbol_id_index, matched_import_for,
    resolve_call_explained,
};

#[derive(Debug, Serialize)]
pub struct ExplainRow {
    /// Qualified name of the calling symbol.
    pub source_qualified_name: String,
    pub source_file: String,
    /// Call-site line.
    pub line: usize,
    /// Call shape at the site: bare, member, or scoped.
    pub kind: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub qualifier: Option<String>,
    /// Called name as written at the call site.
    pub target_name: String,
    /// Resolution path label, e.g. "import-strict" or "no-candidates".
    pub resolution: String,
    /// Human sentence for the path.
    pub reason: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub matched_import: Option<ExplainImport>,
    /// Targets the resolver produced. Empty when the path is unresolved.
    pub targets: Vec<ExplainTarget>,
}

#[derive(Debug, Serialize)]
pub struct ExplainImport {
    pub module: String,
    pub line: usize,
}

#[derive(Debug, Serialize)]
pub struct ExplainTarget {
    pub qualified_name: String,
    pub file_path: String,
    pub start_line: usize,
    pub confidence: f64,
}

/// Explain every stored call from symbols matching `source` (name or qualified
/// name) to `target` (the called name as written). Replays resolution against
/// the current graph, read-only.
pub fn explain_calls(conn: &Connection, source: &str, target: &str) -> Result<Vec<ExplainRow>> {
    let name_index = load_name_index(conn)?;
    let import_index = load_import_index(conn)?;
    let source_index = load_symbol_id_index(conn)?;
    let file_index = super::resolve::load_file_index(conn)?;

    let mut stmt = conn.prepare(
        "SELECT p.source_id, p.file_path, p.target_name, p.kind, p.qualifier, p.line,
                s.qualified_name
         FROM pending_calls p
         JOIN symbols s ON s.id = p.source_id
         WHERE (s.name = ?1 OR s.qualified_name = ?1) AND p.target_name = ?2
         ORDER BY p.file_path, p.line",
    )?;
    let calls = stmt.query_map(rusqlite::params![source, target], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, Option<String>>(4)?,
            row.get::<_, i64>(5)?,
            row.get::<_, String>(6)?,
        ))
    })?;

    let mut rows = Vec::new();
    for call in calls {
        let (source_id, source_file, target_name, kind, qualifier, line, source_qualified) = call?;
        let Some(kind) = CallKind::parse(&kind) else {
            continue;
        };
        let pending = PendingCall {
            source_id,
            target_name,
            qualifier,
            kind,
            line: line.max(0) as usize,
            source_file,
        };
        let resolved = resolve_call_explained(
            &pending,
            &name_index,
            &import_index,
            &source_index,
            &file_index,
        );
        let matched_import = import_index
            .get(&pending.source_file)
            .and_then(|imports| matched_import_for(&pending, imports))
            .map(|import| ExplainImport {
                module: import.module.clone(),
                line: import.line,
            });
        let confidence = resolved.path.confidence().unwrap_or(0.0);
        let targets = resolved
            .targets_for_explain()
            .iter()
            .filter_map(|candidate| {
                let symbol = symbol_row(conn, &candidate.id).ok()??;
                Some(ExplainTarget {
                    qualified_name: symbol.0,
                    file_path: symbol.1,
                    start_line: symbol.2,
                    confidence,
                })
            })
            .collect();
        rows.push(ExplainRow {
            source_qualified_name: source_qualified,
            source_file: pending.source_file,
            line: pending.line,
            kind: pending.kind.as_str().to_string(),
            qualifier: pending.qualifier,
            target_name: pending.target_name,
            resolution: resolved.path.label().to_string(),
            reason: resolved.path.describe().to_string(),
            matched_import,
            targets,
        });
    }
    Ok(rows)
}

fn symbol_row(conn: &Connection, id: &str) -> Result<Option<(String, String, usize)>> {
    let mut stmt =
        conn.prepare("SELECT qualified_name, file_path, start_line FROM symbols WHERE id = ?1")?;
    let mut rows = stmt.query_map([id], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)? as usize,
        ))
    })?;
    Ok(rows.next().transpose()?)
}
