//! Freshness contract for deciding whether the graph can be trusted right now.

use std::path::{Path, PathBuf};

use anyhow::Result;
use rusqlite::Connection;
use serde::Serialize;

use crate::model::{IgnoredSummary, PendingChanges};
use crate::store::db::now_ts;
use crate::store::{
    SCHEMA_VERSION, SkippedFile, meta, parse_error_census, pending_changes, skipped_census,
};

/// How many paths `doctor` lists for skipped and parse-error files.
const SAMPLE_LIMIT: usize = 5;

#[derive(Debug, Serialize)]
pub struct DoctorReport {
    pub repo_root: String,
    pub db_path: String,
    pub tool_version: String,
    pub schema: SchemaStatus,
    pub resolver: ResolverStatus,
    pub last_sync: LastSync,
    pub branch: BranchStatus,
    pub pending: PendingChanges,
    pub ignored: IgnoredSummary,
    /// Files sync could not index. They do not affect the verdict.
    pub skipped: SkippedSummary,
    /// Indexed files that tree-sitter parsed only by recovering from syntax
    /// errors. They do not affect the verdict.
    pub parse_errors: ParseErrorSummary,
    /// Human-readable notes about skipped and parse-error files.
    pub warnings: Vec<String>,
    pub verdict: &'static str,
}

#[derive(Debug, Default, Serialize)]
pub struct SkippedSummary {
    pub count: usize,
    pub sample: Vec<SkippedFile>,
}

#[derive(Debug, Default, Serialize)]
pub struct ParseErrorSummary {
    pub count: usize,
    pub sample: Vec<String>,
}

/// Which branch the graph was synced on versus the branch checked out now.
/// A drifted graph describes the other branch's code, so doctor reports STALE
/// even when file stats happen to look fresh.
#[derive(Debug, Default, Serialize)]
pub struct BranchStatus {
    pub synced: Option<String>,
    pub current: Option<String>,
    pub drifted: bool,
}

#[derive(Debug, Serialize)]
pub struct SchemaStatus {
    pub stored: Option<u32>,
    pub current: u32,
    pub needs_migration: bool,
}

#[derive(Debug, Serialize)]
pub struct ResolverStatus {
    pub stored: Option<String>,
    pub current: &'static str,
    pub stale: bool,
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
    let branch = branch_status(conn, &repo_root)?;
    let resolver = ResolverStatus {
        stored: meta::read(conn, "resolver_version")?,
        current: crate::store::RESOLVER_VERSION,
        stale: meta::resolver_is_stale(conn)?,
    };
    let (skipped_count, skipped_sample) = skipped_census(conn, SAMPLE_LIMIT)?;
    let skipped = SkippedSummary {
        count: skipped_count,
        sample: skipped_sample,
    };
    let (parse_error_count, parse_error_sample) = parse_error_census(conn, SAMPLE_LIMIT)?;
    let parse_errors = ParseErrorSummary {
        count: parse_error_count,
        sample: parse_error_sample,
    };
    let warnings = warnings(&skipped, &parse_errors);
    let verdict = if needs_migration {
        "NEEDS-MIGRATION"
    } else if pending.is_empty() && !branch.drifted && !resolver.stale {
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
        resolver,
        last_sync: LastSync {
            synced_at,
            age_seconds,
        },
        branch,
        pending,
        ignored,
        skipped,
        parse_errors,
        warnings,
        verdict,
    })
}

fn warnings(skipped: &SkippedSummary, parse_errors: &ParseErrorSummary) -> Vec<String> {
    let mut warnings = Vec::new();
    if skipped.count > 0 {
        warnings.push(format!(
            "{} {} could not be indexed and {} missing from the graph",
            skipped.count,
            plural(skipped.count, "file"),
            if skipped.count == 1 { "is" } else { "are" }
        ));
    }
    if parse_errors.count > 0 {
        warnings.push(format!(
            "{} {} indexed with parse errors, so symbols or edges from {} may be missing",
            parse_errors.count,
            plural(parse_errors.count, "file"),
            if parse_errors.count == 1 {
                "it"
            } else {
                "them"
            }
        ));
    }
    warnings
}

fn plural(count: usize, noun: &str) -> String {
    if count == 1 {
        noun.to_string()
    } else {
        format!("{noun}s")
    }
}

fn branch_status(conn: &Connection, repo_root: &Path) -> Result<BranchStatus> {
    let synced = meta::read(conn, "synced_branch")?;
    let current = crate::store::current_git_branch(repo_root);
    let drifted = matches!((&synced, &current), (Some(synced), Some(current)) if synced != current);
    Ok(BranchStatus {
        synced,
        current,
        drifted,
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
        resolver: ResolverStatus {
            stored: None,
            current: crate::store::RESOLVER_VERSION,
            stale: true,
        },
        last_sync: LastSync {
            synced_at: None,
            age_seconds: None,
        },
        branch: BranchStatus::default(),
        pending: PendingChanges::default(),
        ignored: IgnoredSummary::default(),
        skipped: SkippedSummary::default(),
        parse_errors: ParseErrorSummary::default(),
        warnings: Vec::new(),
        verdict: "NEEDS-MIGRATION",
    }
}

fn resolve_path(path: &Path) -> PathBuf {
    path.canonicalize().unwrap_or_else(|_| path.to_path_buf())
}
