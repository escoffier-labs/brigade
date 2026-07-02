//! Go extractor: AST-based symbols, imports, and call edges.

use anyhow::Result;
use tree_sitter::Node as TsNode;

use crate::extractors::common::{LangSpec, extract_with, node_text};
use crate::model::{CallTarget, FileGraph, Import};

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

    fn symbol_container(&self, node: TsNode<'_>, source: &[u8]) -> Option<String> {
        if node.kind() != "method_declaration" {
            return None;
        }
        node.child_by_field_name("receiver")
            .map(|receiver| normalize_go_receiver(&node_text(receiver, source)))
            .filter(|receiver| !receiver.is_empty())
    }

    fn collect_import(&self, node: TsNode<'_>, source: &[u8], out: &mut Vec<Import>) {
        if node.kind() == "import_spec"
            && let Some(path) = node.child_by_field_name("path")
        {
            let module = node_text(path, source).trim_matches('"').to_string();
            if !module.is_empty() {
                let alias = node
                    .child_by_field_name("name")
                    .map(|alias| node_text(alias, source))
                    .filter(|alias| alias != "." && alias != "_");
                let local_name = alias
                    .clone()
                    .or_else(|| module.rsplit('/').next().map(str::to_string));
                out.push(Import {
                    module,
                    local_name,
                    imported_name: None,
                    alias,
                    line: node.start_position().row + 1,
                });
            }
        }
    }

    fn call_target(&self, node: TsNode<'_>, source: &[u8]) -> Option<CallTarget> {
        if node.kind() != "call_expression" {
            return None;
        }
        let func = node.child_by_field_name("function")?;
        let target = match func.kind() {
            "identifier" => CallTarget::bare(node_text(func, source)),
            "selector_expression" => CallTarget::member(
                node_text(func.child_by_field_name("field")?, source),
                func.child_by_field_name("operand")
                    .or_else(|| func.child_by_field_name("object"))
                    .map(|operand| node_text(operand, source)),
            ),
            _ => return None,
        };
        if target.name.is_empty() || GO_SKIP.contains(&target.name.as_str()) {
            return None;
        }
        Some(target)
    }
}

fn normalize_go_receiver(text: &str) -> String {
    text.trim()
        .trim_start_matches('(')
        .trim_end_matches(')')
        .split_whitespace()
        .last()
        .unwrap_or("")
        .trim_start_matches('*')
        .rsplit('.')
        .next()
        .unwrap_or("")
        .to_string()
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
        assert!(g.imports.iter().any(|import| import.module == "fmt"));
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
        let modules: Vec<&str> = g
            .imports
            .iter()
            .map(|import| import.module.as_str())
            .collect();
        assert!(modules.contains(&"fmt"));
        assert!(modules.contains(&"os"));
        assert!(g.calls.iter().all(|c| c.target_name != "make")); // builtin skipped
    }

    #[test]
    fn go_preserves_import_aliases_receivers_and_selector_qualifiers() {
        let g = extract_go(
            "x.go",
            r#"
package main

import j "encoding/json"

type Runner struct{}

func (r *Runner) Start() {
    j.Marshal(r)
}
"#,
            "hash",
        )
        .unwrap();

        let json_import = g
            .imports
            .iter()
            .find(|import| import.local_name.as_deref() == Some("j"))
            .expect("aliased go import");
        assert_eq!(json_import.module, "encoding/json");
        assert_eq!(json_import.alias.as_deref(), Some("j"));

        let start = g
            .symbols
            .iter()
            .find(|symbol| symbol.name == "Start")
            .expect("receiver method");
        assert_eq!(start.container.as_deref(), Some("Runner"));
        assert_eq!(start.qualified_name, "Runner.Start");

        let marshal = g
            .calls
            .iter()
            .find(|call| call.target_name == "Marshal")
            .expect("selector call");
        assert_eq!(marshal.qualifier.as_deref(), Some("j"));
    }
}
