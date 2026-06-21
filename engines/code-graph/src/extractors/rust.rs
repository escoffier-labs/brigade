//! Rust extractor: AST-based symbols, imports, and call edges.

use anyhow::Result;
use tree_sitter::Node as TsNode;

use crate::extractors::common::{LangSpec, extract_with, node_text};
use crate::model::FileGraph;

/// Common enum constructors that parse as calls but rarely resolve to a project symbol.
const RUST_SKIP: &[&str] = &["Some", "Ok", "Err", "None", "Box"];

struct RustSpec;

impl LangSpec for RustSpec {
    fn symbol_candidate<'t>(&self, node: TsNode<'t>) -> Option<(&'static str, TsNode<'t>)> {
        let kind = match node.kind() {
            "function_item" => "function",
            "struct_item" => "struct",
            "enum_item" => "enum",
            "union_item" => "struct",
            "trait_item" => "trait",
            "type_item" => "type",
            "mod_item" => "module",
            _ => return None,
        };
        node.child_by_field_name("name").map(|name| (kind, name))
    }

    fn collect_import(&self, node: TsNode<'_>, source: &[u8], out: &mut Vec<(String, usize)>) {
        if node.kind() == "use_declaration"
            && let Some(arg) = node.child_by_field_name("argument")
        {
            let module = node_text(arg, source);
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
            "field_expression" => node_text(func.child_by_field_name("field")?, source),
            "scoped_identifier" => node_text(func.child_by_field_name("name")?, source),
            _ => return None,
        };
        if name.is_empty() || RUST_SKIP.contains(&name.as_str()) {
            return None;
        }
        Some(name)
    }
}

pub fn extract_rust(path: &str, content: &str, content_hash: &str) -> Result<FileGraph> {
    extract_with(
        &RustSpec,
        path,
        content,
        content_hash,
        tree_sitter_rust::LANGUAGE.into(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rust_finds_symbols_imports_and_calls() {
        let g = extract_rust(
            "src/x.rs",
            r#"
use std::collections::HashMap;

struct Runner;

fn helper() -> i32 {
    1
}

fn run() -> i32 {
    helper()
}
"#,
            "hash",
        )
        .unwrap();
        let names: Vec<&str> = g.symbols.iter().map(|s| s.name.as_str()).collect();
        assert!(names.contains(&"Runner"));
        assert!(names.contains(&"helper"));
        assert!(names.contains(&"run"));
        assert!(g.imports.iter().any(|(m, _)| m.contains("HashMap")));
        assert!(g.calls.iter().any(|c| c.target_name == "helper"));
    }

    #[test]
    fn rust_method_and_scoped_calls_resolve() {
        let g = extract_rust(
            "src/x.rs",
            r#"
fn run() {
    let m = HashMap::new();
    m.insert(1);
}
"#,
            "hash",
        )
        .unwrap();
        let targets: Vec<&str> = g.calls.iter().map(|c| c.target_name.as_str()).collect();
        assert!(targets.contains(&"new")); // scoped_identifier HashMap::new -> new
        assert!(targets.contains(&"insert")); // field_expression m.insert -> insert
    }
}
