//! Freshness contract for deciding whether the graph can be trusted right now.

use std::path::{Path, PathBuf};

use anyhow::Result;
use rusqlite::Connection;
use serde::Serialize;

use crate::model::{IgnoredSummary, PendingChanges};
use crate::store::db::now_ts;
use crate::store::{SCHEMA_VERSION, meta, pending_changes};

#[derive(Debug, Serialize)]
pub struct DoctorReport {
    pub repo_root: String,
    pub db_path: String,
    pub tool_version: String,
    pub schema: SchemaStatus,
    pub last_sync: LastSync,
    pub pending: PendingChanges,
    pub ignored: IgnoredSummary,
    pub verdict: &'static str,
}

#[derive(Debug, Serialize)]
pub struct SchemaStatus {
    pub stored: Option<u32>,
    pub current: u32,
    pub needs_migration: bool,
}

#[derive(Debug, Serialize)]
pub struct LastSync {
    pub synced_at: Option<String>,
    pub age_seconds: Option<i64>,
}

impl DoctorReport {
    pub fn exit_code(&self) -> i32 {
        match self.verdict {
            "FRESH" => 0,
            "STALE" => 1,
            "NEEDS-MIGRATION" => 2,
            _ => 2,
        }
    }
}

pub fn doctor(conn: &Connection, repo_root: &Path, db_path: &Path) -> Result<DoctorReport> {
    let repo_root = resolve_path(repo_root);
    let db_path = resolve_path(db_path);
    let stored_schema =
        meta::read(conn, "schema_version")?.and_then(|value| value.parse::<u32>().ok());
    let synced_at = meta::read(conn, "synced_at")?;
    let age_seconds = synced_at
        .as_deref()
        .and_then(|value| value.parse::<i64>().ok())
        .map(|timestamp| (now_ts() - timestamp).max(0));
    let (pending, ignored) = pending_changes(conn, &repo_root)?;
    let needs_migration = stored_schema != Some(SCHEMA_VERSION);
    let verdict = if needs_migration {
        "NEEDS-MIGRATION"
    } else if pending.is_empty() {
        "FRESH"
    } else {
        "STALE"
    };

    Ok(DoctorReport {
        repo_root: repo_root.to_string_lossy().to_string(),
        db_path: db_path.to_string_lossy().to_string(),
        tool_version: env!("CARGO_PKG_VERSION").to_string(),
        schema: SchemaStatus {
            stored: stored_schema,
            current: SCHEMA_VERSION,
            needs_migration,
        },
        last_sync: LastSync {
            synced_at,
            age_seconds,
        },
        pending,
        ignored,
        verdict,
    })
}

pub fn missing_db_report(repo_root: &Path, db_path: &Path) -> DoctorReport {
    let repo_root = resolve_path(repo_root);
    let db_path = resolve_path(db_path);
    DoctorReport {
        repo_root: repo_root.to_string_lossy().to_string(),
        db_path: db_path.to_string_lossy().to_string(),
        tool_version: env!("CARGO_PKG_VERSION").to_string(),
        schema: SchemaStatus {
            stored: None,
            current: SCHEMA_VERSION,
            needs_migration: true,
        },
        last_sync: LastSync {
            synced_at: None,
            age_seconds: None,
        },
        pending: PendingChanges::default(),
        ignored: IgnoredSummary::default(),
        verdict: "NEEDS-MIGRATION",
    }
}

fn resolve_path(path: &Path) -> PathBuf {
    path.canonicalize().unwrap_or_else(|_| path.to_path_buf())
}
