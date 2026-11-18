//! Dates the lineage edges that `aliases` writes (#2671). A merge writes the
//! retired id into the keeper's `aliases`, so the name can outlive the card it
//! named. If a new card later takes that id, the alias must not link it. An
//! alias edge forms only when the target's current incarnation began before the
//! keeper revision that first wrote the alias. Both dates come from the log
//! sequence numbers in `memory_card_revisions`, which are globally ordered.
use super::ranking;
use rusqlite::{Connection, params};
use std::collections::{HashMap, HashSet};

/// Revisions read back to date a card. A longer run leaves the date unknown,
/// and an unknown date forms no edge.
const RUN_CAP: usize = 256;
/// The row limit that tells a run cut by the cap from one that ended.
const READ_LIMIT: i64 = RUN_CAP as i64 + 1;

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
    let mut s = db.prepare_cached(
        "SELECT seq,state FROM memory_card_revisions WHERE scope=?1 AND card_id=?2 AND seq<=?3 ORDER BY seq DESC LIMIT ?4",
    )?;
    let rows = s
        .query_map(params![scope, card_id, head, READ_LIMIT], |r| {
            Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut start = None;
    for (i, (seq, state)) in rows.iter().enumerate() {
        if i == 0 && (*seq != head || state != "live") {
            return Ok(None);
        }
        if state != "live" {
            return Ok(start);
        }
        start = Some(*seq);
    }
    Ok((rows.len() <= RUN_CAP).then_some(start).flatten())
}

/// For each id the head's `aliases` names, the log sequence of the oldest
/// revision in the unbroken run of revisions, ending at the head, that all
/// name it. An alias dropped and written again dates from the second write.
/// An id whose run cannot be read to its start, because a blob is missing or
/// the history is too long, is left out, so it forms no edge.
pub(super) fn alias_since(
    db: &Connection,
    scope: &str,
    card_id: &str,
    head: i64,
) -> rusqlite::Result<HashMap<String, i64>> {
    let mut s = db.prepare_cached(
        "SELECT r.seq,b.bytes FROM memory_card_revisions r LEFT JOIN memory_blobs b ON b.content_hash=r.content_hash WHERE r.scope=?1 AND r.card_id=?2 AND r.seq<=?3 ORDER BY r.seq DESC LIMIT ?4",
    )?;
    let mut rows = s.query(params![scope, card_id, head, READ_LIMIT])?;
    let mut since = HashMap::new();
    let mut open: HashSet<String> = HashSet::new();
    let (mut read, mut unreadable) = (0, false);
    while let Some(row) = rows.next()? {
        read += 1;
        let seq: i64 = row.get(0)?;
        let bytes: Option<Vec<u8>> = row.get(1)?;
        if read == 1 && seq != head {
            return Ok(HashMap::new());
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
