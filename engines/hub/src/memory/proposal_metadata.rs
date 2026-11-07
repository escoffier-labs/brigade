//! Bounded metadata edits for proposal v2 ops. Complex YAML is refused.
//! Evidence references remain unverified citations, never attested proof (S19e).
use super::{ids, proposal_blocks};
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
fn get(text: &str, key: &str) -> Result<Option<String>, &'static str> {
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
pub fn set(text: &str, updates: &[(&str, Json)]) -> Result<String, &'static str> {
    let (meta, body) = front(text)?;
    for (key, _) in updates {
        get(text, key)?;
    }
    let mut lines = Vec::new();
    for line in meta {
        if !updates
            .iter()
            .any(|(key, _)| line.split_once(':').is_some_and(|(k, _)| k == *key))
        {
            lines.push(line.to_string());
        }
    }
    for (key, value) in updates {
        lines.push(format!("{key}: {value}"));
    }
    Ok(ids::normalize(&format!(
        "---\n{}\n---\n{body}",
        lines.join("\n")
    )))
}
fn claims(text: &str) -> Result<String, &'static str> {
    let (_, body) = front(text)?;
    // Remove only supported anchor suffixes, retaining opaque syntax as bytes.
    let result = proposal_blocks::strip_anchors(body);
    // Normalization has already fixed line endings. Preserve every other byte,
    // including indentation and spacing inside opaque Markdown and code.
    Ok(result)
}
pub fn apply(text: &str, op: &Json, other: Option<(&str, &str)>) -> Result<String, &'static str> {
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
            aliases.extend(list(other, "aliases")?);
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
    #[test]
    fn duplicate_comparison_strips_only_the_addressable_anchor() {
        assert_eq!(
            claims("> Claim. ^c-abcdef\n\nClaim. ^c-abcdef\n").unwrap(),
            "> Claim. ^c-abcdef\n\nClaim.\n"
        );
    }
}
