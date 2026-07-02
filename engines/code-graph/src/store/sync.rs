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
use rusqlite::{Connection, params};
use walkdir::{DirEntry, WalkDir};

use crate::extractors::common::hex_hash;
use crate::extractors::{index_file, language_for};
use crate::model::Lang;
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

/// Incremental sync (skips work when nothing changed).
pub fn sync_repo(conn: &Connection, root: &Path) -> Result<SyncSummary> {
    sync_repo_force(conn, root, false)
}

/// Like [`sync_repo`] but `force` rebuilds every file regardless of the stat/hash check.
pub fn sync_repo_force(conn: &Connection, root: &Path, force: bool) -> Result<SyncSummary> {
    let root = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());

    // Stat pass: enumerate supported files without parsing them.
    let mut entries: Vec<Entry> = Vec::new();
    for entry in WalkDir::new(&root).into_iter().filter_entry(keep_entry) {
        let entry = entry?;
        if !entry.file_type().is_file() {
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
                "INSERT INTO symbols(id, kind, name, qualified_name, file_path, start_line, end_line, signature, container, content_hash)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
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
        for (module, line) in &graph.imports {
            tx.execute(
                "INSERT INTO imports(file_path, module, line) VALUES (?1, ?2, ?3)",
                params![graph.path, module, *line as i64],
            )?;
        }
    }

    let name_index = load_name_index(&tx)?;
    let mut inserted_calls = 0;
    for graph in &graphs {
        for call in &graph.calls {
            let Some(candidates) = name_index.get(&call.target_name) else {
                continue;
            };
            // Prefer same-file candidates (precise, low fan-out); else fall back to cross-file capped.
            let same_file: Vec<&String> = candidates
                .iter()
                .filter(|(_, file)| *file == call.source_file)
                .map(|(id, _)| id)
                .collect();
            let chosen: Vec<&String> = if same_file.is_empty() {
                candidates.iter().map(|(id, _)| id).take(8).collect()
            } else {
                same_file
            };
            for target in chosen {
                if target == &call.source_id {
                    continue;
                }
                tx.execute(
                    "INSERT OR IGNORE INTO edges(source, target, kind, line) VALUES (?1, ?2, 'calls', ?3)",
                    params![call.source_id, target, call.line as i64],
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
            | "__pycache__"
    )
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

/// Map symbol name -> list of (symbol id, file path), used to resolve call targets.
fn load_name_index(conn: &Connection) -> Result<HashMap<String, Vec<(String, String)>>> {
    let mut stmt = conn.prepare("SELECT name, id, file_path FROM symbols")?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
        ))
    })?;
    let mut map: HashMap<String, Vec<(String, String)>> = HashMap::new();
    for row in rows {
        let (name, id, file) = row?;
        map.entry(name).or_default().push((id, file));
    }
    for candidates in map.values_mut() {
        candidates.sort_by(|(left_id, left_file), (right_id, right_file)| {
            left_file
                .cmp(right_file)
                .then_with(|| left_id.cmp(right_id))
        });
    }
    Ok(map)
}
