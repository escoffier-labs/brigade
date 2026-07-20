//! Read-only links into MiseLedger's evidence archive. Feature: `miseledger`.
//!
//! GraphTrail does not own receipts; this only *reads* MiseLedger's SQLite FTS index to surface
//! prior sessions/items that mention a symbol or term, so a symbol can be tied back to evidence.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use rusqlite::{Connection, OpenFlags, params};
use serde::Serialize;

/// Emitted once by each deprecated CLI command invocation, always on stderr.
pub const DEPRECATION_WARNING: &str = "warning: GraphTrail's direct MiseLedger adapter is deprecated. Use `brigade code sync`, `brigade code context`, `brigade code impact`, `brigade evidence crawl`, `brigade evidence search`, and `brigade evidence doctor` instead. The adapter remains functional for at least two minor GraphTrail releases or 90 days after the first GraphTrail release containing this deprecation, whichever is longer. It will not be removed before that compatibility policy is satisfied.";

pub fn emit_deprecation_warning() {
    eprintln!("{DEPRECATION_WARNING}");
}

#[derive(Debug, Serialize)]
pub struct EvidenceHit {
    pub item_id: String,
    pub source_kind: String,
    pub snippet: String,
}

/// Default MiseLedger db path: `$MISELEDGER_DB` or `~/.local/share/miseledger/miseledger.db`.
pub fn default_db_path() -> PathBuf {
    if let Ok(path) = std::env::var("MISELEDGER_DB") {
        return PathBuf::from(path);
    }
    let home = std::env::var("HOME").unwrap_or_default();
    PathBuf::from(home).join(".local/share/miseledger/miseledger.db")
}

/// Full-text search MiseLedger item bodies for `term`, returning matching evidence items.
pub fn search_evidence(db: &Path, term: &str, limit: usize) -> Result<Vec<EvidenceHit>> {
    let conn = Connection::open_with_flags(
        db,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .with_context(|| format!("failed to open miseledger db {}", db.display()))?;

    let mut stmt = conn.prepare(
        r#"
        SELECT item_id, source_kind, snippet(item_fts, -1, '[', ']', ' … ', 12)
        FROM item_fts
        WHERE item_fts MATCH ?1
        LIMIT ?2
        "#,
    )?;
    let mapped = stmt.query_map(params![fts_phrase(term), limit as i64], |row| {
        Ok(EvidenceHit {
            item_id: row.get(0)?,
            source_kind: row.get(1)?,
            snippet: row.get(2)?,
        })
    })?;
    let mut hits = Vec::new();
    for row in mapped {
        hits.push(row?);
    }
    Ok(hits)
}

/// Wrap a term as a quoted FTS5 phrase so identifiers with punctuation are matched literally.
fn fts_phrase(term: &str) -> String {
    format!("\"{}\"", term.replace('"', "\"\""))
}
