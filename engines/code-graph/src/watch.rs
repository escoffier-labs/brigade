//! Foreground file watcher: debounced incremental syncs on source changes.
//!
//! Borrowed stance from CocoIndex's live update mode, scaled to the sidecar
//! rules: this is an explicit foreground command you start and Ctrl-C, not a
//! daemon anything installs. Sync stays incremental, and the advisory sync
//! lock means a watcher and a timer or MCP refresh cannot duplicate work.

use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::time::Duration;

use anyhow::Result;
use notify::{RecursiveMode, Watcher};

use crate::extractors::language_for;
use crate::store::{db_path, init_schema, open_db, sync_repo};

/// True when a filesystem event path could change what sync would index.
///
/// Directory events pass (a rename can move many source files at once);
/// everything under `.git/` and `.graphtrail/` is noise, including the sync
/// lock and the database itself, which would otherwise loop the watcher.
pub fn is_relevant_change(root: &Path, path: &Path) -> bool {
    let rel = path.strip_prefix(root).unwrap_or(path);
    for component in rel.components() {
        let name = component.as_os_str().to_string_lossy();
        if name == ".git" || name == ".graphtrail" {
            return false;
        }
    }
    // Extension says source file; no extension usually says directory.
    match rel.extension() {
        Some(_) => language_for(rel).is_some(),
        None => true,
    }
}

/// Watch `root` and run an incremental sync after `debounce` of quiet.
/// Runs until interrupted. One sync always runs up front so the graph is
/// current before the first event.
#[allow(clippy::print_stdout)]
pub fn watch(explicit_db: Option<PathBuf>, root: &Path, debounce: Duration) -> Result<()> {
    let root = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());
    crate::store::guard_unsafe_root(&root)?;
    let db = db_path(explicit_db, &root);
    let conn = open_db(&db)?;
    init_schema(&conn)?;

    let summary = sync_repo(&conn, &root)?;
    println!(
        "watching {} (db {}): files={} symbols={} edges={}",
        root.display(),
        db.display(),
        summary.files,
        summary.symbols,
        summary.calls
    );

    let (tx, rx) = mpsc::channel::<notify::Result<notify::Event>>();
    let mut watcher = notify::recommended_watcher(tx)?;
    watcher.watch(&root, RecursiveMode::Recursive)?;

    loop {
        // Block until something happens at all.
        let first = match rx.recv() {
            Ok(event) => event,
            Err(_) => return Ok(()), // watcher dropped
        };
        let mut relevant = event_is_relevant(&root, first);
        // Then absorb the burst: keep draining until `debounce` passes quietly.
        loop {
            match rx.recv_timeout(debounce) {
                Ok(event) => relevant = event_is_relevant(&root, event) || relevant,
                Err(mpsc::RecvTimeoutError::Timeout) => break,
                Err(mpsc::RecvTimeoutError::Disconnected) => return Ok(()),
            }
        }
        if !relevant {
            continue;
        }
        match sync_repo(&conn, &root) {
            Ok(summary) if summary.unchanged => {}
            Ok(summary) => println!(
                "synced: files={} symbols={} edges={} deleted={}",
                summary.files, summary.symbols, summary.calls, summary.deleted
            ),
            // Fail-open: a transient error (mid-save file, lock contention)
            // must not kill the watcher.
            Err(err) => eprintln!("sync failed (will retry on next change): {err}"),
        }
    }
}

fn event_is_relevant(root: &Path, event: notify::Result<notify::Event>) -> bool {
    match event {
        Ok(event) => event
            .paths
            .iter()
            .any(|path| is_relevant_change(root, path)),
        Err(_) => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_files_are_relevant_and_internal_dirs_are_not() {
        let root = Path::new("/repo");
        assert!(is_relevant_change(root, Path::new("/repo/src/app.py")));
        assert!(is_relevant_change(root, Path::new("/repo/src/newdir")));
        assert!(!is_relevant_change(root, Path::new("/repo/notes.md")));
        assert!(!is_relevant_change(
            root,
            Path::new("/repo/.graphtrail/graphtrail.db")
        ));
        assert!(!is_relevant_change(
            root,
            Path::new("/repo/.graphtrail/graphtrail.db.lock")
        ));
        assert!(!is_relevant_change(root, Path::new("/repo/.git/index")));
    }
}
