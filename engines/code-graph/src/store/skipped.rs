//! Bookkeeping for files sync could not index and files indexed with parse
//! errors.
//!
//! A skipped file keeps the size and mtime it had when it failed. Freshness
//! checks treat it as current until either changes (or, for an I/O failure,
//! until it becomes readable), so one permanently bad file neither forces a
//! resync nor pins `doctor` to STALE. Read paths
//! tolerate databases written before schema v8, which lack this state.

use std::collections::{BTreeMap, HashMap};
use std::fs;
use std::io::Read;
use std::path::Path;

use anyhow::Result;
use rusqlite::{Connection, params};
use serde::Serialize;

use crate::extractors::{IndexFailure, SkipReason};
use crate::store::schema::{table_exists, table_has_column};
use crate::store::walk::Entry;

/// The stat a skipped file had when sync last tried it, and why it failed.
pub(super) struct SkippedStat {
    pub(super) size: u64,
    pub(super) mtime: i64,
    pub(super) reason: String,
}

impl SkippedStat {
    /// Whether a skipped file still has to be skipped.
    ///
    /// Decode and parse failures depend only on content, so an unchanged size
    /// and mtime mean retrying would fail the same way. An I/O failure can
    /// clear without touching either (a chmod, say), so it is probed for
    /// readability instead. Probing rather than always retrying keeps a file
    /// that is still unreadable from turning every sync into a reindex and
    /// edge rebuild, while a file that became readable shows up as new to both
    /// sync and `doctor`.
    pub(super) fn still_skipped(&self, entry: &Entry) -> bool {
        if self.size != entry.size || self.mtime != entry.mtime {
            return false;
        }
        if self.reason == SkipReason::IoError.as_str() {
            return !is_readable(&entry.path);
        }
        true
    }
}

/// Whether `path` is a regular file whose first byte can be read.
///
/// The path may have changed since the walk captured it, so the probe trusts
/// nothing: a symlink or special file counts as unreadable, the open neither
/// follows symlinks nor blocks (a FIFO would otherwise hang until a writer
/// appears), and the opened descriptor must itself be a regular file.
fn is_readable(path: &Path) -> bool {
    let is_regular = |metadata: fs::Metadata| metadata.file_type().is_file();
    if !fs::symlink_metadata(path).is_ok_and(is_regular) {
        return false;
    }
    let Ok(mut file) = open_for_probe(path) else {
        return false;
    };
    file.metadata().is_ok_and(is_regular) && file.read(&mut [0u8; 1]).is_ok()
}

#[cfg(unix)]
fn open_for_probe(path: &Path) -> std::io::Result<fs::File> {
    use std::os::unix::fs::OpenOptionsExt;
    fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NONBLOCK | libc::O_NOFOLLOW)
        .open(path)
}

#[cfg(not(unix))]
fn open_for_probe(path: &Path) -> std::io::Result<fs::File> {
    fs::File::open(path)
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
    let mut stmt = conn.prepare("SELECT path, size, modified_at, reason FROM skipped_files")?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            SkippedStat {
                size: row.get::<_, i64>(1)? as u64,
                mtime: row.get::<_, i64>(2)?,
                reason: row.get::<_, String>(3)?,
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

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::sync::mpsc;
    use std::time::Duration;

    use crate::model::Lang;

    /// An io_error skip whose walk entry was captured while `path` was a
    /// regular file of `size` bytes, as if the path changed after the walk.
    fn io_error_skip(path: PathBuf, size: u64) -> (SkippedStat, Entry) {
        let stat = SkippedStat {
            size,
            mtime: 7,
            reason: SkipReason::IoError.as_str().to_string(),
        };
        let entry = Entry {
            rel: path.file_name().unwrap().to_string_lossy().into_owned(),
            path,
            lang: Lang::Python,
            size,
            mtime: 7,
        };
        (stat, entry)
    }

    #[test]
    fn probe_refuses_a_fifo_without_blocking() {
        let dir = tempfile::tempdir().unwrap();
        let fifo = dir.path().join("swapped.py");
        let made = std::process::Command::new("mkfifo").arg(&fifo).status();
        if !made.is_ok_and(|status| status.success()) {
            return; // mkfifo unavailable: cannot reproduce.
        }
        let (stat, entry) = io_error_skip(fifo.clone(), 12);

        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let _ = tx.send(stat.still_skipped(&entry));
        });
        let result = rx.recv_timeout(Duration::from_secs(5));
        if result.is_err() {
            // Release the blocked reader so the thread can exit.
            let _ = fs::OpenOptions::new().write(true).open(&fifo);
        }

        assert_eq!(
            result.ok(),
            Some(true),
            "a FIFO must count as still unreadable, promptly"
        );
    }

    #[test]
    fn probe_refuses_a_symlink_to_a_readable_file() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("target.py");
        fs::write(&target, "def ok():\n    pass\n").unwrap();
        let link = dir.path().join("swapped.py");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        let (stat, entry) = io_error_skip(link, 12);

        assert!(stat.still_skipped(&entry));
    }

    #[test]
    fn probe_accepts_a_readable_regular_file() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("restored.py");
        fs::write(&file, "def ok():\n    pass\n").unwrap();
        let (stat, entry) = io_error_skip(file, 12);

        assert!(!stat.still_skipped(&entry));
    }
}
