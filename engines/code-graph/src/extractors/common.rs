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
    /// Return the wrapped definition when `node` only decorates it (Python `@decorator`).
    ///
    /// The wrapper's span becomes the symbol's span, and calls inside the
    /// decorators are attributed to the decorated symbol.
    fn decorated_definition<'t>(&self, _node: TsNode<'t>) -> Option<TsNode<'t>> {
        None
    }
    /// Whether the language has a Python-like module scope. When `true`, calls
    /// outside every symbol belong to a per-file [`MODULE_SYMBOL_NAME`]
    /// pseudo-symbol, and imports inside a symbol are marked as not binding at
    /// module scope. When `false`, module-level calls are dropped and every
    /// import keeps `module_scope = true`.
    fn module_scope_calls(&self) -> bool {
        false
    }
    /// The module's literal export list (Python `__all__`), or `None` when it
    /// declares none or builds it in a way extraction cannot read.
    fn module_exports(&self, _root: TsNode<'_>, _source: &[u8]) -> Option<Vec<String>> {
        None
    }
    /// Whether a node of this kind runs its body only conditionally (an `if`,
    /// `try`, `with`, loop, or `match`). Module-scope imports and definitions
    /// under one are recorded as conditional.
    fn conditional_block(&self, _kind: &str) -> bool {
        false
    }
}

/// Whether `node` sits under a block that may not run its body.
fn under_conditional_block<L: LangSpec>(spec: &L, node: TsNode<'_>) -> bool {
    std::iter::successors(node.parent(), TsNode::parent)
        .any(|parent| spec.conditional_block(parent.kind()))
}

/// Name and qualified name of the per-file pseudo-symbol that owns module-level calls.
pub const MODULE_SYMBOL_NAME: &str = "<module>";
/// Kind of the module pseudo-symbol.
pub const MODULE_SYMBOL_KIND: &str = "module";

struct Frame {
    qualified_name: String,
    symbol_id: String,
}

#[derive(Default)]
struct SymbolState {
    /// Occurrences seen per identity key, for ordinal disambiguation of
    /// same-named symbols of the same kind in one file.
    occurrences: std::collections::HashMap<String, usize>,
    symbols: Vec<Symbol>,
    /// Ids of module-level symbols defined under a conditional block.
    conditional: Vec<String>,
}

struct Ctx<'a> {
    path: &'a str,
    content_hash: &'a str,
    source: &'a [u8],
    lines: &'a [&'a str],
    /// Id of the module pseudo-symbol when the language attributes module-level calls.
    module_id: Option<String>,
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
        module_id: spec
            .module_scope_calls()
            .then(|| symbol_id(path, MODULE_SYMBOL_NAME, MODULE_SYMBOL_KIND, 0)),
    };

    let mut symbol_state = SymbolState::default();
    let mut imports = Vec::new();
    let mut calls = Vec::new();
    let mut stack: Vec<Frame> = Vec::new();
    visit(
        spec,
        &ctx,
        tree.root_node(),
        &mut stack,
        &mut symbol_state,
        &mut imports,
        &mut calls,
    );
    // Emit the module pseudo-symbol only for files that have module-level calls.
    if let Some(module_id) = ctx.module_id.as_deref() {
        if calls.iter().any(|call| call.source_id == module_id) {
            let module = module_symbol(&ctx, module_id, &symbol_state.symbols);
            symbol_state.symbols.push(module);
        }
    }

    Ok(FileGraph {
        path: path.to_string(),
        language: String::new(),
        hash: content_hash.to_string(),
        size: 0,
        modified_at: 0,
        symbols: symbol_state.symbols,
        imports,
        calls,
        exports: spec.module_exports(tree.root_node(), ctx.source),
        conditional_symbols: symbol_state.conditional,
    })
}

fn visit<L: LangSpec>(
    spec: &L,
    ctx: &Ctx,
    node: TsNode<'_>,
    stack: &mut Vec<Frame>,
    symbol_state: &mut SymbolState,
    imports: &mut Vec<Import>,
    calls: &mut Vec<PendingCall>,
) {
    let first_new_import = imports.len();
    spec.collect_import(node, ctx.source, imports);
    if ctx.module_id.is_some() && imports.len() > first_new_import {
        // A function- or class-local import binds in that body, not the module.
        // A module-scope import under an `if` or `try` may not run at all.
        let module_scope = stack.is_empty();
        let conditional = module_scope && under_conditional_block(spec, node);
        for import in &mut imports[first_new_import..] {
            import.module_scope = module_scope;
            import.conditional = conditional;
        }
    }

    if let Some(target) = spec.call_target(node, ctx.source) {
        // Attribute the call to the innermost enclosing symbol. Module-level calls go to
        // the module pseudo-symbol when the language has one and are dropped otherwise.
        if let Some(source_id) = stack
            .last()
            .map(|frame| &frame.symbol_id)
            .or(ctx.module_id.as_ref())
        {
            calls.push(PendingCall {
                source_id: source_id.clone(),
                target_name: target.name,
                qualifier: target.qualifier,
                kind: target.kind,
                line: node.start_position().row + 1,
                source_file: ctx.path.to_string(),
            });
        }
    }

    // A decorator wrapper spans its definition: the symbol starts at the first
    // decorator, and the signature stays on the definition line.
    let definition = spec.decorated_definition(node).unwrap_or(node);
    if let Some((kind, name_node)) = spec.symbol_candidate(definition) {
        let name = node_text(name_node, ctx.source);
        if !name.is_empty() {
            let start_line = node.start_position().row + 1;
            let end_line = node.end_position().row + 1;
            let signature = ctx
                .lines
                .get(definition.start_position().row)
                .map_or("", |line| *line)
                .trim()
                .to_string();
            let body_hash = hex_hash(line_span_text(ctx.source, start_line, end_line).as_bytes());
            let container = spec
                .symbol_container(definition, ctx.source)
                .or_else(|| stack.last().map(|frame| frame.qualified_name.clone()));
            let qualified_name = container
                .as_ref()
                .map_or_else(|| name.clone(), |parent| format!("{parent}.{name}"));
            let occurrence = {
                let counter = symbol_state
                    .occurrences
                    .entry(format!("{}:{qualified_name}:{kind}", ctx.path))
                    .or_insert(0);
                let current = *counter;
                *counter += 1;
                current
            };
            let id = symbol_id(ctx.path, &qualified_name, kind, occurrence);
            if ctx.module_id.is_some() && stack.is_empty() && under_conditional_block(spec, node) {
                symbol_state.conditional.push(id.clone());
            }
            symbol_state.symbols.push(Symbol {
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
            if definition != node {
                // Decorators run in the decorated symbol's frame, before its body.
                let mut cursor = node.walk();
                for child in node.children(&mut cursor) {
                    if child != definition {
                        visit(spec, ctx, child, stack, symbol_state, imports, calls);
                    }
                }
            }
            visit_children(spec, ctx, definition, stack, symbol_state, imports, calls);
            stack.pop();
            return;
        }
    }

    visit_children(spec, ctx, node, stack, symbol_state, imports, calls);
}

fn visit_children<L: LangSpec>(
    spec: &L,
    ctx: &Ctx,
    node: TsNode<'_>,
    stack: &mut Vec<Frame>,
    symbol_state: &mut SymbolState,
    imports: &mut Vec<Import>,
    calls: &mut Vec<PendingCall>,
) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        visit(spec, ctx, child, stack, symbol_state, imports, calls);
    }
}

/// The per-file module pseudo-symbol that owns module-level calls.
///
/// Its span is fixed at line 1 and its body hash covers only the non-blank
/// lines outside top-level symbols. Editing or growing a function therefore
/// leaves the module node unchanged in a graph diff, while editing a
/// module-level statement changes it.
fn module_symbol(ctx: &Ctx, id: &str, symbols: &[Symbol]) -> Symbol {
    let covered: Vec<(usize, usize)> = symbols
        .iter()
        .filter(|symbol| symbol.container.is_none())
        .map(|symbol| (symbol.start_line, symbol.end_line))
        .collect();
    let module_text: Vec<&str> = ctx
        .lines
        .iter()
        .enumerate()
        .filter(|(idx, line)| {
            let line_no = idx + 1;
            !line.trim().is_empty()
                && !covered
                    .iter()
                    .any(|(start, end)| *start <= line_no && line_no <= *end)
        })
        .map(|(_, line)| *line)
        .collect();
    Symbol {
        id: id.to_string(),
        kind: MODULE_SYMBOL_KIND.to_string(),
        name: MODULE_SYMBOL_NAME.to_string(),
        qualified_name: MODULE_SYMBOL_NAME.to_string(),
        file_path: ctx.path.to_string(),
        start_line: 1,
        end_line: 1,
        signature: String::new(),
        container: None,
        content_hash: ctx.content_hash.to_string(),
        body_hash: Some(hex_hash(module_text.join("\n").as_bytes())),
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

/// Line-independent symbol identity (schema v7): a symbol keeps its id when it
/// only moves lines. Same-named symbols of the same kind in one file take an
/// occurrence ordinal in traversal (source) order; the first occurrence has no
/// suffix so the common case stays stable when a duplicate appears later.
pub fn symbol_id(path: &str, qualified_name: &str, kind: &str, occurrence: usize) -> String {
    if occurrence == 0 {
        hex_hash(format!("{path}:{qualified_name}:{kind}").as_bytes())
    } else {
        hex_hash(format!("{path}:{qualified_name}:{kind}#{occurrence}").as_bytes())
    }
}

pub fn hex_hash(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}
