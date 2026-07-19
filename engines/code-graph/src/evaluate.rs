//! Dry-run extraction: what a sync WOULD produce, with zero database writes.
//!
//! Borrowed stance from CocoIndex's `evaluate`: materialize the transform's
//! output somewhere diffable without touching the target store. The extractors
//! are pure functions of file content, so this just runs them and reports.
//! Pair it with an extractor-fingerprint bump: evaluate before, evaluate
//! after, diff the JSON.

use std::path::Path;

use anyhow::{Result, bail};
use serde::Serialize;

use crate::extractors::{index_file, language_for};
use crate::model::FileGraph;
use crate::store::list_indexable;

#[derive(Debug, Serialize)]
pub struct EvaluateReport {
    pub root: String,
    pub files: Vec<EvaluatedFile>,
    pub totals: EvaluateTotals,
}

#[derive(Debug, Default, Serialize)]
pub struct EvaluateTotals {
    pub files: usize,
    pub symbols: usize,
    pub imports: usize,
    pub calls: usize,
}

#[derive(Debug, Serialize)]
pub struct EvaluatedFile {
    pub path: String,
    pub language: String,
    pub symbols: Vec<EvaluatedSymbol>,
    pub imports: Vec<EvaluatedImport>,
    pub calls: Vec<EvaluatedCall>,
}

#[derive(Debug, Serialize)]
pub struct EvaluatedSymbol {
    pub kind: String,
    pub name: String,
    pub qualified_name: String,
    pub start_line: usize,
    pub end_line: usize,
    pub signature: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub container: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct EvaluatedImport {
    pub module: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub local_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub imported_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub alias: Option<String>,
    pub line: usize,
}

#[derive(Debug, Serialize)]
pub struct EvaluatedCall {
    /// Qualified name of the enclosing symbol making the call.
    pub source: String,
    pub target_name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub qualifier: Option<String>,
    pub kind: String,
    pub line: usize,
}

/// Extract `target` (a file or a directory walked with sync's rules) and
/// report what sync would store. Opens no database and writes nothing.
pub fn evaluate_path(target: &Path) -> Result<EvaluateReport> {
    let canonical = target
        .canonicalize()
        .unwrap_or_else(|_| target.to_path_buf());
    let mut files = Vec::new();
    if canonical.is_file() {
        let Some(lang) = language_for(&canonical) else {
            bail!(
                "unsupported file type: {} (python, typescript/javascript, rust, go)",
                canonical.display()
            );
        };
        let root = canonical.parent().unwrap_or(Path::new("."));
        files.push(evaluated(index_file(root, &canonical, lang)?));
    } else {
        crate::store::guard_unsafe_root(&canonical)?;
        for entry in list_indexable(&canonical)? {
            files.push(evaluated(index_file(&canonical, &entry.path, entry.lang)?));
        }
    }
    files.sort_by(|a, b| a.path.cmp(&b.path));
    let totals = EvaluateTotals {
        files: files.len(),
        symbols: files.iter().map(|f| f.symbols.len()).sum(),
        imports: files.iter().map(|f| f.imports.len()).sum(),
        calls: files.iter().map(|f| f.calls.len()).sum(),
    };
    Ok(EvaluateReport {
        root: canonical.to_string_lossy().into_owned(),
        files,
        totals,
    })
}

fn evaluated(graph: FileGraph) -> EvaluatedFile {
    // Calls reference enclosing symbols by id; report them by qualified name.
    let name_of = |id: &str| {
        graph
            .symbols
            .iter()
            .find(|symbol| symbol.id == id)
            .map_or_else(|| id.to_string(), |symbol| symbol.qualified_name.clone())
    };
    EvaluatedFile {
        symbols: graph
            .symbols
            .iter()
            .map(|s| EvaluatedSymbol {
                kind: s.kind.clone(),
                name: s.name.clone(),
                qualified_name: s.qualified_name.clone(),
                start_line: s.start_line,
                end_line: s.end_line,
                signature: s.signature.clone(),
                container: s.container.clone(),
            })
            .collect(),
        imports: graph
            .imports
            .iter()
            .map(|i| EvaluatedImport {
                module: i.module.clone(),
                local_name: i.local_name.clone(),
                imported_name: i.imported_name.clone(),
                alias: i.alias.clone(),
                line: i.line,
            })
            .collect(),
        calls: graph
            .calls
            .iter()
            .map(|c| EvaluatedCall {
                source: name_of(&c.source_id),
                target_name: c.target_name.clone(),
                qualifier: c.qualifier.clone(),
                kind: c.kind.as_str().to_string(),
                line: c.line,
            })
            .collect(),
        path: graph.path,
        language: graph.language,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn evaluate_extracts_a_single_file_without_a_database() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("app.py");
        fs::write(
            &file,
            "def outer():\n    helper()\n\ndef helper():\n    pass\n",
        )
        .unwrap();

        let report = evaluate_path(&file).unwrap();

        assert_eq!(report.totals.files, 1);
        assert_eq!(report.totals.symbols, 2);
        assert_eq!(report.totals.calls, 1);
        let calls = &report.files[0].calls;
        assert_eq!(calls[0].source, "outer");
        assert_eq!(calls[0].target_name, "helper");
        // The dry run must not create a database or graph dir.
        assert!(!dir.path().join(".graphtrail").exists());
    }

    #[test]
    fn evaluate_walks_a_directory_with_sync_rules() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("a.py"), "def a():\n    pass\n").unwrap();
        fs::create_dir(dir.path().join("node_modules")).unwrap();
        fs::write(
            dir.path().join("node_modules").join("skip.js"),
            "function skipped() {}\n",
        )
        .unwrap();

        let report = evaluate_path(dir.path()).unwrap();

        assert_eq!(report.totals.files, 1);
        assert_eq!(report.files[0].path, "a.py");
        assert!(!dir.path().join(".graphtrail").exists());
    }

    #[test]
    fn evaluate_rejects_unsupported_single_files() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("notes.md");
        fs::write(&file, "# not code\n").unwrap();

        assert!(evaluate_path(&file).is_err());
    }
}
