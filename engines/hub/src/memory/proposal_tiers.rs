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
fn merge_metadata<'a>(text: &'a str, excluded: &[&str]) -> Option<Vec<&'a str>> {
    let Some(rest) = text.strip_prefix("---\n") else {
        return Some(Vec::new());
    };
    let end = rest.find("\n---\n")?;
    let meta = &rest[..=end];
    // Reject reader boundary disagreements before excluding any field.
    if !super::text::py_splitlines(meta)
        .into_iter()
        .eq(meta.split_terminator('\n'))
    {
        return None;
    }
    // A decoded boundary in a rewritten list stays review-only even when the
    // serializer preserves its escapes. Do not infer safe meaning from it.
    for key in excluded {
        let values = super::proposal_metadata::list(text, key).ok()?;
        if values
            .iter()
            .any(|v| v.chars().any(super::text::py_line_break))
        {
            return None;
        }
    }
    Some(
        meta.split_inclusive('\n')
            .filter(|line| {
                !line
                    .split_once(':')
                    .is_some_and(|(key, _)| excluded.contains(&key))
            })
            .collect(),
    )
}

/// Proof is reconstructed from the current hub blob and writer provenance.
/// No candidate-supplied evidence, text or provenance can establish it.
// This is a derived read fact, never a caller-provided authorization input.
pub(super) fn auto_authored<T: ReadTx + ?Sized>(
    tx: &T,
    scope: &str,
    id: &str,
    revision: i64,
) -> rusqlite::Result<bool> {
    let rows = tx.query("SELECT e.payload FROM memory_cards c JOIN memory_events e ON e.scope=c.scope AND e.card_id=c.card_id AND e.revision=c.revision AND e.content_hash=c.content_hash AND e.principal=c.writer WHERE c.scope=?1 AND c.card_id=?2 AND c.revision=?3 AND e.kind='proposal.apply'", &[ops::t(scope), ops::t(id), rusqlite::types::Value::Integer(revision)])?;
    Ok(rows.iter().any(|row| {
        serde_json::from_str::<Json>(&ops::s(&row[0]))
            .is_ok_and(|payload| payload["decision_mode"] == "automatic")
    }))
}

pub(super) fn anchor_proof<T: ReadTx + ?Sized>(tx: &T, body: &Json) -> rusqlite::Result<bool> {
    let Some(operations) = body["ops"].as_array().filter(|o| !o.is_empty()) else {
        return Ok(false);
    };
    let cards = super::proposals::cards(body);
    if cards.len() != operations.len()
        || operations.iter().any(|op| {
            string(op, "type") != "anchor_blocks" || super::proposals::op_schema(op).is_err()
        })
    {
        return Ok(false);
    }
    for card in cards {
        let scope = string(card, "scope");
        let id = string(card, "card_id");
        if operations
            .iter()
            .filter(|op| {
                string(op, "scope") == scope
                    && string(op, "card_id") == id
                    && op["base_revision"] == card["base_revision"]
            })
            .count()
            != 1
        {
            return Ok(false);
        }
        let Some(head) = ops::head(tx, scope, id)? else {
            return Ok(false);
        };
        if card["base_revision"] != head.revision
            || string(card, "before_hash") != head.hash
            || card["before_state"] != "live"
            || card["after_state"] != "live"
            || head.state != "live"
        {
            return Ok(false);
        }
        // No existing credential class is automation-only. Only a head made
        // by the hub's gated automatic apply path establishes authorship.
        // Bind the exact event, never an older put/import or caller origin.
        let rows = tx.query("SELECT e.kind, e.payload, b.bytes FROM memory_cards c JOIN memory_blobs b ON b.content_hash=c.content_hash JOIN memory_events e ON e.scope=c.scope AND e.card_id=c.card_id AND e.revision=c.revision AND e.content_hash=c.content_hash AND e.principal=c.writer WHERE c.scope=?1 AND c.card_id=?2", &[ops::t(scope), ops::t(id)])?;
        let Some(row) = rows.first() else {
            return Ok(false);
        };
        let payload = serde_json::from_str::<Json>(&ops::s(&row[1])).unwrap_or(Json::Null);
        if ops::s(&row[0]) != "proposal.apply"
            || payload["decision_mode"] != "automatic"
            || !auto_authored(tx, scope, id, head.revision)?
        {
            return Ok(false);
        }
        let rusqlite::types::Value::Blob(bytes) = &row[2] else {
            return Ok(false);
        };
        let Ok(before) = std::str::from_utf8(bytes) else {
            return Ok(false);
        };
        if before != string(card, "before_text") || super::ids::sha256_hex(bytes) != head.hash {
            return Ok(false);
        }
        let Ok(after) = super::proposal_blocks::anchor_blocks(before, id) else {
            return Ok(false);
        };
        if after == before
            || after != string(card, "after_text")
            || !super::proposal_blocks::proven_delta(before, &after)
        {
            return Ok(false);
        }
    }
    Ok(true)
}

/// Whether another live card in `scope` names `id`, as `names` judges its text.
/// With `scan_all` false the prefilter reads the lineage key names
/// `Links::parse` acts on, never the target id. Values go through
/// `literal_eval`, so escapes and implicit concatenation can spell an id that
/// no byte search finds. A key cannot be escaped, because `front_matter`
/// splits on the raw colon. A caller whose `names` reads wiki and markdown
/// links or keys beyond the lineage list passes `scan_all`, which parses every
/// live card in the scope, since no byte or key filter can promise to see them.
pub(super) fn named_by<T: ReadTx + ?Sized>(
    tx: &T,
    scope: &str,
    id: &str,
    scan_all: bool,
    names: impl Fn(&str) -> bool,
) -> rusqlite::Result<bool> {
    let filter = if scan_all {
        String::new()
    } else {
        let mentions: Vec<String> = super::ranking::LINEAGE_KEYS
            .iter()
            .map(|key| format!("instr(b.bytes,CAST('{key}' AS BLOB))>0"))
            .collect();
        format!(" AND ({})", mentions.join(" OR "))
    };
    let rows = tx.query(&format!("SELECT b.bytes FROM memory_cards c JOIN memory_blobs b ON b.content_hash=c.content_hash WHERE c.scope=?1 AND c.state='live' AND c.card_id<>?2{filter}"), &[ops::t(scope), ops::t(id)])?;
    Ok(rows.iter().any(|row| {
        let rusqlite::types::Value::Blob(bytes) = &row[0] else {
            return false;
        };
        names(&String::from_utf8_lossy(bytes))
    }))
}

/// A trusted card, or any card of a merge, that declares lineage links, or that
/// another live card in its scope names, can change search order when its revision moves. Lineage
/// recency compares head revisions, so any apply there needs a human decision.
/// The reserved fixture scope keeps its report-bound gate.
pub(super) fn lineage_review<T: ReadTx + ?Sized>(tx: &T, body: &Json) -> rusqlite::Result<bool> {
    for card in super::proposals::cards(body) {
        let (scope, id) = (string(card, "scope"), string(card, "card_id"));
        if scope == super::retrieval_eval::FIXTURE_SCOPE {
            continue;
        }
        let Some(head) = ops::head(tx, scope, id)? else {
            continue;
        };
        // An unreviewed card only reaches automatic apply as half of one exact
        // merge pair (#2591), so a merge card gets the full lineage review at
        // any trust. Skipping it would let declared or inbound lineage apply
        // without a reviewer. Every other untrusted card stays skipped.
        if !super::ranking::trusted(&head.trust) && !super::merge_gate::in_merge(body, card) {
            continue;
        }
        let (before, after) = (string(card, "before_text"), string(card, "after_text"));
        // A merge card's lineage keys must read the same to the merge and to
        // lineage review. A spelling only one of them decodes would hide a link
        // here that another reader, or the merge's own rewrite, then follows.
        if super::merge_gate::in_merge(body, card)
            && [before, after]
                .iter()
                .any(|text| super::merge_gate::lineage_ambiguous(text, scope))
        {
            return Ok(true);
        }
        // The keeper of an all-merge proposal gains one alias per retired card.
        // Its after text is judged whole, less exactly those aliases, so a link
        // the merge did not add still counts, however the before text spells it.
        // An alias the before text already lists is not one the merge adds, so
        // it is never subtracted, and no other check has to catch it.
        let was = super::ranking::Links::parse(before, scope);
        let mut links = super::ranking::Links::parse(after, scope);
        for retired in super::merge_gate::aliases_added(body, card) {
            if was.predecessors.iter().any(|p| p == retired) {
                continue;
            }
            if let Some(at) = links.predecessors.iter().position(|p| p == retired) {
                links.predecessors.remove(at);
            }
        }
        let declares = links.successor.is_some()
            || !links.predecessors.is_empty()
            || was.successor.is_some()
            || !was.predecessors.is_empty();
        let named = named_by(tx, scope, id, false, |text| {
            let links = super::ranking::Links::parse(text, scope);
            links.successor.as_deref() == Some(id) || links.predecessors.iter().any(|p| p == id)
        })?;
        if declares || named {
            return Ok(true);
        }
    }
    Ok(false)
}

pub fn classify<T: ReadTx + ?Sized>(
    tx: &T,
    body: &Json,
    scanner: &dyn Scanner,
) -> rusqlite::Result<u8> {
    if !body["rollback_of"].is_null() {
        return Ok(if super::compensation::exact(tx, body)? {
            0
        } else {
            2
        });
    }
    // D4: a proposal recorded from a verifier principal is Tier 2 at submit,
    // preflight and every decision, whatever its ops or the policy lists.
    if body["verifier"] == true {
        return Ok(2);
    }
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
        // The final snapshot includes every op on this card. Permit their
        // union of typed fields, so cosmetic plus evidence edits remain valid.
        let mut excluded = BTreeSet::new();
        for op in operations
            .iter()
            .filter(|op| op["scope"] == card["scope"] && op["card_id"] == card["card_id"])
        {
            match string(op, "type") {
                "append_evidence" => {
                    excluded.insert("evidence");
                }
                "merge_exact_duplicates" => {
                    excluded.extend(["aliases", "evidence"]);
                }
                // The structural proof below binds the exact reservation list
                // to the suffixes this transform adds, so only that field is
                // excluded here and every other metadata line must still match.
                "anchor_blocks" => {
                    excluded.insert("claim_anchor_ids");
                }
                _ => (),
            }
        }
        let excluded: Vec<_> = excluded.into_iter().collect();
        if !matches!(
            (merge_metadata(string(card, "before_text"), &excluded), merge_metadata(string(card, "after_text"), &excluded)),
            (Some(before), Some(after)) if before == after
        ) {
            return Ok(2);
        }
    }
    if scopes.len() != 1 || trusts.len() != 1 || lineage_review(tx, body)? {
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
            "anchor_blocks" => {
                if !anchor_proof(tx, body)? {
                    return Ok(2);
                }
            }
            "append_evidence" => (),
            "merge_exact_duplicates" => {
                let Some(other) = cards.iter().find(|c| {
                    c["scope"] == op["other_card"]["scope"]
                        && c["card_id"] == op["other_card"]["card_id"]
                }) else {
                    return Ok(2);
                };
                if !matches!(
                    (merge_metadata(string(card, "before_text"), &["aliases", "evidence"]), merge_metadata(string(other, "before_text"), &["aliases", "evidence"])),
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
