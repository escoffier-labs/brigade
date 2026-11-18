//! Bounded-drift gate for `merge_exact_duplicates` on a real scope (#2591).
//! Retiring a card moves the scope's document count, token total and document
//! frequencies, so no ranking digest can stay equal. The merge is admitted
//! only when the keeper holds everything the retired card made searchable and
//! nothing else can notice the card is gone. A recomputation at preflight and
//! again at apply decides, never the proposal's own claims. The retired card
//! must be the newer of the pair, so a copy cannot retire the card it copied.
use super::{
    ids,
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
use std::collections::{BTreeSet, HashMap, HashSet};

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
const RETIRES_OLDER: &str = "retrieval_eval_merge_retires_older";
const LITERAL: &str = "retrieval_eval_merge_literal_lost";

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
    /// The log sequence of the card's first revision, or None with no history.
    first_seq: Option<i64>,
}

/// The live head, or None when it moved or is no longer live.
fn load<T: ReadTx + ?Sized>(tx: &T, target: &Json) -> rusqlite::Result<Option<Head>> {
    let (scope, id) = (string(target, "scope"), string(target, "card_id"));
    let rows = tx.query(&format!("SELECT c.revision,c.content_hash,c.trust,b.bytes,c.writer_class,{},c.state,c.writer,(SELECT MIN(r.seq) FROM memory_card_revisions r WHERE r.scope=c.scope AND r.card_id=c.card_id) FROM memory_cards c JOIN memory_blobs b ON b.content_hash=c.content_hash WHERE c.scope=?1 AND c.card_id=?2", index::ORIGIN_AUTOMATED_SQL), &[ops::t(scope), ops::t(id)])?;
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
        first_seq: match row[8] {
            Value::Integer(n) => Some(n),
            _ => None,
        },
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

/// A wiki link, markdown link or reference-style definition in the card that
/// points at `id`.
fn links_to(text: &str, scope: &str, id: &str) -> bool {
    let target = |raw: &str| {
        // A wiki link may carry an alias after a bar.
        let raw = raw.split('|').next().unwrap_or("");
        ranking::mentioned_id(raw, scope) == Some(id)
    };
    let inner = |open: &str, close: &str| -> Vec<String> {
        text.split(open)
            .skip(1)
            .filter_map(|rest| rest.split_once(close).map(|(inner, _)| inner.to_string()))
            .collect()
    };
    // A reference definition is `[label]: destination "title"` on its own line.
    let definition = |line: &str| {
        let rest = line.trim_start().strip_prefix('[')?;
        let (_, after) = rest.split_once("]:")?;
        after.split_whitespace().next().map(&target)
    };
    inner("[[", "]]").iter().any(|link| target(link))
        // A markdown destination may carry a title after a space.
        || inner("](", ")")
            .iter()
            .any(|link| target(link.split_whitespace().next().unwrap_or("")))
        || text.lines().any(|line| definition(line) == Some(true))
}

/// Any front-matter value, wiki link, markdown link or reference definition
/// that names `id`. Every key counts and every list item, past the cap that
/// `Links::parse` applies, because a dangling link is a defect at any position.
fn names(text: &str, scope: &str, id: &str) -> bool {
    ranking::front_values(text, |_| true, usize::MAX)
        .iter()
        .any(|(_, value)| ranking::mentioned_id(value, scope) == Some(id))
        || links_to(text, scope, id)
}

/// Whether search would still find everything the retired card's literal text
/// holds. Literal search matches substrings of the lowered indexed text, which
/// token coverage cannot promise. The retired card's front-matter lines, other
/// than the aliases the merge rewrites, must appear as lines of the keeper's
/// ranking text. Its title, tags and summary must appear in the keeper's
/// lowered indexed text.
fn literal_kept(retired: &Card, before: &index::Projection, kept: &Kept) -> bool {
    let ranking = text::ranking_text(&retired.text);
    proposal_metadata::lines_without_key(&ranking, "aliases")
        .iter()
        .all(|line| kept.lines.contains(line))
        && std::iter::once(&before.fields.title)
            .chain(&before.fields.tags)
            .chain(std::iter::once(&before.fields.summary))
            .filter(|s| !s.is_empty())
            .all(|s| kept.lowered.contains(&text::py_lower(s)))
}

/// What literal search sees in the keeper's after text.
struct Kept<'a> {
    lines: HashSet<&'a str>,
    lowered: String,
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
    // An apply rewrites the writer and class of each card it writes to the
    // applier's, and a connector sees an agent-unreviewed head only for the
    // credential that wrote it. The ranking digest cannot see that.
    let foreign = |writer: &str, class: &str| writer != live.principal || class != applier;
    for (target, merges) in plan {
        let Some(Head {
            card: keeper,
            origin: keeper_origin,
            writer,
            writer_class,
            first_seq: keeper_first,
        }) = load(tx, target)?
        else {
            return Ok(Some(DRIFTED));
        };
        let scope = keeper.scope.as_str();
        let after_text = string(target, "after_text");
        // The apply stores the blob under `after_hash`, so it must name this text.
        if string(target, "after_hash") != ids::sha256_hex(after_text.as_bytes()) {
            return Ok(Some(KEEPER));
        }
        let after = Card {
            content_hash: string(target, "after_hash").into(),
            text: after_text.into(),
            automated: automated(keeper_origin),
            ..keeper.clone()
        };
        // A principal applier writes the keeper as automated, which flips the
        // value on a keeper that was not. Name that before any digest does.
        if after.automated != keeper.automated {
            return Ok(Some(AUTOMATED));
        }
        let after_projection = index::projection(&after, true);
        let mut expected = keeper.text.clone();
        let mut gone = Vec::new();
        for (op, retired_target) in merges {
            let Some(Head {
                card: retired,
                origin: retired_origin,
                writer: retired_writer,
                writer_class: retired_class,
                first_seq: retired_first,
            }) = load(tx, retired_target)?
            else {
                return Ok(Some(DRIFTED));
            };
            // A copy made later never retires the card it copied. Without a
            // first revision for both cards, nothing proves who came first.
            if !matches!((keeper_first, retired_first), (Some(k), Some(r)) if r > k) {
                return Ok(Some(RETIRES_OLDER));
            }
            if keeper.trust != retired.trust {
                return Ok(Some(TRUST));
            }
            if automated(keeper_origin) != automated(retired_origin) {
                return Ok(Some(AUTOMATED));
            }
            if keeper.trust == "agent-unreviewed" && foreign(&retired_writer, &retired_class) {
                return Ok(Some(WRITER));
            }
            // Only the keeper changes. The retired card's side is its own text.
            if string(retired_target, "before_text") != retired.text
                || string(retired_target, "after_text") != retired.text
            {
                return Ok(Some(SHAPE));
            }
            // A retired card leaves `heads`, so a link to it would dangle. Every
            // live card is parsed, because no byte or key filter sees an id that
            // a literal spells with escapes or concatenation.
            let id = retired.card_id.as_str();
            if proposal_tiers::named_by(tx, scope, id, true, |text| names(text, scope, id))? {
                return Ok(Some(INBOUND));
            }
            let before = index::projection(&retired, true);
            if !covers(
                &after_projection.tokens,
                &text::tokenize(&before.fields.title),
            ) {
                return Ok(Some(TITLE));
            }
            if !covers(&after_projection.tokens, &before.tokens) {
                return Ok(Some(TOKENS));
            }
            gone.push((retired.clone(), before));
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
        if expected != after_text || ranked(&keeper) != ranked(&after) {
            return Ok(Some(KEEPER));
        }
        // The text is now the one the apply makes, so literal search is judged
        // against it and not against the proposal's claim.
        let kept_text = text::ranking_text(&expected);
        let kept = Kept {
            lines: kept_text.lines().collect(),
            lowered: text::py_lower(&after_projection.indexed),
        };
        if gone
            .iter()
            .any(|(retired, before)| !literal_kept(retired, before, &kept))
        {
            return Ok(Some(LITERAL));
        }
        if keeper.trust == "agent-unreviewed" && foreign(&writer, &writer_class) {
            return Ok(Some(WRITER));
        }
    }
    Ok(None)
}
