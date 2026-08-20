//! Astro extractor: frontmatter and `<script>` as TypeScript, plus template
//! component tags that match those imports.

use anyhow::Result;
use tree_sitter_typescript::LANGUAGE_TYPESCRIPT;

use crate::extractors::common::{hex_hash, symbol_id};
use crate::extractors::typescript::extract_typescript_as;
use crate::model::{CallKind, FileGraph, Import, PendingCall, Symbol};

/// Bump when Astro extraction output can change for the same file content.
pub const EXTRACTOR_FINGERPRINT: &str = "astro-extractor-v1";

pub fn extract_astro(path: &str, content: &str, content_hash: &str) -> Result<FileGraph> {
    let script_source = mask_to_script_source(content);
    let mut graph = extract_typescript_as(
        path,
        &script_source,
        content_hash,
        LANGUAGE_TYPESCRIPT.into(),
    )?;
    rewrite_astro_default_imports(&mut graph.imports);
    let component_id = ensure_component_symbol(&mut graph, path, content, content_hash);
    collect_template_component_calls(&mut graph, path, content, &component_id);
    Ok(graph)
}

fn mask_to_script_source(content: &str) -> String {
    let mut keep = vec![false; content.len()];
    mark_range(&mut keep, frontmatter_body_range(content));
    for range in script_body_ranges(content) {
        mark_range(&mut keep, Some(range));
    }
    content
        .char_indices()
        .map(|(index, ch)| {
            if keep.get(index).copied().unwrap_or(false) || ch == '\n' || ch == '\r' {
                ch
            } else {
                ' '
            }
        })
        .collect()
}

fn mark_range(keep: &mut [bool], range: Option<std::ops::Range<usize>>) {
    let Some(range) = range else {
        return;
    };
    let end = range.end.min(keep.len());
    for slot in keep.iter_mut().take(end).skip(range.start) {
        *slot = true;
    }
}

fn frontmatter_body_range(content: &str) -> Option<std::ops::Range<usize>> {
    let opener_len = if content.starts_with("---\r\n") {
        5
    } else if content.starts_with("---\n") {
        4
    } else {
        return None;
    };
    let mut offset = opener_len;
    for line in content[opener_len..].split_inclusive('\n') {
        let trimmed = line.trim_end_matches(['\n', '\r']);
        if trimmed == "---" {
            return Some(opener_len..offset);
        }
        offset += line.len();
    }
    None
}

fn script_body_ranges(content: &str) -> Vec<std::ops::Range<usize>> {
    let lower = content.to_ascii_lowercase();
    let mut ranges = Vec::new();
    let mut search_from = 0;
    while let Some(rel) = lower[search_from..].find("<script") {
        let open = search_from + rel;
        let Some(gt_rel) = content[open..].find('>') else {
            break;
        };
        let tag_end = open + gt_rel;
        let tag = &lower[open..=tag_end];
        search_from = tag_end + 1;
        if tag.contains("src=") || !is_javascript_script_tag(tag) {
            continue;
        }
        let body_start = tag_end + 1;
        let Some(close_rel) = lower[body_start..].find("</script") else {
            break;
        };
        ranges.push(body_start..body_start + close_rel);
        search_from = body_start + close_rel;
    }
    ranges
}

fn is_javascript_script_tag(tag_lower: &str) -> bool {
    if !tag_lower.contains("type=") {
        return true;
    }
    ["module", "text/javascript", "text/typescript", "text/tsx"]
        .iter()
        .any(|allowed| {
            tag_lower.contains(&format!("type=\"{allowed}\""))
                || tag_lower.contains(&format!("type='{allowed}'"))
        })
}

fn rewrite_astro_default_imports(imports: &mut [Import]) {
    for import in imports {
        if astro_module_stem(&import.module).is_none() {
            continue;
        }
        if import.imported_name.as_deref() != Some("default") {
            continue;
        }
        if let Some(stem) = astro_module_stem(&import.module) {
            import.imported_name = Some(stem);
        }
    }
}

fn astro_module_stem(module: &str) -> Option<String> {
    let file = module.rsplit('/').next()?;
    let stem = file.strip_suffix(".astro")?;
    if stem.is_empty() {
        None
    } else {
        Some(stem.to_string())
    }
}

fn component_name(path: &str) -> Option<String> {
    astro_module_stem(path).or_else(|| {
        std::path::Path::new(path)
            .file_stem()
            .and_then(|stem| stem.to_str())
            .filter(|stem| !stem.is_empty())
            .map(ToString::to_string)
    })
}

fn ensure_component_symbol(
    graph: &mut FileGraph,
    path: &str,
    content: &str,
    content_hash: &str,
) -> String {
    let name = component_name(path).unwrap_or_else(|| "default".to_string());
    if let Some(existing) = graph
        .symbols
        .iter()
        .find(|symbol| symbol.name == name && symbol.kind == "function")
    {
        return existing.id.clone();
    }
    let end_line = content.lines().count().max(1);
    let id = symbol_id(path, &name, "function", 0);
    graph.symbols.insert(
        0,
        Symbol {
            id: id.clone(),
            kind: "function".to_string(),
            name: name.clone(),
            qualified_name: name.clone(),
            file_path: path.to_string(),
            start_line: 1,
            end_line,
            signature: format!("export default function {name}"),
            container: None,
            content_hash: content_hash.to_string(),
            body_hash: Some(hex_hash(content.as_bytes())),
        },
    );
    id
}

fn collect_template_component_calls(
    graph: &mut FileGraph,
    path: &str,
    content: &str,
    source_id: &str,
) {
    let imported: std::collections::HashSet<&str> = graph
        .imports
        .iter()
        .filter_map(|import| import.local_name.as_deref())
        .collect();
    if imported.is_empty() {
        return;
    }
    let skip = skipped_byte_ranges(content);
    let bytes = content.as_bytes();
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] != b'<' {
            index += 1;
            continue;
        }
        let name_start = index + 1;
        if name_start >= bytes.len() || !bytes[name_start].is_ascii_uppercase() {
            index += 1;
            continue;
        }
        let mut name_end = name_start + 1;
        while name_end < bytes.len()
            && (bytes[name_end].is_ascii_alphanumeric() || bytes[name_end] == b'_')
        {
            name_end += 1;
        }
        if skip.iter().any(|range| range.contains(&index)) {
            index = name_end;
            continue;
        }
        let name = std::str::from_utf8(&bytes[name_start..name_end]).unwrap_or("");
        if imported.contains(name) {
            graph.calls.push(PendingCall {
                source_id: source_id.to_string(),
                target_name: name.to_string(),
                qualifier: None,
                kind: CallKind::Bare,
                line: line_number_at(content, index),
                source_file: path.to_string(),
            });
        }
        index = name_end;
    }
}

fn skipped_byte_ranges(content: &str) -> Vec<std::ops::Range<usize>> {
    let mut ranges = Vec::new();
    if let Some(frontmatter) = frontmatter_body_range(content) {
        // Include the opening fence so JSX-like text in frontmatter is not scanned twice.
        ranges.push(0..frontmatter.end);
        let after = frontmatter.end;
        if let Some(rel) = content[after..].find("---") {
            ranges.push(after..after + rel + 3);
        }
    }
    ranges.extend(script_body_ranges(content));
    ranges
}

fn line_number_at(content: &str, byte_index: usize) -> usize {
    content.get(..byte_index).map_or(1, |prefix| {
        prefix.bytes().filter(|byte| *byte == b'\n').count() + 1
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn graph(path: &str, source: &str) -> FileGraph {
        extract_astro(path, source, "hash").unwrap()
    }

    #[test]
    fn astro_frontmatter_imports_and_functions_are_indexed() {
        let g = graph(
            "src/pages/index.astro",
            r#"---
import Layout from "../layouts/Layout.astro";
export function greeting() {
  return title();
}
const title = () => "page";
---
<Layout>
  <h1>Hello</h1>
</Layout>
"#,
        );
        let names: Vec<&str> = g
            .symbols
            .iter()
            .map(|symbol| symbol.name.as_str())
            .collect();
        assert!(names.contains(&"greeting"), "{names:?}");
        assert!(names.contains(&"title"), "{names:?}");
        assert!(names.contains(&"index"), "{names:?}");
        assert_eq!(g.imports[0].module, "../layouts/Layout.astro");
        assert_eq!(g.imports[0].local_name.as_deref(), Some("Layout"));
        assert_eq!(g.imports[0].imported_name.as_deref(), Some("Layout"));
        assert!(
            g.calls
                .iter()
                .any(|call| call.target_name == "Layout"
                    && call.source_file == "src/pages/index.astro"),
            "{:?}",
            g.calls
                .iter()
                .map(|call| &call.target_name)
                .collect::<Vec<_>>()
        );
        let greeting = g
            .symbols
            .iter()
            .find(|symbol| symbol.name == "greeting")
            .expect("greeting");
        assert_eq!(greeting.start_line, 3);
    }

    #[test]
    fn astro_script_symbols_are_indexed() {
        let g = graph(
            "src/components/Widget.astro",
            r#"---
const n = 1;
---
<script>
function hydrate() {
  setup();
}
</script>
"#,
        );
        let names: Vec<&str> = g
            .symbols
            .iter()
            .map(|symbol| symbol.name.as_str())
            .collect();
        assert!(names.contains(&"hydrate"), "{names:?}");
        assert!(names.contains(&"Widget"), "{names:?}");
        assert!(g.calls.iter().any(|call| call.target_name == "setup"));
    }

    #[test]
    fn astro_file_without_frontmatter_still_has_a_component_symbol() {
        let g = graph(
            "src/layouts/Page.astro",
            "<html><body><slot /></body></html>\n",
        );
        assert!(g.symbols.iter().any(|symbol| symbol.name == "Page"));
        assert!(g.imports.is_empty());
    }

    #[test]
    fn aliased_layout_import_targets_the_file_stem() {
        let g = graph(
            "src/pages/index.astro",
            r#"---
import BaseLayout from "../layouts/Layout.astro";
---
<BaseLayout />
"#,
        );
        assert_eq!(g.imports[0].local_name.as_deref(), Some("BaseLayout"));
        assert_eq!(g.imports[0].imported_name.as_deref(), Some("Layout"));
        assert!(g.calls.iter().any(|call| call.target_name == "BaseLayout"));
    }
}
