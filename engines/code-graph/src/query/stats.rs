//! Aggregate counts and sync metadata over the graph tables.

use anyhow::Result;
use rusqlite::Connection;

use crate::model::Stats;
use crate::store::SCHEMA_VERSION;
use crate::store::meta;

pub fn stats(conn: &Connection) -> Result<Stats> {
    let count = |sql: &str| -> Result<i64> { Ok(conn.query_row(sql, [], |row| row.get(0))?) };
    let mut stmt = conn.prepare(
        "SELECT language, COUNT(*) AS files FROM files GROUP BY language ORDER BY language",
    )?;
    let rows = stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
    })?;
    let mut language_files = std::collections::BTreeMap::new();
    for row in rows {
        let (language, files) = row?;
        language_files.insert(language, files);
    }
    Ok(Stats {
        schema_version: SCHEMA_VERSION,
        files: count("SELECT COUNT(*) FROM files")?,
        symbols: count("SELECT COUNT(*) FROM symbols")?,
        edges: count("SELECT COUNT(*) FROM edges")?,
        imports: count("SELECT COUNT(*) FROM imports")?,
        synced_at: meta::read(conn, "synced_at")?,
        tool_version: meta::read(conn, "tool_version")?,
        language_files,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::{init_schema, meta};
    use rusqlite::params;
    use serde_json::json;

    #[test]
    fn stats_include_sync_metadata_and_language_file_counts() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        for (path, language) in [
            ("a.py", "python"),
            ("b.ts", "typescript"),
            ("c.py", "python"),
        ] {
            conn.execute(
                "INSERT INTO files(path, content_hash, size, modified_at, indexed_at, language)
                 VALUES (?1, 'hash', 1, 1, 1, ?2)",
                params![path, language],
            )
            .unwrap();
        }
        meta::upsert(&conn, "synced_at", "12345").unwrap();
        meta::upsert(&conn, "tool_version", "9.9.9").unwrap();

        let value = serde_json::to_value(stats(&conn).unwrap()).unwrap();

        assert_eq!(value["synced_at"], json!("12345"));
        assert_eq!(value["tool_version"], json!("9.9.9"));
        assert_eq!(value["language_files"]["python"], json!(2));
        assert_eq!(value["language_files"]["typescript"], json!(1));
    }
}
