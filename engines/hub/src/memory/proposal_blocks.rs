//! Conservative card-block addressing. Unsupported Markdown stays opaque.
//! No fallback to substring or position is permitted for an absent block id.
use super::ids;
use serde_json::{Value as Json, json};
use std::collections::BTreeSet;

pub fn anchor(id: &str) -> bool {
    id.len() == 8
        && id.starts_with("c-")
        && id[2..]
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn bullet(line: &str) -> bool {
    ["- ", "+ ", "* "].iter().any(|s| line.starts_with(s))
}
/// Exact byte ranges, excluding the final newline. Unknown containers, tables,
/// HTML, references and code are deliberately refused rather than re-addressed.
fn spans(text: &str) -> Vec<(usize, usize)> {
    // Raw HTML can span blank lines and nested containers. Until this bounded
    // classifier can prove its extent, the entire card stays opaque.
    if text.contains('<') {
        return Vec::new();
    }

    let mut result = Vec::new();
    let mut offset = 0;
    let mut front = text.starts_with("---\n");
    let mut fence: Option<(char, usize)> = None;
    let mut start = None;
    let mut end = 0;
    let mut opaque = false;
    for line in text.split_inclusive('\n') {
        let raw = line.trim_end_matches('\n');
        let trimmed = raw.trim_start();
        if front {
            if offset > 0 && raw == "---" {
                front = false;
            }
            offset += line.len();
            continue;
        }
        let marker = trimmed.chars().next().filter(|c| *c == '`' || *c == '~');
        let width = marker
            .map(|m| trimmed.chars().take_while(|c| *c == m).count())
            .unwrap_or(0);
        if let Some((m, n)) = fence {
            if marker == Some(m) && width >= n && trimmed[width..].trim().is_empty() {
                fence = None;
            }
            offset += line.len();
            continue;
        }
        if raw.is_empty() || bullet(raw) || width >= 3 {
            if let Some(s) = start.take() {
                if !opaque {
                    result.push((s, end));
                }
            }
            opaque = false;
        }
        if width >= 3 {
            fence = Some((marker.unwrap(), width));
        } else if !raw.is_empty() {
            if start.is_none() {
                start = Some(offset);
            }
            // Refuse structural ambiguity, including lazy quote continuations,
            // nested lists and suffixes inside inline HTML or code.
            opaque |= raw.starts_with(char::is_whitespace)
                || trimmed.starts_with(['#', '>', '<', '['])
                || trimmed.starts_with("previously")
                || raw.contains(['|', '`', '<', '>'])
                || raw
                    .chars()
                    .all(|c| matches!(c, '-' | '=' | '*' | '_' | ' '));
            end = offset + raw.len();
        }
        offset += line.len();
    }
    if let Some(s) = start {
        if !opaque {
            result.push((s, end));
        }
    }
    result
}

pub fn pin(text: &str, id: &str, hash: &str) -> Result<Json, &'static str> {
    if !anchor(id) {
        return Err("invalid block id");
    }
    let mut seen = BTreeSet::new();
    let mut found = None;
    for (start, end) in spans(text) {
        let block = &text[start..end];
        if let Some((_, name)) = block.rsplit_once(" ^") {
            if anchor(name) {
                if !seen.insert(name) {
                    return Err("duplicate block id");
                }
                if name == id {
                    found = Some(block);
                }
            }
        }
    }
    let bytes = found.ok_or("block id is absent or not structurally addressable")?;
    if ids::sha256_hex(bytes.as_bytes()) != hash {
        return Err("pinned block hash mismatch");
    }
    Ok(json!({"block_id":id,"hash":hash,"bytes":bytes}))
}

/// Pins for every addressable anchor in the current version, for compensation.
pub fn pins(text: &str) -> Vec<Json> {
    spans(text)
        .into_iter()
        .filter_map(|(s, e)| {
            let block = &text[s..e];
            let (_, id) = block.rsplit_once(" ^")?;
            anchor(id).then(
                || json!({"block_id":id,"hash":ids::sha256_hex(block.as_bytes()),"bytes":block}),
            )
        })
        .collect()
}

pub fn apply(text: &str, op: &Json) -> Result<(String, Json), &'static str> {
    let id = op["block_id"].as_str().ok_or("block_id required")?;
    let hash = op["expected_block_hash"]
        .as_str()
        .ok_or("expected_block_hash required")?;
    let pinned = pin(text, id, hash)?;
    let (start, end) = spans(text)
        .into_iter()
        .find(|(s, e)| &text[*s..*e] == pinned["bytes"].as_str().unwrap())
        .ok_or("block absent")?;
    let mut result = text.to_string();
    let old = pinned["bytes"].as_str().unwrap();
    let old_claim = old.strip_suffix(&format!(" ^{id}")).unwrap();
    match op["type"].as_str() {
        Some("replace_block" | "add_previously") => {
            let wording = op["text"].as_str().ok_or("text required")?;
            if wording.trim().is_empty()
                || wording.len() > 8192
                || wording.contains(['\r', '\n', '^'])
                || spans(wording) != vec![(0, wording.len())]
            {
                return Err("text must be one unanchored claim block");
            }
            if op["type"] == "replace_block" {
                result.replace_range(start..end, &format!("{wording} ^{id}"));
                result = format!(
                    "{}\n\npreviously {old_claim}, now {wording}\n",
                    result.trim_end_matches('\n')
                );
            } else {
                result = format!(
                    "{}\n\npreviously {wording}\n",
                    result.trim_end_matches('\n')
                );
            }
        }
        Some("remove_block") => {
            result.replace_range(start..end, "");
            result = format!(
                "{}\n\npreviously {old_claim}\n",
                result.trim_end_matches('\n')
            );
        }
        _ => return Err("unsupported typed block op"),
    }
    let result = ids::normalize(&result);
    if result.len() > ids::MAX_TEXT_BYTES {
        return Err("result exceeds card byte limit");
    }
    Ok((result, pinned))
}

/// Whole-file unified diff. Every old/new line appears once, so this remains
/// linear even for the largest admitted card and reveals the exact review bytes.
pub fn diff(scope: &str, card: &str, before: &str, after: &str) -> String {
    if before == after {
        return String::new();
    }
    let mut out = format!(
        "--- a/{scope}/{card}\n+++ b/{scope}/{card}\n@@ -1,{} +1,{} @@\n",
        before.lines().count(),
        after.lines().count()
    );
    for (prefix, text) in [('-', before), ('+', after)] {
        for line in text.lines() {
            out.push(prefix);
            out.push_str(line);
            out.push('\n');
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn opaque_markdown_and_duplicate_ids_never_resolve_by_position() {
        for body in [
            "```\nClaim. ^c-abcdef\n```\n",
            "<!--\n\nClaim. ^c-abcdef\n\n-->\n",
            "<div>\n\nClaim. ^c-abcdef\n\n</div>\n",
            "> Quote\nClaim. ^c-abcdef\n",
            "    Claim. ^c-abcdef\n",
            "# Claim. ^c-abcdef\n",
            "Claim. ^c-abcdef\n\nAnother. ^c-abcdef\n",
        ] {
            assert!(
                pin(body, "c-abcdef", &ids::sha256_hex(b"Claim. ^c-abcdef")).is_err(),
                "{body}"
            );
        }
    }
    #[test]
    fn supported_claim_edits_preserve_anchors_and_history() {
        let text = "---\ntitle: Notes\n---\n# Notes\n\n- Claim. ^c-abcdef\n";
        let base = json!({"block_id":"c-abcdef","expected_block_hash":ids::sha256_hex(b"- Claim. ^c-abcdef")});
        for kind in ["replace_block", "add_previously", "remove_block"] {
            let mut op = base.clone();
            op["type"] = json!(kind);
            op["text"] = json!("- Revised.");
            let (after, _) = apply(text, &op).unwrap();
            assert!(after.starts_with("---\ntitle: Notes\n---\n# Notes\n"));
            assert!(after.contains("previously"));
            if kind == "replace_block" {
                assert!(after.contains("- Revised. ^c-abcdef"));
            }
            if kind == "remove_block" {
                assert!(!after.contains("Claim. ^c-abcdef"));
            }
        }
    }
}
