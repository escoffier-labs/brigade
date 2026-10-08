//! File indexing: language detection and per-file extraction dispatch.

pub mod astro;
pub mod common;
pub mod go;
pub mod python;
pub mod rust;
pub mod typescript;

use std::fs;
use std::path::Path;
use std::time::UNIX_EPOCH;

use anyhow::Result;

use crate::extractors::common::hex_hash;
use crate::model::{FileGraph, Lang};

/// Map a file path to its source language, or `None` if unsupported.
pub fn language_for(path: &Path) -> Option<Lang> {
    match path.extension().and_then(|e| e.to_str())? {
        "py" => Some(Lang::Python),
        "js" | "jsx" | "ts" | "tsx" => Some(Lang::TypeScript),
        "astro" => Some(Lang::Astro),
        "rs" => Some(Lang::Rust),
        "go" => Some(Lang::Go),
        _ => None,
    }
}

/// Current extractor fingerprint for a supported source language.
pub fn extractor_fingerprint_for(lang: Lang) -> &'static str {
    match lang {
        Lang::Python => python::EXTRACTOR_FINGERPRINT,
        Lang::TypeScript => typescript::EXTRACTOR_FINGERPRINT,
        Lang::Astro => astro::EXTRACTOR_FINGERPRINT,
        Lang::Rust => rust::EXTRACTOR_FINGERPRINT,
        Lang::Go => go::EXTRACTOR_FINGERPRINT,
    }
}

/// Why a file could not be indexed. The text form is stored in
/// `skipped_files.reason` and reported by `doctor` and `evaluate`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SkipReason {
    /// The file is not valid UTF-8 (Latin-1 fixtures, stray bytes, binaries).
    UnreadableUtf8,
    /// Reading the file or its metadata failed (permissions, races).
    IoError,
    /// The extractor could not produce a parse tree for the file.
    ParseError,
    /// Minified or generated content: a line over [`MINIFIED_MAX_LINE_BYTES`]
    /// or an average line over [`MINIFIED_MAX_AVG_LINE_BYTES`].
    Minified,
}

impl SkipReason {
    pub fn as_str(self) -> &'static str {
        match self {
            SkipReason::UnreadableUtf8 => "unreadable_utf8",
            SkipReason::IoError => "io_error",
            SkipReason::ParseError => "parse_error",
            SkipReason::Minified => "minified",
        }
    }
}

/// Files at or below this size are never treated as minified.
const MINIFIED_MIN_FILE_BYTES: usize = 5_000;
/// A line longer than this marks a minified or generated file.
const MINIFIED_MAX_LINE_BYTES: usize = 5_000;
/// An average line longer than this marks a minified or generated file.
const MINIFIED_MAX_AVG_LINE_BYTES: usize = 500;

/// Whether `bytes` look like minified or generated source. It runs on the
/// bytes `index_file` already read, so freshness checks never pay for it.
fn looks_minified(bytes: &[u8]) -> bool {
    if bytes.len() <= MINIFIED_MIN_FILE_BYTES {
        return false;
    }
    let longest = bytes
        .split(|byte| *byte == b'\n')
        .map(<[u8]>::len)
        .max()
        .unwrap_or(0);
    let mut lines = bytes.iter().filter(|byte| **byte == b'\n').count();
    if bytes.last().is_some_and(|byte| *byte != b'\n') {
        lines += 1;
    }
    longest > MINIFIED_MAX_LINE_BYTES || bytes.len() / lines.max(1) > MINIFIED_MAX_AVG_LINE_BYTES
}

/// A per-file indexing failure. Sync and evaluate skip the file and record
/// this instead of aborting the whole run.
#[derive(Debug)]
pub struct IndexFailure {
    pub reason: SkipReason,
    pub detail: String,
}

impl IndexFailure {
    fn new(reason: SkipReason, detail: impl std::fmt::Display) -> Self {
        Self {
            reason,
            detail: detail.to_string(),
        }
    }
}

impl std::fmt::Display for IndexFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.reason.as_str(), self.detail)
    }
}

impl std::error::Error for IndexFailure {}

/// Read and extract a single file into a [`FileGraph`].
///
/// Files that are not valid UTF-8 are skipped, not decoded lossily, so every
/// stored line number refers to the real file. Minified or generated content
/// is skipped too and recorded with its stat, so it is rechecked only when the
/// file changes.
pub fn index_file(root: &Path, path: &Path, lang: Lang) -> Result<FileGraph, IndexFailure> {
    let bytes = fs::read(path).map_err(|err| IndexFailure::new(SkipReason::IoError, err))?;
    let metadata = fs::metadata(path).map_err(|err| IndexFailure::new(SkipReason::IoError, err))?;
    if looks_minified(&bytes) {
        return Err(IndexFailure::new(
            SkipReason::Minified,
            "minified or generated source (very long lines)",
        ));
    }
    let content = String::from_utf8(bytes)
        .map_err(|err| IndexFailure::new(SkipReason::UnreadableUtf8, err.utf8_error()))?;
    let rel = path
        .strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/");
    let modified_at = metadata
        .modified()
        .ok()
        .and_then(|m| m.duration_since(UNIX_EPOCH).ok())
        .map_or(0, |d| d.as_secs() as i64);
    let hash = hex_hash(content.as_bytes());

    let extracted: Result<FileGraph> = match lang {
        Lang::Python => python::extract_python(&rel, &content, &hash),
        Lang::TypeScript => typescript::extract_typescript(&rel, &content, &hash),
        Lang::Astro => astro::extract_astro(&rel, &content, &hash),
        Lang::Rust => rust::extract_rust(&rel, &content, &hash),
        Lang::Go => go::extract_go(&rel, &content, &hash),
    };
    let mut graph =
        extracted.map_err(|err| IndexFailure::new(SkipReason::ParseError, format!("{err:#}")))?;
    graph.language = lang.db_label().to_string();
    graph.size = metadata.len();
    graph.modified_at = modified_at;
    Ok(graph)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    #[test]
    fn language_for_maps_astro_and_skips_css() {
        assert!(matches!(
            language_for(Path::new("src/layouts/Page.astro")),
            Some(Lang::Astro)
        ));
        assert!(language_for(Path::new("src/styles/global.css")).is_none());
        assert!(matches!(
            language_for(Path::new("src/app.ts")),
            Some(Lang::TypeScript)
        ));
    }
}
