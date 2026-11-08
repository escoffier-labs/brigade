//! Bounded operator reads. Diffs use the same review gate as proposal replays.
use super::{Reply, auth, ids, proposal_cursor::Cursors, proposals};
use crate::writer::ReadTx;
use rusqlite::types::Value;
use serde_json::{Map, Value as Json, json};

pub(super) fn read<T: ReadTx + ?Sized>(
    tx: &T,
    who: &auth::Credential,
    action: &str,
    map: &Map<String, Json>,
    cursors: &Cursors,
) -> rusqlite::Result<Reply> {
    // Global listing is an explicit operator capability. Without it, even the
    // continuation shape must be independent of hidden proposal population.
    if action == "proposal_list" && !auth::has_right(tx, who, Some("*"), "list_proposals")? {
        return Ok(Reply::new(200, json!({"proposals":[],"next_after":null})));
    }
    let epoch = tx
        .query("SELECT value FROM memory_meta WHERE key='log_epoch'", &[])?
        .first()
        .and_then(|r| {
            if let Value::Text(value) = &r[0] {
                Some(value.clone())
            } else {
                None
            }
        })
        .unwrap_or_default();
    let allowed: &[&str] = if action == "proposal_show" {
        &["action", "proposal_id"]
    } else {
        &["action", "all", "limit", "after"]
    };
    if map.keys().any(|key| !allowed.contains(&key.as_str())) {
        return Ok(super::invalid("unknown proposal read field"));
    }
    if action == "proposal_show" {
        let Some((id, _)) = map
            .get("proposal_id")
            .and_then(Json::as_str)
            .and_then(ids::parse_event_id)
        else {
            return Ok(super::invalid("proposal_id must be a time-ordered id"));
        };
        let request = json!({"action":"preflight","proposal_id":id});
        if let Some(denied) = proposals::authorize(tx, who, &request)? {
            return Ok(denied);
        }
        let body = proposals::load(tx, &id)?.unwrap();
        let mut response = proposals::reply(&body, 200);
        proposals::redact_auto_reply(tx, who, &body, &mut response.body)?;
        super::manifest_binding::reveal(tx, who, &body, &mut response.body)?;
        response.body["log_epoch"] = json!(epoch);
        return Ok(response);
    }
    let all = match map.get("all") {
        None => false,
        Some(Json::Bool(value)) => *value,
        _ => return Ok(super::invalid("all must be a boolean")),
    };
    let limit = match map.get("limit") {
        None => 100,
        Some(value) => match value.as_i64().filter(|n| (1..=100).contains(n)) {
            Some(n) => n as usize,
            None => return Ok(super::invalid("limit must be from 1 to 100")),
        },
    };
    let after = match map.get("after") {
        None => String::new(),
        Some(value) => match value
            .as_str()
            .and_then(|handle| cursors.resolve(handle, who, &epoch, all))
        {
            Some(position) => position,
            None => {
                return Ok(super::invalid(
                    "invalid or expired proposal cursor. Restart the list without after",
                ));
            }
        },
    };
    // Scan at most one page, including hidden proposals. The cursor always advances.
    let rows = tx.query(
        "SELECT proposal_id FROM memory_proposals WHERE proposal_id>?1 AND (?2 OR state='pending') ORDER BY proposal_id LIMIT ?3",
        &[Value::Text(after), Value::Integer(i64::from(all)), Value::Integer((limit + 1) as i64)],
    )?;
    let mut summaries = Vec::new();
    let mut last = None;
    for row in rows.iter().take(limit) {
        let Value::Text(id) = &row[0] else { continue };
        last = Some(id.clone());
        let body = proposals::load(tx, id)?.unwrap();
        let mut visible = true;
        for card in proposals::cards(&body) {
            if !proposals::scoped_right(tx, who, proposals::string(card, "scope"), "read")? {
                visible = false;
                break;
            }
        }
        if visible {
            summaries.push(json!({"proposal_id":id,"state":body["state"],"tier":body["tier"],"policy_digest":body["policy_digest"]}));
        }
    }
    let next = if rows.len() > limit {
        let Some(handle) = last.and_then(|position| cursors.issue(position, who, &epoch, all))
        else {
            return Ok(super::error(
                503,
                "memory_cursor_unavailable",
                "proposal pagination unavailable",
            ));
        };
        Some(handle)
    } else {
        None
    };
    Ok(Reply::new(
        200,
        json!({"proposals":summaries,"next_after":next}),
    ))
}
