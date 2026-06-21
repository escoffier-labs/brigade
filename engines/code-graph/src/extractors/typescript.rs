//! TypeScript/JavaScript extractor: AST-based symbols, imports, and call edges.

use anyhow::Result;
use tree_sitter::Node as TsNode;

use crate::extractors::common::{LangSpec, extract_with, node_text, string_literal_text};
use crate::model::FileGraph;

/// Ubiquitous globals/methods that would only ever produce noise edges.
const JS_SKIP: &[&str] = &[
    "console",
    "log",
    "map",
    "filter",
    "reduce",
    "then",
    "catch",
    "setTimeout",
    "Promise",
    "require",
];

struct TypeScriptSpec;

impl LangSpec for TypeScriptSpec {
    fn symbol_candidate<'t>(&self, node: TsNode<'t>) -> Option<(&'static str, TsNode<'t>)> {
        match node.kind() {
            "class_declaration" => node.child_by_field_name("name").map(|name| ("class", name)),
            "function_declaration" => node
                .child_by_field_name("name")
                .map(|name| ("function", name)),
            "method_definition" => node
                .child_by_field_name("name")
                .map(|name| ("method", name)),
            "variable_declarator" => {
                let value = node.child_by_field_name("value")?;
                if matches!(
                    value.kind(),
                    "arrow_function" | "function" | "function_expression"
                ) {
                    node.child_by_field_name("name")
                        .map(|name| ("function", name))
                } else {
                    None
                }
            }
            _ => None,
        }
    }

    fn collect_import(&self, node: TsNode<'_>, source: &[u8], out: &mut Vec<(String, usize)>) {
        let line = node.start_position().row + 1;
        match node.kind() {
            "import_statement" => {
                if let Some(src) = node.child_by_field_name("source")
                    && let Some(module) = string_literal_text(src, source)
                    && !module.is_empty()
                {
                    out.push((module, line));
                }
            }
            "call_expression" => {
                // CommonJS: require("module")
                let Some(func) = node.child_by_field_name("function") else {
                    return;
                };
                if func.kind() != "identifier" || node_text(func, source) != "require" {
                    return;
                }
                let Some(args) = node.child_by_field_name("arguments") else {
                    return;
                };
                let mut cursor = args.walk();
                for arg in args.named_children(&mut cursor) {
                    if arg.kind() == "string" {
                        if let Some(module) = string_literal_text(arg, source)
                            && !module.is_empty()
                        {
                            out.push((module, line));
                        }
                        break;
                    }
                }
            }
            _ => {}
        }
    }

    fn call_target(&self, node: TsNode<'_>, source: &[u8]) -> Option<String> {
        if node.kind() != "call_expression" {
            return None;
        }
        let func = node.child_by_field_name("function")?;
        let name = match func.kind() {
            "identifier" => node_text(func, source),
            "member_expression" => node_text(func.child_by_field_name("property")?, source),
            _ => return None,
        };
        if name.is_empty() || JS_SKIP.contains(&name.as_str()) {
            return None;
        }
        Some(name)
    }
}

pub fn extract_typescript(path: &str, content: &str, content_hash: &str) -> Result<FileGraph> {
    let language = if path.ends_with(".tsx") {
        tree_sitter_typescript::LANGUAGE_TSX.into()
    } else if path.ends_with(".ts") {
        tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into()
    } else {
        tree_sitter_javascript::LANGUAGE.into()
    };
    extract_with(&TypeScriptSpec, path, content, content_hash, language)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn graph(path: &str, source: &str) -> FileGraph {
        extract_typescript(path, source, "hash").unwrap()
    }

    #[test]
    fn typescript_extractor_finds_common_symbol_shapes() {
        let g = graph(
            "src/demo.ts",
            r#"
import { x } from "./x";

export class Runner {}
export function start() {
  helper();
}
const helper = () => x();
"#,
        );
        let names: Vec<&str> = g.symbols.iter().map(|s| s.name.as_str()).collect();
        assert!(names.contains(&"Runner"));
        assert!(names.contains(&"start"));
        assert!(names.contains(&"helper"));
        assert_eq!(g.imports[0].0, "./x");
        assert!(g.calls.iter().any(|c| c.target_name == "helper"));
    }

    #[test]
    fn ts_import_named_and_bare() {
        let g = graph(
            "src/demo.ts",
            r#"
import { a } from "./named";
import "./side-effect";
"#,
        );
        let modules: Vec<&str> = g.imports.iter().map(|(m, _)| m.as_str()).collect();
        assert!(modules.contains(&"./named"));
        assert!(modules.contains(&"./side-effect"));
    }

    #[test]
    fn ts_require_form() {
        let g = graph("src/demo.js", r#"const fs = require("fs");"#);
        let modules: Vec<&str> = g.imports.iter().map(|(m, _)| m.as_str()).collect();
        assert!(modules.contains(&"fs"));
    }

    #[test]
    fn ts_member_call_resolves_property() {
        let g = graph(
            "src/demo.ts",
            r#"
function start() {
  this.svc.run();
  helper();
}
"#,
        );
        let targets: Vec<&str> = g.calls.iter().map(|c| c.target_name.as_str()).collect();
        assert!(targets.contains(&"run"));
        assert!(targets.contains(&"helper"));
    }

    #[test]
    fn ts_arrow_caller_attribution() {
        let g = graph(
            "src/demo.ts",
            r#"
const helper = () => work();
"#,
        );
        let helper = g
            .symbols
            .iter()
            .find(|s| s.name == "helper")
            .expect("helper symbol");
        let call = g
            .calls
            .iter()
            .find(|c| c.target_name == "work")
            .expect("work call");
        assert_eq!(call.source_id, helper.id);
    }
}
