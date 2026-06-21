//! Aggregate counts over the graph tables.

use std::collections::BTreeMap;

use anyhow::Result;
use rusqlite::Connection;

pub fn stats(conn: &Connection) -> Result<BTreeMap<String, i64>> {
    let mut map = BTreeMap::new();
    for (key, sql) in [
        ("files", "SELECT COUNT(*) FROM files"),
        ("symbols", "SELECT COUNT(*) FROM symbols"),
        ("edges", "SELECT COUNT(*) FROM edges"),
        ("imports", "SELECT COUNT(*) FROM imports"),
    ] {
        let count: i64 = conn.query_row(sql, [], |row| row.get(0))?;
        map.insert(key.to_string(), count);
    }
    Ok(map)
}
