//! TypeScript/JavaScript extractor (chunk-1 regex imports/calls; AST in chunk 2).

use std::collections::HashSet;

use anyhow::Result;
use regex::Regex;

use crate::extractors::common::{collect_calls, extract_tree_sitter_symbols};
use crate::model::{FileGraph, Lang};

pub fn extract_typescript(path: &str, content: &str, content_hash: &str) -> Result<FileGraph> {
    let import_re =
        Regex::new(r#"^\s*import.*?from\s+['"]([^'"]+)['"]|^\s*import\s+['"]([^'"]+)['"]"#)?;
    let call_re = Regex::new(r"\b([A-Za-z_$][A-Za-z0-9_$\.]*)\s*\(")?;
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

    let language = if path.ends_with(".tsx") {
        tree_sitter_typescript::LANGUAGE_TSX.into()
    } else if path.ends_with(".ts") {
        tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into()
    } else {
        tree_sitter_javascript::LANGUAGE.into()
    };
    let symbols =
        extract_tree_sitter_symbols(path, content, content_hash, language, Lang::TypeScript)?;
    let calls = collect_calls(path, &lines, &symbols, &call_re, js_call_skip());
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

fn js_call_skip() -> HashSet<&'static str> {
    HashSet::from([
        "if",
        "for",
        "while",
        "switch",
        "return",
        "console",
        "log",
        "map",
        "filter",
        "reduce",
        "then",
        "catch",
        "setTimeout",
        "Promise",
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn typescript_extractor_finds_common_symbol_shapes() {
        let source = r#"
import { x } from "./x";

export class Runner {}
export function start() {
  helper();
}
const helper = () => x();
"#;
        let graph = extract_typescript("src/demo.ts", source, "hash").unwrap();
        let names: Vec<&str> = graph.symbols.iter().map(|s| s.name.as_str()).collect();
        assert!(names.contains(&"Runner"));
        assert!(names.contains(&"start"));
        assert!(names.contains(&"helper"));
        assert_eq!(graph.imports[0].0, "./x");
        assert!(graph.calls.iter().any(|c| c.target_name == "helper"));
    }
}
