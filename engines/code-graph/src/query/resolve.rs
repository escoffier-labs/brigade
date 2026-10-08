//! Seed resolution for graph queries (callers, callees, impact).
//!
//! A graph query is about one named symbol, so the seed is resolved by exact
//! match first, in this order: symbol id, qualified name (`Class.method`),
//! name path (`path/to/file.py::name` or `path/to/file.py:name`), then bare
//! name. Only when nothing matches exactly does it fall back to the fuzzy
//! prefix search that `search` and `context` use, and the result says so.

use anyhow::Result;
use rusqlite::{Connection, params};

use crate::model::{SearchRow, SymbolCandidate};
use crate::query::search::search_symbols;

/// How many fuzzy hits seed a graph query when nothing matches exactly.
pub const FUZZY_SEED_LIMIT: usize = 20;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ResolutionMethod {
    Id,
    QualifiedName,
    NamePath,
    Name,
    Fuzzy,
    None,
}

impl ResolutionMethod {
    pub fn as_str(self) -> &'static str {
        match self {
            ResolutionMethod::Id => "id",
            ResolutionMethod::QualifiedName => "qualified_name",
            ResolutionMethod::NamePath => "name_path",
            ResolutionMethod::Name => "name",
            ResolutionMethod::Fuzzy => "fuzzy",
            ResolutionMethod::None => "none",
        }
    }
}

#[derive(Debug, Clone)]
pub struct SymbolResolution {
    pub method: ResolutionMethod,
    /// Every symbol the winning resolution step matched, in file/line order.
    pub candidates: Vec<SymbolCandidate>,
}

impl SymbolResolution {
    pub fn is_fuzzy(&self) -> bool {
        self.method == ResolutionMethod::Fuzzy
    }

    pub fn is_ambiguous(&self) -> bool {
        self.candidates.len() > 1
    }
}

pub fn resolve_graph_symbol(conn: &Connection, query: &str) -> Result<SymbolResolution> {
    let query = query.trim();
    if query.is_empty() {
        return Ok(resolved(ResolutionMethod::None, Vec::new()));
    }
    let exact_steps: [(ResolutionMethod, &str); 2] = [
        (ResolutionMethod::Id, "id = ?1"),
        (ResolutionMethod::QualifiedName, "qualified_name = ?1"),
    ];
    for (method, predicate) in exact_steps {
        let rows = select_symbols(conn, predicate, params![query])?;
        if !rows.is_empty() {
            return Ok(resolved(method, rows));
        }
    }
    if let Some(rows) = name_path_symbols(conn, query)? {
        return Ok(resolved(ResolutionMethod::NamePath, rows));
    }
    let rows = select_symbols(conn, "name = ?1", params![query])?;
    if !rows.is_empty() {
        return Ok(resolved(ResolutionMethod::Name, rows));
    }
    let fuzzy: Vec<SymbolCandidate> = search_symbols(conn, query, FUZZY_SEED_LIMIT)?
        .into_iter()
        .map(candidate_from_search_row)
        .collect();
    if fuzzy.is_empty() {
        return Ok(resolved(ResolutionMethod::None, Vec::new()));
    }
    Ok(resolved(ResolutionMethod::Fuzzy, fuzzy))
}

fn resolved(method: ResolutionMethod, candidates: Vec<SymbolCandidate>) -> SymbolResolution {
    SymbolResolution { method, candidates }
}

/// `path::name` or `path:name`, where `name` is a qualified or bare name in
/// that file. Qualified names use `.` separators, so `::` and `:` are free to
/// split the path from the name.
fn name_path_symbols(conn: &Connection, query: &str) -> Result<Option<Vec<SymbolCandidate>>> {
    let split = query
        .split_once("::")
        .or_else(|| query.split_once(':'))
        .map(|(path, name)| (normalize_path(path), name.trim()));
    let Some((path, name)) = split else {
        return Ok(None);
    };
    if path.is_empty() || name.is_empty() {
        return Ok(None);
    }
    for predicate in [
        "file_path = ?1 AND qualified_name = ?2",
        "file_path = ?1 AND name = ?2",
    ] {
        let rows = select_symbols(conn, predicate, params![path, name])?;
        if !rows.is_empty() {
            return Ok(Some(rows));
        }
    }
    Ok(None)
}

fn normalize_path(path: &str) -> String {
    let path = path.trim().replace('\\', "/");
    path.trim_start_matches("./").trim_matches('/').to_string()
}

fn select_symbols(
    conn: &Connection,
    predicate: &str,
    params: &[&dyn rusqlite::ToSql],
) -> Result<Vec<SymbolCandidate>> {
    let sql = format!(
        "SELECT id, kind, name, qualified_name, file_path, start_line, end_line
         FROM symbols WHERE {predicate}
         ORDER BY file_path, start_line, id"
    );
    let mut stmt = conn.prepare(&sql)?;
    let mapped = stmt.query_map(params, |row| {
        Ok(SymbolCandidate {
            id: row.get(0)?,
            kind: row.get(1)?,
            name: row.get(2)?,
            qualified_name: row.get(3)?,
            file_path: row.get(4)?,
            start_line: row.get::<_, i64>(5)? as usize,
            end_line: row.get::<_, i64>(6)? as usize,
        })
    })?;
    let mut rows = Vec::new();
    for row in mapped {
        rows.push(row?);
    }
    Ok(rows)
}

fn candidate_from_search_row(row: SearchRow) -> SymbolCandidate {
    SymbolCandidate {
        id: row.id,
        kind: row.kind,
        name: row.name,
        qualified_name: row.qualified_name,
        file_path: row.file_path,
        start_line: row.start_line,
        end_line: row.end_line,
    }
}
