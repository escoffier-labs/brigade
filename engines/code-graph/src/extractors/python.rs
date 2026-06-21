//! Python extractor: AST-based symbols, imports, and call edges.

use anyhow::Result;
use tree_sitter::Node as TsNode;

use crate::extractors::common::{LangSpec, extract_with, node_text};
use crate::model::FileGraph;

/// Ubiquitous builtins that would only ever produce noise edges; AST already excludes keywords.
const PY_SKIP: &[&str] = &[
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
];

struct PythonSpec;

impl LangSpec for PythonSpec {
    fn symbol_candidate<'t>(&self, node: TsNode<'t>) -> Option<(&'static str, TsNode<'t>)> {
        match node.kind() {
            "class_definition" => node.child_by_field_name("name").map(|name| ("class", name)),
            "function_definition" => node
                .child_by_field_name("name")
                .map(|name| ("function", name)),
            _ => None,
        }
    }

    fn collect_import(&self, node: TsNode<'_>, source: &[u8], out: &mut Vec<(String, usize)>) {
        let line = node.start_position().row + 1;
        match node.kind() {
            "import_statement" => {
                let mut cursor = node.walk();
                for child in node.named_children(&mut cursor) {
                    let module = match child.kind() {
                        "dotted_name" => node_text(child, source),
                        "aliased_import" => child
                            .child_by_field_name("name")
                            .map(|name| node_text(name, source))
                            .unwrap_or_default(),
                        _ => continue,
                    };
                    if !module.is_empty() {
                        out.push((module, line));
                    }
                }
            }
            "import_from_statement" => {
                if let Some(module) = node.child_by_field_name("module_name") {
                    let text = node_text(module, source);
                    if !text.is_empty() {
                        out.push((text, line));
                    }
                }
            }
            _ => {}
        }
    }

    fn call_target(&self, node: TsNode<'_>, source: &[u8]) -> Option<String> {
        if node.kind() != "call" {
            return None;
        }
        let func = node.child_by_field_name("function")?;
        let name = match func.kind() {
            "identifier" => node_text(func, source),
            "attribute" => node_text(func.child_by_field_name("attribute")?, source),
            _ => return None,
        };
        if name.is_empty() || PY_SKIP.contains(&name.as_str()) {
            return None;
        }
        Some(name)
    }
}

pub fn extract_python(path: &str, content: &str, content_hash: &str) -> Result<FileGraph> {
    extract_with(
        &PythonSpec,
        path,
        content,
        content_hash,
        tree_sitter_python::LANGUAGE.into(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn graph(source: &str) -> FileGraph {
        extract_python("src/demo.py", source, "hash").unwrap()
    }

    #[test]
    fn python_extractor_finds_symbols_imports_and_calls() {
        let g = graph(
            r#"
import os

class Runner:
    def start(self):
        helper()

def helper():
    return os.getcwd()
"#,
        );
        let names: Vec<&str> = g.symbols.iter().map(|s| s.name.as_str()).collect();
        assert!(names.contains(&"Runner"));
        assert!(names.contains(&"start"));
        assert!(names.contains(&"helper"));
        assert_eq!(g.imports[0].0, "os");
        assert!(g.calls.iter().any(|c| c.target_name == "helper"));
    }

    #[test]
    fn python_import_from_plain_and_alias() {
        let g = graph(
            r#"
from a.b import c
import os
import numpy as np
"#,
        );
        let modules: Vec<&str> = g.imports.iter().map(|(m, _)| m.as_str()).collect();
        assert!(modules.contains(&"a.b"));
        assert!(modules.contains(&"os"));
        assert!(modules.contains(&"numpy"));
    }

    #[test]
    fn python_call_attributes_to_enclosing_symbol() {
        let g = graph(
            r#"
class Runner:
    def start(self):
        self.svc.method()
"#,
        );
        let start = g
            .symbols
            .iter()
            .find(|s| s.name == "start")
            .expect("start symbol");
        let call = g
            .calls
            .iter()
            .find(|c| c.target_name == "method")
            .expect("method call");
        assert_eq!(call.source_id, start.id);
        assert_eq!(call.source_file, "src/demo.py");
    }

    #[test]
    fn python_module_level_call_dropped() {
        let g = graph("foo()\n");
        assert!(g.calls.iter().all(|c| c.target_name != "foo"));
    }

    #[test]
    fn python_builtins_skipped() {
        let g = graph(
            r#"
def f(xs):
    print(len(xs))
    return work(xs)
"#,
        );
        assert!(g.calls.iter().all(|c| c.target_name != "print"));
        assert!(g.calls.iter().all(|c| c.target_name != "len"));
        assert!(g.calls.iter().any(|c| c.target_name == "work"));
    }
}
