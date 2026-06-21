//! Database lifecycle: path resolution, connection opening, and time helpers.

use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use rusqlite::Connection;

/// Resolve the db path: an explicit `--db` wins, else `<root>/.graphtrail/graphtrail.db`.
pub fn db_path(explicit: Option<PathBuf>, root: &Path) -> PathBuf {
    explicit.unwrap_or_else(|| root.join(".graphtrail").join("graphtrail.db"))
}

/// Open the db for a query command, defaulting to `.graphtrail/graphtrail.db` in the cwd.
pub fn open_default(explicit: Option<PathBuf>) -> Result<Connection> {
    let db = explicit.unwrap_or_else(|| PathBuf::from(".graphtrail/graphtrail.db"));
    open_db(&db)
}

/// Open (creating parent dirs) a WAL-mode SQLite connection.
pub fn open_db(path: &Path) -> Result<Connection> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("failed to create db directory {}", parent.display()))?;
    }
    let conn =
        Connection::open(path).with_context(|| format!("failed to open {}", path.display()))?;
    conn.pragma_update(None, "journal_mode", "WAL")?;
    Ok(conn)
}

pub fn now_ts() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs() as i64)
}
