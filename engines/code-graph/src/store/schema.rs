//! SQLite schema definition.

use anyhow::Result;
use rusqlite::Connection;

/// Bumped when the on-disk schema changes; surfaced in JSON packs from Phase 2 on.
pub const SCHEMA_VERSION: u32 = 7;

/// What sync must redo after a schema upgrade, from nothing to everything.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum SchemaUpgrade {
    /// Schema is current; sync normally.
    None,
    /// Derived edge data changed shape; re-resolve edges from stored pending
    /// calls without re-parsing any file.
    RebuildEdges,
    /// Extraction output changed shape; re-parse every file.
    FullReindex,
}

pub fn init_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            size INTEGER NOT NULL,
            modified_at INTEGER NOT NULL,
            indexed_at INTEGER NOT NULL,
            language TEXT NOT NULL,
            extractor_fingerprint TEXT
        );

        CREATE TABLE IF NOT EXISTS symbols (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            signature TEXT NOT NULL,
            container TEXT,
            content_hash TEXT NOT NULL,
            body_hash TEXT,
            FOREIGN KEY(file_path) REFERENCES files(path) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS edges (
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            kind TEXT NOT NULL,
            line INTEGER,
            confidence REAL,
            PRIMARY KEY(source, target, kind, line),
            FOREIGN KEY(source) REFERENCES symbols(id) ON DELETE CASCADE,
            FOREIGN KEY(target) REFERENCES symbols(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            module TEXT NOT NULL,
            local_name TEXT,
            imported_name TEXT,
            alias TEXT,
            line INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pending_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            file_path TEXT NOT NULL,
            target_name TEXT NOT NULL,
            kind TEXT NOT NULL,
            qualifier TEXT,
            line INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
            symbol_id UNINDEXED,
            name,
            qualified_name,
            signature,
            file_path
        );

        CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
        CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
        CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
        CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
        CREATE INDEX IF NOT EXISTS idx_pending_calls_file ON pending_calls(file_path);
        "#,
    )?;
    ensure_import_columns(conn)?;
    Ok(())
}

/// Apply write-path schema upgrades needed before sync can insert current rows,
/// returning how much derived state the caller must rebuild.
pub fn upgrade_for_sync(conn: &Connection) -> Result<SchemaUpgrade> {
    let mut upgrade = SchemaUpgrade::None;
    if !table_has_column(conn, "symbols", "body_hash")? {
        conn.execute("ALTER TABLE symbols ADD COLUMN body_hash TEXT", [])?;
        upgrade = upgrade.max(SchemaUpgrade::FullReindex);
    }
    if !table_has_column(conn, "files", "extractor_fingerprint")? {
        conn.execute(
            "ALTER TABLE files ADD COLUMN extractor_fingerprint TEXT",
            [],
        )?;
    }
    // v5 introduced persisted pending calls. A pre-v5 DB has none, so edges can
    // no longer be derived for its files; reindex once to populate them. The
    // stored version (not table existence) is the signal, because init_schema
    // creates the empty table before this runs.
    if stored_schema_version(conn)?.is_some_and(|version| version < 5) {
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pending_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                target_name TEXT NOT NULL,
                kind TEXT NOT NULL,
                qualifier TEXT,
                line INTEGER NOT NULL
            )",
            [],
        )?;
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_calls_file ON pending_calls(file_path)",
            [],
        )?;
        upgrade = upgrade.max(SchemaUpgrade::FullReindex);
    }
    // v6 added edges.confidence. Edges are derived from pending_calls, so a
    // resolution pass (no re-parse) fills the column. The column check is the
    // signal here: CREATE TABLE IF NOT EXISTS never alters an existing table.
    if !table_has_column(conn, "edges", "confidence")? {
        conn.execute("ALTER TABLE edges ADD COLUMN confidence REAL", [])?;
        upgrade = upgrade.max(SchemaUpgrade::RebuildEdges);
    }
    // v7 dropped start_line from symbol identity. New ids derive entirely from
    // columns the symbols table already stores, so the migration rewrites ids
    // in place (symbols, pending_calls, FTS) and re-resolves edges, re-parsing
    // nothing. Pre-v5 databases skip this: their FullReindex regenerates ids.
    if stored_schema_version(conn)?.is_some_and(|version| (5..7).contains(&version)) {
        rewrite_symbol_ids_v7(conn)?;
        upgrade = upgrade.max(SchemaUpgrade::RebuildEdges);
    }
    Ok(upgrade)
}

/// Rewrite every symbol id to the v7 line-independent form.
///
/// Occurrence ordinals for same-named symbols are assigned in
/// `(start_line, old id)` order, which matches extraction's traversal order
/// except for exotic same-line duplicates; those converge to traversal order
/// the next time their file re-extracts, and edges rebuild either way.
fn rewrite_symbol_ids_v7(conn: &Connection) -> Result<()> {
    use crate::extractors::common::symbol_id;
    use std::collections::HashMap;

    let tx = conn.unchecked_transaction()?;
    let mut mapping: Vec<(String, String)> = Vec::new();
    {
        let mut stmt = tx.prepare(
            "SELECT id, file_path, qualified_name, kind FROM symbols
             ORDER BY file_path, start_line, id",
        )?;
        let rows = stmt.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
            ))
        })?;
        let mut occurrences: HashMap<String, usize> = HashMap::new();
        for row in rows {
            let (old_id, file_path, qualified_name, kind) = row?;
            let counter = occurrences
                .entry(format!("{file_path}:{qualified_name}:{kind}"))
                .or_insert(0);
            let new_id = symbol_id(&file_path, &qualified_name, &kind, *counter);
            *counter += 1;
            if new_id != old_id {
                mapping.push((old_id, new_id));
            }
        }
    }
    // Edges are derived state and the caller rebuilds them; dropping them
    // first keeps the id rewrite free of dangling references mid-flight.
    tx.execute("DELETE FROM edges", [])?;
    {
        let mut update_symbol = tx.prepare("UPDATE symbols SET id = ?2 WHERE id = ?1")?;
        let mut update_calls =
            tx.prepare("UPDATE pending_calls SET source_id = ?2 WHERE source_id = ?1")?;
        let mut update_fts =
            tx.prepare("UPDATE symbols_fts SET symbol_id = ?2 WHERE symbol_id = ?1")?;
        for (old_id, new_id) in &mapping {
            update_symbol.execute(rusqlite::params![old_id, new_id])?;
            update_calls.execute(rusqlite::params![old_id, new_id])?;
            update_fts.execute(rusqlite::params![old_id, new_id])?;
        }
    }
    tx.commit()?;
    Ok(())
}

fn stored_schema_version(conn: &Connection) -> Result<Option<u32>> {
    let meta_exists: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta')",
        [],
        |row| row.get(0),
    )?;
    if !meta_exists {
        return Ok(None);
    }
    Ok(crate::store::meta::read(conn, "schema_version")?.and_then(|value| value.parse().ok()))
}

fn ensure_import_columns(conn: &Connection) -> Result<()> {
    let mut stmt = conn.prepare("PRAGMA table_info(imports)")?;
    let columns = stmt
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for column in ["local_name", "imported_name", "alias"] {
        if !columns.iter().any(|existing| existing == column) {
            conn.execute(&format!("ALTER TABLE imports ADD COLUMN {column} TEXT"), [])?;
        }
    }
    Ok(())
}

pub fn table_has_column(conn: &Connection, table: &str, column: &str) -> Result<bool> {
    let mut stmt = conn.prepare(&format!("PRAGMA table_info({table})"))?;
    let columns = stmt
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(columns.iter().any(|existing| existing == column))
}
