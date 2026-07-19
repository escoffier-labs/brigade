//! Key/value provenance stored in the `meta` table (schema version, tool version, sync time).

use anyhow::Result;
use rusqlite::{Connection, OptionalExtension, params};

use crate::store::db::now_ts;
use crate::store::schema::SCHEMA_VERSION;

pub fn upsert(conn: &Connection, key: &str, value: &str) -> Result<()> {
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?1, ?2)
         ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        params![key, value],
    )?;
    Ok(())
}

pub fn read(conn: &Connection, key: &str) -> Result<Option<String>> {
    let value = conn
        .query_row(
            "SELECT value FROM meta WHERE key = ?1",
            params![key],
            |row| row.get(0),
        )
        .optional()?;
    Ok(value)
}

/// Record provenance after a sync: schema version, tool version, and timestamp.
pub fn write_sync_meta(conn: &Connection) -> Result<()> {
    upsert(conn, "schema_version", &SCHEMA_VERSION.to_string())?;
    upsert(conn, "tool_version", env!("CARGO_PKG_VERSION"))?;
    upsert(conn, "synced_at", &now_ts().to_string())?;
    Ok(())
}
