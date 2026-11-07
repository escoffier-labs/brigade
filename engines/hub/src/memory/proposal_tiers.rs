//! Fail-closed classification from hub-owned proposal snapshots and heads.
//! A tier describes the edit, never permission to apply it (S19b).
use super::{
    ops,
    proposals::string,
    scan::{Scan, Scanner},
};
use crate::writer::ReadTx;
use serde_json::Value as Json;
use std::collections::BTreeSet;

fn clean(scanner: &dyn Scanner, text: &str) -> bool {
    matches!(scanner.scan(text), Scan::Clean { ruleset } if !ruleset.is_empty())
}

/// Only repeated ASCII spaces in plain prose are proven cosmetic here. Other
/// claim edits, including polarity changes, require Tier 2 until proven safe.
fn cosmetic(op: &Json, card: &Json) -> bool {
    let Some(pin) = card["pins"]
        .as_array()
        .and_then(|pins| pins.iter().find(|p| p["block_id"] == op["block_id"]))
    else {
        return false;
    };
    let old = string(pin, "bytes");
    let Some((old, _)) = old.rsplit_once(" ^") else {
        return false;
    };
    let new = string(op, "text");
    let prose = |s: &str| {
        !s.is_empty()
            && s.bytes()
                .all(|b| b.is_ascii_alphabetic() || b" .,!?".contains(&b))
    };
    prose(old)
        && prose(new)
        && old
            .split(' ')
            .filter(|s| !s.is_empty())
            .eq(new.split(' ').filter(|s| !s.is_empty()))
}

/// Compare every non-union metadata byte, including spacing and field order.
/// Keep each line's newline so aliases/evidence position does not change the
/// retained bytes. Only their validated, single-line fields may be excluded.
fn merge_metadata(text: &str) -> Option<Vec<&str>> {
    let Some(rest) = text.strip_prefix("---\n") else {
        return Some(Vec::new());
    };
    let end = rest.find("\n---\n")?;
    Some(
        rest[..=end]
            .split_inclusive('\n')
            .filter(|line| {
                !matches!(
                    line.split_once(':').map(|(key, _)| key),
                    Some("aliases" | "evidence")
                )
            })
            .collect(),
    )
}

pub fn classify<T: ReadTx + ?Sized>(
    tx: &T,
    body: &Json,
    scanner: &dyn Scanner,
) -> rusqlite::Result<u8> {
    let Some(cards) = body["cards"].as_array().filter(|c| !c.is_empty()) else {
        return Ok(2);
    };
    let Some(operations) = body["ops"].as_array().filter(|o| !o.is_empty()) else {
        return Ok(2); // Compensations and unknown input shapes stay review-only.
    };
    if !body["rollback_of"].is_null() {
        return Ok(2);
    }
    let mut scopes = BTreeSet::new();
    let mut trusts = BTreeSet::new();
    for card in cards {
        scopes.insert(string(card, "scope"));
        let Some(head) = ops::head(tx, string(card, "scope"), string(card, "card_id"))? else {
            return Ok(2);
        };
        // Any newer edit gets Tier 2, including human edits. Credential class
        // alone is not proof of a human, so do not infer one from a token.
        if card["base_revision"] != head.revision
            || string(card, "before_hash") != head.hash
            || string(card, "before_trust") != head.trust
            || !matches!(
                head.trust.as_str(),
                "agent-unreviewed" | "reviewed" | "imported" | "policy-promoted"
            )
        {
            return Ok(2);
        }
        trusts.insert(head.trust);
        let rows = tx.query(
            "SELECT scan_state FROM memory_card_revisions WHERE scope=?1 AND card_id=?2 AND revision=?3",
            &[ops::t(string(card, "scope")), ops::t(string(card, "card_id")), rusqlite::types::Value::Integer(head.revision)],
        )?;
        if rows.first().is_none_or(|r| ops::s(&r[0]) != "clean")
            || !clean(scanner, string(card, "before_text"))
            || !clean(scanner, string(card, "after_text"))
        {
            return Ok(2);
        }
    }
    if scopes.len() != 1 || trusts.len() != 1 {
        return Ok(2);
    }
    let mut tier = 0;
    for op in operations {
        if let Some(text) = op["text"].as_str() {
            if !clean(scanner, text) {
                return Ok(2);
            }
        }
        let Some(card) = cards
            .iter()
            .find(|c| c["scope"] == op["scope"] && c["card_id"] == op["card_id"])
        else {
            return Ok(2);
        };
        match string(op, "type") {
            "append_evidence" => (),
            "merge_exact_duplicates" => {
                let Some(other) = cards.iter().find(|c| {
                    c["scope"] == op["other_card"]["scope"]
                        && c["card_id"] == op["other_card"]["card_id"]
                }) else {
                    return Ok(2);
                };
                if !matches!(
                    (merge_metadata(string(card, "before_text")), merge_metadata(string(other, "before_text"))),
                    (Some(keeper), Some(source)) if keeper == source
                ) {
                    return Ok(2);
                }
            }
            "replace_block" if cosmetic(op, card) => tier = 1,
            _ => return Ok(2),
        }
    }
    Ok(tier)
}
