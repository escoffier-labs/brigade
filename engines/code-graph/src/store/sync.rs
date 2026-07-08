//! Repository sync: walk files, extract graphs, and write them transactionally.
//!
//! Sync is incremental: a stat pass (size + mtime, confirmed by content hash when those differ)
//! decides whether anything actually changed. If nothing did, sync skips all parsing and only
//! refreshes sync metadata. When something changed (or `force`), it rebuilds the present files and
//! purges rows for files that were deleted from disk.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

use anyhow::Result;
use ignore::{DirEntry, WalkBuilder};
use rusqlite::{Connection, params};

use crate::extractors::common::hex_hash;
use crate::extractors::{index_file, language_for};
use crate::model::{CallKind, Import, Lang, PendingCall};
use crate::store::db::now_ts;

#[derive(Default)]
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

struct Entry {
    path: PathBuf,
    rel: String,
    lang: Lang,
    size: u64,
    mtime: i64,
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
    let upgraded = crate::store::schema::upgrade_for_sync(conn)?;
    let force = force || upgraded;

    // Stat pass: enumerate supported files without parsing them.
    let mut entries: Vec<Entry> = Vec::new();
    let has_git_context = has_git_context(&root);
    let mut walker = WalkBuilder::new(&root);
    walker
        .hidden(false)
        .git_ignore(has_git_context)
        .git_global(false)
        .git_exclude(has_git_context)
        .ignore(false)
        .parents(true)
        .filter_entry(keep_entry);
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
        let rel = entry
            .path()
            .strip_prefix(&root)
            .unwrap_or(entry.path())
            .to_string_lossy()
            .replace('\\', "/");
        let metadata = entry.metadata()?;
        let mtime = metadata
            .modified()
            .ok()
            .and_then(|m| m.duration_since(UNIX_EPOCH).ok())
            .map_or(0, |d| d.as_secs() as i64);
        entries.push(Entry {
            path: entry.path().to_path_buf(),
            rel,
            lang,
            size: metadata.len(),
            mtime,
        });
    }

    let db_files = load_db_files(conn)?;
    let on_disk: HashSet<&str> = entries.iter().map(|e| e.rel.as_str()).collect();
    let deleted: Vec<String> = db_files
        .keys()
        .filter(|path| !on_disk.contains(path.as_str()))
        .cloned()
        .collect();

    let changed = force || !deleted.is_empty() || has_real_change(&entries, &db_files)?;
    if !changed {
        let tx = conn.unchecked_transaction()?;
        crate::store::meta::write_sync_meta(&tx)?;
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

    // Rebuild: re-index every present file and purge rows for deleted files.
    let mut graphs = Vec::with_capacity(entries.len());
    for entry in &entries {
        graphs.push(index_file(&root, &entry.path, entry.lang)?);
    }

    let tx = conn.unchecked_transaction()?;
    let mut purge: Vec<String> = graphs.iter().map(|g| g.path.clone()).collect();
    purge.extend(deleted.iter().cloned());
    for path in &purge {
        tx.execute(
            "DELETE FROM edges WHERE source IN (SELECT id FROM symbols WHERE file_path = ?1)",
            params![path],
        )?;
        tx.execute(
            "DELETE FROM symbols_fts WHERE file_path = ?1",
            params![path],
        )?;
        tx.execute("DELETE FROM symbols WHERE file_path = ?1", params![path])?;
        tx.execute("DELETE FROM imports WHERE file_path = ?1", params![path])?;
        tx.execute("DELETE FROM files WHERE path = ?1", params![path])?;
    }

    let now = now_ts();
    for graph in &graphs {
        tx.execute(
            "INSERT OR REPLACE INTO files(path, content_hash, size, modified_at, indexed_at, language)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                graph.path,
                graph.hash,
                graph.size as i64,
                graph.modified_at,
                now,
                graph.language
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
    }

    let name_index = load_name_index(&tx)?;
    let import_index = load_import_index(&tx)?;
    let source_index = load_symbol_id_index(&tx)?;
    let file_index = load_file_index(&tx)?;
    let mut inserted_calls = 0;
    for graph in &graphs {
        for call in &graph.calls {
            let chosen = resolve_call(call, &name_index, &import_index, &source_index, &file_index);
            for target in chosen {
                if target.id == call.source_id {
                    continue;
                }
                tx.execute(
                    "INSERT OR IGNORE INTO edges(source, target, kind, line) VALUES (?1, ?2, 'calls', ?3)",
                    params![call.source_id, target.id, call.line as i64],
                )?;
                inserted_calls += 1;
            }
        }
    }
    crate::store::meta::write_sync_meta(&tx)?;
    tx.commit()?;

    Ok(SyncSummary {
        files: graphs.len(),
        symbols: graphs.iter().map(|g| g.symbols.len()).sum(),
        imports: graphs.iter().map(|g| g.imports.len()).sum(),
        calls: inserted_calls,
        unchanged: false,
        deleted: deleted.len(),
    })
}

/// True if any file is new, or differs in size/mtime AND content hash, from what's indexed.
fn has_real_change(
    entries: &[Entry],
    db_files: &HashMap<String, (String, u64, i64)>,
) -> Result<bool> {
    for entry in entries {
        match db_files.get(&entry.rel) {
            None => return Ok(true),
            Some((hash, size, mtime)) => {
                if *size != entry.size || *mtime != entry.mtime {
                    // Cheap stat differs; confirm with content hash to ignore mtime-only touches.
                    let content = fs::read_to_string(&entry.path)?;
                    if &hex_hash(content.as_bytes()) != hash {
                        return Ok(true);
                    }
                }
            }
        }
    }
    Ok(false)
}

fn keep_entry(entry: &DirEntry) -> bool {
    let name = entry.file_name().to_string_lossy();
    !matches!(
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

fn has_git_context(root: &Path) -> bool {
    root.ancestors().any(has_git_marker)
}

fn has_git_marker(dir: &Path) -> bool {
    let git = dir.join(".git");
    git.is_file() || git.join("HEAD").is_file()
}

/// Map file path -> (content_hash, size, modified_at) from the `files` table.
fn load_db_files(conn: &Connection) -> Result<HashMap<String, (String, u64, i64)>> {
    let mut stmt = conn.prepare("SELECT path, content_hash, size, modified_at FROM files")?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)? as u64,
            row.get::<_, i64>(3)?,
        ))
    })?;
    let mut map = HashMap::new();
    for row in rows {
        let (path, hash, size, mtime) = row?;
        map.insert(path, (hash, size, mtime));
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

fn resolve_call(
    call: &PendingCall,
    name_index: &HashMap<String, Vec<SymbolCandidate>>,
    import_index: &HashMap<String, Vec<Import>>,
    source_index: &HashMap<String, SymbolCandidate>,
    file_index: &HashSet<String>,
) -> Vec<SymbolCandidate> {
    let import_resolution = resolve_imported_call(call, name_index, import_index, file_index);
    let use_name_fallback = match import_resolution {
        ImportResolution::Resolved(import_targets) => return import_targets,
        ImportResolution::Unresolved => return Vec::new(),
        ImportResolution::Fallback => true,
        ImportResolution::NoImport => false,
    };

    let Some(candidates) = name_index.get(&call.target_name) else {
        return Vec::new();
    };

    if use_name_fallback {
        return candidates.iter().take(8).cloned().collect();
    }

    if let Some(same_file) = resolve_same_file_call(call, candidates, source_index)
        && !same_file.is_empty()
    {
        return same_file;
    }

    if call.kind != CallKind::Bare {
        return Vec::new();
    }

    candidates.iter().take(8).cloned().collect()
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
