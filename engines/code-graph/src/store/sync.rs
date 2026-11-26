//! Repository sync orchestration.
//!
//! Sync is incremental: walking, freshness checks, persistence, repository
//! policy, and edge resolution live in focused sibling modules. This façade
//! preserves the public sync surface and the transaction order.

use std::collections::{BTreeMap, HashSet};
use std::path::Path;

use anyhow::Result;
use rusqlite::Connection;
use serde::Serialize;

use crate::extractors::index_file;
use crate::model::{IgnoredSummary, PendingChanges};
use crate::store::db::now_ts;
use crate::store::persist::{
    EntryFreshness, entry_freshness, load_db_files, purge_file_graph, stale_plan, table_counts,
    write_file_graph,
};
use crate::store::repo_policy::{
    ensure_index_ignored, guard_unsafe_root, has_git_marker, write_branch_meta,
};
use crate::store::resolve::rebuild_edges;
use crate::store::schema::{
    GraphRevision, SCHEMA_VERSION, SchemaUpgrade, advance_graph_revision,
    clear_parse_census_pending, graph_revision, schema_cookie, stored_schema_version,
    upgrade_for_sync,
};
use crate::store::skipped::{clear_skipped, load_skipped, record_skipped, skipped_reason_counts};
use crate::store::walk::{Entry, collect_sync_walk};

#[derive(Debug, Default, Serialize)]
pub struct SyncSummary {
    pub files: usize,
    pub symbols: usize,
    pub calls: usize,
    pub imports: usize,
    /// Legacy summary flag: no indexed files changed and edges did not need a rebuild.
    pub unchanged: bool,
    /// Files removed from the index because they no longer exist on disk.
    pub deleted: usize,
    /// Files currently recorded as skipped (unreadable or unparseable),
    /// including ones skipped by earlier syncs and left unchanged since.
    pub skipped: usize,
    /// `skipped` broken down by reason (`unreadable_utf8`, `io_error`, `parse_error`, `minified`).
    pub skipped_by_reason: BTreeMap<String, usize>,
    #[serde(flatten)]
    pub revision: GraphRevision,
}

/// Incremental sync (skips work when nothing changed).
pub fn sync_repo(conn: &Connection, root: &Path) -> Result<SyncSummary> {
    sync_repo_force(conn, root, false)
}

/// Like [`sync_repo`] but `force` rebuilds every file regardless of the stat/hash check.
pub fn sync_repo_force(conn: &Connection, root: &Path, force: bool) -> Result<SyncSummary> {
    let root = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());
    guard_unsafe_root(&root)?;
    let _lock = acquire_sync_lock(conn)?;
    // The first sync that created an index directory adds it to `.gitignore`; check both layouts
    // so neither creation record lingers.
    let created_index_dir = {
        let canonical = crate::store::db::index_dir(&root);
        let legacy = crate::store::db::legacy_index_dir(&root);
        let legacy_created = crate::store::db::take_created_graph_dir(&legacy);
        if crate::store::db::take_created_graph_dir(&canonical) {
            Some(canonical)
        } else {
            legacy_created.then_some(legacy)
        }
    };
    let tx = conn.unchecked_transaction()?;
    let previous_schema = schema_cookie(&tx)?;
    let previous_changes = tx.total_changes();
    let stored_schema = stored_schema_version(&tx)?;
    anyhow::ensure!(
        stored_schema.is_none_or(|version| version <= SCHEMA_VERSION),
        "database schema is newer than this engine"
    );
    let previous_engine = stored_schema.is_some_and(|version| version < SCHEMA_VERSION);
    let upgrade = upgrade_for_sync(&tx)?;
    let force_full_reindex = force || upgrade == SchemaUpgrade::FullReindex;

    let entries = collect_sync_walk(&root, false)?.entries;
    let db_files = load_db_files(&tx)?;
    let skipped = load_skipped(&tx)?;
    if let Some(dir) = created_index_dir.filter(|_| db_files.is_empty() && has_git_marker(&root)) {
        ensure_index_ignored(&root, &dir)?;
    }

    let on_disk: HashSet<&str> = entries.iter().map(|entry| entry.rel.as_str()).collect();
    let deleted: Vec<String> = db_files
        .keys()
        .filter(|path| !on_disk.contains(path.as_str()))
        .cloned()
        .collect();
    let skipped_gone: Vec<&str> = skipped
        .keys()
        .filter(|path| !on_disk.contains(path.as_str()))
        .map(String::as_str)
        .collect();

    let files_to_index: Vec<&Entry> = if force_full_reindex {
        entries.iter().collect()
    } else {
        stale_plan(&entries, &db_files, &skipped)?.entries
    };

    let changed =
        !deleted.is_empty() || !files_to_index.is_empty() || upgrade == SchemaUpgrade::RebuildEdges;
    if changed {
        let mut purge: Vec<&str> = files_to_index
            .iter()
            .map(|entry| entry.rel.as_str())
            .collect();
        purge.extend(deleted.iter().map(String::as_str));
        for path in purge {
            purge_file_graph(&tx, path)?;
        }

        // Parse and IO failures are recorded (old rows were purged above).
        // Database errors abort the transaction.
        let now = now_ts();
        for entry in files_to_index {
            match index_file(&root, &entry.path, entry.lang) {
                Ok(graph) => {
                    write_file_graph(&tx, &graph, now)?;
                    clear_skipped(&tx, &entry.rel)?;
                }
                Err(failure) => record_skipped(&tx, entry, &failure, now)?,
            }
        }
        rebuild_edges(&tx)?;
    }
    for path in skipped_gone {
        clear_skipped(&tx, path)?;
    }

    // Count every graph DML operation and DDL change, including migrations
    // and skipped-only pruning, before writing sync provenance.
    if tx.total_changes() != previous_changes
        || schema_cookie(&tx)? != previous_schema
        || previous_engine
    {
        advance_graph_revision(&tx, force || previous_engine)?;
    }
    if force_full_reindex {
        clear_parse_census_pending(&tx)?;
    }
    crate::store::meta::write_sync_meta(&tx)?;
    write_branch_meta(&tx, &root)?;
    let summary = summarize(&tx, !changed, deleted.len())?;
    tx.commit()?;
    Ok(summary)
}

fn summarize(conn: &Connection, unchanged: bool, deleted: usize) -> Result<SyncSummary> {
    let counts = table_counts(conn)?;
    let skipped_by_reason = skipped_reason_counts(conn)?;
    Ok(SyncSummary {
        files: counts.0,
        symbols: counts.1,
        calls: counts.2,
        imports: counts.3,
        unchanged,
        deleted,
        skipped: skipped_by_reason.values().sum(),
        skipped_by_reason,
        revision: graph_revision(conn)?,
    })
}

pub fn pending_changes(conn: &Connection, root: &Path) -> Result<(PendingChanges, IgnoredSummary)> {
    let root = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());
    let walk = collect_sync_walk(&root, true)?;
    let db_files = load_db_files(conn)?;
    let skipped = load_skipped(conn)?;
    let on_disk: HashSet<&str> = walk
        .entries
        .iter()
        .map(|entry| entry.rel.as_str())
        .collect();
    let mut pending = PendingChanges {
        deleted_files: db_files
            .keys()
            .filter(|path| !on_disk.contains(path.as_str()))
            .count(),
        ..PendingChanges::default()
    };
    for entry in &walk.entries {
        match entry_freshness(entry, &db_files, &skipped)? {
            EntryFreshness::New => pending.new_files += 1,
            EntryFreshness::Changed => pending.changed_files += 1,
            EntryFreshness::FingerprintStale => pending.fingerprint_stale += 1,
            EntryFreshness::Fresh | EntryFreshness::Skipped => {}
        }
    }
    Ok((pending, walk.ignored))
}

/// Take the advisory sync lock for the database behind `conn`, when it has an
/// on-disk path (in-memory databases need no cross-process exclusion).
fn acquire_sync_lock(conn: &Connection) -> Result<Option<crate::store::lock::SyncLock>> {
    match conn.path() {
        Some(path) if !path.is_empty() => Ok(Some(crate::store::lock::SyncLock::acquire(
            Path::new(path),
        )?)),
        _ => Ok(None),
    }
}
