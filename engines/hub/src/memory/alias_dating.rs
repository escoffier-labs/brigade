//! Dates the lineage edges that `aliases` writes (#2671). A merge writes the
//! retired id into the keeper's `aliases`, so the name can outlive the card it
//! named. If a new card later takes that id, the alias must not link it. An
//! alias edge forms only when the target's current incarnation began before the
//! keeper revision that first wrote the alias. Both dates come from the log
//! sequence numbers in `memory_card_revisions`, which are globally ordered.
//!
//! A merge also copies the retired card's own aliases into the keeper, and the
//! keeper's run dates them from the merge. [`stale_inherited`] names the copies
//! that would turn a reused id into a linked one, so the merge leaves them out.
use super::{ops, proposal_metadata, ranking};
use crate::writer::ReadTx;
use rusqlite::{Connection, params, types::Value};
use std::collections::{BTreeSet, HashMap, HashSet};

/// Revisions read back to date a card. A longer run leaves the date unknown,
/// and an unknown date forms no edge.
pub(super) const RUN_CAP: usize = 256;
/// The row limit that tells a run cut by the cap from one that ended.
const READ_LIMIT: i64 = RUN_CAP as i64 + 1;
/// `revision` is the log sequence of the row (`seq` always equals it), and it
/// ends the primary key, so the filter and the order walk the key backwards
/// and SQLite sorts nothing.
pub(super) const STATES: &str = "SELECT revision,state FROM memory_card_revisions WHERE scope=?1 AND card_id=?2 AND revision<=?3 ORDER BY revision DESC LIMIT ?4";
pub(super) const RUN: &str = "SELECT r.revision,r.state,b.bytes FROM memory_card_revisions r LEFT JOIN memory_blobs b ON b.content_hash=r.content_hash WHERE r.scope=?1 AND r.card_id=?2 AND r.revision<=?3 ORDER BY r.revision DESC LIMIT ?4";

/// The log sequence of the first live revision after the card's latest
/// non-live revision, or of its first revision when it was never retired. None
/// when `head` is not the card's own live revision or the run is too long to
/// read. A revived or recreated id therefore dates from its revival.
pub(super) fn incarnation_start(
    db: &Connection,
    scope: &str,
    card_id: &str,
    head: i64,
) -> rusqlite::Result<Option<i64>> {
    let mut s = db.prepare_cached(STATES)?;
    let rows = s
        .query_map(params![scope, card_id, head, READ_LIMIT], |r| {
            Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)? == "live"))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(start_of(&rows, head))
}
/// [`incarnation_start`] inside a writer transaction.
fn incarnation_start_in<T: ReadTx + ?Sized>(
    tx: &T,
    scope: &str,
    card_id: &str,
    head: i64,
) -> rusqlite::Result<Option<i64>> {
    let rows = tx
        .query(
            STATES,
            &[
                ops::t(scope),
                ops::t(card_id),
                Value::Integer(head),
                Value::Integer(READ_LIMIT),
            ],
        )?
        .iter()
        .map(|r| (integer(&r[0]), ops::s(&r[1]) == "live"))
        .collect::<Vec<_>>();
    Ok(start_of(&rows, head))
}
fn start_of(rows: &[(i64, bool)], head: i64) -> Option<i64> {
    let mut start = None;
    for (i, (seq, live)) in rows.iter().enumerate() {
        if i == 0 && (*seq != head || !live) {
            return None;
        }
        if !live {
            return start;
        }
        start = Some(*seq);
    }
    (rows.len() <= RUN_CAP).then_some(start).flatten()
}

/// For each id the head's `aliases` names, the log sequence of the oldest
/// revision in the unbroken run of revisions, ending at the head, that all
/// name it. An alias dropped and written again dates from the second write. The
/// run also ends at the card's own first non-live revision, as an incarnation
/// does, so a retired and then reused card id does not inherit the alias
/// history of the card it replaced. An id whose run cannot be read to its
/// start, because a blob is missing or the history is too long, is left out,
/// so it forms no edge.
pub(super) fn alias_since(
    db: &Connection,
    scope: &str,
    card_id: &str,
    head: i64,
) -> rusqlite::Result<HashMap<String, i64>> {
    let mut s = db.prepare_cached(RUN)?;
    let rows = s.query_map(params![scope, card_id, head, READ_LIMIT], |r| {
        Ok((
            r.get::<_, i64>(0)?,
            r.get::<_, String>(1)? == "live",
            r.get::<_, Option<Vec<u8>>>(2)?,
        ))
    })?;
    since_of(rows, scope, head)
}
/// [`alias_since`] inside a writer transaction.
fn alias_since_in<T: ReadTx + ?Sized>(
    tx: &T,
    scope: &str,
    card_id: &str,
    head: i64,
) -> rusqlite::Result<HashMap<String, i64>> {
    let rows = tx.query(
        RUN,
        &[
            ops::t(scope),
            ops::t(card_id),
            Value::Integer(head),
            Value::Integer(READ_LIMIT),
        ],
    )?;
    since_of(
        rows.iter().map(|r| {
            Ok((
                integer(&r[0]),
                ops::s(&r[1]) == "live",
                match &r[2] {
                    Value::Blob(b) => Some(b.clone()),
                    _ => None,
                },
            ))
        }),
        scope,
        head,
    )
}
fn since_of(
    rows: impl Iterator<Item = rusqlite::Result<(i64, bool, Option<Vec<u8>>)>>,
    scope: &str,
    head: i64,
) -> rusqlite::Result<HashMap<String, i64>> {
    let mut since = HashMap::new();
    let mut open: HashSet<String> = HashSet::new();
    let (mut read, mut unreadable) = (0, false);
    for row in rows {
        let (seq, live, bytes) = row?;
        read += 1;
        if read == 1 && (seq != head || !live) {
            return Ok(HashMap::new());
        }
        // The run ends where the id's current incarnation began.
        if !live {
            return Ok(since);
        }
        let Some(bytes) = bytes else {
            unreadable = true;
            break;
        };
        let named: HashSet<String> =
            ranking::front_targets(&String::from_utf8_lossy(&bytes), scope, |key| {
                key == "aliases"
            })
            .into_iter()
            .map(|(_, id)| id)
            .collect();
        if read == 1 {
            open = named;
        } else {
            open.retain(|id| named.contains(id));
        }
        for id in &open {
            since.insert(id.clone(), seq);
        }
        if open.is_empty() {
            return Ok(since);
        }
    }
    // The rows ran out, or the read was cut, with ids still open.
    if unreadable || read > RUN_CAP {
        for id in &open {
            since.remove(id);
        }
    }
    Ok(since)
}
fn integer(v: &Value) -> i64 {
    match v {
        Value::Integer(i) => *i,
        _ => 0,
    }
}

/// The aliases a merge must not copy from the retired card into the keeper, as
/// the spellings the retired card wrote. The keeper's run dates an inherited
/// alias from the merge, so a card that took the named id before the merge
/// would look older than the alias and link under the keeper. An inherited
/// alias is dropped when a card in the scope holds the id now and either
/// date is unknown, or that card's current incarnation began after the retired
/// card first wrote the alias. An id nothing holds is kept, because a card
/// that takes it later starts after the merge. An alias the keeper already
/// names keeps its own date and is not inherited.
///
/// Preflight, the merge gate and the apply all call this, so each computes the
/// same keeper text. Dropping only removes an edge, so an unreadable history
/// never hides a card.
pub(super) fn stale_inherited<T: ReadTx + ?Sized>(
    tx: &T,
    scope: &str,
    keeper_text: &str,
    retired: (&str, i64, &str),
) -> rusqlite::Result<BTreeSet<String>> {
    let (card_id, head, text) = retired;
    let (Ok(own), Ok(inherited)) = (
        proposal_metadata::list(keeper_text, "aliases"),
        proposal_metadata::list(text, "aliases"),
    ) else {
        // The merge itself refuses a list it cannot read.
        return Ok(BTreeSet::new());
    };
    let id = |raw: &str| ranking::link_id(raw, scope).map(str::to_string);
    let held: HashSet<String> = own.iter().filter_map(|raw| id(raw)).collect();
    let mut written: Option<HashMap<String, i64>> = None;
    let mut stale = BTreeSet::new();
    for raw in inherited {
        let Some(named) = id(&raw).filter(|named| !held.contains(named)) else {
            continue;
        };
        let live = tx.query(
            "SELECT revision FROM memory_cards WHERE scope=?1 AND card_id=?2 AND state='live'",
            &[ops::t(scope), ops::t(&named)],
        )?;
        let Some(row) = live.first() else {
            continue;
        };
        let born = incarnation_start_in(tx, scope, &named, integer(&row[0]))?;
        let since = match &written {
            Some(since) => since,
            None => written.insert(alias_since_in(tx, scope, card_id, head)?),
        };
        if !matches!((born, since.get(&named)), (Some(born), Some(&at)) if born < at) {
            stale.insert(raw);
        }
    }
    Ok(stale)
}
