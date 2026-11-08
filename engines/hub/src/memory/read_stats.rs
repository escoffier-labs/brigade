//! `read_stats`: per-card read aggregates for one scope (D2 of the memory
//! verification design, #2387). Slice 1 records the counts. This module
//! answers for them.
//!
//! The caller needs `read` and `read_stats` on the exact scope. Only a grant
//! that names the scope carries `read_stats`: no wildcard and no node default
//! opens it, and `reflect` does not imply it. Pages hold at most
//! [`PAGE_MAX`] rows, ordered by card id, and continue through an opaque
//! handle bound to the principal, the scope and the log epoch, which expires
//! after [`CURSOR_TTL_MS`]. One principal may make one request per second and
//! [`DAILY_MAX`] per UTC day. Every request, refused ones included, writes one
//! row to the hub's audit log through an [`AuditLog`]. The memory event log is
//! never used, so the record does not replicate to card readers. This module
//! never writes the hub file itself (D23): the crate-level `memory_audit`
//! sink does, on the hub writer, with no memory lock held.
use super::{Reply, auth, error, ids, invalid, proposal_cursor::Cursors, read_counts};
use crate::writer::ReadTx;
use rusqlite::types::Value;
use serde_json::{Map, Value as Json, json};
use std::{collections::HashMap, sync::Mutex};

/// The most rows one page holds, and the default.
pub const PAGE_MAX: i64 = 1000;
/// How long a continuation handle resolves after it was issued.
pub const CURSOR_TTL_MS: i64 = 15 * 60 * 1000;
/// The shortest gap between two admitted requests from one principal.
pub const MIN_GAP_MS: i64 = 1000;
/// Admitted requests one principal may make per UTC day.
pub const DAILY_MAX: u32 = 600;
/// Principals the limiter tracks at once. Past this, a new principal is
/// refused until an earlier day's entries can be dropped.
const PRINCIPALS: usize = 4096;
/// The audit action. `brigade.fleet_hub_audit` accepts it on read.
pub const AUDIT_ACTION: &str = "memory.read_stats";

/// Where `read_stats` audit rows go. `append` resolves true only once the row
/// is committed. A request whose row is not committed is answered 503.
pub trait AuditLog: Send + Sync {
    fn append(
        &self,
        row: Audit,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = bool> + Send + '_>>;
}

pub(super) fn cursors(hub_secret: &str) -> Cursors {
    Cursors::keyed(
        hub_secret,
        "brigade.read-stats-cursor.v1",
        "s1.",
        Some(CURSOR_TTL_MS),
    )
}

/// The scope a request names, when it is a valid one. Used for the audit row,
/// before and whatever the request's other fields say.
pub(super) fn requested_scope(map: &Map<String, Json>) -> Option<String> {
    map.get("scope")
        .and_then(Json::as_str)
        .filter(|s| ids::valid_scope(s))
        .map(String::from)
}

struct Entry {
    last_ms: i64,
    day: i64,
    count: u32,
}

/// The per-principal request limit. It lives only in memory and resets on
/// restart, which at most lets one extra day's quota through.
#[derive(Default)]
pub(super) struct Limiter {
    entries: Mutex<HashMap<String, Entry>>,
}
impl Limiter {
    /// True when this request is admitted. A refused request does not move
    /// the principal's window, so waiting a second always recovers.
    pub(super) fn admit(&self, principal: &str, now_ms: i64) -> bool {
        let Ok(mut entries) = self.entries.lock() else {
            return false;
        };
        let today = read_counts::day(now_ms);
        if !entries.contains_key(principal) && entries.len() >= PRINCIPALS {
            entries.retain(|_, e| e.day == today);
            if entries.len() >= PRINCIPALS {
                return false;
            }
        }
        let entry = entries.entry(principal.to_string()).or_insert(Entry {
            last_ms: i64::MIN,
            day: today,
            count: 0,
        });
        if entry.day != today {
            entry.day = today;
            entry.count = 0;
        }
        if now_ms.saturating_sub(entry.last_ms) < MIN_GAP_MS || entry.count >= DAILY_MAX {
            return false;
        }
        entry.last_ms = now_ms;
        entry.count += 1;
        true
    }
}

pub(super) fn rate_limited() -> Reply {
    error(
        429,
        "memory_read_stats_rate_limited",
        "read_stats allows one request per second and 600 per day for each principal",
    )
}

/// One page of aggregates. Runs on a memory reader, inside its snapshot.
pub(super) fn read<T: ReadTx + ?Sized>(
    tx: &T,
    who: &auth::Credential,
    map: &Map<String, Json>,
    cursors: &Cursors,
    now_ms: i64,
) -> rusqlite::Result<Reply> {
    if let Some(key) = map
        .keys()
        .find(|k| !["action", "scope", "limit", "after"].contains(&k.as_str()))
    {
        return Ok(invalid(&format!("unknown field {key:?} for read_stats")));
    }
    let Some(scope) = requested_scope(map) else {
        return Ok(invalid(
            "scope must be fleet, repo:<identity>, collection:<name> or principal:<id>",
        ));
    };
    let limit = match map.get("limit") {
        None | Some(Json::Null) => PAGE_MAX,
        Some(value) => match value.as_i64().filter(|n| (1..=PAGE_MAX).contains(n)) {
            Some(n) => n,
            None => return Ok(invalid(&format!("limit must be from 1 to {PAGE_MAX}"))),
        },
    };
    if !auth::has_right(tx, who, Some(&scope), "read")?
        || !auth::has_exact_right(tx, who, &scope, "read_stats")?
    {
        return Ok(error(
            403,
            "memory_grant_missing",
            "read_stats needs read and read_stats granted on this exact scope",
        ));
    }
    let epoch = match tx
        .query("SELECT value FROM memory_meta WHERE key='log_epoch'", &[])?
        .first()
        .map(|r| r[0].clone())
    {
        Some(Value::Text(epoch)) => epoch,
        _ => String::new(),
    };
    // Scope names cannot hold a newline, so this binding is unambiguous.
    let binding = format!("{epoch}\n{scope}");
    let after = match map.get("after") {
        None | Some(Json::Null) => String::new(),
        Some(value) => match value
            .as_str()
            .and_then(|handle| cursors.resolve_at(handle, who, &binding, false, now_ms))
        {
            Some(position) => position,
            None => {
                return Ok(invalid(
                    "invalid or expired read_stats cursor. Restart without after",
                ));
            }
        },
    };
    let since = tx
        .query(
            "SELECT value FROM memory_meta WHERE key='read_counts_since_day'",
            &[],
        )?
        .first()
        .and_then(|r| match &r[0] {
            Value::Text(day) => day.parse::<i64>().ok(),
            _ => None,
        });
    let today = read_counts::day(now_ms);
    // Live cards only. A window holds `today` and the days before it.
    let rows = tx.query(
        "SELECT t.card_id, t.last_read_day, t.total_reads,
           COALESCE((SELECT sum(d.search_hits + d.card_reads) FROM memory_read_days d
                     WHERE d.scope=t.scope AND d.card_id=t.card_id AND d.day>?3 - 7), 0),
           COALESCE((SELECT sum(d.search_hits + d.card_reads) FROM memory_read_days d
                     WHERE d.scope=t.scope AND d.card_id=t.card_id AND d.day>?3 - 30), 0)
         FROM memory_read_totals t
         JOIN memory_cards c ON c.scope=t.scope AND c.card_id=t.card_id AND c.state='live'
         WHERE t.scope=?1 AND t.card_id>?2
         ORDER BY t.card_id LIMIT ?4",
        &[
            Value::Text(scope.clone()),
            Value::Text(after),
            Value::Integer(today),
            Value::Integer(limit + 1),
        ],
    )?;
    let int = |v: &Value| if let Value::Integer(i) = v { *i } else { 0 };
    let mut page = Vec::new();
    let mut last = None;
    for row in rows.iter().take(limit as usize) {
        let Value::Text(card_id) = &row[0] else {
            continue;
        };
        last = Some(card_id.clone());
        page.push(json!({
            "card_id": card_id,
            "reads_7d": int(&row[3]),
            "reads_30d": int(&row[4]),
            "last_read_day": int(&row[1]),
            "total_reads": int(&row[2]),
        }));
    }
    let next = if rows.len() as i64 > limit {
        match last.and_then(|position| cursors.issue_at(position, who, &binding, false, now_ms)) {
            Some(handle) => Json::String(handle),
            None => {
                return Ok(error(
                    503,
                    "memory_cursor_unavailable",
                    "read_stats pagination unavailable",
                ));
            }
        }
    } else {
        Json::Null
    };
    Ok(Reply::new(
        200,
        json!({
            "scope": scope,
            "rows": page,
            "next_after": next,
            "read_counts_since_day": since,
        }),
    ))
}

/// One audit row for one `read_stats` request. The detail holds
/// the scope (when the request named a valid one), the rows returned and the
/// HTTP status. No query, cursor, card id or count value is recorded.
pub struct Audit {
    pub actor: String,
    pub scope: Option<String>,
    pub rows: usize,
    pub status: u16,
    pub at: String,
}
impl Audit {
    pub(super) fn new(
        who: &auth::Credential,
        scope: Option<String>,
        reply: &Reply,
        now_ms: i64,
    ) -> Self {
        Self {
            // The registration audit names the admin token `admin`, so this does too.
            actor: match who {
                auth::Credential::Admin => "admin".into(),
                other => other.principal().into(),
            },
            scope,
            rows: reply.body["rows"].as_array().map_or(0, Vec::len),
            status: reply.status,
            at: ids::isoformat(now_ms),
        }
    }
    /// The `detail` JSON the audit log stores.
    pub fn detail(&self) -> Json {
        let mut detail = json!({"count": self.rows, "status": self.status});
        if let Some(scope) = &self.scope {
            detail["scope"] = json!(scope);
        }
        detail
    }
}
