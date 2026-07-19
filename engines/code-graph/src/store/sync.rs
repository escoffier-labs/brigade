//! Repository sync orchestration.
//!
//! Sync is incremental: walking, freshness checks, persistence, repository
//! policy, and edge resolution live in focused sibling modules. This façade
//! preserves the public sync surface and the transaction order.

use std::collections::HashSet;
use std::path::Path;

use anyhow::Result;
use rusqlite::Connection;

use crate::extractors::index_file;
use crate::model::{IgnoredSummary, PendingChanges};
use crate::store::db::now_ts;
use crate::store::persist::{
    EntryFreshness, entry_freshness, load_db_files, purge_file_graph, stale_plan, table_counts,
    write_file_graph,
};
use crate::store::repo_policy::{
    ensure_graphtrail_ignored, guard_unsafe_root, has_git_marker, write_branch_meta,
};
use crate::store::resolve::rebuild_edges;
use crate::store::schema::SchemaUpgrade;
use crate::store::walk::{Entry, collect_sync_walk};

#[derive(Debug, Default)]
pub struct SyncSummary {
    pub files: usize,
    pub symbols: usize,
    pub calls: usize,
    pub imports: usize,
    /// True when nothing changed and the sync was a no-op (no parsing, meta write only).
    pub unchanged: bool,
    /// Files removed from the index because they no longer exist on disk.
    pub deleted: usize,
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
    let graph_dir_created = crate::store::db::take_created_graph_dir(&root.join(".graphtrail"));
    let upgrade = crate::store::schema::upgrade_for_sync(conn)?;
    let force_full_reindex = force || upgrade == SchemaUpgrade::FullReindex;

    let entries = collect_sync_walk(&root, false)?.entries;
    let db_files = load_db_files(conn)?;
    if graph_dir_created && db_files.is_empty() && has_git_marker(&root) {
        ensure_graphtrail_ignored(&root)?;
    }

    let on_disk: HashSet<&str> = entries.iter().map(|entry| entry.rel.as_str()).collect();
    let deleted: Vec<String> = db_files
        .keys()
        .filter(|path| !on_disk.contains(path.as_str()))
        .cloned()
        .collect();

    let files_to_index: Vec<&Entry> = if force_full_reindex {
        entries.iter().collect()
    } else {
        stale_plan(&entries, &db_files)?.entries
    };

    let changed =
        !deleted.is_empty() || !files_to_index.is_empty() || upgrade == SchemaUpgrade::RebuildEdges;
    if !changed {
        let tx = conn.unchecked_transaction()?;
        crate::store::meta::write_sync_meta(&tx)?;
        write_branch_meta(&tx, &root)?;
        tx.commit()?;

        let counts = table_counts(conn)?;
        return Ok(SyncSummary {
            files: counts.0,
            symbols: counts.1,
            calls: counts.2,
            imports: counts.3,
            unchanged: true,
            deleted: 0,
        });
    }

    let tx = conn.unchecked_transaction()?;

    let mut purge: Vec<&str> = files_to_index
        .iter()
        .map(|entry| entry.rel.as_str())
        .collect();
    purge.extend(deleted.iter().map(String::as_str));
    for path in &purge {
        purge_file_graph(&tx, path)?;
    }

    let now = now_ts();
    for entry in files_to_index {
        let graph = index_file(&root, &entry.path, entry.lang)?;
        write_file_graph(&tx, &graph, now)?;
    }

    rebuild_edges(&tx)?;
    crate::store::meta::write_sync_meta(&tx)?;
    write_branch_meta(&tx, &root)?;
    tx.commit()?;

    let counts = table_counts(conn)?;
    Ok(SyncSummary {
        files: counts.0,
        symbols: counts.1,
        calls: counts.2,
        imports: counts.3,
        unchanged: false,
        deleted: deleted.len(),
    })
}

pub fn pending_changes(conn: &Connection, root: &Path) -> Result<(PendingChanges, IgnoredSummary)> {
    let root = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());
    let walk = collect_sync_walk(&root, true)?;
    let db_files = load_db_files(conn)?;
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
        match entry_freshness(entry, &db_files)? {
            EntryFreshness::New => pending.new_files += 1,
            EntryFreshness::Changed => pending.changed_files += 1,
            EntryFreshness::FingerprintStale => pending.fingerprint_stale += 1,
            EntryFreshness::Fresh => {}
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
