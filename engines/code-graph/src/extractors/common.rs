//! Shared extraction scaffolding: tree-sitter symbol traversal and helpers used by every
//! per-language extractor.

use std::collections::HashSet;

use anyhow::{Result, anyhow};
use regex::Regex;
use sha2::{Digest, Sha256};
use tree_sitter::{Language, Node as TsNode, Parser as TsParser};

use crate::model::{Lang, PendingCall, Symbol};

/// Parse `content` with the given tree-sitter `language` and collect symbol definitions.
pub fn extract_tree_sitter_symbols(
    path: &str,
    content: &str,
    content_hash: &str,
    language: Language,
    symbol_language: Lang,
) -> Result<Vec<Symbol>> {
    let mut parser = TsParser::new();
    parser
        .set_language(&language)
        .map_err(|err| anyhow!("failed to set tree-sitter language: {err}"))?;
    let tree = parser
        .parse(content, None)
        .ok_or_else(|| anyhow!("tree-sitter returned no parse tree for {path}"))?;
    let lines: Vec<&str> = content.lines().collect();
    let mut symbols = Vec::new();
    let mut stack = Vec::new();
    visit_symbol_node(
        tree.root_node(),
        path,
        content_hash,
        content.as_bytes(),
        &lines,
        symbol_language,
        &mut stack,
        &mut symbols,
    );
    Ok(symbols)
}

#[allow(clippy::too_many_arguments)]
fn visit_symbol_node(
    node: TsNode<'_>,
    path: &str,
    content_hash: &str,
    source: &[u8],
    lines: &[&str],
    language: Lang,
    stack: &mut Vec<String>,
    symbols: &mut Vec<Symbol>,
) {
    if let Some((kind, name_node)) = symbol_candidate(node, language) {
        let name = node_text(name_node, source);
        if !name.is_empty() {
            let start_line = node.start_position().row + 1;
            let end_line = node.end_position().row + 1;
            let signature = lines
                .get(start_line.saturating_sub(1))
                .map_or("", |line| *line)
                .trim()
                .to_string();
            let container = stack.last().cloned();
            let qualified_name = container
                .as_ref()
                .map_or_else(|| name.clone(), |parent| format!("{parent}.{name}"));
            let id = symbol_id(path, &qualified_name, start_line, kind);
            symbols.push(Symbol {
                id,
                kind: kind.to_string(),
                name: name.clone(),
                qualified_name: qualified_name.clone(),
                file_path: path.to_string(),
                start_line,
                end_line,
                signature,
                container,
                content_hash: content_hash.to_string(),
            });

            stack.push(qualified_name);
            visit_symbol_children(
                node,
                path,
                content_hash,
                source,
                lines,
                language,
                stack,
                symbols,
            );
            stack.pop();
            return;
        }
    }

    visit_symbol_children(
        node,
        path,
        content_hash,
        source,
        lines,
        language,
        stack,
        symbols,
    );
}

#[allow(clippy::too_many_arguments)]
fn visit_symbol_children(
    node: TsNode<'_>,
    path: &str,
    content_hash: &str,
    source: &[u8],
    lines: &[&str],
    language: Lang,
    stack: &mut Vec<String>,
    symbols: &mut Vec<Symbol>,
) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        visit_symbol_node(
            child,
            path,
            content_hash,
            source,
            lines,
            language,
            stack,
            symbols,
        );
    }
}

fn symbol_candidate(node: TsNode<'_>, language: Lang) -> Option<(&'static str, TsNode<'_>)> {
    match language {
        Lang::Python => match node.kind() {
            "class_definition" => node.child_by_field_name("name").map(|name| ("class", name)),
            "function_definition" => node
                .child_by_field_name("name")
                .map(|name| ("function", name)),
            _ => None,
        },
        Lang::TypeScript => match node.kind() {
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
        },
    }
}

pub fn node_text(node: TsNode<'_>, source: &[u8]) -> String {
    node.utf8_text(source).unwrap_or("").to_string()
}

/// Regex-based call collection (chunk-1 behavior; replaced by AST extraction in chunk 2).
pub fn collect_calls(
    _path: &str,
    lines: &[&str],
    symbols: &[Symbol],
    call_re: &Regex,
    skip: HashSet<&'static str>,
) -> Vec<PendingCall> {
    let mut calls = Vec::new();
    for (idx, line) in lines.iter().enumerate() {
        let line_no = idx + 1;
        let Some(source) = symbols
            .iter()
            .find(|s| s.start_line <= line_no && s.end_line >= line_no)
        else {
            continue;
        };
        for cap in call_re.captures_iter(line) {
            let raw = cap.get(1).map_or("", |m| m.as_str());
            let target = raw.rsplit('.').next().unwrap_or(raw);
            if target.is_empty() || skip.contains(target) || target == source.name {
                continue;
            }
            calls.push(PendingCall {
                source_id: source.id.clone(),
                target_name: target.to_string(),
                line: line_no,
                source_file: source.file_path.clone(),
            });
        }
    }
    calls
}

pub fn symbol_id(path: &str, qualified_name: &str, line: usize, kind: &str) -> String {
    hex_hash(format!("{path}:{qualified_name}:{line}:{kind}").as_bytes())
}

pub fn hex_hash(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}
