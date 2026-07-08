//! TypeScript/JavaScript extractor: AST-based symbols, imports, and call edges.

use anyhow::Result;
use tree_sitter::Node as TsNode;

use crate::extractors::common::{LangSpec, extract_with, node_text, string_literal_text};
use crate::model::{CallTarget, FileGraph, Import};

/// Bump when TypeScript or JavaScript extraction output can change for the same file content.
pub const EXTRACTOR_FINGERPRINT: &str = "typescript-extractor-v1";

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

    fn collect_import(&self, node: TsNode<'_>, source: &[u8], out: &mut Vec<Import>) {
        let line = node.start_position().row + 1;
        match node.kind() {
            "import_statement" => {
                if let Some(src) = node.child_by_field_name("source")
                    && let Some(module) = string_literal_text(src, source)
                    && !module.is_empty()
                {
                    let before = out.len();
                    collect_ts_import_bindings(node, src, &module, line, source, out);
                    if before == out.len() {
                        out.push(Import {
                            module,
                            local_name: None,
                            imported_name: None,
                            alias: None,
                            line,
                        });
                    }
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
                            let local_name = node
                                .parent()
                                .filter(|parent| parent.kind() == "variable_declarator")
                                .and_then(|parent| parent.child_by_field_name("name"))
                                .map(|name| node_text(name, source));
                            out.push(Import {
                                module,
                                local_name,
                                imported_name: None,
                                alias: None,
                                line,
                            });
                        }
                        break;
                    }
                }
            }
            _ => {}
        }
    }

    fn call_target(&self, node: TsNode<'_>, source: &[u8]) -> Option<CallTarget> {
        if node.kind() != "call_expression" {
            return None;
        }
        let func = node.child_by_field_name("function")?;
        let target = match func.kind() {
            "identifier" => CallTarget::bare(node_text(func, source)),
            "member_expression" => CallTarget::member(
                node_text(func.child_by_field_name("property")?, source),
                func.child_by_field_name("object")
                    .map(|object| node_text(object, source)),
            ),
            _ => return None,
        };
        if target.name.is_empty() || JS_SKIP.contains(&target.name.as_str()) {
            return None;
        }
        Some(target)
    }
}

fn collect_ts_import_bindings(
    node: TsNode<'_>,
    source_node: TsNode<'_>,
    module: &str,
    line: usize,
    source: &[u8],
    out: &mut Vec<Import>,
) {
    if node == source_node {
        return;
    }
    match node.kind() {
        "import_specifier" => {
            let imported = node
                .child_by_field_name("name")
                .map(|name| node_text(name, source))
                .unwrap_or_default();
            if imported.is_empty() {
                return;
            }
            let alias = node
                .child_by_field_name("alias")
                .map(|alias| node_text(alias, source));
            out.push(Import {
                module: module.to_string(),
                local_name: alias.clone().or_else(|| Some(imported.clone())),
                imported_name: Some(imported),
                alias,
                line,
            });
        }
        "namespace_import" => {
            if let Some(name) = node.child_by_field_name("name") {
                let local = node_text(name, source);
                if !local.is_empty() {
                    out.push(Import {
                        module: module.to_string(),
                        local_name: Some(local.clone()),
                        imported_name: None,
                        alias: Some(local),
                        line,
                    });
                }
            }
        }
        "identifier" => {
            if node
                .parent()
                .is_some_and(|parent| parent.kind() == "import_clause")
            {
                let local = node_text(node, source);
                if !local.is_empty() {
                    out.push(Import {
                        module: module.to_string(),
                        local_name: Some(local),
                        imported_name: Some("default".to_string()),
                        alias: None,
                        line,
                    });
                }
            }
        }
        _ => {
            let mut cursor = node.walk();
            for child in node.named_children(&mut cursor) {
                collect_ts_import_bindings(child, source_node, module, line, source, out);
            }
        }
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
        assert_eq!(g.imports[0].module, "./x");
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
        let modules: Vec<&str> = g
            .imports
            .iter()
            .map(|import| import.module.as_str())
            .collect();
        assert!(modules.contains(&"./named"));
        assert!(modules.contains(&"./side-effect"));
    }

    #[test]
    fn ts_require_form() {
        let g = graph("src/demo.js", r#"const fs = require("fs");"#);
        let modules: Vec<&str> = g
            .imports
            .iter()
            .map(|import| import.module.as_str())
            .collect();
        assert!(modules.contains(&"fs"));
    }

    #[test]
    fn ts_preserves_named_import_aliases_and_member_qualifiers() {
        let g = graph(
            "src/demo.ts",
            r#"
import { parse as parseCsv } from "./csv";
import * as codec from "./codec";

function run() {
  parseCsv();
  codec.encode();
}
"#,
        );
        let parse_import = g
            .imports
            .iter()
            .find(|import| import.local_name.as_deref() == Some("parseCsv"))
            .expect("aliased named import");
        assert_eq!(parse_import.module, "./csv");
        assert_eq!(parse_import.imported_name.as_deref(), Some("parse"));
        assert_eq!(parse_import.alias.as_deref(), Some("parseCsv"));

        let encode = g
            .calls
            .iter()
            .find(|call| call.target_name == "encode")
            .expect("member call");
        assert_eq!(encode.qualifier.as_deref(), Some("codec"));
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
