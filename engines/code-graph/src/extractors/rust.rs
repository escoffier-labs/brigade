//! Rust extractor: AST-based symbols, imports, and call edges.

use anyhow::Result;
use tree_sitter::Node as TsNode;

use crate::extractors::common::{LangSpec, extract_with, node_text};
use crate::model::{CallTarget, FileGraph, Import};

/// Bump when Rust extraction output can change for the same file content.
pub const EXTRACTOR_FINGERPRINT: &str = "rust-extractor-v1";

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

    fn symbol_container(&self, node: TsNode<'_>, source: &[u8]) -> Option<String> {
        if node.kind() != "function_item" {
            return None;
        }
        let parent = rust_impl_ancestor(node)?;
        parent
            .child_by_field_name("type")
            .or_else(|| rust_impl_type_child(parent))
            .map(|type_node| normalize_rust_type(&node_text(type_node, source)))
            .filter(|name| !name.is_empty())
    }

    fn collect_import(&self, node: TsNode<'_>, source: &[u8], out: &mut Vec<Import>) {
        if node.kind() == "use_declaration"
            && let Some(arg) = node.child_by_field_name("argument")
        {
            collect_rust_use(&node_text(arg, source), node.start_position().row + 1, out);
        }
    }

    fn call_target(&self, node: TsNode<'_>, source: &[u8]) -> Option<CallTarget> {
        if node.kind() != "call_expression" {
            return None;
        }
        let func = node.child_by_field_name("function")?;
        let target = match func.kind() {
            "identifier" => CallTarget::bare(node_text(func, source)),
            "field_expression" => CallTarget::member(
                node_text(func.child_by_field_name("field")?, source),
                func.child_by_field_name("value")
                    .or_else(|| func.child_by_field_name("object"))
                    .map(|value| node_text(value, source)),
            ),
            "scoped_identifier" => CallTarget::scoped(
                node_text(func.child_by_field_name("name")?, source),
                func.child_by_field_name("path")
                    .or_else(|| func.child_by_field_name("scope"))
                    .map(|path| node_text(path, source)),
            ),
            _ => return None,
        };
        if target.name.is_empty() || RUST_SKIP.contains(&target.name.as_str()) {
            return None;
        }
        Some(target)
    }
}

fn collect_rust_use(text: &str, line: usize, out: &mut Vec<Import>) {
    let text = text.trim().trim_end_matches(';').trim();
    if text.is_empty() {
        return;
    }
    if let Some((prefix, group)) = split_grouped_rust_use(text) {
        for item in group
            .split(',')
            .map(str::trim)
            .filter(|item| !item.is_empty())
        {
            collect_single_rust_use(&format!("{prefix}::{item}"), line, out);
        }
        return;
    }
    collect_single_rust_use(text, line, out);
}

fn collect_single_rust_use(text: &str, line: usize, out: &mut Vec<Import>) {
    let (path, alias) = match text.rsplit_once(" as ") {
        Some((path, alias)) => (path.trim(), Some(alias.trim().to_string())),
        None => (text, None),
    };
    let Some((module, imported)) = path.rsplit_once("::") else {
        return;
    };
    let imported = imported.trim();
    if module.is_empty() || imported.is_empty() {
        return;
    }
    out.push(Import {
        module: module.to_string(),
        local_name: alias.clone().or_else(|| Some(imported.to_string())),
        imported_name: Some(imported.to_string()),
        alias,
        line,
    });
}

fn split_grouped_rust_use(text: &str) -> Option<(&str, &str)> {
    let (prefix, group) = text.split_once('{')?;
    let group = group.rsplit_once('}')?.0.trim();
    let prefix = prefix.trim().trim_end_matches("::");
    if prefix.is_empty() || group.is_empty() {
        None
    } else {
        Some((prefix, group))
    }
}

fn rust_impl_type_child(node: TsNode<'_>) -> Option<TsNode<'_>> {
    let mut cursor = node.walk();
    node.named_children(&mut cursor).find(|child| {
        matches!(
            child.kind(),
            "type_identifier" | "generic_type" | "scoped_type_identifier"
        )
    })
}

fn rust_impl_ancestor(node: TsNode<'_>) -> Option<TsNode<'_>> {
    let mut current = node.parent();
    while let Some(parent) = current {
        if parent.kind() == "impl_item" {
            return Some(parent);
        }
        current = parent.parent();
    }
    None
}

fn normalize_rust_type(text: &str) -> String {
    text.trim()
        .trim_start_matches('&')
        .trim_start_matches("mut ")
        .trim_start_matches('*')
        .trim_start_matches("const ")
        .trim_start_matches("mut ")
        .split('<')
        .next()
        .unwrap_or("")
        .rsplit("::")
        .next()
        .unwrap_or("")
        .trim()
        .to_string()
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
        assert!(
            g.imports
                .iter()
                .any(|import| import.imported_name.as_deref() == Some("HashMap"))
        );
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

    #[test]
    fn rust_preserves_use_aliases_and_scoped_qualifiers() {
        let g = extract_rust(
            "src/x.rs",
            r#"
use crate::factory::build as make;
use crate::codec::{encode as enc};

struct Runner;

impl Runner {
    fn new() -> Runner { Runner }
}

fn run() {
    make();
    enc();
    Runner::new();
}
"#,
            "hash",
        )
        .unwrap();

        let make_import = g
            .imports
            .iter()
            .find(|import| import.local_name.as_deref() == Some("make"))
            .expect("aliased use import");
        assert_eq!(make_import.module, "crate::factory");
        assert_eq!(make_import.imported_name.as_deref(), Some("build"));
        assert_eq!(make_import.alias.as_deref(), Some("make"));

        let grouped_import = g
            .imports
            .iter()
            .find(|import| import.local_name.as_deref() == Some("enc"))
            .expect("grouped aliased use import");
        assert_eq!(grouped_import.module, "crate::codec");
        assert_eq!(grouped_import.imported_name.as_deref(), Some("encode"));
        assert_eq!(grouped_import.alias.as_deref(), Some("enc"));

        let scoped = g
            .calls
            .iter()
            .find(|call| call.target_name == "new")
            .expect("scoped call");
        assert_eq!(scoped.qualifier.as_deref(), Some("Runner"));

        let method = g
            .symbols
            .iter()
            .find(|symbol| symbol.name == "new")
            .expect("impl method");
        assert_eq!(method.container.as_deref(), Some("Runner"));
        assert_eq!(method.qualified_name, "Runner.new");
    }
}
