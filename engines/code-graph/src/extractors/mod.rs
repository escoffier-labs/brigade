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

use anyhow::{Context, Result};

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
        Lang::Astro => astro::extract_astro(&rel, &content, &hash)?,
        Lang::Rust => rust::extract_rust(&rel, &content, &hash)?,
        Lang::Go => go::extract_go(&rel, &content, &hash)?,
    };
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
