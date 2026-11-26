//! Canonical content digest of a committed graph snapshot.

use anyhow::{Context, Result, ensure};
use rusqlite::{Connection, types::ValueRef};
use serde::Serialize;
use sha2::{Digest, Sha256};

use crate::store::schema::{SCHEMA_VERSION, checked_schema_version};

const FORMAT_VERSION: u32 = 2;
const CONTENT_TABLES: &[&str] = &[
    "conditional_symbols",
    "edges",
    "files",
    "imports",
    "module_assignments",
    "module_exports",
    "pending_calls",
    "skipped_files",
    "symbols",
];

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct GraphDigest {
    pub format_version: u32,
    pub schema_version: u32,
    pub sha256: String,
}

pub fn graph_digest(conn: &Connection) -> Result<GraphDigest> {
    let snapshot = conn.unchecked_transaction()?;
    ensure!(
        checked_schema_version(&snapshot)? == Some(SCHEMA_VERSION),
        "graph digest requires a database synced with schema {SCHEMA_VERSION}"
    );

    let mut hash = Sha256::new();
    let mut buffer = digest_prefix(FORMAT_VERSION, SCHEMA_VERSION);
    for table in CONTENT_TABLES {
        hash_table(&snapshot, table, &mut hash, &mut buffer)
            .with_context(|| format!("cannot digest graph table {table}"))?;
    }
    snapshot.commit()?;
    hash.update(&buffer);
    Ok(GraphDigest {
        format_version: FORMAT_VERSION,
        schema_version: SCHEMA_VERSION,
        sha256: format!("{:x}", hash.finalize()),
    })
}

fn digest_prefix(format: u32, schema: u32) -> Vec<u8> {
    let mut prefix = Vec::new();
    field(&mut prefix, b'D', b"code-graph-content");
    field(&mut prefix, b'V', &format.to_be_bytes());
    field(&mut prefix, b'V', &schema.to_be_bytes());
    prefix
}

fn hash_table(
    conn: &Connection,
    table: &str,
    hash: &mut Sha256,
    buffer: &mut Vec<u8>,
) -> Result<()> {
    let ordinary_table: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_table_list
                       WHERE schema = 'main' AND name = ?1 AND type = 'table')",
        [table],
        |row| row.get(0),
    )?;
    ensure!(ordinary_table, "graph digest requires table {table}");
    let join_columns = match table {
        "symbols" => &["id"][..],
        "edges" => &["source", "target"][..],
        _ => &[],
    };
    // The diff uses BINARY collation and TEXT affinity for its endpoint joins.
    // Require the expected affinities so altered declarations cannot change
    // join semantics without changing the selected values.
    for name in join_columns {
        let is_text: bool = conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM pragma_table_xinfo(?1, 'main')
                           WHERE name = ?2 AND upper(type) = 'TEXT')",
            rusqlite::params![table, name],
            |row| row.get(0),
        )?;
        ensure!(
            is_text,
            "graph digest requires a TEXT column for {table}.{name}"
        );
    }
    let mut info =
        conn.prepare("SELECT name, pk FROM pragma_table_xinfo(?1, 'main') ORDER BY cid")?;
    let columns = info
        .query_map([table], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, u32>(1)?))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut keys: Vec<_> = columns.iter().filter(|(_, pk)| *pk > 0).collect();
    keys.sort_unstable_by_key(|(_, pk)| *pk);
    ensure!(
        !keys.is_empty(),
        "graph digest requires a primary key for {table}"
    );

    let nullable_keys = keys
        .iter()
        .map(|(name, _)| format!("{} IS NULL", quote(name)))
        .collect::<Vec<_>>()
        .join(" OR ");
    let has_null_key: bool = conn.query_row(
        &format!("SELECT EXISTS(SELECT 1 FROM main.{table} WHERE {nullable_keys})"),
        [],
        |row| row.get(0),
    )?;
    let mut order: Vec<_> = keys
        .iter()
        .map(|(name, _)| format!("{} COLLATE BINARY", quote(name)))
        .collect();
    if has_null_key {
        // SQLite permits duplicate composite keys containing NULL. Extend the
        // order over complete values and storage classes, never over rowids.
        for (name, _) in &columns {
            let column = quote(name);
            order.push(format!("{column} COLLATE BINARY"));
            order.push(format!("typeof({column})"));
        }
    }

    let projection = columns
        .iter()
        .map(|(name, _)| quote(name))
        .collect::<Vec<_>>()
        .join(", ");
    let mut stmt = conn.prepare(&format!(
        "SELECT {projection} FROM main.{table} ORDER BY {}",
        order.join(", ")
    ))?;
    let required = diff_columns(table, &columns)?;
    let mut rows = stmt.query([])?;
    field(buffer, b'T', table.as_bytes());
    for (name, _) in &columns {
        field(buffer, b'C', name.as_bytes());
    }

    while let Some(row) = rows.next()? {
        validate_diff_row(row, &required)?;
        buffer.push(b'R');
        for idx in 0..columns.len() {
            match row.get_ref(idx)? {
                ValueRef::Null => field(buffer, b'N', &[]),
                ValueRef::Integer(value) => field(buffer, b'I', &value.to_be_bytes()),
                ValueRef::Real(value) => {
                    // SQLite compares both signs of zero as the same real value.
                    let canonical = if value == 0.0 { 0.0 } else { value };
                    field(buffer, b'F', &canonical.to_be_bytes());
                }
                ValueRef::Text(value) => field(buffer, b'S', value),
                ValueRef::Blob(value) => field(buffer, b'B', value),
            }
        }
        if buffer.len() >= 256 * 1024 {
            hash.update(&*buffer);
            buffer.clear();
        }
    }
    buffer.push(b'E');
    Ok(())
}

fn quote(identifier: &str) -> String {
    format!("\"{}\"", identifier.replace('"', "\"\""))
}

fn field(buffer: &mut Vec<u8>, tag: u8, bytes: &[u8]) {
    buffer.push(tag);
    buffer.extend_from_slice(&(bytes.len() as u64).to_be_bytes());
    buffer.extend_from_slice(bytes);
}

enum DiffColumns {
    Symbols {
        text: [usize; 4],
        lines: [usize; 2],
        body: usize,
    },
    Edges {
        kind: usize,
        line: usize,
        joins: [usize; 2],
    },
    Other,
}

/// A successful digest must be usable by the diff reader. A schema marker
/// alone cannot exclude missing columns or invalid SQLite storage classes.
fn diff_columns(table: &str, columns: &[(String, u32)]) -> Result<DiffColumns> {
    let index = |name: &str| {
        columns
            .iter()
            .position(|(column, _)| column == name)
            .ok_or_else(|| anyhow::anyhow!("graph digest requires {table}.{name}"))
    };
    match table {
        "symbols" => {
            index("id")?;
            Ok(DiffColumns::Symbols {
                text: [
                    index("kind")?,
                    index("qualified_name")?,
                    index("file_path")?,
                    index("signature")?,
                ],
                lines: [index("start_line")?, index("end_line")?],
                body: index("body_hash")?,
            })
        }
        "edges" => Ok(DiffColumns::Edges {
            kind: index("kind")?,
            line: index("line")?,
            joins: [index("source")?, index("target")?],
        }),
        _ => Ok(DiffColumns::Other),
    }
}

fn validate_diff_row(row: &rusqlite::Row<'_>, columns: &DiffColumns) -> Result<()> {
    match columns {
        DiffColumns::Symbols { text, lines, body } => {
            for &idx in text {
                row.get_ref(idx)?.as_str()?;
            }
            for &idx in lines {
                row.get_ref(idx)?.as_i64()?;
            }
            if !matches!(row.get_ref(*body)?, ValueRef::Null) {
                row.get_ref(*body)?.as_str()?;
            }
        }
        DiffColumns::Edges { kind, line, joins } => {
            for &idx in joins {
                row.get_ref(idx)?.as_str()?;
            }
            if row.get_ref(*kind)?.as_str()? == "calls" {
                row.get_ref(*line)?.as_i64()?;
            }
        }
        DiffColumns::Other => {}
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn digest_encoding_binds_both_versions() {
        let hash = |format, schema| {
            let mut bytes = digest_prefix(format, schema);
            field(&mut bytes, b'S', b"same content");
            Sha256::digest(bytes)
        };
        let baseline = hash(FORMAT_VERSION, SCHEMA_VERSION);
        assert_ne!(baseline, hash(FORMAT_VERSION + 1, SCHEMA_VERSION));
        assert_ne!(baseline, hash(FORMAT_VERSION, SCHEMA_VERSION + 1));
    }
}
