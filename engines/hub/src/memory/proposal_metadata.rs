//! Bounded metadata edits for proposal v2 ops. Complex YAML is refused.
//! Evidence references remain unverified citations, never attested proof (S19e).
use super::{ids, proposal_blocks, text};
use serde_json::{Value as Json, json};
use std::collections::BTreeSet;

fn front(text: &str) -> Result<(Vec<&str>, &str), &'static str> {
    if let Some(rest) = text.strip_prefix("---\n") {
        let (meta, body) = rest
            .split_once("\n---\n")
            .ok_or("unterminated card metadata")?;
        Ok((meta.lines().collect(), body))
    } else {
        Ok((Vec::new(), text))
    }
}
pub(super) fn get(text: &str, key: &str) -> Result<Option<String>, &'static str> {
    let (meta, _) = front(text)?;
    let mut found = None;
    for (i, line) in meta.iter().enumerate() {
        if let Some((k, value)) = line.split_once(':') {
            if k == key {
                if found.is_some()
                    || value.trim_start().starts_with(['|', '>'])
                    || meta
                        .iter()
                        .skip(i + 1)
                        .find(|s| !s.trim().is_empty())
                        .is_some_and(|s| s.starts_with(char::is_whitespace) || s.starts_with("- "))
                {
                    return Err("ambiguous or multiline metadata field");
                }
                found = Some(value.trim().to_string());
            }
        }
    }
    Ok(found)
}
pub fn list(text: &str, key: &str) -> Result<BTreeSet<String>, &'static str> {
    let Some(value) = get(text, key)? else {
        return Ok(BTreeSet::new());
    };
    let values: Vec<String> = serde_json::from_str(&value)
        .map_err(|_| "existing metadata list must use JSON string syntax")?;
    if values.len() > 256 || values.iter().any(|v| v.len() > 1024) {
        return Err("metadata list exceeds bounds");
    }
    Ok(values.into_iter().collect())
}
/// Compact JSON with every reader boundary encoded as a Unicode escape.
/// Work from valid serializer output, preserving escaped literal backslashes.
fn front_json(value: &Json) -> String {
    let encoded = value.to_string();
    let mut chars = encoded.chars();
    let mut out = String::with_capacity(encoded.len());
    while let Some(c) = chars.next() {
        if c == '\\' {
            let escaped = chars.next().unwrap();
            // These are JSON's short escapes for line boundaries. Other
            // controls already use Unicode escapes or are not line breaks.
            let boundary = match escaped {
                'n' => Some('\n'),
                'r' => Some('\r'),
                'f' => Some('\u{c}'),
                _ => None,
            };
            if let Some(c) = boundary {
                out.push_str(&format!("\\u{:04x}", c as u32));
            } else {
                out.push('\\');
                out.push(escaped);
            }
        } else if text::py_line_break(c) {
            out.push_str(&format!("\\u{:04x}", c as u32));
        } else {
            out.push(c);
        }
    }
    out
}
/// The one key rule shared by the writer and by search. A front-matter line
/// belongs to `key` only when the text before its first colon is exactly `key`.
fn is_key(line: &str, key: &str) -> bool {
    line.split_once(':').is_some_and(|(k, _)| k == key)
}
/// The card without any front-matter line that [`set`] would treat as `key`.
/// Search uses it to leave out `evidence`, so the drop is never looser than
/// the writer's parser and every other byte stays as stored. The lines come
/// from [`front`], the parser `set` uses, so a blank line or a final newline
/// in the metadata reads the same before and after a `set`. A front matter
/// left with no lines goes too, so a card that gains its first `evidence`
/// field reads the same as before. Nothing is dropped when the writer's
/// closing fence and the search parser's [`text::front_matter_body`] disagree
/// on where the metadata ends, because the two would then name different lines.
pub fn without_key(text: &str, key: &str) -> String {
    let Ok((meta, body)) = front(text) else {
        return text.to_string();
    };
    if !meta.iter().any(|line| is_key(line, key)) || text::front_matter_body(text) != Some(body) {
        return text.to_string();
    }
    let kept: Vec<&str> = meta.into_iter().filter(|l| !is_key(l, key)).collect();
    if kept.is_empty() {
        return body.to_string();
    }
    format!("---\n{}\n---\n{body}", kept.join("\n"))
}
/// The most ids the anchoring transform reserves for one card.
const MAX_ANCHORS: usize = 4096;
/// A `claim_anchor_ids` line holding only ids the writer mints: the exact key,
/// one space, a bounded JSON list of anchor ids and nothing after it. Any
/// other value, such as a comment or a non-id string, stays searchable.
fn anchor_id_line(line: &str) -> bool {
    let Some(value) = line.strip_prefix("claim_anchor_ids: ") else {
        return false;
    };
    let Ok(ids) = serde_json::from_str::<Vec<String>>(value) else {
        return false;
    };
    ids.len() <= MAX_ANCHORS && ids.iter().all(|id| proposal_blocks::anchor(id))
}
/// The card without the front-matter lines [`set`] would treat as any of
/// `keys`, and without blank front-matter lines. Search reads it, because the
/// writer inserts a field at the closing fence, after a trailing blank line,
/// and the blank must not change the text a card reads before and after that.
/// A `claim_anchor_ids` line is dropped only while it holds nothing but ids,
/// so a comment someone adds to it is still searched and digest-relevant.
/// Nothing is dropped when the writer's closing fence and the search parser's
/// differ.
pub fn without_keys(text: &str, keys: &[&str]) -> String {
    let Ok((meta, body)) = front(text) else {
        return text.to_string();
    };
    if text::front_matter_body(text) != Some(body) {
        return text.to_string();
    }
    let kept: Vec<&str> = meta
        .into_iter()
        .filter(|line| {
            if line.trim().is_empty() {
                return false;
            }
            !keys.iter().any(|key| {
                is_key(line, key) && (*key != "claim_anchor_ids" || anchor_id_line(line))
            })
        })
        .collect();
    if kept.is_empty() {
        return body.to_string();
    }
    format!("---\n{}\n---\n{body}", kept.join("\n"))
}
/// The non-blank front-matter lines the writer's parser reads, except any line
/// [`set`] would treat as `key`. Empty when the card has no front matter.
pub fn lines_without_key<'a>(text: &'a str, key: &str) -> Vec<&'a str> {
    let Ok((meta, _)) = front(text) else {
        return Vec::new();
    };
    meta.into_iter()
        .filter(|l| !l.trim().is_empty() && !is_key(l, key))
        .collect()
}
/// One front-matter fence line: three dashes, then only spaces or tabs.
fn fence(line: &str) -> bool {
    let line = line.strip_suffix('\n').unwrap_or(line);
    let line = line.strip_suffix('\r').unwrap_or(line);
    line.strip_prefix("---")
        .is_some_and(|rest| rest.chars().all(|c| c == ' ' || c == '\t'))
}
/// Python `memory_proposals._body_offset`: the byte extent of the leading front
/// matter, fence included, or `None` when the card has no front matter. A card
/// that opens a fence without closing it is left to the caller to refuse.
fn body_offset(text: &str) -> Option<usize> {
    let first = text.split_inclusive('\n').next()?;
    if !first.ends_with('\n') || !fence(first) {
        return None;
    }
    let mut offset = first.len();
    for line in text[offset..].split_inclusive('\n') {
        offset += line.len();
        if fence(line) {
            return Some(offset);
        }
    }
    None
}
/// The local proposal path's reservation list rendering, exactly
/// `json.dumps(sorted(ids))`: quoted ids with a comma and a space between them.
fn anchor_list(ids: &BTreeSet<String>) -> String {
    let quoted: Vec<String> = ids.iter().map(|id| format!("\"{id}\"")).collect();
    format!("[{}]", quoted.join(", "))
}
/// Record claim anchor ids exactly as the local proposal path writes them. The
/// field keeps its place when present and is inserted at the closing fence
/// otherwise; a card without front matter gains one. Every other byte stays.
///
/// An indented `claim_anchor_ids` line is refused rather than rewritten: the
/// local writer treats it as the field by its stripped key, while this reader
/// and the structural proof use the exact key, so rewriting it could drop
/// reservations the reader never saw.
pub fn reserve_anchors(text: &str, ids: &BTreeSet<String>) -> Result<String, &'static str> {
    if ids.iter().any(|id| !proposal_blocks::anchor(id)) {
        return Err("invalid claim anchor reservation");
    }
    let found = body_offset(text);
    if found.is_none() && text.starts_with("---") {
        return Err("unsupported card front matter");
    }
    let offset = found.unwrap_or(0);
    let header = if offset == 0 {
        "---\n---\n".to_string()
    } else {
        text[..offset].replace("\r\n", "\n")
    };
    let mut lines: Vec<String> = header.split('\n').map(str::to_string).collect();
    let closing = (1..lines.len())
        .find(|index| lines[*index].trim() == "---")
        .ok_or("unsupported card front matter")?;
    let field = format!("claim_anchor_ids: {}", anchor_list(ids));
    let mut replaced = 0;
    for line in lines.iter_mut().take(closing).skip(1) {
        let (exact, indented) = match line.split_once(':') {
            Some((key, _)) => (key == "claim_anchor_ids", key.trim() == "claim_anchor_ids"),
            None => (false, false),
        };
        if exact {
            *line = field.clone();
            replaced += 1;
        } else if indented {
            return Err("ambiguous claim anchor reservations");
        }
    }
    if replaced > 1 {
        return Err("ambiguous claim anchor reservations");
    }
    if replaced == 0 {
        // Insert at the closing fence exactly as `_upsert_frontmatter_fields`
        // does, after any trailing blank line, so both writers produce the
        // same bytes for the same card.
        lines.insert(closing, field);
    }
    let mut updated = lines.join("\n");
    if !updated.ends_with('\n') {
        updated.push('\n');
    }
    let ending = if text.starts_with("---\r\n") {
        "\r\n"
    } else {
        "\n"
    };
    Ok(updated.replace('\n', ending) + &text[offset..])
}
/// The raw front matter, fences included, and the body. `None` when the card
/// has no front matter. A card that opens a fence without closing it reads as
/// an error, so a proof refuses it.
pub fn front_bytes(text: &str) -> Result<Option<(&str, &str)>, &'static str> {
    if let Some(offset) = body_offset(text) {
        return Ok(Some((&text[..offset], &text[offset..])));
    }
    let first = text.split_inclusive('\n').next().unwrap_or("");
    if first.ends_with('\n') && fence(first) {
        return Err("unsupported card front matter");
    }
    Ok(None)
}
/// One raw front-matter line as its content and its terminator. A carriage
/// return is only a terminator when a line feed follows it.
fn front_line(line: &str) -> (&str, &str) {
    let content = line
        .strip_suffix("\r\n")
        .or_else(|| line.strip_suffix('\n'))
        .unwrap_or(line);
    (content, &line[content.len()..])
}
/// Whether every line in this front matter ends the same way, all LF or all
/// CRLF, fences included, with no lone carriage return.
pub fn uniform_front_endings(header: &str) -> bool {
    let bytes = header.as_bytes();
    let mut crlf = None;
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'\r' => {
                if bytes.get(index + 1) != Some(&b'\n') || crlf == Some(false) {
                    return false;
                }
                crlf = Some(true);
                index += 2;
            }
            b'\n' => {
                if crlf == Some(true) {
                    return false;
                }
                crlf = Some(false);
                index += 1;
            }
            _ => index += 1,
        }
    }
    true
}
/// The exact `claim_anchor_ids` line content, without its terminator, or
/// `None` when the field is absent. A line whose stripped key names the field
/// but whose exact key does not, or more than one such line, is refused: the
/// writer would replace a line the exact-key reader never sees. The bytes are
/// read raw so a CRLF card names the same field as an LF one.
pub fn anchor_reservation_line(text: &str) -> Result<Option<String>, &'static str> {
    let Some((header, _)) = front_bytes(text)? else {
        return Ok(None);
    };
    let mut found = Vec::new();
    for line in header.split_inclusive('\n') {
        let (content, _) = front_line(line);
        let Some((key, _)) = content.split_once(':') else {
            continue;
        };
        if key.trim() == "claim_anchor_ids" {
            found.push((key, content));
        }
    }
    match found.as_slice() {
        [] => Ok(None),
        [(key, content)] if *key == "claim_anchor_ids" => Ok(Some((*content).to_string())),
        _ => Err("ambiguous claim anchor reservations"),
    }
}
/// The ids on an exact `claim_anchor_ids` line, checked against the writer's
/// format and reservation limit.
pub fn reservation_ids(line: &str) -> Result<BTreeSet<String>, &'static str> {
    let value = line
        .strip_prefix("claim_anchor_ids: ")
        .ok_or("invalid claim anchor reservation")?;
    let ids: Vec<String> =
        serde_json::from_str(value).map_err(|_| "invalid claim anchor reservation")?;
    if ids.len() > MAX_ANCHORS || ids.iter().any(|id| !proposal_blocks::anchor(id)) {
        return Err("invalid claim anchor reservation");
    }
    Ok(ids.into_iter().collect())
}
/// The exact `claim_anchor_ids` line [`reserve_anchors`] writes for `ids`.
pub fn canonical_anchor_reservation(ids: &BTreeSet<String>) -> String {
    format!("claim_anchor_ids: {}", anchor_list(ids))
}
/// The raw front matter with its `claim_anchor_ids` line and terminator
/// removed, plus whether it had one. An indented or duplicated field is
/// refused. Every other byte, line endings included, is retained.
pub fn header_without_reservation(header: &str) -> Result<(String, bool), &'static str> {
    let mut kept = String::new();
    let mut found = 0;
    for line in header.split_inclusive('\n') {
        let (content, _) = front_line(line);
        let key = content.split_once(':').map(|(key, _)| key);
        if key == Some("claim_anchor_ids") {
            found += 1;
            continue;
        }
        if key.is_some_and(|key| key.trim() == "claim_anchor_ids") {
            return Err("ambiguous claim anchor reservations");
        }
        kept.push_str(line);
    }
    if found > 1 {
        return Err("ambiguous claim anchor reservations");
    }
    Ok((kept, found == 1))
}

pub fn set(text: &str, updates: &[(&str, Json)]) -> Result<String, &'static str> {
    let (meta, body) = front(text)?;
    for (key, _) in updates {
        get(text, key)?;
    }
    let mut lines = Vec::new();
    for line in meta {
        if !updates.iter().any(|(key, _)| is_key(line, key)) {
            lines.push(line.to_string());
        }
    }
    for (key, value) in updates {
        lines.push(format!("{key}: {}", front_json(value)));
    }
    Ok(ids::normalize(&format!(
        "---\n{}\n---\n{body}",
        lines.join("\n")
    )))
}
/// The card's claim body, the text a merge compares: the body without its
/// front matter and without supported anchor suffixes.
pub(super) fn claims(text: &str) -> Result<String, &'static str> {
    let (_, body) = front(text)?;
    // Remove only supported anchor suffixes, retaining opaque syntax as bytes.
    let result = proposal_blocks::strip_anchors(body);
    // Normalization has already fixed line endings. Preserve every other byte,
    // including indentation and spacing inside opaque Markdown and code.
    Ok(result)
}
/// The comparison `merge_exact_duplicates` validates with, as a plain fact.
/// A text that does not parse never counts as a duplicate.
pub(super) fn same_claims(a: &str, b: &str) -> bool {
    matches!((claims(a), claims(b)), (Ok(x), Ok(y)) if x == y)
}
/// `stale` names the aliases a merge must not inherit from the retired card, as
/// `alias_dating::stale_inherited` computes them from the store. Only a merge
/// reads it.
pub fn apply(
    text: &str,
    op: &Json,
    other: Option<(&str, &str)>,
    stale: &BTreeSet<String>,
) -> Result<String, &'static str> {
    match op["type"].as_str() {
        Some("append_evidence") => {
            let refs = op["evidence_ids"]
                .as_array()
                .filter(|r| !r.is_empty() && r.len() <= 256)
                .ok_or("evidence_ids must be a bounded nonempty list")?;
            let mut evidence = list(text, "evidence")?;
            for value in refs {
                let value = value
                    .as_str()
                    .ok_or("evidence reference must be a string")?;
                if value.len() > 1024
                    || value.split('/').count() < 3
                    || value
                        .split('/')
                        .any(|s| s.is_empty() || s == "." || s == "..")
                    || !value.bytes().all(|b| b.is_ascii_graphic())
                {
                    return Err("evidence reference must be namespaced");
                }
                evidence.insert(value.to_string());
            }
            if evidence.len() > 256 {
                return Err("evidence list exceeds bounds");
            }
            // No proof_count or consolidation timestamp is inferred from strings.
            set(text, &[("evidence", json!(evidence))])
        }
        Some("supersede_card") => {
            let (_, name) = other.ok_or("other_card required")?;
            set(
                text,
                &[
                    ("status", json!("superseded")),
                    ("superseded_by", json!(name)),
                ],
            )
        }
        Some("merge_exact_duplicates") => {
            let (other, name) = other.ok_or("other_card required")?;
            if claims(text)? != claims(other)? {
                return Err("merge requires exact duplicate claims");
            }
            let mut aliases = list(text, "aliases")?;
            aliases.extend(
                list(other, "aliases")?
                    .into_iter()
                    .filter(|a| !stale.contains(a)),
            );
            aliases.insert(name.into());
            let mut refs = list(text, "evidence")?;
            refs.extend(list(other, "evidence")?);
            if aliases.len() > 256 || refs.len() > 256 {
                return Err("merged metadata list exceeds bounds");
            }
            set(
                text,
                &[("aliases", json!(aliases)), ("evidence", json!(refs))],
            )
        }
        _ => Err("unsupported metadata op"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    /// `memory_proposals._update_metadata` is the oracle for the reservation
    /// bytes, written by the generator beside the fixture.
    #[test]
    fn reservations_match_the_local_proposal_writer() {
        let cases: Json =
            serde_json::from_str(include_str!("../../tests/anchor-reservation-golden.json"))
                .unwrap();
        for case in cases.as_array().unwrap() {
            let ids: BTreeSet<String> = case["ids"]
                .as_array()
                .unwrap()
                .iter()
                .map(|id| id.as_str().unwrap().to_string())
                .collect();
            let written = reserve_anchors(case["text"].as_str().unwrap(), &ids);
            if case["expected"].is_null() {
                // An indented field is an upsert target for the writer but not
                // the field for the exact-key reader, so the hub refuses it.
                assert!(written.is_err(), "{}", case["name"]);
            } else {
                assert_eq!(
                    written.unwrap(),
                    case["expected"].as_str().unwrap(),
                    "{}",
                    case["name"]
                );
            }
        }
    }
    #[test]
    fn duplicate_comparison_strips_only_the_addressable_anchor() {
        assert_eq!(
            claims("> Claim. ^c-abcdef\n\nClaim. ^c-abcdef\n").unwrap(),
            "> Claim. ^c-abcdef\n\nClaim.\n"
        );
    }
}

#[cfg(test)]
mod serialization_tests {
    use super::*;

    #[test]
    fn frontmatter_json_roundtrip_escapes_all_reader_boundaries() {
        let values = json!([
            "\n",
            "\r",
            "\u{b}",
            "\u{c}",
            "\u{1c}",
            "\u{1d}",
            "\u{1e}",
            "\u{85}",
            "\u{2028}",
            "\u{2029}",
            "literal\\u2028",
            "café"
        ]);
        let encoded = set("Claim. ^c-abcdef\n", &[("evidence", values.clone())]).unwrap();
        let value = get(&encoded, "evidence").unwrap().unwrap();
        assert_eq!(
            value,
            r#"["\u000a","\u000d","\u000b","\u000c","\u001c","\u001d","\u001e","\u0085","\u2028","\u2029","literal\\u2028","café"]"#
        );
        assert_eq!(serde_json::from_str::<Json>(&value).unwrap(), values);
        assert_eq!(front(&encoded).unwrap().1, "Claim. ^c-abcdef\n");
    }

    #[test]
    fn frontmatter_json_values_cannot_create_reader_fields() {
        let hidden = "\u{2028}expires: 2020-01-01\u{2029}";
        for value in [json!(hidden), json!({(hidden): [hidden]})] {
            let encoded = set("Claim. ^c-abcdef\n", &[("note", value.clone())]).unwrap();
            let parsed = get(&encoded, "note").unwrap().unwrap();
            assert_eq!(serde_json::from_str::<Json>(&parsed).unwrap(), value);
            let fields = super::super::text::front_matter(&encoded);
            assert_eq!(
                fields
                    .iter()
                    .map(|(key, _)| key.as_str())
                    .collect::<Vec<_>>(),
                ["note"]
            );
        }
    }
}
