//! Freshness contract for deciding whether the graph can be trusted right now.
//!
//! `doctor` reports the full contract. `freshness` reduces the same contract to
//! the compact object the MCP server attaches to graph query responses, so the
//! two can never disagree on a verdict.

use std::collections::BTreeMap;
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
    /// How well each language's call sites resolve, keyed by language. Lets a
    /// caller read "no callers" as "none" or as "this language resolves poorly".
    /// Empty until sync repairs an outdated schema or stale resolver state.
    pub coverage: BTreeMap<String, LanguageCoverage>,
    pub verdict: &'static str,
}

/// Per-language index and resolution counts. Call sites are every call the
/// extractor recorded; a site is resolved when it produced at least one edge.
/// Unresolved sites include calls into the standard library and third-party
/// code, which the graph never indexes.
#[derive(Debug, Default, Clone, Serialize, PartialEq, Eq)]
pub struct LanguageCoverage {
    pub files: i64,
    pub symbols: i64,
    pub call_sites: i64,
    pub resolved_call_sites: i64,
    pub unresolved_call_sites: i64,
    /// Resolved call edges whose target lives in a different file.
    pub cross_file_edges: i64,
}

/// Compact freshness verdict for query responses: the `doctor` contract
/// without its diagnostics, small enough to ship on every call.
#[derive(Debug, Clone, Serialize)]
pub struct Freshness {
    pub synced_at: Option<String>,
    pub age_seconds: Option<i64>,
    /// The checked-out branch differs from the one the graph was synced on.
    pub branch_moved: bool,
    /// HEAD points at a different commit than at sync. Informational: a new
    /// commit over already-indexed content leaves the verdict FRESH, while
    /// any file it changed on disk shows up in `pending_files`.
    pub head_moved: bool,
    /// New, changed, deleted, and fingerprint-stale files not yet synced.
    pub pending_files: usize,
    /// FRESH, STALE, NEEDS-MIGRATION, or UNKNOWN when the check itself failed.
    pub verdict: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

impl Freshness {
    /// Freshness could not be computed. Reported as UNKNOWN rather than
    /// omitted, so a caller never mistakes a failed check for a fresh graph.
    pub fn unknown(error: impl std::fmt::Display) -> Self {
        Freshness {
            synced_at: None,
            age_seconds: None,
            branch_moved: false,
            head_moved: false,
            pending_files: 0,
            verdict: "UNKNOWN",
            error: Some(error.to_string()),
        }
    }
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
    /// HEAD resolves to a different commit than at sync. Does not affect the
    /// verdict; changed content already does through `pending`.
    pub head_moved: bool,
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

/// The verdict-bearing part of the doctor report, shared with `freshness`.
struct Contract {
    schema: SchemaStatus,
    resolver: ResolverStatus,
    last_sync: LastSync,
    branch: BranchStatus,
    pending: PendingChanges,
    ignored: IgnoredSummary,
    verdict: &'static str,
}

fn contract(conn: &Connection, repo_root: &Path) -> Result<Contract> {
    let stored_schema =
        meta::read(conn, "schema_version")?.and_then(|value| value.parse::<u32>().ok());
    let synced_at = meta::read(conn, "synced_at")?;
    let age_seconds = synced_at
        .as_deref()
        .and_then(|value| value.parse::<i64>().ok())
        .map(|timestamp| (now_ts() - timestamp).max(0));
    let (pending, ignored) = pending_changes(conn, repo_root)?;
    let needs_migration = stored_schema != Some(SCHEMA_VERSION);
    let branch = branch_status(conn, repo_root)?;
    let resolver = ResolverStatus {
        stored: meta::read(conn, "resolver_version")?,
        current: crate::store::RESOLVER_VERSION,
        stale: meta::resolver_is_stale(conn)?,
    };
    let verdict = if needs_migration {
        "NEEDS-MIGRATION"
    } else if pending.is_empty() && !branch.drifted && !resolver.stale {
        "FRESH"
    } else {
        "STALE"
    };
    Ok(Contract {
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
        verdict,
    })
}

/// Compact freshness for `repo_root`, with the same verdict `doctor` gives.
pub fn freshness(conn: &Connection, repo_root: &Path) -> Result<Freshness> {
    let contract = contract(conn, &resolve_path(repo_root))?;
    let pending = contract.pending;
    Ok(Freshness {
        synced_at: contract.last_sync.synced_at,
        age_seconds: contract.last_sync.age_seconds,
        branch_moved: contract.branch.drifted,
        head_moved: contract.branch.head_moved,
        pending_files: pending.new_files
            + pending.changed_files
            + pending.deleted_files
            + pending.fingerprint_stale,
        verdict: contract.verdict,
        error: None,
    })
}

pub fn doctor(conn: &Connection, repo_root: &Path, db_path: &Path) -> Result<DoctorReport> {
    let repo_root = resolve_path(repo_root);
    let db_path = resolve_path(db_path);
    let contract = contract(conn, &repo_root)?;
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
    // An unmigrated graph may predate the tables coverage reads. Resolver
    // replay must also use the same rules that produced the persisted edges.
    let coverage = if contract.schema.needs_migration || contract.resolver.stale {
        BTreeMap::new()
    } else {
        language_coverage(conn)?
    };

    Ok(DoctorReport {
        repo_root: repo_root.to_string_lossy().to_string(),
        db_path: db_path.to_string_lossy().to_string(),
        tool_version: env!("CARGO_PKG_VERSION").to_string(),
        schema: contract.schema,
        resolver: contract.resolver,
        last_sync: contract.last_sync,
        branch: contract.branch,
        pending: contract.pending,
        ignored: contract.ignored,
        skipped,
        parse_errors,
        warnings,
        coverage,
        verdict: contract.verdict,
    })
}

/// Per-language coverage, attributing call sites and edges to the language of
/// the calling file.
fn language_coverage(conn: &Connection) -> Result<BTreeMap<String, LanguageCoverage>> {
    let mut coverage: BTreeMap<String, LanguageCoverage> = BTreeMap::new();
    let mut fill = |sql: &str, apply: fn(&mut LanguageCoverage, i64, i64)| -> Result<()> {
        let mut stmt = conn.prepare(sql)?;
        let rows = stmt.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, i64>(1)?,
                row.get::<_, Option<i64>>(2)?.unwrap_or(0),
            ))
        })?;
        for row in rows {
            let (language, first, second) = row?;
            apply(coverage.entry(language).or_default(), first, second);
        }
        Ok(())
    };
    fill(
        "SELECT language, COUNT(*), 0 FROM files GROUP BY language",
        |entry, files, _| entry.files = files,
    )?;
    fill(
        "SELECT f.language, COUNT(*), 0 FROM symbols s
         JOIN files f ON f.path = s.file_path GROUP BY f.language",
        |entry, symbols, _| entry.symbols = symbols,
    )?;
    fill(
        "SELECT f.language, COUNT(*), 0 FROM edges e
         JOIN symbols s ON s.id = e.source
         JOIN symbols t ON t.id = e.target
         JOIN files f ON f.path = s.file_path
         WHERE e.kind = 'calls' AND s.file_path <> t.file_path GROUP BY f.language",
        |entry, edges, _| entry.cross_file_edges = edges,
    )?;
    let resolved = crate::store::resolved_call_site_ids(conn)?;
    let mut stmt = conn.prepare(
        "SELECT p.id, f.language FROM pending_calls p
         JOIN files f ON f.path = p.file_path",
    )?;
    let rows = stmt.query_map([], |row| {
        Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
    })?;
    for row in rows {
        let (id, language) = row?;
        let entry = coverage.entry(language).or_default();
        entry.call_sites += 1;
        if resolved.contains(&id) {
            entry.resolved_call_sites += 1;
        } else {
            entry.unresolved_call_sites += 1;
        }
    }
    Ok(coverage)
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
    let synced_head = meta::read(conn, "synced_head")?;
    let current_head = crate::store::current_git_head(repo_root);
    let head_moved = matches!(
        (&synced_head, &current_head),
        (Some(synced), Some(current)) if synced != current
    );
    Ok(BranchStatus {
        synced,
        current,
        drifted,
        head_moved,
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
        coverage: BTreeMap::new(),
        verdict: "NEEDS-MIGRATION",
    }
}

fn resolve_path(path: &Path) -> PathBuf {
    path.canonicalize().unwrap_or_else(|_| path.to_path_buf())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::{init_schema, open_db, sync_repo};

    #[test]
    fn coverage_distinguishes_same_line_call_identities() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let db = root.join("graphtrail.db");
        std::fs::write(root.join("util.py"), "def helper(): pass\n").unwrap();
        std::fs::write(
            root.join("app.py"),
            "from util import helper as alias\nimport util as mod\ndef run():\n    alias(); missing_name()\n    alias(missing_name())\n    mod.helper(); unknown.helper()\n    alias(); alias()\n",
        )
        .unwrap();
        let conn = open_db(&db).unwrap();
        init_schema(&conn).unwrap();
        sync_repo(&conn, root).unwrap();
        let report = doctor(&conn, root, &db).unwrap();
        let python = &report.coverage["python"];
        assert_eq!(python.call_sites, 8);
        assert_eq!(python.resolved_call_sites, 5);
        assert_eq!(python.unresolved_call_sites, 3);
        // Identical targets on a line share an edge, but remain distinct sites.
        assert_eq!(python.cross_file_edges, 4);
    }
}
