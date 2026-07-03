//! Shared plain-data types used across extractors, store, and query layers.

use std::collections::BTreeMap;

use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct Symbol {
    pub id: String,
    pub kind: String,
    pub name: String,
    pub qualified_name: String,
    pub file_path: String,
    pub start_line: usize,
    pub end_line: usize,
    pub signature: String,
    pub container: Option<String>,
    pub content_hash: String,
}

#[derive(Debug, Clone)]
pub struct Import {
    pub module: String,
    pub local_name: Option<String>,
    pub imported_name: Option<String>,
    pub alias: Option<String>,
    pub line: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CallKind {
    Bare,
    Member,
    Scoped,
}

#[derive(Debug)]
pub struct CallTarget {
    pub name: String,
    pub qualifier: Option<String>,
    pub kind: CallKind,
}

impl CallTarget {
    pub fn bare(name: String) -> Self {
        Self {
            name,
            qualifier: None,
            kind: CallKind::Bare,
        }
    }

    pub fn member(name: String, qualifier: Option<String>) -> Self {
        Self {
            name,
            qualifier,
            kind: CallKind::Member,
        }
    }

    pub fn scoped(name: String, qualifier: Option<String>) -> Self {
        Self {
            name,
            qualifier,
            kind: CallKind::Scoped,
        }
    }
}

#[derive(Debug)]
pub struct PendingCall {
    pub source_id: String,
    pub target_name: String,
    pub qualifier: Option<String>,
    pub kind: CallKind,
    pub line: usize,
    /// File of the calling symbol; used for same-file-first edge resolution.
    pub source_file: String,
}

#[derive(Debug)]
pub struct FileGraph {
    pub path: String,
    pub language: String,
    pub hash: String,
    pub size: u64,
    pub modified_at: i64,
    pub symbols: Vec<Symbol>,
    pub imports: Vec<Import>,
    pub calls: Vec<PendingCall>,
}

#[derive(Debug, Serialize)]
pub struct EdgeRow {
    pub source_id: String,
    pub source: String,
    pub target_id: String,
    pub target: String,
    pub kind: String,
    pub line: Option<usize>,
    pub source_file: String,
    pub target_file: String,
}

#[derive(Debug, Serialize)]
pub struct SearchRow {
    pub id: String,
    pub kind: String,
    pub name: String,
    pub qualified_name: String,
    pub file_path: String,
    pub start_line: usize,
    pub end_line: usize,
    pub signature: String,
    pub score: f64,
}

#[derive(Debug, Serialize)]
pub struct FileNeighbor {
    pub file_path: String,
    pub incoming_edges: i64,
    pub outgoing_edges: i64,
}

#[derive(Debug, Serialize)]
pub struct Stats {
    pub schema_version: u32,
    pub files: i64,
    pub symbols: i64,
    pub edges: i64,
    pub imports: i64,
    pub synced_at: Option<String>,
    pub tool_version: Option<String>,
    pub language_files: BTreeMap<String, i64>,
}

#[derive(Debug, Serialize)]
pub struct ContextPack {
    /// Version of the JSON pack shape, so consumers can detect format changes.
    pub schema_version: u32,
    pub task: String,
    pub entry_points: Vec<SearchRow>,
    pub callers: Vec<EdgeRow>,
    pub callees: Vec<EdgeRow>,
    pub related_files: Vec<String>,
}

#[derive(Clone, Copy)]
pub enum Direction {
    Incoming,
    Outgoing,
}

/// Source language of an indexed file.
#[derive(Clone, Copy)]
pub enum Lang {
    Python,
    TypeScript,
    Rust,
    Go,
}

impl Lang {
    /// The language label stored in the `files` table (kept stable for DB compatibility).
    pub fn db_label(self) -> &'static str {
        match self {
            Lang::Python => "python",
            Lang::TypeScript => "typescript",
            Lang::Rust => "rust",
            Lang::Go => "go",
        }
    }
}
