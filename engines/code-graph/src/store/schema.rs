//! SQLite schema definition.

use anyhow::Result;
use rusqlite::Connection;

/// Bumped when the on-disk schema changes; surfaced in JSON packs from Phase 2 on.
pub const SCHEMA_VERSION: u32 = 3;

pub fn init_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            size INTEGER NOT NULL,
            modified_at INTEGER NOT NULL,
            indexed_at INTEGER NOT NULL,
            language TEXT NOT NULL
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
        "#,
    )?;
    ensure_import_columns(conn)?;
    Ok(())
}

/// Apply write-path schema upgrades needed before sync can insert current rows.
///
/// Returns true when the symbols table was upgraded and the caller should force
/// a full reindex so newly added nullable columns are populated for existing files.
pub fn upgrade_for_sync(conn: &Connection) -> Result<bool> {
    let mut upgraded = false;
    if !table_has_column(conn, "symbols", "body_hash")? {
        conn.execute("ALTER TABLE symbols ADD COLUMN body_hash TEXT", [])?;
        upgraded = true;
    }
    Ok(upgraded)
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
