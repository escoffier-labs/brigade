//! Database lifecycle: path resolution, connection opening, and time helpers.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};
use std::{collections::HashSet, ffi::OsStr};

use anyhow::{Context, Result};
use rusqlite::{Connection, OpenFlags};

static CREATED_DB_DIRS: OnceLock<Mutex<HashSet<PathBuf>>> = OnceLock::new();

/// Resolve the db path: an explicit `--db` wins, else `<root>/.graphtrail/graphtrail.db`.
pub fn db_path(explicit: Option<PathBuf>, root: &Path) -> PathBuf {
    explicit.unwrap_or_else(|| root.join(".graphtrail").join("graphtrail.db"))
}

/// Open the db for a query command, defaulting to `.graphtrail/graphtrail.db` in the cwd.
pub fn open_default(explicit: Option<PathBuf>) -> Result<Connection> {
    let db = explicit.unwrap_or_else(|| PathBuf::from(".graphtrail/graphtrail.db"));
    open_db(&db)
}

/// Open the db read-only for a query command, defaulting to `.graphtrail/graphtrail.db`.
pub fn open_default_read_only(explicit: Option<PathBuf>) -> Result<Connection> {
    let db = explicit.unwrap_or_else(|| PathBuf::from(".graphtrail/graphtrail.db"));
    open_read_only(&db)
}

/// Open (creating parent dirs) a WAL-mode SQLite connection.
pub fn open_db(path: &Path) -> Result<Connection> {
    if let Some(parent) = path.parent() {
        let records_graphtrail_creation =
            parent.file_name() == Some(OsStr::new(".graphtrail")) && !parent.exists();
        fs::create_dir_all(parent)
            .with_context(|| format!("failed to create db directory {}", parent.display()))?;
        if records_graphtrail_creation {
            record_created_db_dir(parent);
        }
    }
    let conn =
        Connection::open(path).with_context(|| format!("failed to open {}", path.display()))?;
    conn.pragma_update(None, "journal_mode", "WAL")?;
    Ok(conn)
}

pub(crate) fn take_created_graph_dir(path: &Path) -> bool {
    let normalized = normalize_dir(path);
    created_db_dirs()
        .lock()
        .map(|mut dirs| dirs.remove(&normalized))
        .unwrap_or(false)
}

fn record_created_db_dir(path: &Path) {
    let normalized = normalize_dir(path);
    if let Ok(mut dirs) = created_db_dirs().lock() {
        dirs.insert(normalized);
    }
}

fn created_db_dirs() -> &'static Mutex<HashSet<PathBuf>> {
    CREATED_DB_DIRS.get_or_init(|| Mutex::new(HashSet::new()))
}

fn normalize_dir(path: &Path) -> PathBuf {
    path.canonicalize().unwrap_or_else(|_| path.to_path_buf())
}

/// Open an existing db read-only. Used by the MCP server and query commands so they can never
/// mutate the graph. Deliberately NOT `immutable=1`: these dbs are rewritten by a background
/// sync, and immutable connections skip locking entirely, so a concurrent write could serve
/// torn reads. SQLite (3.22+) reads a quiescent WAL db without creating sidecar files.
pub fn open_read_only(path: &Path) -> Result<Connection> {
    let conn = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .with_context(|| format!("failed to open {} read-only", path.display()))?;
    Ok(conn)
}

pub fn now_ts() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs() as i64)
}
