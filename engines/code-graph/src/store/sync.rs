//! Repository sync: walk files, extract graphs, and write them transactionally.
//!
//! Sync is incremental: a stat pass (size + mtime, confirmed by content hash when those differ)
//! decides which files actually changed. Only those files are re-parsed, one at a time, so peak
//! memory stays at a single file's graph regardless of repository size. Unresolved calls are
//! persisted in `pending_calls`, and edges are derived state: after any change they are rebuilt
//! from every stored pending call, so a new definition in one file updates call resolutions in
//! files that did not change. If nothing changed, sync skips all parsing and only refreshes sync
//! metadata. A `<db>.lock` file (stale locks from dead processes are reclaimed) keeps concurrent
//! syncs from duplicating the work.

use std::collections::{HashMap, HashSet};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};
use std::time::UNIX_EPOCH;

use anyhow::{Context, Result};
use ignore::{DirEntry, WalkBuilder, gitignore::GitignoreBuilder};
use rusqlite::{Connection, params};

use crate::extractors::common::hex_hash;
use crate::extractors::{extractor_fingerprint_for, index_file, language_for};
use crate::model::{CallKind, IgnoredSummary, Import, Lang, PendingCall, PendingChanges};
use crate::store::db::now_ts;
use crate::store::schema::SchemaUpgrade;

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

pub(crate) struct Entry {
    path: PathBuf,
    rel: String,
    lang: Lang,
    size: u64,
    mtime: i64,
}

struct DbFile {
    content_hash: String,
    size: u64,
    mtime: i64,
    extractor_fingerprint: Option<String>,
}

struct StalePlan<'a> {
    entries: Vec<&'a Entry>,
}

pub(crate) struct SyncWalk {
    entries: Vec<Entry>,
    ignored: IgnoredSummary,
}

#[derive(Clone)]
struct SymbolCandidate {
    id: String,
    file_path: String,
    container: Option<String>,
}

enum ImportResolution {
    NoImport,
    Resolved(Vec<SymbolCandidate>),
    Unresolved,
    Fallback,
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

    // Stat pass: enumerate supported files without parsing them.
    let entries = collect_sync_walk(&root, false)?.entries;

    let db_files = load_db_files(conn)?;
    if graph_dir_created && db_files.is_empty() && has_git_marker(&root) {
        ensure_graphtrail_ignored(&root)?;
    }

    let on_disk: HashSet<&str> = entries.iter().map(|e| e.rel.as_str()).collect();
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

    // Purge rows for files about to be re-indexed and files deleted from disk.
    // Edges are not purged per file: they are derived state, rebuilt below.
    let mut purge: Vec<&str> = files_to_index
        .iter()
        .map(|entry| entry.rel.as_str())
        .collect();
    purge.extend(deleted.iter().map(String::as_str));
    for path in &purge {
        tx.execute(
            "DELETE FROM symbols_fts WHERE file_path = ?1",
            params![path],
        )?;
        tx.execute("DELETE FROM symbols WHERE file_path = ?1", params![path])?;
        tx.execute("DELETE FROM imports WHERE file_path = ?1", params![path])?;
        tx.execute(
            "DELETE FROM pending_calls WHERE file_path = ?1",
            params![path],
        )?;
        tx.execute("DELETE FROM files WHERE path = ?1", params![path])?;
    }

    // Extract and write one file at a time so peak memory stays at a single
    // file's graph no matter how many files changed.
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

fn collect_sync_walk(root: &Path, count_ignored: bool) -> Result<SyncWalk> {
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
            .and_then(|m| m.duration_since(UNIX_EPOCH).ok())
            .map_or(0, |d| d.as_secs() as i64);
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

fn stale_plan<'a>(
    entries: &'a [Entry],
    db_files: &HashMap<String, DbFile>,
) -> Result<StalePlan<'a>> {
    let mut stale = Vec::new();
    for entry in entries {
        match entry_freshness(entry, db_files)? {
            EntryFreshness::New | EntryFreshness::Changed | EntryFreshness::FingerprintStale => {
                stale.push(entry);
            }
            EntryFreshness::Fresh => {}
        }
    }
    Ok(StalePlan { entries: stale })
}

/// Insert one extracted file's rows: the file record, its symbols (plus FTS),
/// imports, and pending calls awaiting cross-file resolution.
fn write_file_graph(tx: &Connection, graph: &crate::model::FileGraph, now: i64) -> Result<()> {
    let lang = language_for(Path::new(&graph.path)).expect("indexed graph has a known language");
    tx.execute(
        "INSERT OR REPLACE INTO files(path, content_hash, size, modified_at, indexed_at, language, extractor_fingerprint)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![
            graph.path,
            graph.hash,
            graph.size as i64,
            graph.modified_at,
            now,
            graph.language,
            extractor_fingerprint_for(lang)
        ],
    )?;
    for symbol in &graph.symbols {
        tx.execute(
            "INSERT INTO symbols(id, kind, name, qualified_name, file_path, start_line, end_line, signature, container, content_hash, body_hash)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
            params![
                symbol.id,
                symbol.kind,
                symbol.name,
                symbol.qualified_name,
                symbol.file_path,
                symbol.start_line as i64,
                symbol.end_line as i64,
                symbol.signature,
                symbol.container,
                symbol.content_hash,
                symbol.body_hash,
            ],
        )?;
        tx.execute(
            "INSERT INTO symbols_fts(symbol_id, name, qualified_name, signature, file_path)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![
                symbol.id,
                symbol.name,
                symbol.qualified_name,
                symbol.signature,
                symbol.file_path
            ],
        )?;
    }
    for import in &graph.imports {
        tx.execute(
            "INSERT INTO imports(file_path, module, local_name, imported_name, alias, line)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                graph.path,
                import.module,
                import.local_name,
                import.imported_name,
                import.alias,
                import.line as i64
            ],
        )?;
    }
    for call in &graph.calls {
        tx.execute(
            "INSERT INTO pending_calls(source_id, file_path, target_name, kind, qualifier, line)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                call.source_id,
                call.source_file,
                call.target_name,
                call.kind.as_str(),
                call.qualifier,
                call.line as i64
            ],
        )?;
    }
    Ok(())
}

/// Derive the `edges` table from every stored pending call.
///
/// Rebuilding from scratch keeps resolution a pure function of the current
/// symbols, imports, and pending calls: a definition added in one file gains
/// edges from callers in unchanged files, and resolutions that a change made
/// stale (a fallback superseded by a strict match, a deleted target) disappear
/// instead of lingering.
fn rebuild_edges(tx: &Connection) -> Result<()> {
    tx.execute("DELETE FROM edges", [])?;
    let name_index = load_name_index(tx)?;
    let import_index = load_import_index(tx)?;
    let source_index = load_symbol_id_index(tx)?;
    let file_index = load_file_index(tx)?;

    let mut select = tx.prepare(
        "SELECT source_id, file_path, target_name, kind, qualifier, line FROM pending_calls",
    )?;
    let mut insert = tx.prepare(
        "INSERT OR IGNORE INTO edges(source, target, kind, line, confidence)
         VALUES (?1, ?2, 'calls', ?3, ?4)",
    )?;
    let rows = select.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, Option<String>>(4)?,
            row.get::<_, i64>(5)?,
        ))
    })?;
    for row in rows {
        let (source_id, source_file, target_name, kind, qualifier, line) = row?;
        let Some(kind) = CallKind::parse(&kind) else {
            continue;
        };
        let call = PendingCall {
            source_id,
            target_name,
            qualifier,
            kind,
            line: line.max(0) as usize,
            source_file,
        };
        for target in resolve_call(
            &call,
            &name_index,
            &import_index,
            &source_index,
            &file_index,
        ) {
            if target.candidate.id == call.source_id {
                continue;
            }
            insert.execute(params![
                call.source_id,
                target.candidate.id,
                call.line as i64,
                target.confidence
            ])?;
        }
    }
    Ok(())
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

enum EntryFreshness {
    Fresh,
    New,
    Changed,
    FingerprintStale,
}

fn entry_freshness(entry: &Entry, db_files: &HashMap<String, DbFile>) -> Result<EntryFreshness> {
    let Some(db_file) = db_files.get(&entry.rel) else {
        return Ok(EntryFreshness::New);
    };
    if db_file.size != entry.size || db_file.mtime != entry.mtime {
        // Cheap stat differs; confirm with content hash to ignore mtime-only touches.
        let content = fs::read_to_string(&entry.path)?;
        if hex_hash(content.as_bytes()) != db_file.content_hash {
            return Ok(EntryFreshness::Changed);
        }
    }
    if db_file.extractor_fingerprint.as_deref() != Some(extractor_fingerprint_for(entry.lang)) {
        return Ok(EntryFreshness::FingerprintStale);
    }
    Ok(EntryFreshness::Fresh)
}

fn keep_entry(entry: &DirEntry) -> bool {
    !is_hardcoded_floor(entry)
}

fn keep_entry_counted(entry: &DirEntry, hardcoded_counter: Option<&Arc<AtomicUsize>>) -> bool {
    let keep = keep_entry(entry);
    if !keep && let Some(counter) = hardcoded_counter {
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

/// Refuse to sync roots that are never a real project: the filesystem root and the user's home
/// directory. Outside a git repo the walker has no gitignore to lean on, so a sync there parses
/// every cache, toolchain, and vendored source tree on the machine into one giant graph nobody
/// asked for. Set `GRAPHTRAIL_ALLOW_UNSAFE_ROOT=1` to bypass.
pub(crate) fn guard_unsafe_root(root: &Path) -> Result<()> {
    if std::env::var_os("GRAPHTRAIL_ALLOW_UNSAFE_ROOT").is_some_and(|v| v == "1") {
        return Ok(());
    }
    let home = std::env::var_os("HOME").map(|h| {
        let home = PathBuf::from(h);
        home.canonicalize().unwrap_or(home)
    });
    if let Some(reason) = unsafe_root_reason(root, home.as_deref()) {
        anyhow::bail!(
            "refusing to sync {}: {reason}. Run sync from a project directory, or set \
             GRAPHTRAIL_ALLOW_UNSAFE_ROOT=1 to override.",
            root.display()
        );
    }
    Ok(())
}

fn unsafe_root_reason(root: &Path, home: Option<&Path>) -> Option<&'static str> {
    if root.parent().is_none() {
        return Some("root is the filesystem root");
    }
    if home.is_some_and(|home| root == home) {
        return Some("root is the home directory");
    }
    None
}

fn has_git_context(root: &Path) -> bool {
    root.ancestors().any(has_git_marker)
}

fn has_git_marker(dir: &Path) -> bool {
    let git = dir.join(".git");
    git.is_file() || git.join("HEAD").is_file()
}

/// Record which branch the graph describes, so `doctor` can flag a checkout
/// of a different branch as drift. Removed when the root has no git context,
/// so a repo that stops being one does not pin a stale branch forever.
fn write_branch_meta(tx: &Connection, root: &Path) -> Result<()> {
    match current_git_branch(root) {
        Some(branch) => crate::store::meta::upsert(tx, "synced_branch", &branch)?,
        None => {
            tx.execute("DELETE FROM meta WHERE key = 'synced_branch'", [])?;
        }
    }
    Ok(())
}

/// Current branch name from `.git/HEAD`, without spawning git. Follows the
/// `gitdir:` pointer of linked worktrees. Detached heads report the short
/// commit as `detached@<12 hex>`.
pub(crate) fn current_git_branch(root: &Path) -> Option<String> {
    let git_dir = root.ancestors().find_map(|dir| {
        let git = dir.join(".git");
        if git.join("HEAD").is_file() {
            return Some(git);
        }
        if git.is_file() {
            // Linked worktree: `.git` is a file containing `gitdir: <path>`.
            let content = fs::read_to_string(&git).ok()?;
            let pointed = content.strip_prefix("gitdir:")?.trim();
            let pointed = if Path::new(pointed).is_absolute() {
                PathBuf::from(pointed)
            } else {
                dir.join(pointed)
            };
            if pointed.join("HEAD").is_file() {
                return Some(pointed);
            }
        }
        None
    })?;
    let head = fs::read_to_string(git_dir.join("HEAD")).ok()?;
    let head = head.trim();
    if let Some(reference) = head.strip_prefix("ref:") {
        let reference = reference.trim();
        let branch = reference.strip_prefix("refs/heads/").unwrap_or(reference);
        return Some(branch.to_string());
    }
    // Detached HEAD: the file holds a commit hash.
    let short: String = head.chars().take(12).collect();
    if short.chars().all(|c| c.is_ascii_hexdigit()) && !short.is_empty() {
        Some(format!("detached@{short}"))
    } else {
        None
    }
}

fn ensure_graphtrail_ignored(root: &Path) -> Result<()> {
    let gitignore = root.join(".gitignore");
    if gitignore_covers_graphtrail(root, &gitignore)? {
        return Ok(());
    }

    let mut needs_leading_newline = false;
    if gitignore.exists() {
        let content = fs::read_to_string(&gitignore)
            .with_context(|| format!("failed to read {}", gitignore.display()))?;
        needs_leading_newline = !content.is_empty() && !content.ends_with('\n');
    }

    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&gitignore)
        .with_context(|| format!("failed to open {}", gitignore.display()))?;
    if needs_leading_newline {
        writeln!(file)?;
    }
    writeln!(file, ".graphtrail/")?;
    println!("updated {} to ignore .graphtrail/", gitignore.display());
    Ok(())
}

fn gitignore_covers_graphtrail(root: &Path, gitignore: &Path) -> Result<bool> {
    if !gitignore.exists() {
        return Ok(false);
    }

    let mut builder = GitignoreBuilder::new(root);
    if let Some(err) = builder.add(gitignore) {
        return Err(err).with_context(|| format!("failed to parse {}", gitignore.display()));
    }
    let matcher = builder
        .build()
        .with_context(|| format!("failed to parse {}", gitignore.display()))?;
    Ok(matcher.matched(root.join(".graphtrail"), true).is_ignore())
}

/// Map file path -> freshness metadata from the `files` table.
fn load_db_files(conn: &Connection) -> Result<HashMap<String, DbFile>> {
    let has_fingerprint =
        crate::store::schema::table_has_column(conn, "files", "extractor_fingerprint")?;
    let sql = if has_fingerprint {
        "SELECT path, content_hash, size, modified_at, extractor_fingerprint FROM files"
    } else {
        "SELECT path, content_hash, size, modified_at, NULL AS extractor_fingerprint FROM files"
    };
    let mut stmt = conn.prepare(sql)?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            DbFile {
                content_hash: row.get::<_, String>(1)?,
                size: row.get::<_, i64>(2)? as u64,
                mtime: row.get::<_, i64>(3)?,
                extractor_fingerprint: row.get::<_, Option<String>>(4)?,
            },
        ))
    })?;
    let mut map = HashMap::new();
    for row in rows {
        let (path, db_file) = row?;
        map.insert(path, db_file);
    }
    Ok(map)
}

/// (files, symbols, edges, imports) row counts, used for the no-op summary.
fn table_counts(conn: &Connection) -> Result<(usize, usize, usize, usize)> {
    let count = |sql: &str| -> Result<usize> {
        Ok(conn.query_row(sql, [], |row| row.get::<_, i64>(0))? as usize)
    };
    Ok((
        count("SELECT COUNT(*) FROM files")?,
        count("SELECT COUNT(*) FROM symbols")?,
        count("SELECT COUNT(*) FROM edges")?,
        count("SELECT COUNT(*) FROM imports")?,
    ))
}

/// Map symbol name -> candidates, used to resolve call targets.
fn load_name_index(conn: &Connection) -> Result<HashMap<String, Vec<SymbolCandidate>>> {
    let mut stmt = conn.prepare("SELECT name, id, file_path, container FROM symbols")?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            SymbolCandidate {
                id: row.get::<_, String>(1)?,
                file_path: row.get::<_, String>(2)?,
                container: row.get::<_, Option<String>>(3)?,
            },
        ))
    })?;
    let mut map: HashMap<String, Vec<SymbolCandidate>> = HashMap::new();
    for row in rows {
        let (name, candidate) = row?;
        map.entry(name).or_default().push(candidate);
    }
    for candidates in map.values_mut() {
        candidates.sort_by(|left, right| {
            left.file_path
                .cmp(&right.file_path)
                .then_with(|| left.id.cmp(&right.id))
        });
    }
    Ok(map)
}

fn load_symbol_id_index(conn: &Connection) -> Result<HashMap<String, SymbolCandidate>> {
    let mut stmt = conn.prepare("SELECT id, file_path, container FROM symbols")?;
    let rows = stmt.query_map([], |row| {
        Ok(SymbolCandidate {
            id: row.get::<_, String>(0)?,
            file_path: row.get::<_, String>(1)?,
            container: row.get::<_, Option<String>>(2)?,
        })
    })?;
    let mut map = HashMap::new();
    for row in rows {
        let candidate = row?;
        map.insert(candidate.id.clone(), candidate);
    }
    Ok(map)
}

fn load_file_index(conn: &Connection) -> Result<HashSet<String>> {
    let mut stmt = conn.prepare("SELECT path FROM files")?;
    let rows = stmt.query_map([], |row| row.get::<_, String>(0))?;
    let mut files = HashSet::new();
    for row in rows {
        files.insert(row?);
    }
    Ok(files)
}

fn load_import_index(conn: &Connection) -> Result<HashMap<String, Vec<Import>>> {
    let mut stmt = conn.prepare(
        "SELECT file_path, module, local_name, imported_name, alias, line FROM imports
         ORDER BY file_path, line, module, local_name",
    )?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            Import {
                module: row.get::<_, String>(1)?,
                local_name: row.get::<_, Option<String>>(2)?,
                imported_name: row.get::<_, Option<String>>(3)?,
                alias: row.get::<_, Option<String>>(4)?,
                line: row.get::<_, i64>(5)? as usize,
            },
        ))
    })?;
    let mut map: HashMap<String, Vec<Import>> = HashMap::new();
    for row in rows {
        let (file, import) = row?;
        map.entry(file).or_default().push(import);
    }
    Ok(map)
}

/// A resolved call target plus how the resolver got there.
///
/// Confidence encodes the resolution path, not a probability: import-strict
/// matches beat same-file matches, which beat cross-file name guesses. The
/// values order the paths and leave room between them; consumers should treat
/// them ordinally.
struct ScoredTarget {
    candidate: SymbolCandidate,
    confidence: f64,
}

/// Import matched and the target's file agrees with the imported module.
const CONFIDENCE_IMPORT_STRICT: f64 = 0.9;
/// Same file, and the qualifier matched the candidate's container.
const CONFIDENCE_SAME_FILE_QUALIFIED: f64 = 0.85;
/// Same file, bare call.
const CONFIDENCE_SAME_FILE_BARE: f64 = 0.8;
/// Cross-file bare call and exactly one symbol has this name.
const CONFIDENCE_NAME_UNIQUE: f64 = 0.7;
/// Import matched but the module could not be pinned to indexed files.
const CONFIDENCE_IMPORT_FALLBACK: f64 = 0.55;
/// Cross-file bare call with several same-named candidates.
const CONFIDENCE_NAME_AMBIGUOUS: f64 = 0.5;

fn resolve_call(
    call: &PendingCall,
    name_index: &HashMap<String, Vec<SymbolCandidate>>,
    import_index: &HashMap<String, Vec<Import>>,
    source_index: &HashMap<String, SymbolCandidate>,
    file_index: &HashSet<String>,
) -> Vec<ScoredTarget> {
    let import_resolution = resolve_imported_call(call, name_index, import_index, file_index);
    let use_name_fallback = match import_resolution {
        ImportResolution::Resolved(import_targets) => {
            return scored(import_targets, CONFIDENCE_IMPORT_STRICT);
        }
        ImportResolution::Unresolved => return Vec::new(),
        ImportResolution::Fallback => true,
        ImportResolution::NoImport => false,
    };

    let Some(candidates) = name_index.get(&call.target_name) else {
        return Vec::new();
    };

    if use_name_fallback {
        return scored(
            candidates.iter().take(8).cloned().collect(),
            CONFIDENCE_IMPORT_FALLBACK,
        );
    }

    if let Some(same_file) = resolve_same_file_call(call, candidates, source_index)
        && !same_file.is_empty()
    {
        let confidence = if call.kind == CallKind::Bare {
            CONFIDENCE_SAME_FILE_BARE
        } else {
            CONFIDENCE_SAME_FILE_QUALIFIED
        };
        return scored(same_file, confidence);
    }

    if call.kind != CallKind::Bare {
        return Vec::new();
    }

    let confidence = if candidates.len() == 1 {
        CONFIDENCE_NAME_UNIQUE
    } else {
        CONFIDENCE_NAME_AMBIGUOUS
    };
    scored(candidates.iter().take(8).cloned().collect(), confidence)
}

fn scored(candidates: Vec<SymbolCandidate>, confidence: f64) -> Vec<ScoredTarget> {
    candidates
        .into_iter()
        .map(|candidate| ScoredTarget {
            candidate,
            confidence,
        })
        .collect()
}

fn resolve_same_file_call(
    call: &PendingCall,
    candidates: &[SymbolCandidate],
    source_index: &HashMap<String, SymbolCandidate>,
) -> Option<Vec<SymbolCandidate>> {
    let same_file: Vec<SymbolCandidate> = candidates
        .iter()
        .filter(|candidate| candidate.file_path == call.source_file)
        .cloned()
        .collect();
    if same_file.is_empty() {
        return None;
    }

    match call.kind {
        CallKind::Bare => Some(same_file),
        CallKind::Scoped => {
            let qualifier = call.qualifier.as_deref()?;
            let scoped: Vec<SymbolCandidate> = same_file
                .into_iter()
                .filter(|candidate| candidate.container.as_deref() == Some(qualifier))
                .collect();
            Some(scoped)
        }
        CallKind::Member => {
            let qualifier = call.qualifier.as_deref()?;
            if matches!(qualifier, "self" | "this") {
                let source_container = source_index
                    .get(&call.source_id)
                    .and_then(|source| source.container.as_deref())?;
                let method_targets = same_file
                    .into_iter()
                    .filter(|candidate| candidate.container.as_deref() == Some(source_container))
                    .collect();
                return Some(method_targets);
            }
            None
        }
    }
}

fn resolve_imported_call(
    call: &PendingCall,
    name_index: &HashMap<String, Vec<SymbolCandidate>>,
    import_index: &HashMap<String, Vec<Import>>,
    file_index: &HashSet<String>,
) -> ImportResolution {
    let Some(imports) = import_index.get(&call.source_file) else {
        return ImportResolution::NoImport;
    };
    let matched_import = imports.iter().find(|import| match call.kind {
        CallKind::Bare => import.local_name.as_deref() == Some(call.target_name.as_str()),
        CallKind::Member | CallKind::Scoped => call
            .qualifier
            .as_deref()
            .is_some_and(|qualifier| import_matches_qualifier(import, qualifier)),
    });
    let Some(matched_import) = matched_import else {
        return ImportResolution::NoImport;
    };

    let target_name = if call.kind == CallKind::Bare {
        matched_import
            .imported_name
            .as_deref()
            .unwrap_or(call.target_name.as_str())
    } else {
        call.target_name.as_str()
    };
    let Some(candidates) = name_index.get(target_name) else {
        let module_targets = module_targets(&call.source_file, matched_import, call.kind);
        return unresolved_import_resolution(call, &module_targets, file_index);
    };
    let module_targets = module_targets(&call.source_file, matched_import, call.kind);
    let targets: Vec<SymbolCandidate> = candidates
        .iter()
        .filter(|candidate| module_targets.matches(&candidate.file_path))
        .take(8)
        .cloned()
        .collect();
    if targets.is_empty() {
        unresolved_import_resolution(call, &module_targets, file_index)
    } else {
        ImportResolution::Resolved(targets)
    }
}

fn unresolved_import_resolution(
    call: &PendingCall,
    module_targets: &ModuleTargets,
    file_index: &HashSet<String>,
) -> ImportResolution {
    if call.kind == CallKind::Bare || module_targets.is_external(file_index) {
        ImportResolution::Unresolved
    } else {
        ImportResolution::Fallback
    }
}

fn import_matches_qualifier(import: &Import, qualifier: &str) -> bool {
    import.local_name.as_deref() == Some(qualifier)
        || import.alias.as_deref() == Some(qualifier)
        || import.module == qualifier
}

#[derive(Default)]
struct ModuleTargets {
    files: Vec<String>,
    dirs: Vec<String>,
    relative: bool,
}

impl ModuleTargets {
    fn matches(&self, file_path: &str) -> bool {
        self.files.iter().any(|file| file == file_path)
            || self.dirs.iter().any(|dir| file_path.starts_with(dir))
    }

    fn has_indexed_match(&self, file_index: &HashSet<String>) -> bool {
        file_index.iter().any(|file| self.matches(file))
    }

    fn is_external(&self, file_index: &HashSet<String>) -> bool {
        !self.relative && !self.has_indexed_match(file_index)
    }

    fn finish(&mut self) {
        self.files.sort();
        self.files.dedup();
        self.dirs.sort();
        self.dirs.dedup();
    }
}

fn module_targets(source_file: &str, import: &Import, call_kind: CallKind) -> ModuleTargets {
    let mut targets = ModuleTargets::default();
    if source_file.ends_with(".py") {
        targets.relative = import.module.starts_with('.');
        if let Some(prefix) = python_module_prefix(source_file, import, call_kind) {
            push_module_variants(&mut targets.files, &prefix, &["py"]);
        }
    } else if source_file.ends_with(".go") {
        push_go_module_targets(&mut targets, &import.module);
    } else if source_file.ends_with(".rs") {
        targets.relative = rust_module_prefix(&import.module).is_some();
        if let Some(prefix) = rust_module_prefix(&import.module) {
            push_module_variants(&mut targets.files, &prefix, &["rs"]);
            // `use crate::store::X` reaches X through src/store/mod.rs
            // re-exports, so any file under the module directory is a
            // legitimate definition site.
            targets.dirs.push(format!("{prefix}/"));
        }
    } else if import.module.starts_with('.') {
        targets.relative = true;
        if let Some(prefix) = normalize_relative_path_module(source_file, &import.module) {
            push_module_variants(&mut targets.files, &prefix, &["ts", "tsx", "js", "jsx"]);
        }
    }
    targets.finish();
    targets
}

fn python_module_prefix(source_file: &str, import: &Import, call_kind: CallKind) -> Option<String> {
    if import.module.starts_with('.') {
        let mut prefix = normalize_python_relative_module(source_file, &import.module)?;
        if call_kind != CallKind::Bare
            && let Some(imported_name) = import.imported_name.as_deref()
            && !imported_name.is_empty()
        {
            append_module_path(&mut prefix, imported_name);
        }
        Some(prefix)
    } else {
        let mut prefix = import.module.replace('.', "/");
        if call_kind != CallKind::Bare
            && let Some(imported_name) = import.imported_name.as_deref()
            && !imported_name.is_empty()
        {
            append_module_path(&mut prefix, imported_name);
        }
        Some(prefix)
    }
}

fn normalize_python_relative_module(source_file: &str, module: &str) -> Option<String> {
    let dot_count = module.chars().take_while(|ch| *ch == '.').count();
    if dot_count == 0 {
        return Some(module.replace('.', "/"));
    }
    let base_dir = source_file.rsplit_once('/').map_or("", |(dir, _)| dir);
    let mut parts: Vec<&str> = base_dir
        .split('/')
        .filter(|part| !part.is_empty())
        .collect();
    for _ in 1..dot_count {
        parts.pop();
    }
    let rest = &module[dot_count..];
    push_path_components(&mut parts, rest.split('.'));
    if parts.is_empty() {
        None
    } else {
        Some(parts.join("/"))
    }
}

fn push_path_components<'a>(
    parts: &mut Vec<&'a str>,
    components: impl IntoIterator<Item = &'a str>,
) {
    parts.extend(components.into_iter().filter(|part| !part.is_empty()));
}

fn append_module_path(prefix: &mut String, module: &str) {
    if !prefix.is_empty() {
        prefix.push('/');
    }
    prefix.push_str(&module.replace('.', "/"));
}

fn rust_module_prefix(module: &str) -> Option<String> {
    let stripped = module
        .strip_prefix("crate")
        .or_else(|| module.strip_prefix("graphtrail"))?;
    let stripped = stripped.strip_prefix("::").unwrap_or(stripped);
    if stripped.is_empty() {
        Some("src/lib".to_string())
    } else {
        Some(format!("src/{}", stripped.replace("::", "/")))
    }
}

fn push_go_module_targets(targets: &mut ModuleTargets, module: &str) {
    let parts: Vec<&str> = module.split('/').filter(|part| !part.is_empty()).collect();
    for start in 0..parts.len() {
        targets.dirs.push(format!("{}/", parts[start..].join("/")));
    }
}

fn normalize_relative_path_module(source_file: &str, module: &str) -> Option<String> {
    let base_dir = source_file.rsplit_once('/').map_or("", |(dir, _)| dir);
    let mut parts: Vec<&str> = base_dir
        .split('/')
        .filter(|part| !part.is_empty())
        .collect();
    for part in module.split('/') {
        match part {
            "" | "." => {}
            ".." => {
                parts.pop();
            }
            name => parts.push(name),
        }
    }
    if parts.is_empty() {
        None
    } else {
        Some(parts.join("/"))
    }
}

fn push_module_variants(files: &mut Vec<String>, prefix: &str, exts: &[&str]) {
    for ext in exts {
        files.push(format!("{prefix}.{ext}"));
        files.push(format!("{prefix}/index.{ext}"));
        if *ext == "py" {
            files.push(format!("{prefix}/__init__.py"));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::unsafe_root_reason;
    use std::path::Path;

    #[test]
    fn filesystem_root_is_unsafe() {
        assert_eq!(
            unsafe_root_reason(Path::new("/"), None),
            Some("root is the filesystem root")
        );
    }

    #[test]
    fn home_directory_is_unsafe() {
        let home = Path::new("/home/someone");
        assert_eq!(
            unsafe_root_reason(home, Some(home)),
            Some("root is the home directory")
        );
    }

    #[test]
    fn project_directory_under_home_is_safe() {
        let home = Path::new("/home/someone");
        assert_eq!(
            unsafe_root_reason(Path::new("/home/someone/repos/project"), Some(home)),
            None
        );
    }

    #[test]
    fn any_directory_is_safe_without_home() {
        assert_eq!(unsafe_root_reason(Path::new("/srv/project"), None), None);
    }
}
