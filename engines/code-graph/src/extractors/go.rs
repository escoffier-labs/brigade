//! Go extractor: AST-based symbols, imports, and call edges.

use anyhow::Result;
use tree_sitter::Node as TsNode;

use crate::extractors::common::{LangSpec, extract_with, node_text};
use crate::model::FileGraph;

/// Go builtins that parse as calls but never resolve to a project symbol.
const GO_SKIP: &[&str] = &[
    "make", "len", "cap", "append", "panic", "recover", "new", "print", "println", "close",
    "delete", "copy",
];

struct GoSpec;

impl LangSpec for GoSpec {
    fn symbol_candidate<'t>(&self, node: TsNode<'t>) -> Option<(&'static str, TsNode<'t>)> {
        match node.kind() {
            "function_declaration" => node.child_by_field_name("name").map(|n| ("function", n)),
            "method_declaration" => node.child_by_field_name("name").map(|n| ("method", n)),
            "type_spec" => node.child_by_field_name("name").map(|n| ("type", n)),
            _ => None,
        }
    }

    fn collect_import(&self, node: TsNode<'_>, source: &[u8], out: &mut Vec<(String, usize)>) {
        if node.kind() == "import_spec"
            && let Some(path) = node.child_by_field_name("path")
        {
            let module = node_text(path, source).trim_matches('"').to_string();
            if !module.is_empty() {
                out.push((module, node.start_position().row + 1));
            }
        }
    }

    fn call_target(&self, node: TsNode<'_>, source: &[u8]) -> Option<String> {
        if node.kind() != "call_expression" {
            return None;
        }
        let func = node.child_by_field_name("function")?;
        let name = match func.kind() {
            "identifier" => node_text(func, source),
            "selector_expression" => node_text(func.child_by_field_name("field")?, source),
            _ => return None,
        };
        if name.is_empty() || GO_SKIP.contains(&name.as_str()) {
            return None;
        }
        Some(name)
    }
}

pub fn extract_go(path: &str, content: &str, content_hash: &str) -> Result<FileGraph> {
    extract_with(
        &GoSpec,
        path,
        content,
        content_hash,
        tree_sitter_go::LANGUAGE.into(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn go_finds_symbols_imports_and_calls() {
        let g = extract_go(
            "x.go",
            r#"
package main

import "fmt"

type Runner struct{}

func helper() int {
    return 1
}

func run() int {
    fmt.Println("x")
    return helper()
}
"#,
            "hash",
        )
        .unwrap();
        let names: Vec<&str> = g.symbols.iter().map(|s| s.name.as_str()).collect();
        assert!(names.contains(&"Runner"));
        assert!(names.contains(&"helper"));
        assert!(names.contains(&"run"));
        assert!(g.imports.iter().any(|(m, _)| m == "fmt"));
        assert!(g.calls.iter().any(|c| c.target_name == "helper"));
        assert!(g.calls.iter().any(|c| c.target_name == "Println")); // selector field
    }

    #[test]
    fn go_grouped_imports_and_builtins_skipped() {
        let g = extract_go(
            "x.go",
            r#"
package main

import (
    "fmt"
    "os"
)

func run() {
    _ = make([]int, 0)
    fmt.Println(os.Args)
}
"#,
            "hash",
        )
        .unwrap();
        let modules: Vec<&str> = g.imports.iter().map(|(m, _)| m.as_str()).collect();
        assert!(modules.contains(&"fmt"));
        assert!(modules.contains(&"os"));
        assert!(g.calls.iter().all(|c| c.target_name != "make")); // builtin skipped
    }
}
