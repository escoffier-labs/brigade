//! Python extractor: AST-based symbols, imports, and call edges.

use anyhow::Result;
use tree_sitter::Node as TsNode;

use crate::extractors::common::{LangSpec, extract_with, node_text};
use crate::model::{CallTarget, FileGraph, Import};

/// Bump when Python extraction output can change for the same file content.
/// v3: module-level calls belong to a `<module>` pseudo-symbol, decorated
/// definitions span their decorators, and `from x import *` is recorded.
/// v4: literal `__all__` lists, import scope, and a stable `<module>` span.
/// v5: conditional module-scope imports and definitions are recorded.
pub const EXTRACTOR_FINGERPRINT: &str = "python-extractor-v5";

/// `imported_name` of a `from x import *` row.
pub const WILDCARD_IMPORT: &str = "*";

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

    fn decorated_definition<'t>(&self, node: TsNode<'t>) -> Option<TsNode<'t>> {
        (node.kind() == "decorated_definition")
            .then(|| node.child_by_field_name("definition"))
            .flatten()
    }

    fn module_scope_calls(&self) -> bool {
        true
    }

    fn conditional_block(&self, kind: &str) -> bool {
        matches!(
            kind,
            "if_statement"
                | "try_statement"
                | "with_statement"
                | "for_statement"
                | "while_statement"
                | "match_statement"
        )
    }

    fn module_exports(&self, root: TsNode<'_>, source: &[u8]) -> Option<Vec<String>> {
        let mut exports: Option<Vec<String>> = None;
        let mut understood = 0;
        let mut cursor = root.walk();
        for statement in root.named_children(&mut cursor) {
            let Some(assignment) = statement
                .named_child(0)
                .filter(|_| statement.kind() == "expression_statement")
            else {
                continue;
            };
            let extend = match assignment.kind() {
                "assignment" => false,
                "augmented_assignment" => true,
                _ => continue,
            };
            let Some(left) = assignment.child_by_field_name("left") else {
                continue;
            };
            if left.kind() != "identifier" || node_text(left, source) != DUNDER_ALL {
                continue;
            }
            if extend
                && assignment
                    .child_by_field_name("operator")
                    .is_none_or(|op| node_text(op, source) != "+=")
            {
                return None;
            }
            let names = literal_string_sequence(assignment.child_by_field_name("right")?, source)?;
            match (&mut exports, extend) {
                (Some(current), true) => current.extend(names),
                (None, true) => return None,
                (_, false) => exports = Some(names),
            }
            understood += 1;
        }
        // Any other mention (a nested assignment, `__all__.append(...)`) makes
        // the list dynamic, so extraction claims no export list at all.
        (count_identifier(root, source, DUNDER_ALL) == understood)
            .then_some(exports)
            .flatten()
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
                            module_scope: true,
                            conditional: false,
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
                        if child.kind() == "wildcard_import" {
                            // `from x import *` binds no single local name.
                            out.push(Import {
                                module: module_text.clone(),
                                local_name: None,
                                imported_name: Some(WILDCARD_IMPORT.to_string()),
                                alias: None,
                                line,
                                module_scope: true,
                                conditional: false,
                            });
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
                            module_scope: true,
                            conditional: false,
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

const DUNDER_ALL: &str = "__all__";

/// The strings of a list or tuple literal made only of plain string literals.
fn literal_string_sequence(node: TsNode<'_>, source: &[u8]) -> Option<Vec<String>> {
    if !matches!(node.kind(), "list" | "tuple") {
        return None;
    }
    let mut cursor = node.walk();
    node.named_children(&mut cursor)
        .map(|item| {
            if item.kind() != "string" {
                return None;
            }
            let mut parts = item.walk();
            let mut text = String::new();
            for part in item.named_children(&mut parts) {
                match part.kind() {
                    "string_start" | "string_end" => {}
                    "string_content" => text.push_str(&node_text(part, source)),
                    // Interpolations and escapes make the value non-literal.
                    _ => return None,
                }
            }
            Some(text)
        })
        .collect()
}

fn count_identifier(node: TsNode<'_>, source: &[u8], name: &str) -> usize {
    if node.kind() == "identifier" {
        return usize::from(node_text(node, source) == name);
    }
    let mut cursor = node.walk();
    node.children(&mut cursor)
        .map(|child| count_identifier(child, source, name))
        .sum()
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
    fn python_module_level_call_attributed_to_module_symbol() {
        let g = graph(
            r#"
def main():
    pass

if __name__ == "__main__":
    main()
"#,
        );
        let module = g
            .symbols
            .iter()
            .find(|s| s.kind == "module")
            .expect("module pseudo-symbol");
        assert_eq!(module.name, "<module>");
        assert_eq!(module.qualified_name, "<module>");
        assert_eq!(module.container, None);
        // A fixed one-line span keeps the module node stable in graph diffs.
        assert_eq!((module.start_line, module.end_line), (1, 1));
        let call = g
            .calls
            .iter()
            .find(|c| c.target_name == "main")
            .expect("module-level call is kept");
        assert_eq!(call.source_id, module.id);
        assert_eq!(call.line, 6);
    }

    #[test]
    fn python_module_body_hash_ignores_function_bodies() {
        let module_hash = |source: &str| {
            graph(source)
                .symbols
                .into_iter()
                .find(|s| s.kind == "module")
                .and_then(|s| s.body_hash)
                .expect("module body hash")
        };
        let base = module_hash("def main():\n    return 1\n\nmain()\n");
        assert_eq!(
            base,
            module_hash("def main():\n    return 2\n\nmain()\n"),
            "a function body edit leaves the module unchanged"
        );
        assert_ne!(
            base,
            module_hash("def main():\n    return 1\n\nmain()\nmain()\n")
        );
    }

    #[test]
    fn python_module_symbol_only_when_module_level_calls_exist() {
        let g = graph("import os\n\ndef run():\n    os.getcwd()\n");
        assert!(g.symbols.iter().all(|s| s.kind != "module"));
    }

    #[test]
    fn python_decorated_definition_span_includes_decorators() {
        let g = graph(
            r#"import functools

@functools.lru_cache(maxsize=None)
def cached():
    return helper()

class Box:
    @staticmethod
    @register("x")
    def build():
        pass
"#,
        );
        let cached = g
            .symbols
            .iter()
            .find(|s| s.name == "cached")
            .expect("cached symbol");
        assert_eq!((cached.start_line, cached.end_line), (3, 5));
        assert_eq!(cached.signature, "def cached():");
        let lru = g
            .calls
            .iter()
            .find(|c| c.target_name == "lru_cache")
            .expect("decorator call is kept");
        assert_eq!(lru.source_id, cached.id);
        assert_eq!(lru.line, 3);

        let build = g
            .symbols
            .iter()
            .find(|s| s.name == "build")
            .expect("build symbol");
        assert_eq!((build.start_line, build.end_line), (8, 11));
        assert_eq!(build.signature, "def build():");
        assert_eq!(build.container.as_deref(), Some("Box"));
        let register = g
            .calls
            .iter()
            .find(|c| c.target_name == "register")
            .expect("method decorator call");
        assert_eq!(register.source_id, build.id);
        // Decorators never need the module pseudo-symbol.
        assert!(g.symbols.iter().all(|s| s.kind != "module"));
    }

    #[test]
    fn python_literal_dunder_all_is_extracted() {
        assert_eq!(graph("def f():\n    pass\n").exports, None);
        assert_eq!(
            graph("__all__ = ['a', \"b\"]\n__all__ += ('c',)\n").exports,
            Some(vec!["a".to_string(), "b".to_string(), "c".to_string()])
        );
        assert_eq!(graph("__all__ = []\n").exports, Some(Vec::new()));
        for dynamic in [
            "__all__ = names()\n",
            "__all__ = ['a']\n__all__.append('b')\n",
            "__all__ = ['a']\nif x:\n    __all__ = ['b']\n",
            "__all__ = [f'{x}']\n",
            "__all__ += ['a']\n",
        ] {
            assert_eq!(graph(dynamic).exports, None, "{dynamic}");
        }
    }

    #[test]
    fn python_import_scope_is_recorded() {
        let g = graph(
            "import os\ntry:\n    from a import x\nexcept ImportError:\n    pass\n\ndef f():\n    from b import y\n\nclass C:\n    from c import z\n",
        );
        let scopes: Vec<(&str, bool)> = g
            .imports
            .iter()
            .map(|import| (import.module.as_str(), import.module_scope))
            .collect();
        assert_eq!(
            scopes,
            [("os", true), ("a", true), ("b", false), ("c", false)]
        );
        let conditional: Vec<(&str, bool)> = g
            .imports
            .iter()
            .map(|import| (import.module.as_str(), import.conditional))
            .collect();
        assert_eq!(
            conditional,
            [("os", false), ("a", true), ("b", false), ("c", false)]
        );
    }

    #[test]
    fn python_conditional_top_level_definitions_are_recorded() {
        let g = graph(
            "def plain():
    pass

if FLAG:
    def guarded():
        def inner():
            pass
else:
    @wrap
    def other():
        pass

class C:
    if FLAG:
        def method(self):
            pass
",
        );
        let conditional: Vec<&str> = g
            .symbols
            .iter()
            .filter(|symbol| g.conditional_symbols.contains(&symbol.id))
            .map(|symbol| symbol.qualified_name.as_str())
            .collect();
        assert_eq!(conditional, ["guarded", "other"]);
    }

    #[test]
    fn python_wildcard_import_is_recorded() {
        let g = graph("from .core import *\nfrom pkg.util import *\n");
        let stars: Vec<(&str, Option<&str>, Option<&str>)> = g
            .imports
            .iter()
            .map(|import| {
                (
                    import.module.as_str(),
                    import.imported_name.as_deref(),
                    import.local_name.as_deref(),
                )
            })
            .collect();
        assert_eq!(
            stars,
            [(".core", Some("*"), None), ("pkg.util", Some("*"), None)]
        );
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
