//! Bounded-drift gate for `merge_exact_duplicates` on a real scope (#2591).
//! Retiring a card moves the scope's document count, token total and document
//! frequencies, so no ranking digest can stay equal. The merge is admitted
//! only when the keeper holds everything the retired card made searchable and
//! nothing else can notice the card is gone. A recomputation at preflight and
//! again at apply decides, never the proposal's own claims. The retired card
//! must be the newer of the pair, so a copy cannot retire the card it copied.
//! Newer is judged by when each card's current claim body first appeared, so
//! neither retiring an id and writing it again nor editing an old card into a
//! copy backdates it.
use super::{
    ids,
    index::{self, Card},
    ops, proposal_metadata,
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
const GROUP_TOO_LARGE: &str = "retrieval_eval_merge_group_too_large";
/// Most cards one keeper may retire in a single merge.
const MAX_GROUP: usize = 8;

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

/// Revisions read back to date a card's current content. A longer run holds.
const RUN_CAP: usize = 256;

/// The log sequence at which the card's current claim body first appeared, as
/// one unbroken run of live revisions ending at the head. Walking back from the
/// head, a revision belongs to the run while it is live and its claim body
/// equals the head's, so front-matter, alias and evidence rewrites keep a
/// card's date. Any other edit of the claim body, a retirement, or a revival
/// starts a new run, and a body edited away and back dates from the return.
/// This is what stops a card from passing as older than the text it was
/// rewritten to copy. None, which holds the merge, when the head has no live
/// revision row of its own, a revision's text is missing or unreadable, or the
/// run is longer than `RUN_CAP`, because then the history does not date it.
fn content_start<T: ReadTx + ?Sized>(
    tx: &T,
    scope: &str,
    id: &str,
    head: (i64, &str, &str),
) -> rusqlite::Result<Option<i64>> {
    let (revision, hash, text) = head;
    let Ok(body) = proposal_metadata::claims(text) else {
        return Ok(None);
    };
    let rows = tx.query(
        &format!("SELECT seq,state,content_hash FROM memory_card_revisions WHERE scope=?1 AND card_id=?2 AND seq<=?3 ORDER BY seq DESC LIMIT {}", RUN_CAP + 1),
        &[ops::t(scope), ops::t(id), Value::Integer(revision)],
    )?;
    let mut start = None;
    // Equal claim bodies, by revision text, so each text is read once.
    let mut bodies: HashMap<String, bool> = HashMap::new();
    for (i, row) in rows.iter().enumerate() {
        let Value::Integer(seq) = row[0] else {
            return Ok(None);
        };
        let (state, row_hash) = (ops::s(&row[1]), ops::s(&row[2]));
        if i == 0 && (seq != revision || state != "live" || row_hash != hash) {
            return Ok(None);
        }
        if state != "live" {
            return Ok(start);
        }
        if row_hash != hash {
            let same = match bodies.get(&row_hash) {
                Some(same) => *same,
                None => {
                    let blob = tx.query(
                        "SELECT bytes FROM memory_blobs WHERE content_hash=?1",
                        &[ops::t(&row_hash)],
                    )?;
                    let Some(Value::Blob(bytes)) = blob.first().and_then(|r| r.first()) else {
                        return Ok(None);
                    };
                    let same = proposal_metadata::claims(&String::from_utf8_lossy(bytes))
                        .is_ok_and(|other| other == body);
                    bodies.insert(row_hash, same);
                    same
                }
            };
            if !same {
                return Ok(start);
            }
        }
        start = Some(seq);
    }
    // Every row read belonged to the run. It ended in a first revision only
    // when the read was not cut off by the cap.
    Ok((rows.len() <= RUN_CAP).then_some(start).flatten())
}

/// A live head as the gate reads it.
struct Head {
    card: Card,
    /// Whether its latest put or import was automated.
    origin: bool,
    writer: String,
    writer_class: String,
    /// The log sequence at which the card's current claim body first appeared,
    /// or None when its history does not date it.
    first_seq: Option<i64>,
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
    let text = String::from_utf8_lossy(bytes).into_owned();
    let first_seq = content_start(tx, scope, id, (*revision, &ops::s(&row[1]), &text))?;
    let card = Card {
        scope: scope.into(),
        card_id: id.into(),
        revision: *revision,
        content_hash: ops::s(&row[1]),
        trust: ops::s(&row[2]),
        text,
        automated: ops::s(&row[4]) == "principal" || origin,
    };
    Ok(Some(Head {
        card,
        origin,
        writer: ops::s(&row[7]),
        writer_class: ops::s(&row[4]),
        first_seq,
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

/// The name a link resolves by, as the broken-link resolver reads it
/// (`_loose` in `memory_reflect_links.py`): case folded, with every run of
/// whitespace or hyphens a single space. Python's `str.casefold` also turns
/// `ß`, `ſ` and the `ﬁ` family of ligatures into plain letters, so those fold
/// here too. Folding only widens what matches, which can only hold a merge.
fn loose(name: &str) -> String {
    let mut folded = String::with_capacity(name.len());
    for c in text::py_lower(name).chars() {
        match c {
            'ß' => folded.push_str("ss"),
            'ſ' => folded.push('s'),
            '\u{fb00}' => folded.push_str("ff"),
            '\u{fb01}' => folded.push_str("fi"),
            '\u{fb02}' => folded.push_str("fl"),
            '\u{fb03}' => folded.push_str("ffi"),
            '\u{fb04}' => folded.push_str("ffl"),
            '\u{fb05}' | '\u{fb06}' => folded.push_str("st"),
            _ => folded.push(c),
        }
    }
    let words: Vec<&str> = folded
        .split(|c: char| c.is_whitespace() || c == '-')
        .filter(|word| !word.is_empty())
        .collect();
    words.join(" ")
}

/// Every name a link target or front-matter value could resolve to, loosely
/// spelled: the strict ids `mentioned_id` reads and the same path forms with any
/// case, spaces or runs of hyphens, since the resolver compares names loosely.
/// `scoped` is the loose spelling of `{scope}/`.
fn candidates(value: &str, scope: &str, scoped: &str, out: &mut HashSet<String>) {
    if let Some(id) = ranking::mentioned_id(value, scope) {
        out.insert(loose(id));
    }
    let mut path = value.trim();
    if let Some(inner) = path.strip_prefix('<') {
        path = inner.strip_suffix('>').unwrap_or(inner);
    }
    path = path.split('#').next().unwrap_or("");
    let mut name = loose(path);
    while let Some(rest) = ["./", "../", "/"]
        .iter()
        .find_map(|prefix| name.strip_prefix(prefix))
    {
        name = rest.to_string();
    }
    // A folder prefix is a card path, so any extension after it goes too, as
    // `link_id` strips one. Without a prefix only `.md` goes.
    let mut prefixed = false;
    while let Some(rest) = ["memory/cards/", "cards/", scoped]
        .iter()
        .find_map(|prefix| name.strip_prefix(prefix))
    {
        name = rest.to_string();
        prefixed = true;
    }
    if let Some(bare) = name.strip_suffix(".md") {
        out.insert(bare.to_string());
    }
    if let (true, Some((stem, _))) = (prefixed, name.rsplit_once('.')) {
        out.insert(stem.to_string());
    }
    out.insert(name);
}

/// A link destination: an angle-bracketed form that may hold spaces, or the
/// text up to the first space, which may be followed by a title.
fn destination(text: &str) -> &str {
    let text = text.trim_start();
    if text.starts_with('<') {
        if let Some(end) = text.find('>') {
            return &text[..=end];
        }
    }
    text.split_whitespace().next().unwrap_or("")
}

/// Every name a card's front-matter values, wiki links, markdown links and
/// reference definitions could resolve to, loosely spelled. Every key counts and
/// every list item, past the cap that `Links::parse` applies, because a dangling
/// link is a defect at any position.
fn named_in(text: &str, scope: &str) -> HashSet<String> {
    let scoped = format!("{}/", loose(scope));
    let mut out = HashSet::new();
    for (_, value) in ranking::front_values(text, |_| true, usize::MAX) {
        candidates(&value, scope, &scoped, &mut out);
    }
    let inner = |open: &str, close: &str| -> Vec<String> {
        text.split(open)
            .skip(1)
            .filter_map(|rest| rest.split_once(close).map(|(inner, _)| inner.to_string()))
            .collect()
    };
    let mut targets: Vec<String> = inner("[[", "]]");
    targets.extend(inner("](", ")").iter().map(|d| destination(d).to_string()));
    // A reference definition is `[label]: destination "title"` on its own line.
    for line in text.lines() {
        let rest = line.trim_start().strip_prefix('[');
        if let Some((_, after)) = rest.and_then(|rest| rest.split_once("]:")) {
            targets.push(destination(after).to_string());
        }
    }
    for target in targets {
        // A wiki link may carry an alias after a bar.
        let raw = target.split('|').next().unwrap_or("");
        candidates(raw, scope, &scoped, &mut out);
    }
    out
}

/// The ids among `ids` that some other live card in the scope names. One pass
/// parses each live card once, however many ids are asked about. Every live card
/// is read, because no byte or key filter sees an id that a literal spells with
/// escapes or concatenation.
fn inbound<T: ReadTx + ?Sized>(
    tx: &T,
    scope: &str,
    ids: &[&str],
) -> rusqlite::Result<HashSet<String>> {
    let rows = tx.query("SELECT c.card_id,b.bytes FROM memory_cards c JOIN memory_blobs b ON b.content_hash=c.content_hash WHERE c.scope=?1 AND c.state='live'", &[ops::t(scope)])?;
    let wanted: Vec<(&str, String)> = ids.iter().map(|id| (*id, loose(id))).collect();
    let mut named = HashSet::new();
    for row in &rows {
        let Value::Blob(bytes) = &row[1] else {
            continue;
        };
        let owner = ops::s(&row[0]);
        let found = named_in(&String::from_utf8_lossy(bytes), scope);
        for (id, spelled) in &wanted {
            if *id != owner && found.contains(spelled) {
                named.insert((*id).to_string());
            }
        }
    }
    Ok(named)
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

/// The structural fact that makes one merge safe to treat as a single change:
/// the proposal holds exactly one merge pair, both live heads still match what
/// it read, and their claim bodies are identical. It is recomputed from the
/// live heads here, so nothing in the proposal body can supply it. Preflight
/// relaxes the blast cap and the attested evidence floors on this fact alone.
pub(super) fn exact_pair<T: ReadTx + ?Sized>(tx: &T, proposal: &Json) -> rusqlite::Result<bool> {
    let ops = proposal["ops"].as_array().map(Vec::as_slice).unwrap_or(&[]);
    if !matches!(ops, [op] if is_merge(op)) || cards(proposal).len() != 2 {
        return Ok(false);
    }
    let Some(plan) = plan(proposal) else {
        return Ok(false);
    };
    let [(keeper, merges)] = plan.as_slice() else {
        return Ok(false);
    };
    let [(_, retired)] = merges.as_slice() else {
        return Ok(false);
    };
    let (Some((keeper, _)), Some((retired, _))) = (load(tx, keeper)?, load(tx, retired)?) else {
        return Ok(false);
    };
    Ok(proposal_metadata::same_claims(&keeper.text, &retired.text))
}

/// Whether `card` is the keeper or the retired card of a merge op in `proposal`.
pub(super) fn in_merge(proposal: &Json, card: &Json) -> bool {
    let is = |scope: &Json, id: &Json| card["scope"] == *scope && card["card_id"] == *id;
    proposal["ops"].as_array().is_some_and(|ops| {
        ops.iter().filter(|op| is_merge(op)).any(|op| {
            is(&op["scope"], &op["card_id"])
                || is(&op["other_card"]["scope"], &op["other_card"]["card_id"])
        })
    })
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
    if plan.iter().any(|(_, merges)| merges.len() > MAX_GROUP) {
        return Ok(Some(GROUP_TOO_LARGE));
    }
    let applier = live.applier;
    let automated = |origin: bool| applier == "principal" || origin;
    // An apply rewrites the writer and class of each card it writes to the
    // applier's, and a connector sees an agent-unreviewed head only for the
    // credential that wrote it. The ranking digest cannot see that.
    let foreign = |writer: &str, class: &str| writer != live.principal || class != applier;
    // The ids some live card names, read in one pass the first time a retired
    // card reaches the check, so the cost does not grow with the group.
    let retired_ids: Vec<&str> = plan
        .iter()
        .flat_map(|(_, merges)| merges.iter().map(|(_, r)| string(r, "card_id")))
        .collect();
    let mut named: Option<HashSet<String>> = None;
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
            // A copy made later never retires the card it copied. Each card is
            // dated from when its current claim body first appeared, so a card
            // revived or edited into a copy is as new as that write. Without a
            // date for both cards, nothing proves who came first.
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
            // A retired card leaves `heads`, so a link to it would dangle.
            let id = retired.card_id.as_str();
            if named
                .get_or_insert(inbound(tx, scope, &retired_ids)?)
                .contains(id)
            {
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
