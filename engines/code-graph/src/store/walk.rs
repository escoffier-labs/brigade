//! Repository walking and ignore accounting for sync.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};
use std::time::UNIX_EPOCH;

use anyhow::Result;
use ignore::{DirEntry, WalkBuilder};

use crate::extractors::language_for;
use crate::model::{IgnoredSummary, Lang};
use crate::store::repo_policy::has_git_context;

pub(super) struct Entry {
    pub(super) path: PathBuf,
    pub(super) rel: String,
    pub(super) lang: Lang,
    pub(super) size: u64,
    pub(super) mtime: i64,
}

pub(super) struct SyncWalk {
    pub(super) entries: Vec<Entry>,
    pub(super) ignored: IgnoredSummary,
}

pub(super) fn collect_sync_walk(root: &Path, count_ignored: bool) -> Result<SyncWalk> {
    let ignored = IgnoredSummary {
        hardcoded_floor: 0,
        gitignore: if count_ignored {
            count_gitignored_entries(root)?
        } else {
            0
        },
    };
    let hardcoded_floor = Arc::new(AtomicUsize::new(0));
    let entries = collect_supported_entries(
        root,
        has_git_context(root),
        count_ignored.then_some(hardcoded_floor.clone()),
    )?;
    Ok(SyncWalk {
        entries,
        ignored: IgnoredSummary {
            hardcoded_floor: hardcoded_floor.load(Ordering::Relaxed),
            ..ignored
        },
    })
}

fn collect_supported_entries(
    root: &Path,
    use_gitignore: bool,
    hardcoded_counter: Option<Arc<AtomicUsize>>,
) -> Result<Vec<Entry>> {
    let mut entries: Vec<Entry> = Vec::new();
    let mut walker = WalkBuilder::new(root);
    walker
        .hidden(false)
        .git_ignore(use_gitignore)
        .git_global(false)
        .git_exclude(use_gitignore)
        .ignore(false)
        .parents(true)
        .filter_entry(move |entry| keep_entry_counted(entry, hardcoded_counter.as_ref()));
    for entry in walker.build() {
        let entry = entry?;
        if !entry
            .file_type()
            .is_some_and(|file_type| file_type.is_file())
        {
            continue;
        }
        let Some(lang) = language_for(entry.path()) else {
            continue;
        };
        let metadata = entry.metadata()?;
        let mtime = metadata
            .modified()
            .ok()
            .and_then(|modified| modified.duration_since(UNIX_EPOCH).ok())
            .map_or(0, |duration| duration.as_secs() as i64);
        entries.push(Entry {
            path: entry.path().to_path_buf(),
            rel: rel_path(root, entry.path()),
            lang,
            size: metadata.len(),
            mtime,
        });
    }
    Ok(entries)
}

fn count_gitignored_entries(root: &Path) -> Result<usize> {
    if !has_git_context(root) {
        return Ok(0);
    }
    let without_gitignore = collect_walk_paths(root, false)?;
    let with_gitignore = collect_walk_paths(root, true)?;
    Ok(without_gitignore.difference(&with_gitignore).count())
}

fn collect_walk_paths(root: &Path, use_gitignore: bool) -> Result<HashSet<String>> {
    let mut paths = HashSet::new();
    let mut walker = WalkBuilder::new(root);
    walker
        .hidden(false)
        .git_ignore(use_gitignore)
        .git_global(false)
        .git_exclude(use_gitignore)
        .ignore(false)
        .parents(true)
        .filter_entry(|entry| keep_entry_counted(entry, None));
    for entry in walker.build() {
        let entry = entry?;
        if entry.depth() == 0 {
            continue;
        }
        paths.insert(rel_path(root, entry.path()));
    }
    Ok(paths)
}

fn rel_path(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/")
}

fn keep_entry(entry: &DirEntry) -> bool {
    !is_hardcoded_floor(entry)
}

fn keep_entry_counted(entry: &DirEntry, hardcoded_counter: Option<&Arc<AtomicUsize>>) -> bool {
    let keep = keep_entry(entry);
    if let Some(counter) = hardcoded_counter.filter(|_| !keep) {
        counter.fetch_add(1, Ordering::Relaxed);
    }
    keep
}

fn is_hardcoded_floor(entry: &DirEntry) -> bool {
    let name = entry.file_name().to_string_lossy();
    matches!(
        name.as_ref(),
        ".git"
            | ".graphtrail"
            | "node_modules"
            | "target"
            | "dist"
            | "build"
            | ".next"
            | ".turbo"
            | ".venv"
            | "venv"
            | "__pycache__"
    )
}
