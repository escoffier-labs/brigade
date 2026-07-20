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
    pub body_hash: Option<String>,
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

impl CallKind {
    /// Stable text form used by the `pending_calls.kind` column.
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            CallKind::Bare => "bare",
            CallKind::Member => "member",
            CallKind::Scoped => "scoped",
        }
    }

    pub(crate) fn parse(value: &str) -> Option<CallKind> {
        match value {
            "bare" => Some(CallKind::Bare),
            "member" => Some(CallKind::Member),
            "scoped" => Some(CallKind::Scoped),
            _ => None,
        }
    }
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
    pub hops: usize,
    /// Resolution-path confidence written at sync time; absent on marker rows
    /// and on databases indexed before the column existed.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub confidence: Option<f64>,
}

#[derive(Debug, Clone, Serialize)]
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

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct PendingChanges {
    pub new_files: usize,
    pub changed_files: usize,
    pub deleted_files: usize,
    pub fingerprint_stale: usize,
}

impl PendingChanges {
    pub fn is_empty(self) -> bool {
        self.new_files == 0
            && self.changed_files == 0
            && self.deleted_files == 0
            && self.fingerprint_stale == 0
    }
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct IgnoredSummary {
    pub hardcoded_floor: usize,
    pub gitignore: usize,
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

/// A single node (symbol) in a graph diff. Compact by design so a diff stays
/// small enough to attach to a Brigade receipt.
#[derive(Debug, Clone, Serialize)]
pub struct DiffNode {
    pub kind: String,
    pub qualified_name: String,
    pub file_path: String,
    pub start_line: usize,
    pub end_line: usize,
    pub signature: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub previous: Option<DiffNodePrevious>,
}

#[derive(Debug, Clone, Serialize)]
pub struct DiffNodePrevious {
    pub start_line: usize,
    pub signature: String,
}

/// A single call edge in a graph diff, canonicalized to symbol names + file so
/// it is stable across re-index (symbol ids bake in line numbers).
#[derive(Debug, Clone, Serialize)]
pub struct DiffEdge {
    pub source: String,
    pub source_file: String,
    pub target: String,
    pub target_file: String,
    pub line: usize,
}

/// Counts for a quick receipt-friendly summary line.
#[derive(Debug, Serialize)]
pub struct GraphDiffCounts {
    pub added_nodes: usize,
    pub removed_nodes: usize,
    pub changed_nodes: usize,
    /// Raw added call-edge rows, line-sensitive.
    pub added_edges: usize,
    /// Raw removed call-edge rows, line-sensitive.
    pub removed_edges: usize,
    /// Added call-edge count after canceling pairs that only moved lines.
    pub added_edges_line_insensitive: usize,
    /// Removed call-edge count after canceling pairs that only moved lines.
    pub removed_edges_line_insensitive: usize,
}

/// Structural diff of two code graphs (before -> after). Nodes are keyed by
/// `(file_path, qualified_name, kind)` so a symbol that only moves lines is not
/// a spurious remove+add; a node is `changed` when that key survives but its
/// signature, line-span, or v3 body hash differs. Edges are the `calls` set,
/// diffed both ways.
#[derive(Debug, Serialize)]
pub struct GraphDiff {
    pub schema_version: u32,
    pub summary: GraphDiffCounts,
    pub added_nodes: Vec<DiffNode>,
    pub removed_nodes: Vec<DiffNode>,
    pub changed_nodes: Vec<DiffNode>,
    pub added_edges: Vec<DiffEdge>,
    pub removed_edges: Vec<DiffEdge>,
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
