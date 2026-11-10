//! `brigade-hub memory-restore-epoch`: start a new `log_epoch` on a memory
//! file restored from a copy, before the hub serves it (#2292).
//!
//! Revisions are unique only within one epoch, so a restored copy would hand
//! out revision numbers that clients saw before the loss. With a new epoch, a
//! write based on such a revision is refused with `memory_epoch_changed`, and
//! a client's next pull discards its replica and rebuilds it.
//!
//! The command refuses, changing nothing, when a hub or any other connection
//! holds the file, when a write-ahead log or rollback journal sits beside it,
//! when the file is not a memory file this binary can serve, and when SQLite's
//! quick check fails. It changes only the value of the `log_epoch` row. A
//! rerun mints another epoch no client has seen, which is safe: a marker in
//! the file saying "already rotated" would also be in any copy taken before
//! the hub served, and restoring that copy later would then skip the rotation.
use super::{ids, schema};
use rusqlite::{Connection, ErrorCode, OpenFlags};
use std::{
    ffi::OsString,
    path::{Path, PathBuf},
    time::Duration,
};

/// The first argument that selects this command instead of the server.
pub const COMMAND: &str = "memory-restore-epoch";

#[derive(Debug, PartialEq, Eq)]
pub struct Rotated {
    pub previous: String,
    pub epoch: String,
    pub user_version: i64,
}

fn sidecar(path: &Path, suffix: &str) -> PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push(suffix);
    PathBuf::from(name)
}

/// Rotate the epoch of the memory file at `path`, refusing on any check.
pub fn rotate_restored(path: &Path, now_ms: i64) -> Result<Rotated, String> {
    let shown = path.display();
    // SQLite resolves a symlink and uses the sidecars beside the real file, so
    // checking `<link>-wal` would miss a stale WAL there. Refuse the link, then
    // check sidecars against and open the one canonical path, which also
    // settles any symlinked parent directory.
    let link = std::fs::symlink_metadata(path).map_err(|e| format!("{shown}: {e}"))?;
    if link.file_type().is_symlink() {
        return Err(format!(
            "{shown} is a symbolic link: pass the real path of the memory file, so the -wal and -journal checked are the ones SQLite would use"
        ));
    }
    let real = std::fs::canonicalize(path).map_err(|e| format!("{shown}: {e}"))?;
    let path = real.as_path();
    let metadata = std::fs::metadata(path).map_err(|e| format!("{shown}: {e}"))?;
    if !metadata.is_file() {
        return Err(format!("{shown} is not a regular file"));
    }
    // Frames in a WAL left from the replaced file, or from a running hub,
    // would be replayed onto the restored copy when it is opened.
    for (suffix, any_size) in [("-wal", false), ("-journal", true)] {
        let side = sidecar(path, suffix);
        if let Ok(m) = std::fs::metadata(&side) {
            if any_size || m.len() > 0 {
                return Err(format!(
                    "{} sits beside the memory file: it belongs to the replaced file or to a running hub. \
                     Stop the hub, remove the old file's -wal, -shm and -journal, put the copy in place, then run this again",
                    side.display()
                ));
            }
        }
    }
    // No create flag: a mistyped path must not leave an empty file behind.
    let c = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_WRITE)
        .map_err(|e| format!("{shown}: {e}"))?;
    c.busy_timeout(Duration::ZERO).map_err(|e| e.to_string())?;
    // Every WAL connection keeps a shared lock on the file while it is open,
    // so the exclusive lock fails while a hub or reader holds the file. In
    // exclusive locking mode the lock then stays until this connection closes,
    // so no hub can open the file between these checks and the rotation.
    c.pragma_update(None, "locking_mode", "EXCLUSIVE")
        .map_err(|e| e.to_string())?;
    if let Err(e) = c.execute_batch("BEGIN EXCLUSIVE") {
        return Err(match e.sqlite_error_code() {
            Some(ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked) => format!(
                "{shown} is held by another process; stop the hub (and anything else reading the file) first"
            ),
            _ => format!("{shown}: {e}"),
        });
    }
    let checked = check(&c);
    // Nothing above wrote; the exclusive lock survives the end of the
    // transaction until the connection closes.
    let ended = c.execute_batch(if checked.is_ok() {
        "COMMIT"
    } else {
        "ROLLBACK"
    });
    let (previous, user_version) = checked?;
    ended.map_err(|e| e.to_string())?;
    let epoch = schema::rotate_epoch(&c, now_ms).map_err(|e| e.to_string())?;
    Ok(Rotated {
        previous,
        epoch,
        user_version,
    })
}

/// The file's current epoch and schema version, or why it cannot be rotated.
fn check(c: &Connection) -> Result<(String, i64), String> {
    let pragma = |name: &str| {
        c.pragma_query_value(None, name, |r| r.get::<_, i64>(0))
            .map_err(|e| e.to_string())
    };
    let (application, version) = (pragma("application_id")?, pragma("user_version")?);
    if version == 0 {
        return Err("the file has no memory schema; is this fleet-hub-memory.db?".into());
    }
    if application != schema::APPLICATION_ID {
        return Err("the file has a foreign application_id; is this fleet-hub-memory.db?".into());
    }
    if version > schema::USER_VERSION {
        return Err(format!(
            "memory schema version {version} is newer than this hub ({}); run the command from a brigade-hub at least as new as the copy",
            schema::USER_VERSION
        ));
    }
    let quick: String = c
        .query_row("PRAGMA quick_check", [], |r| r.get(0))
        .map_err(|e| e.to_string())?;
    if quick != "ok" {
        return Err(format!("quick_check failed: {quick}"));
    }
    let previous = schema::log_epoch(c).map_err(|_| "the file has no log_epoch row".to_string())?;
    Ok((previous, version))
}

#[derive(clap::Parser, Debug)]
#[command(
    name = "brigade-hub memory-restore-epoch",
    about = "Start a new log_epoch on a memory file restored from a copy. Run it with the hub stopped, before the hub serves the file."
)]
struct Args {
    /// The restored fleet-hub-memory.db.
    #[arg(long)]
    memory_db: PathBuf,
}

/// Run the command with `args` (the first is this command's name). Prints one
/// JSON line and returns the exit code: 0 rotated, 1 refused, 2 usage.
pub fn main(args: impl IntoIterator<Item = OsString>) -> i32 {
    use clap::Parser;
    let args = match Args::try_parse_from(args) {
        Ok(args) => args,
        Err(e) => {
            let _ = e.print();
            return if e.use_stderr() { 2 } else { 0 };
        }
    };
    let now = ids::Clock { file: None }.now_ms();
    let shown = args.memory_db.display().to_string();
    let (code, line) = match rotate_restored(&args.memory_db, now) {
        Ok(r) => (
            0,
            serde_json::json!({"ok": true, "memory_db": shown, "previous_log_epoch": r.previous,
                               "log_epoch": r.epoch, "user_version": r.user_version}),
        ),
        Err(error) => (
            1,
            serde_json::json!({"ok": false, "memory_db": shown, "error": error}),
        ),
    };
    println!("{line}");
    code
}
