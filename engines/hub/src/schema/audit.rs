//! Adapter for the existing opt-in read-only acceptance audit. It observes
//! native connections too, including writes attempted outside request handlers.
use rusqlite::{
    Connection, OpenFlags,
    hooks::{AuthAction, Authorization},
    trace::{TraceEvent, TraceEventCodes},
};
use serde_json::json;
use std::{io::Write, path::Path, sync::Mutex};
static LOG: Mutex<()> = Mutex::new(());
pub fn enabled() -> bool {
    std::env::var_os("HUB_AUDIT_FILE").is_some()
}
fn emit(kind: &str, action: &str) -> std::io::Result<()> {
    let Some(path) = std::env::var_os("HUB_AUDIT_FILE") else {
        return Ok(());
    };
    let _guard = LOG
        .lock()
        .map_err(|_| std::io::Error::other("audit recorder poisoned"))?;
    let mut file = std::fs::OpenOptions::new().append(true).open(path)?;
    writeln!(
        file,
        "{}",
        json!({"kind":kind,"pid":std::process::id(),"thread":"native-sqlite","action":action})
    )
}
fn trace(event: TraceEvent<'_>) {
    if let TraceEvent::Stmt(_, sql) = event {
        // No SQL or row values are recorded. Strip comments before recognizing
        // write-locking BEGIN forms, as the Python audit does.
        let mut sql = sql.trim_start();
        loop {
            if let Some(rest) = sql.strip_prefix("--") {
                sql = rest
                    .split_once('\n')
                    .map(|(_, s)| s)
                    .unwrap_or("")
                    .trim_start();
            } else if let Some(rest) = sql.strip_prefix("/*") {
                sql = rest
                    .split_once("*/")
                    .map(|(_, s)| s)
                    .unwrap_or("")
                    .trim_start();
            } else {
                break;
            }
        }
        let mut words = sql
            .split(|c: char| !c.is_ascii_alphabetic())
            .filter(|s| !s.is_empty());
        if words
            .next()
            .is_some_and(|s| s.eq_ignore_ascii_case("BEGIN"))
            && words.next().is_some_and(|s| {
                s.eq_ignore_ascii_case("IMMEDIATE") || s.eq_ignore_ascii_case("EXCLUSIVE")
            })
        {
            // A failed audit append must not silently certify a run.
            if emit("begin", "write-lock").is_err() {
                std::process::abort();
            }
        }
    }
}
pub fn open(path: &Path, flags: OpenFlags) -> Result<Connection, String> {
    open_with_vfs(path, flags, None)
}

pub(crate) fn open_with_vfs(
    path: &Path,
    flags: OpenFlags,
    vfs: Option<&str>,
) -> Result<Connection, String> {
    let routed = match (
        std::env::var_os("HUB_AUDIT_DB"),
        std::env::var_os("HUB_AUDIT_DB_ROUTE"),
    ) {
        (Some(db), Some(route))
            if path
                .canonicalize()
                .is_ok_and(|p| Path::new(&db).canonicalize().is_ok_and(|d| p == d)) =>
        {
            std::path::PathBuf::from(route)
        }
        _ => path.to_path_buf(),
    };
    let c = match vfs {
        Some(vfs) => Connection::open_with_flags_and_vfs(routed, flags, vfs),
        None => Connection::open_with_flags(routed, flags),
    }
    .map_err(|e| e.to_string())?;
    if enabled() {
        c.authorizer(Some(|context: rusqlite::hooks::AuthContext<'_>| {
            let writing = match context.action {
                AuthAction::Read { .. }
                | AuthAction::Select
                | AuthAction::Function { .. }
                | AuthAction::Recursive
                | AuthAction::Transaction { .. }
                | AuthAction::Savepoint { .. } => false,
                AuthAction::Pragma {
                    pragma_name,
                    pragma_value,
                } => {
                    let name = pragma_name.to_ascii_lowercase();
                    !matches!(
                        name.as_str(),
                        "busy_timeout"
                            | "query_only"
                            | "foreign_keys"
                            | "cache_size"
                            | "temp_store"
                            | "trusted_schema"
                    ) && (pragma_value.is_some()
                        || matches!(
                            name.as_str(),
                            "wal_checkpoint" | "optimize" | "incremental_vacuum"
                        ))
                }
                _ => true,
            };
            if writing && emit("write", "native-sqlite").is_err() {
                Authorization::Deny
            } else {
                Authorization::Allow
            }
        }))
        .map_err(|e| e.to_string())?;
        c.trace_v2(TraceEventCodes::SQLITE_TRACE_STMT, Some(trace));
        emit("ready", "native-sqlite").map_err(|e| e.to_string())?;
    }
    Ok(c)
}
