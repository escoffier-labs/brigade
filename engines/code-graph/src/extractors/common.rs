//! Shared extraction scaffolding: a single tree-sitter traversal that yields a file's symbols,
//! imports, and call edges together, parameterized by a per-language [`LangSpec`].

use anyhow::{Result, anyhow};
use sha2::{Digest, Sha256};
use tree_sitter::{Language, Node as TsNode, Parser as TsParser};

use crate::model::{CallTarget, FileGraph, Import, PendingCall, Symbol};

/// Per-language plugin describing how to recognize symbols, imports, and calls in an AST.
pub trait LangSpec {
    /// Return `(kind, name_node)` when `node` defines a symbol (class/function/method).
    fn symbol_candidate<'t>(&self, node: TsNode<'t>) -> Option<(&'static str, TsNode<'t>)>;
    /// Override the symbol container when a language encodes it outside normal lexical nesting.
    fn symbol_container(&self, _node: TsNode<'_>, _source: &[u8]) -> Option<String> {
        None
    }
    /// Append any module imports declared by `node` to `out`.
    fn collect_import(&self, node: TsNode<'_>, source: &[u8], out: &mut Vec<Import>);
    /// Resolve the callee name if `node` is a call expression, else `None` (builtins filtered here).
    fn call_target(&self, node: TsNode<'_>, source: &[u8]) -> Option<CallTarget>;
}

struct Frame {
    qualified_name: String,
    symbol_id: String,
}

struct Ctx<'a> {
    path: &'a str,
    content_hash: &'a str,
    source: &'a [u8],
    lines: &'a [&'a str],
}

/// Parse `content` and extract symbols, imports, and pending calls in one pass.
pub fn extract_with<L: LangSpec>(
    spec: &L,
    path: &str,
    content: &str,
    content_hash: &str,
    ts_language: Language,
) -> Result<FileGraph> {
    let mut parser = TsParser::new();
    parser
        .set_language(&ts_language)
        .map_err(|err| anyhow!("failed to set tree-sitter language: {err}"))?;
    let tree = parser
        .parse(content, None)
        .ok_or_else(|| anyhow!("tree-sitter returned no parse tree for {path}"))?;

    let lines: Vec<&str> = content.lines().collect();
    let ctx = Ctx {
        path,
        content_hash,
        source: content.as_bytes(),
        lines: &lines,
    };

    let mut symbols = Vec::new();
    let mut imports = Vec::new();
    let mut calls = Vec::new();
    let mut stack: Vec<Frame> = Vec::new();
    visit(
        spec,
        &ctx,
        tree.root_node(),
        &mut stack,
        &mut symbols,
        &mut imports,
        &mut calls,
    );

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

fn visit<L: LangSpec>(
    spec: &L,
    ctx: &Ctx,
    node: TsNode<'_>,
    stack: &mut Vec<Frame>,
    symbols: &mut Vec<Symbol>,
    imports: &mut Vec<Import>,
    calls: &mut Vec<PendingCall>,
) {
    spec.collect_import(node, ctx.source, imports);

    if let Some(target) = spec.call_target(node, ctx.source) {
        // Attribute the call to the innermost enclosing symbol; module-level calls are dropped.
        if let Some(frame) = stack.last() {
            calls.push(PendingCall {
                source_id: frame.symbol_id.clone(),
                target_name: target.name,
                qualifier: target.qualifier,
                kind: target.kind,
                line: node.start_position().row + 1,
                source_file: ctx.path.to_string(),
            });
        }
    }

    if let Some((kind, name_node)) = spec.symbol_candidate(node) {
        let name = node_text(name_node, ctx.source);
        if !name.is_empty() {
            let start_line = node.start_position().row + 1;
            let end_line = node.end_position().row + 1;
            let signature = ctx
                .lines
                .get(start_line.saturating_sub(1))
                .map_or("", |line| *line)
                .trim()
                .to_string();
            let body_hash = hex_hash(line_span_text(ctx.source, start_line, end_line).as_bytes());
            let container = spec
                .symbol_container(node, ctx.source)
                .or_else(|| stack.last().map(|frame| frame.qualified_name.clone()));
            let qualified_name = container
                .as_ref()
                .map_or_else(|| name.clone(), |parent| format!("{parent}.{name}"));
            let id = symbol_id(ctx.path, &qualified_name, start_line, kind);
            symbols.push(Symbol {
                id: id.clone(),
                kind: kind.to_string(),
                name: name.clone(),
                qualified_name: qualified_name.clone(),
                file_path: ctx.path.to_string(),
                start_line,
                end_line,
                signature,
                container,
                content_hash: ctx.content_hash.to_string(),
                body_hash: Some(body_hash),
            });

            stack.push(Frame {
                qualified_name,
                symbol_id: id,
            });
            visit_children(spec, ctx, node, stack, symbols, imports, calls);
            stack.pop();
            return;
        }
    }

    visit_children(spec, ctx, node, stack, symbols, imports, calls);
}

fn visit_children<L: LangSpec>(
    spec: &L,
    ctx: &Ctx,
    node: TsNode<'_>,
    stack: &mut Vec<Frame>,
    symbols: &mut Vec<Symbol>,
    imports: &mut Vec<Import>,
    calls: &mut Vec<PendingCall>,
) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        visit(spec, ctx, child, stack, symbols, imports, calls);
    }
}

pub fn node_text(node: TsNode<'_>, source: &[u8]) -> String {
    node.utf8_text(source).unwrap_or("").to_string()
}

fn line_span_text(source: &[u8], start_line: usize, end_line: usize) -> String {
    if start_line == 0 || end_line < start_line {
        return String::new();
    }

    String::from_utf8_lossy(source)
        .split_inclusive('\n')
        .enumerate()
        .filter_map(|(idx, line)| {
            let line_no = idx + 1;
            (start_line <= line_no && line_no <= end_line).then_some(line)
        })
        .collect()
}

/// Text of a tree-sitter `string` node with quotes removed (prefers the `string_fragment` child).
pub fn string_literal_text(node: TsNode<'_>, source: &[u8]) -> Option<String> {
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        if child.kind() == "string_fragment" {
            return Some(node_text(child, source));
        }
    }
    let raw = node_text(node, source);
    Some(
        raw.trim_matches(|c| c == '"' || c == '\'' || c == '`')
            .to_string(),
    )
}

pub fn symbol_id(path: &str, qualified_name: &str, line: usize, kind: &str) -> String {
    hex_hash(format!("{path}:{qualified_name}:{line}:{kind}").as_bytes())
}

pub fn hex_hash(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}
