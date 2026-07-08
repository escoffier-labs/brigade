//! Python extractor: AST-based symbols, imports, and call edges.

use anyhow::Result;
use tree_sitter::Node as TsNode;

use crate::extractors::common::{LangSpec, extract_with, node_text};
use crate::model::{CallTarget, FileGraph, Import};

/// Bump when Python extraction output can change for the same file content.
pub const EXTRACTOR_FINGERPRINT: &str = "python-extractor-v1";

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

    fn collect_import(&self, node: TsNode<'_>, source: &[u8], out: &mut Vec<Import>) {
        let line = node.start_position().row + 1;
        match node.kind() {
            "import_statement" => {
                let mut cursor = node.walk();
                for child in node.named_children(&mut cursor) {
                    let (module, alias) = match child.kind() {
                        "dotted_name" => (node_text(child, source), None),
                        "aliased_import" => {
                            let module = child
                                .child_by_field_name("name")
                                .map(|name| node_text(name, source))
                                .unwrap_or_default();
                            let alias = child
                                .child_by_field_name("alias")
                                .map(|alias| node_text(alias, source));
                            (module, alias)
                        }
                        _ => continue,
                    };
                    if !module.is_empty() {
                        let local_name = alias
                            .clone()
                            .or_else(|| module.split('.').next().map(str::to_string));
                        out.push(Import {
                            module,
                            local_name,
                            imported_name: None,
                            alias,
                            line,
                        });
                    }
                }
            }
            "import_from_statement" => {
                if let Some(module) = node.child_by_field_name("module_name") {
                    let module_text = node_text(module, source);
                    if module_text.is_empty() {
                        return;
                    }
                    let mut cursor = node.walk();
                    for child in node.named_children(&mut cursor) {
                        if child == module {
                            continue;
                        }
                        let (imported_name, alias) = match child.kind() {
                            "dotted_name" | "identifier" => (node_text(child, source), None),
                            "aliased_import" => {
                                let imported = child
                                    .child_by_field_name("name")
                                    .map(|name| node_text(name, source))
                                    .unwrap_or_default();
                                let alias = child
                                    .child_by_field_name("alias")
                                    .map(|alias| node_text(alias, source));
                                (imported, alias)
                            }
                            _ => continue,
                        };
                        if imported_name.is_empty() {
                            continue;
                        }
                        out.push(Import {
                            module: module_text.clone(),
                            local_name: alias.clone().or_else(|| Some(imported_name.clone())),
                            imported_name: Some(imported_name),
                            alias,
                            line,
                        });
                    }
                }
            }
            _ => {}
        }
    }

    fn call_target(&self, node: TsNode<'_>, source: &[u8]) -> Option<CallTarget> {
        if node.kind() != "call" {
            return None;
        }
        let func = node.child_by_field_name("function")?;
        let target = match func.kind() {
            "identifier" => CallTarget::bare(node_text(func, source)),
            "attribute" => CallTarget::member(
                node_text(func.child_by_field_name("attribute")?, source),
                func.child_by_field_name("object")
                    .map(|object| node_text(object, source)),
            ),
            _ => return None,
        };
        if target.name.is_empty() || PY_SKIP.contains(&target.name.as_str()) {
            return None;
        }
        Some(target)
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
        assert_eq!(g.imports[0].module, "os");
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
        let modules: Vec<&str> = g
            .imports
            .iter()
            .map(|import| import.module.as_str())
            .collect();
        assert!(modules.contains(&"a.b"));
        assert!(modules.contains(&"os"));
        assert!(modules.contains(&"numpy"));
    }

    #[test]
    fn python_preserves_import_aliases_and_attribute_qualifiers() {
        let g = graph(
            r#"
from services.email import send as send_email
import json as js

def run():
    send_email()
    js.dumps({})
"#,
        );
        let send_import = g
            .imports
            .iter()
            .find(|import| import.local_name.as_deref() == Some("send_email"))
            .expect("aliased from import");
        assert_eq!(send_import.module, "services.email");
        assert_eq!(send_import.imported_name.as_deref(), Some("send"));
        assert_eq!(send_import.alias.as_deref(), Some("send_email"));

        let dumps = g
            .calls
            .iter()
            .find(|call| call.target_name == "dumps")
            .expect("qualified attribute call");
        assert_eq!(dumps.qualifier.as_deref(), Some("js"));
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
