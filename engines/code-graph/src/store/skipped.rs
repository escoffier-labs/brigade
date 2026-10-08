//! Bookkeeping for files sync could not index and files indexed with parse
//! errors.
//!
//! A skipped file keeps the size and mtime it had when it failed. Freshness
//! checks treat it as current until either changes, so one permanently bad
//! file neither forces a resync nor pins `doctor` to STALE. Read paths
//! tolerate databases written before schema v8, which lack this state.

use std::collections::{BTreeMap, HashMap};

use anyhow::Result;
use rusqlite::{Connection, params};
use serde::Serialize;

use crate::extractors::IndexFailure;
use crate::store::schema::{table_exists, table_has_column};
use crate::store::walk::Entry;

/// The stat a skipped file had when sync last tried it.
pub(super) struct SkippedStat {
    pub(super) size: u64,
    pub(super) mtime: i64,
}

/// One recorded skipped file, as reported by `doctor`.
#[derive(Debug, Clone, Serialize)]
pub struct SkippedFile {
    pub path: String,
    pub reason: String,
}

pub(super) fn load_skipped(conn: &Connection) -> Result<HashMap<String, SkippedStat>> {
    if !table_exists(conn, "skipped_files")? {
        return Ok(HashMap::new());
    }
    let mut stmt = conn.prepare("SELECT path, size, modified_at FROM skipped_files")?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            SkippedStat {
                size: row.get::<_, i64>(1)? as u64,
                mtime: row.get::<_, i64>(2)?,
            },
        ))
    })?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}

pub(super) fn record_skipped(
    tx: &Connection,
    entry: &Entry,
    failure: &IndexFailure,
    now: i64,
) -> Result<()> {
    tx.execute(
        "INSERT OR REPLACE INTO skipped_files(path, reason, detail, size, modified_at, skipped_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        params![
            entry.rel,
            failure.reason.as_str(),
            failure.detail,
            entry.size as i64,
            entry.mtime,
            now
        ],
    )?;
    Ok(())
}

pub(super) fn clear_skipped(tx: &Connection, path: &str) -> Result<()> {
    tx.execute("DELETE FROM skipped_files WHERE path = ?1", params![path])?;
    Ok(())
}

/// Recorded skipped files per reason, ordered by reason.
pub(super) fn skipped_reason_counts(conn: &Connection) -> Result<BTreeMap<String, usize>> {
    if !table_exists(conn, "skipped_files")? {
        return Ok(BTreeMap::new());
    }
    let mut stmt =
        conn.prepare("SELECT reason, COUNT(*) FROM skipped_files GROUP BY reason ORDER BY reason")?;
    let rows = stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)? as usize))
    })?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}

/// Total recorded skipped files plus the first `limit` by path.
pub fn skipped_census(conn: &Connection, limit: usize) -> Result<(usize, Vec<SkippedFile>)> {
    if !table_exists(conn, "skipped_files")? {
        return Ok((0, Vec::new()));
    }
    let count: i64 = conn.query_row("SELECT COUNT(*) FROM skipped_files", [], |row| row.get(0))?;
    let mut stmt = conn.prepare("SELECT path, reason FROM skipped_files ORDER BY path LIMIT ?1")?;
    let sample = stmt
        .query_map([limit as i64], |row| {
            Ok(SkippedFile {
                path: row.get(0)?,
                reason: row.get(1)?,
            })
        })?
        .collect::<rusqlite::Result<_>>()?;
    Ok((count as usize, sample))
}

/// Total indexed files that parsed with errors plus the first `limit` paths.
pub fn parse_error_census(conn: &Connection, limit: usize) -> Result<(usize, Vec<String>)> {
    if !table_has_column(conn, "files", "parse_errors")? {
        return Ok((0, Vec::new()));
    }
    let count: i64 = conn.query_row(
        "SELECT COUNT(*) FROM files WHERE parse_errors != 0",
        [],
        |row| row.get(0),
    )?;
    let mut stmt =
        conn.prepare("SELECT path FROM files WHERE parse_errors != 0 ORDER BY path LIMIT ?1")?;
    let sample = stmt
        .query_map([limit as i64], |row| row.get(0))?
        .collect::<rusqlite::Result<_>>()?;
    Ok((count as usize, sample))
}
