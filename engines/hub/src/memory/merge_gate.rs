//! Bounded-drift gate for `merge_exact_duplicates` on a real scope (#2591).
//! Retiring a card moves the scope's document count, token total and document
//! frequencies, so no ranking digest can stay equal. The merge is admitted
//! only when the keeper holds everything the retired card made searchable and
//! nothing else can notice the card is gone. A recomputation at preflight and
//! again at apply decides, never the proposal's own claims.
use super::{
    index::{self, Card},
    ops, proposal_metadata, proposal_tiers,
    proposals::{cards, string},
    ranking,
    retrieval_eval::Live,
    text,
};
use crate::writer::ReadTx;
use rusqlite::types::Value;
use serde_json::Value as Json;
use std::collections::{BTreeSet, HashMap};

const MERGE: &str = "merge_exact_duplicates";
pub(super) const MIXED_OPS: &str = "retrieval_eval_merge_mixed_ops";
const SHAPE: &str = "retrieval_eval_merge_shape";
const DRIFTED: &str = "retrieval_eval_target_drifted";
const TRUST: &str = "retrieval_eval_merge_trust_mismatch";
const AUTOMATED: &str = "retrieval_eval_merge_automated_mismatch";
const INBOUND: &str = "retrieval_eval_merge_inbound_link";
const TITLE: &str = "retrieval_eval_merge_title_uncovered";
const TOKENS: &str = "retrieval_eval_merge_tokens_lost";
const KEEPER: &str = "retrieval_eval_merge_keeper_changed";
const WRITER: &str = "retrieval_eval_writer_changed";

pub(super) fn is_merge(op: &Json) -> bool {
    op["type"] == MERGE
}

/// A keeper's card and each (op, retired card) merged into it.
type Group<'a> = (&'a Json, Vec<(&'a Json, &'a Json)>);

/// Keepers with the merges folded into each, or None for any other shape:
/// one scope, every card a keeper or a retired duplicate, no card twice.
fn plan(proposal: &Json) -> Option<Vec<Group<'_>>> {
    let all = cards(proposal);
    let find = |scope: &Json, id: &Json| {
        all.iter()
            .find(|c| c["scope"] == *scope && c["card_id"] == *id)
    };
    let mut groups: Vec<Group> = Vec::new();
    let mut retired = BTreeSet::new();
    for op in proposal["ops"].as_array()? {
        let (keeper, other) = (
            find(&op["scope"], &op["card_id"])?,
            find(&op["other_card"]["scope"], &op["other_card"]["card_id"])?,
        );
        if keeper["scope"] != other["scope"]
            || keeper["card_id"] == other["card_id"]
            || keeper["after_state"] != "live"
            || other["after_state"] != "retired"
            || !retired.insert(string(other, "card_id"))
        {
            return None;
        }
        match groups
            .iter_mut()
            .find(|g| g.0["card_id"] == keeper["card_id"])
        {
            Some(group) => group.1.push((op, other)),
            None => groups.push((keeper, vec![(op, other)])),
        }
    }
    let keepers: BTreeSet<_> = groups.iter().map(|g| string(g.0, "card_id")).collect();
    (keepers.is_disjoint(&retired)
        && all.len() == keepers.len() + retired.len()
        && all.iter().all(|c| c["scope"] == all[0]["scope"]))
    .then_some(groups)
}

/// A live head as the gate reads it.
struct Head {
    card: Card,
    /// Whether its latest put or import was automated.
    origin: bool,
    writer: String,
    writer_class: String,
}

/// The live head, or None when it moved or is no longer live.
fn load<T: ReadTx + ?Sized>(tx: &T, target: &Json) -> rusqlite::Result<Option<Head>> {
    let (scope, id) = (string(target, "scope"), string(target, "card_id"));
    let rows = tx.query(&format!("SELECT c.revision,c.content_hash,c.trust,b.bytes,c.writer_class,{},c.state,c.writer FROM memory_cards c JOIN memory_blobs b ON b.content_hash=c.content_hash WHERE c.scope=?1 AND c.card_id=?2", index::ORIGIN_AUTOMATED_SQL), &[ops::t(scope), ops::t(id)])?;
    let Some(row) = rows.first() else {
        return Ok(None);
    };
    let (Value::Integer(revision), Value::Blob(bytes)) = (&row[0], &row[3]) else {
        return Ok(None);
    };
    if target["base_revision"] != *revision
        || target["before_hash"] != ops::s(&row[1])
        || ops::s(&row[6]) != "live"
    {
        return Ok(None);
    }
    let origin = matches!(row[5], Value::Integer(n) if n != 0);
    let card = Card {
        scope: scope.into(),
        card_id: id.into(),
        revision: *revision,
        content_hash: ops::s(&row[1]),
        trust: ops::s(&row[2]),
        text: String::from_utf8_lossy(bytes).into_owned(),
        automated: ops::s(&row[4]) == "principal" || origin,
    };
    Ok(Some(Head {
        card,
        origin,
        writer: ops::s(&row[7]),
        writer_class: ops::s(&row[4]),
    }))
}

/// Whether `have` holds every token of `need` at least as many times.
fn covers(have: &[String], need: &[String]) -> bool {
    let mut counts: HashMap<&str, i64> = HashMap::new();
    for token in have {
        *counts.entry(token).or_default() += 1;
    }
    need.iter().all(|token| {
        let left = counts.entry(token).or_default();
        *left -= 1;
        *left >= 0
    })
}

/// A wiki link or markdown link in the card that points at `id`.
fn links_to(text: &str, scope: &str, id: &str) -> bool {
    let target = |raw: &str| {
        let raw = raw.split(['|', '#']).next().unwrap_or("").trim();
        ranking::link_id(raw, scope) == Some(id)
            || ranking::link_id(raw.trim_end_matches(".md"), scope) == Some(id)
    };
    let inner = |open: &str, close: &str| -> Vec<String> {
        text.split(open)
            .skip(1)
            .filter_map(|rest| rest.split_once(close).map(|(inner, _)| inner.to_string()))
            .collect()
    };
    inner("[[", "]]").iter().any(|link| target(link))
        // A markdown destination may carry a title after a space.
        || inner("](", ")")
            .iter()
            .any(|link| target(link.split_whitespace().next().unwrap_or("")))
}

/// Any front-matter value, wiki link or markdown link that names `id`.
fn names(text: &str, scope: &str, id: &str) -> bool {
    ranking::front_targets(text, scope, |_| true)
        .iter()
        .any(|(_, target)| target == id)
        || links_to(text, scope, id)
}

/// Decide a proposal whose ops are all `merge_exact_duplicates`.
pub(super) fn hold<T: ReadTx + ?Sized>(
    tx: &T,
    proposal: &Json,
    live: &Live,
) -> rusqlite::Result<Option<&'static str>> {
    let Some(plan) = plan(proposal) else {
        return Ok(Some(SHAPE));
    };
    let applier = live.applier;
    let automated = |origin: bool| applier == "principal" || origin;
    for (target, merges) in plan {
        let Some(Head {
            card: keeper,
            origin: keeper_origin,
            writer,
            writer_class,
        }) = load(tx, target)?
        else {
            return Ok(Some(DRIFTED));
        };
        // The apply rewrites the keeper's writer and class to the applier's.
        // A connector sees an agent-unreviewed head only for the credential
        // that wrote it, and the digest cannot see that. The retired card
        // leaves search for everyone, so only the keeper is judged here.
        if keeper.trust == "agent-unreviewed"
            && (writer != live.principal || writer_class != applier)
        {
            return Ok(Some(WRITER));
        }
        let scope = keeper.scope.as_str();
        let after = Card {
            content_hash: string(target, "after_hash").into(),
            text: string(target, "after_text").into(),
            automated: automated(keeper_origin),
            ..keeper.clone()
        };
        let after_tokens = index::projection(&after, true).tokens;
        let mut expected = keeper.text.clone();
        for (op, retired_target) in merges {
            let Some(Head {
                card: retired,
                origin: retired_origin,
                ..
            }) = load(tx, retired_target)?
            else {
                return Ok(Some(DRIFTED));
            };
            if keeper.trust != retired.trust {
                return Ok(Some(TRUST));
            }
            if automated(keeper_origin) != automated(retired_origin) {
                return Ok(Some(AUTOMATED));
            }
            // A retired card leaves `heads`, so a link to it would dangle.
            let id = retired.card_id.as_str();
            if proposal_tiers::named_by(tx, scope, id, true, |text| names(text, scope, id))? {
                return Ok(Some(INBOUND));
            }
            let before = index::projection(&retired, true);
            if !covers(&after_tokens, &text::tokenize(&before.fields.title)) {
                return Ok(Some(TITLE));
            }
            if !covers(&after_tokens, &before.tokens) {
                return Ok(Some(TOKENS));
            }
            let name = format!("{scope}/{id}");
            match proposal_metadata::apply(&expected, op, Some((&retired.text, &name))) {
                Ok(text) => expected = text,
                Err(_) => return Ok(Some(SHAPE)),
            }
        }
        // The merge may add aliases and evidence. Search sees only the alias
        // line, which names the retired id. Everything else in the keeper's
        // ranking projection must come out the same.
        let ranked = |card: &Card| {
            let text = proposal_metadata::without_key(&card.text, "aliases");
            index::ranking_digest(
                &Card {
                    text,
                    ..card.clone()
                },
                true,
            )
        };
        if expected != string(target, "after_text") || ranked(&keeper) != ranked(&after) {
            return Ok(Some(KEEPER));
        }
    }
    Ok(None)
}
