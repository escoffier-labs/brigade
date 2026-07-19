//! Advisory sync lock: one writer per database file.
//!
//! Sync from the CLI and the MCP `refresh: true` path can run concurrently (the MCP server
//! spawns refresh syncs on worker threads). SQLite serializes the transactions, but both
//! processes would still plan against the same pre-sync state and duplicate the extraction
//! work. The lockfile makes the second sync fail fast instead.
//!
//! The lock is a `<db>.lock` file containing the owning PID. A lock whose owner is no longer
//! alive is reclaimed automatically, so a crashed or killed sync never wedges the database.

use std::fs;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};

/// How old an existing lock must be before it is reclaimed on platforms where
/// process liveness cannot be checked.
const STALE_LOCK_MAX_AGE_SECS: u64 = 60 * 60;

/// RAII guard for the sync lock; dropping it releases the lock.
#[derive(Debug)]
pub struct SyncLock {
    path: PathBuf,
}

impl SyncLock {
    /// Acquire the sync lock for `db_path`, reclaiming a stale one if its owner died.
    pub fn acquire(db_path: &Path) -> Result<SyncLock> {
        let path = lock_path(db_path);
        for attempt in 0..2 {
            match fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&path)
            {
                Ok(file) => {
                    use std::io::Write;
                    let mut file = file;
                    let _ = writeln!(file, "{}", std::process::id());
                    return Ok(SyncLock { path });
                }
                Err(err) if err.kind() == ErrorKind::AlreadyExists && attempt == 0 => {
                    if lock_is_stale(&path) {
                        // Owner is gone; remove and retry once. A racing reclaim can hit
                        // NotFound here, which is fine: the retry loop settles it.
                        match fs::remove_file(&path) {
                            Ok(()) | Err(_) => continue,
                        }
                    }
                    bail!(
                        "another sync is already running for this database (lock: {}). \
                         If no sync is running, delete the lock file and retry.",
                        path.display()
                    );
                }
                Err(err) if err.kind() == ErrorKind::AlreadyExists => {
                    bail!(
                        "another sync is already running for this database (lock: {}). \
                         If no sync is running, delete the lock file and retry.",
                        path.display()
                    );
                }
                Err(err) => {
                    return Err(err)
                        .with_context(|| format!("failed to create lock {}", path.display()));
                }
            }
        }
        unreachable!("lock acquire loop always returns");
    }
}

impl Drop for SyncLock {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

fn lock_path(db_path: &Path) -> PathBuf {
    let mut file_name = db_path
        .file_name()
        .map_or_else(|| "graphtrail.db".into(), |name| name.to_os_string());
    file_name.push(".lock");
    db_path.with_file_name(file_name)
}

/// A lock is stale when its owning process is provably dead, or (where liveness
/// cannot be checked) when it is implausibly old.
fn lock_is_stale(path: &Path) -> bool {
    let owner_pid = fs::read_to_string(path)
        .ok()
        .and_then(|content| content.trim().parse::<u32>().ok());
    if let Some(alive) = owner_pid.and_then(pid_alive) {
        return !alive;
    }
    // Unreadable owner or no liveness signal: fall back to age.
    fs::metadata(path)
        .and_then(|meta| meta.modified())
        .ok()
        .and_then(|modified| modified.elapsed().ok())
        .is_some_and(|age| age.as_secs() > STALE_LOCK_MAX_AGE_SECS)
}

#[cfg(unix)]
fn pid_alive(pid: u32) -> Option<bool> {
    // Signal 0 performs error checking only. ESRCH means the process is gone;
    // EPERM means it exists but belongs to someone else.
    let result = unsafe { libc::kill(pid as libc::pid_t, 0) };
    if result == 0 {
        return Some(true);
    }
    match std::io::Error::last_os_error().raw_os_error() {
        Some(code) if code == libc::ESRCH => Some(false),
        Some(code) if code == libc::EPERM => Some(true),
        _ => None,
    }
}

#[cfg(not(unix))]
fn pid_alive(_pid: u32) -> Option<bool> {
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn acquire_creates_and_drop_removes_lock() {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("g.db");
        let lock_file = dir.path().join("g.db.lock");

        let lock = SyncLock::acquire(&db).unwrap();
        assert!(lock_file.exists());
        drop(lock);
        assert!(!lock_file.exists());
    }

    #[test]
    fn live_owner_blocks_second_acquire() {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("g.db");

        let _lock = SyncLock::acquire(&db).unwrap();
        let second = SyncLock::acquire(&db);
        assert!(second.is_err());
        assert!(
            second
                .unwrap_err()
                .to_string()
                .contains("another sync is already running")
        );
    }

    #[test]
    fn dead_owner_lock_is_reclaimed() {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("g.db");
        let lock_file = dir.path().join("g.db.lock");
        // PIDs are capped well below this on Linux (default max 4194304), so
        // this owner can never be alive.
        fs::write(&lock_file, "999999999\n").unwrap();

        let lock = SyncLock::acquire(&db);
        #[cfg(unix)]
        {
            let lock = lock.unwrap();
            assert!(lock_file.exists());
            drop(lock);
            assert!(!lock_file.exists());
        }
        #[cfg(not(unix))]
        {
            // Without a liveness check the fresh lock is not yet stale by age.
            assert!(lock.is_err());
        }
    }
}
