//! File indexing: language detection and per-file extraction dispatch.

pub mod common;
pub mod python;
pub mod typescript;

use std::fs;
use std::path::Path;
use std::time::UNIX_EPOCH;

use anyhow::{Context, Result};

use crate::extractors::common::hex_hash;
use crate::model::{FileGraph, Lang};

/// Map a file path to its source language, or `None` if unsupported.
pub fn language_for(path: &Path) -> Option<Lang> {
    match path.extension().and_then(|e| e.to_str())? {
        "py" => Some(Lang::Python),
        "js" | "jsx" | "ts" | "tsx" => Some(Lang::TypeScript),
        _ => None,
    }
}

/// Read and extract a single file into a [`FileGraph`].
pub fn index_file(root: &Path, path: &Path, lang: Lang) -> Result<FileGraph> {
    let content =
        fs::read_to_string(path).with_context(|| format!("failed to read {}", path.display()))?;
    let rel = path
        .strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/");
    let metadata = fs::metadata(path)?;
    let modified_at = metadata
        .modified()
        .ok()
        .and_then(|m| m.duration_since(UNIX_EPOCH).ok())
        .map_or(0, |d| d.as_secs() as i64);
    let hash = hex_hash(content.as_bytes());

    let mut graph = match lang {
        Lang::Python => python::extract_python(&rel, &content, &hash)?,
        Lang::TypeScript => typescript::extract_typescript(&rel, &content, &hash)?,
    };
    graph.language = lang.db_label().to_string();
    graph.size = metadata.len();
    graph.modified_at = modified_at;
    Ok(graph)
}
