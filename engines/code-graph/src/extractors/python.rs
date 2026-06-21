//! Python extractor (chunk-1 regex imports/calls; AST in chunk 2).

use std::collections::HashSet;

use anyhow::Result;
use regex::Regex;

use crate::extractors::common::{collect_calls, extract_tree_sitter_symbols};
use crate::model::{FileGraph, Lang};

pub fn extract_python(path: &str, content: &str, content_hash: &str) -> Result<FileGraph> {
    let import_re =
        Regex::new(r"^\s*(?:from\s+([A-Za-z0-9_\.]+)\s+import|import\s+([A-Za-z0-9_\.]+))")?;
    let call_re = Regex::new(r"\b([A-Za-z_][A-Za-z0-9_\.]*)\s*\(")?;
    let lines: Vec<&str> = content.lines().collect();
    let mut imports = Vec::new();

    for (idx, line) in lines.iter().enumerate() {
        let line_no = idx + 1;
        if let Some(cap) = import_re.captures(line) {
            let module = cap
                .get(1)
                .or_else(|| cap.get(2))
                .map(|m| m.as_str().to_string())
                .unwrap_or_default();
            if !module.is_empty() {
                imports.push((module, line_no));
            }
        }
    }

    let symbols = extract_tree_sitter_symbols(
        path,
        content,
        content_hash,
        tree_sitter_python::LANGUAGE.into(),
        Lang::Python,
    )?;
    let calls = collect_calls(path, &lines, &symbols, &call_re, python_call_skip());
    Ok(FileGraph {
        path: path.to_string(),
        language: String::new(),
        hash: content_hash.to_string(),
        size: 0,
        modified_at: 0,
        symbols,
        imports,
        calls,
    })
}

fn python_call_skip() -> HashSet<&'static str> {
    HashSet::from([
        "if",
        "for",
        "while",
        "with",
        "return",
        "print",
        "len",
        "str",
        "int",
        "float",
        "bool",
        "list",
        "dict",
        "set",
        "tuple",
        "super",
        "isinstance",
        "range",
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_extractor_finds_symbols_imports_and_calls() {
        let source = r#"
import os

class Runner:
    def start(self):
        helper()

def helper():
    return os.getcwd()
"#;
        let graph = extract_python("src/demo.py", source, "hash").unwrap();
        let names: Vec<&str> = graph.symbols.iter().map(|s| s.name.as_str()).collect();
        assert!(names.contains(&"Runner"));
        assert!(names.contains(&"start"));
        assert!(names.contains(&"helper"));
        assert_eq!(graph.imports[0].0, "os");
        assert!(graph.calls.iter().any(|c| c.target_name == "helper"));
    }
}
