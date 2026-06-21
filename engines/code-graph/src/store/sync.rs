//! Repository sync: walk files, extract graphs, and write them transactionally.

use std::collections::HashMap;
use std::path::Path;

use anyhow::Result;
use rusqlite::{Connection, params};
use walkdir::{DirEntry, WalkDir};

use crate::extractors::{index_file, language_for};
use crate::store::db::now_ts;

#[derive(Default)]
pub struct SyncSummary {
    pub files: usize,
    pub symbols: usize,
    pub calls: usize,
    pub imports: usize,
}

pub fn sync_repo(conn: &Connection, root: &Path) -> Result<SyncSummary> {
    let root = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());
    let mut graphs = Vec::new();
    for entry in WalkDir::new(&root).into_iter().filter_entry(keep_entry) {
        let entry = entry?;
        if !entry.file_type().is_file() {
            continue;
        }
        let Some(language) = language_for(entry.path()) else {
            continue;
        };
        let graph = index_file(&root, entry.path(), language)?;
        graphs.push(graph);
    }

    let tx = conn.unchecked_transaction()?;
    let changed_paths: Vec<String> = graphs.iter().map(|g| g.path.clone()).collect();
    for path in &changed_paths {
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
            if let Some(targets) = name_index.get(&call.target_name) {
                for target in targets.iter().take(8) {
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
    }
    tx.commit()?;

    Ok(SyncSummary {
        files: graphs.len(),
        symbols: graphs.iter().map(|g| g.symbols.len()).sum(),
        imports: graphs.iter().map(|g| g.imports.len()).sum(),
        calls: inserted_calls,
    })
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

fn load_name_index(conn: &Connection) -> Result<HashMap<String, Vec<String>>> {
    let mut stmt = conn.prepare("SELECT name, id FROM symbols")?;
    let rows = stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    let mut map: HashMap<String, Vec<String>> = HashMap::new();
    for row in rows {
        let (name, id) = row?;
        map.entry(name).or_default().push(id);
    }
    Ok(map)
}
