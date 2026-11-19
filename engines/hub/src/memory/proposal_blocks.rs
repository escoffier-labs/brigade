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
    let mut front = text.starts_with("---\n") || text.starts_with("---\r\n");
    let mut fence: Option<(char, usize)> = None;
    let mut start = None;
    let mut end = 0;
    let mut opaque = false;
    for line in text.split_inclusive('\n') {
        let raw = line.strip_suffix('\n').unwrap_or(line);
        let raw = raw.strip_suffix('\r').unwrap_or(raw);
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

/// One suffix parser for addressing, anchoring and removal. Horizontal
/// whitespace belongs to the original block and is retained verbatim.
fn suffix(block: &str) -> Option<(usize, usize, &str)> {
    let trimmed = block.trim_end_matches([' ', '\t']);
    let (prefix, id) = trimmed.rsplit_once(" ^")?;
    anchor(id).then_some((prefix.len(), trimmed.len(), id))
}

fn id_counts(text: &str) -> std::collections::BTreeMap<String, usize> {
    let mut counts = std::collections::BTreeMap::new();
    for (i, _) in text.match_indices('^') {
        if let Some(id) = text.get(i + 1..i + 9).filter(|id| anchor(id)) {
            *counts.entry(id.to_string()).or_insert(0) += 1;
        }
    }
    counts
}

/// Add only suffix bytes and the reservations for the ids this transform
/// mints. Every anchor spelling stays reserved, even in opaque text.
pub fn anchor_blocks(text: &str, card_id: &str) -> Result<String, &'static str> {
    let mut used = BTreeSet::new();
    for (i, _) in text.match_indices('^') {
        if let Some(id) = text.get(i + 1..i + 9).filter(|id| anchor(id)) {
            used.insert(id.to_string());
        }
    }
    // Retired anchors in metadata remain reserved too.
    let mut reserved = super::proposal_metadata::list(text, "claim_anchor_ids")?;
    if reserved.iter().any(|id| !anchor(id)) {
        return Err("invalid claim anchor reservation");
    }
    used.extend(reserved.iter().cloned());
    let mut result = text.to_string();
    let mut edits = Vec::new();
    let mut seen = BTreeSet::new();
    for (position, (start, end)) in spans(text).into_iter().enumerate() {
        let block = &text[start..end];
        if let Some((_, _, id)) = suffix(block) {
            if !seen.insert(id) {
                return Err("duplicate block id");
            }
            continue;
        }
        let mut nonce = 0;
        let id = loop {
            let digest =
                ids::sha256_hex(format!("{card_id}\0{position}\0{block}\0{nonce}").as_bytes());
            let id = format!("c-{}", &digest[..6]);
            if used.insert(id.clone()) {
                break id;
            }
            nonce += 1;
        };
        if used.len() > 4096 {
            return Err("claim anchor reservation limit exceeded");
        }
        reserved.insert(id.clone());
        edits.push((start + block.trim_end_matches([' ', '\t']).len(), id));
    }
    if edits.is_empty() {
        return Ok(text.to_string());
    }
    for (end, id) in edits.into_iter().rev() {
        result.insert_str(end, &format!(" ^{id}"));
    }
    super::proposal_metadata::reserve_anchors(&result, &reserved)
}

/// The only diff an anchor transform may make: an added ` ^<id>` suffix on a
/// block that carried none, and exactly those ids added to the card's
/// `claim_anchor_ids` list. Every other claim byte and metadata line stays
/// before's. Fail closed on any ambiguity.
pub fn proven_delta(before: &str, after: &str) -> bool {
    let (Ok(before_reserved), Ok(after_reserved)) = (
        super::proposal_metadata::list(before, "claim_anchor_ids"),
        super::proposal_metadata::list(after, "claim_anchor_ids"),
    ) else {
        return false;
    };
    let (before_counts, after_counts) = (id_counts(before), id_counts(after));
    // No prior spelling loses an occurrence.
    if before_counts
        .iter()
        .any(|(id, count)| after_counts.get(id) != Some(count))
    {
        return false;
    }
    let added: BTreeSet<String> = after_counts
        .keys()
        .filter(|id| !before_counts.contains_key(*id))
        .cloned()
        .collect();
    if added.is_empty() || added.iter().any(|id| after_counts.get(id) != Some(&1)) {
        return false;
    }
    // The reservation list gains exactly the ids the transform added.
    if !before_reserved.is_subset(&after_reserved)
        || after_reserved
            .difference(&before_reserved)
            .cloned()
            .collect::<BTreeSet<_>>()
            != added
    {
        return false;
    }
    // No other metadata line and no other claim byte changes.
    matches!(
        (
            super::proposal_metadata::other_lines(before, "claim_anchor_ids"),
            super::proposal_metadata::other_lines(after, "claim_anchor_ids"),
        ),
        (Ok(kept), Ok(written)) if kept == written
    ) && matches!(
        (
            super::proposal_metadata::claims(before),
            super::proposal_metadata::claims(after),
        ),
        (Ok(kept), Ok(written)) if kept == written
    )
}

pub fn pin(text: &str, id: &str, hash: &str) -> Result<Json, &'static str> {
    if !anchor(id) {
        return Err("invalid block id");
    }
    let mut seen = BTreeSet::new();
    let mut found = None;
    for (start, end) in spans(text) {
        let block = &text[start..end];
        if let Some((_, _, name)) = suffix(block) {
            if !seen.insert(name) {
                return Err("duplicate block id");
            }
            if name == id {
                found = Some(block);
            }
        }
    }
    let bytes = found.ok_or("block id is absent or not structurally addressable")?;
    if ids::sha256_hex(bytes.as_bytes()) != hash {
        return Err("pinned block hash mismatch");
    }
    Ok(json!({"block_id":id,"hash":hash,"bytes":bytes}))
}

/// Strip suffixes by exact structural ranges, preserving opaque block bytes.
pub fn strip_anchors(text: &str) -> String {
    let mut result = text.to_string();
    for (start, end) in spans(text).into_iter().rev() {
        if let Some((s, e, _)) = suffix(&text[start..end]) {
            result.replace_range(start + s..start + e, "");
        }
    }
    result
}

/// Pins for every addressable anchor in the current version, for compensation.
pub fn pins(text: &str) -> Vec<Json> {
    spans(text)
        .into_iter()
        .filter_map(|(s, e)| {
            let block = &text[s..e];
            let (_, _, id) = suffix(block)?;
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
    let (s, e, _) = suffix(old).ok_or("block suffix absent")?;
    let old_claim = format!("{}{}", &old[..s], &old[e..]);
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
pub(super) fn span_texts(text: &str) -> Vec<&str> {
    spans(text).into_iter().map(|(s, e)| &text[s..e]).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn anchor_proof_admits_only_added_suffixes_and_their_reservations() {
        let before = "---\ntitle: Notes\nevidence: []\n---\nFirst claim.\n\n- Second. ^c-abcdef\n";
        let after = anchor_blocks(before, "a").unwrap();
        assert!(proven_delta(before, &after), "{after}");
        assert_eq!(anchor_blocks(&after, "a").unwrap(), after);
        // Another metadata field may not move.
        assert!(!proven_delta(
            before,
            &after.replace("title: Notes", "title: Other")
        ));
        assert!(!proven_delta(
            before,
            &after.replace("evidence: []", "evidence: [\"ns/a/b\"]")
        ));
        // A reserved id that no suffix added is refused.
        let extra = after.replacen("]\n---\n", ", \"c-000000\"]\n---\n", 1);
        assert_ne!(extra, after);
        assert!(!proven_delta(before, &extra));
        // Dropping a claim anchor while keeping its reservation is refused.
        assert!(!proven_delta(
            before,
            &after.replace("- Second. ^c-abcdef", "- Second.")
        ));
    }
    #[test]
    fn an_indented_reservation_line_is_refused_rather_than_rewritten() {
        let before = "---\n claim_anchor_ids: [\"c-111111\"]\n---\nClaim.\n";
        // The writer's stripped key and the reader's exact key disagree, so the
        // transform refuses instead of rewriting a line it never read.
        let ids = BTreeSet::from(["c-111111".to_string(), "c-222222".to_string()]);
        assert!(crate::memory::proposal_metadata::reserve_anchors(before, &ids).is_err());
    }
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
