//! S29 test adapter: instrument the front's dedicated SQLite connection too.
use rusqlite::{
    Connection,
    hooks::{AuthAction, AuthContext, Authorization},
};
use std::{
    io::Write,
    sync::{Arc, Mutex},
};

pub fn path(configured: &str) -> String {
    match (
        std::env::var("HUB_AUDIT_DB"),
        std::env::var("HUB_AUDIT_DB_ROUTE"),
    ) {
        (Ok(db), Ok(route))
            if std::fs::canonicalize(&db).is_ok_and(|target| {
                std::fs::canonicalize(configured).is_ok_and(|candidate| target == candidate)
            }) =>
        {
            route
        }
        _ => configured.into(),
    }
}
pub fn instrument(connection: &Connection) -> Result<(), String> {
    let Ok(path) = std::env::var("HUB_AUDIT_FILE") else {
        return Ok(());
    };
    let file = Arc::new(Mutex::new(
        std::fs::OpenOptions::new()
            .append(true)
            .create(true)
            .open(path)
            .map_err(|e| e.to_string())?,
    ));
    let emit = move |kind: &str, action: String| -> Result<(), std::io::Error> {
        let record = serde_json::json!({"kind":kind,"pid":std::process::id(),"thread":"rust-credential-lookup","action":action});
        let mut bytes = serde_json::to_vec(&record)?;
        bytes.push(b'\n');
        file.lock().unwrap().write_all(&bytes)
    };
    emit("ready", "instrumented SQLite authorizer".into()).map_err(|e| e.to_string())?;
    connection
        .authorizer(Some(move |context: AuthContext<'_>| {
            let write = match context.action {
                AuthAction::Read { .. }
                | AuthAction::Select
                | AuthAction::Function { .. }
                | AuthAction::Recursive => false,
                AuthAction::Pragma {
                    pragma_name,
                    pragma_value,
                } => {
                    pragma_value.is_some()
                        && !["query_only", "wal_autocheckpoint", "busy_timeout"]
                            .contains(&pragma_name)
                }
                _ => true,
            };
            if write && emit("write", format!("{:?}", context.action)).is_err() {
                return Authorization::Deny;
            }
            Authorization::Allow
        }))
        .map_err(|e| e.to_string())?;
    Ok(())
}
