//! Sync persistence, freshness planning, and stored graph bookkeeping.

use std::collections::HashMap;
use std::fs;
use std::path::Path;

use anyhow::Result;
use rusqlite::{Connection, params};

use crate::extractors::common::hex_hash;
use crate::extractors::{extractor_fingerprint_for, language_for};
use crate::store::walk::Entry;

pub(super) struct DbFile {
    content_hash: String,
    size: u64,
    mtime: i64,
    extractor_fingerprint: Option<String>,
}

pub(super) struct StalePlan<'a> {
    pub(super) entries: Vec<&'a Entry>,
}

pub(super) enum EntryFreshness {
    Fresh,
    New,
    Changed,
    FingerprintStale,
}

pub(super) fn stale_plan<'a>(
    entries: &'a [Entry],
    db_files: &HashMap<String, DbFile>,
) -> Result<StalePlan<'a>> {
    let mut stale = Vec::new();
    for entry in entries {
        match entry_freshness(entry, db_files)? {
            EntryFreshness::New | EntryFreshness::Changed | EntryFreshness::FingerprintStale => {
                stale.push(entry)
            }
            EntryFreshness::Fresh => {}
        }
    }
    Ok(StalePlan { entries: stale })
}

/// Insert one extracted file's rows: the file record, its symbols (plus FTS),
/// imports, and pending calls awaiting cross-file resolution.
pub(super) fn write_file_graph(
    tx: &Connection,
    graph: &crate::model::FileGraph,
    now: i64,
) -> Result<()> {
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

pub(super) fn purge_file_graph(tx: &Connection, path: &str) -> Result<()> {
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
    Ok(())
}

pub(super) fn entry_freshness(
    entry: &Entry,
    db_files: &HashMap<String, DbFile>,
) -> Result<EntryFreshness> {
    let Some(db_file) = db_files.get(&entry.rel) else {
        return Ok(EntryFreshness::New);
    };
    if db_file.size != entry.size || db_file.mtime != entry.mtime {
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

/// Map file path -> freshness metadata from the `files` table.
pub(super) fn load_db_files(conn: &Connection) -> Result<HashMap<String, DbFile>> {
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
pub(super) fn table_counts(conn: &Connection) -> Result<(usize, usize, usize, usize)> {
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
