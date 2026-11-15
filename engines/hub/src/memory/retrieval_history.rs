//! Private append-only search/use authority. No workspace file supplies labels.
use super::{
    Reply,
    auth::{self, Credential},
    ids,
    ops::{self, Prepared},
    proposals::string,
};
use crate::writer::{ReadTx, WriteTx};
use rusqlite::types::Value;
use serde_json::{Value as Json, json};
use std::collections::{BTreeMap, BTreeSet};

pub const AGE_MS: i64 = 14 * 86_400_000;
pub const DDL: &str = "CREATE TABLE IF NOT EXISTS memory_retrieval_history(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, principal TEXT NOT NULL, credential TEXT NOT NULL,
 session TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('search','use')),
 created_ms INTEGER NOT NULL, payload TEXT NOT NULL);
 CREATE INDEX IF NOT EXISTS memory_retrieval_history_session ON memory_retrieval_history(credential,session,kind,seq);
 CREATE TRIGGER IF NOT EXISTS memory_retrieval_history_no_update BEFORE UPDATE ON memory_retrieval_history
 BEGIN SELECT RAISE(ABORT,'retrieval history is append-only'); END;
 CREATE TRIGGER IF NOT EXISTS memory_retrieval_history_no_delete BEFORE DELETE ON memory_retrieval_history
 BEGIN SELECT RAISE(ABORT,'retrieval history is append-only'); END;";

fn refused() -> Reply {
    ops::error(
        409,
        "memory_retrieval_history_insufficient",
        "judged history does not meet provenance or diversity requirements",
    )
}
fn budget() -> Reply {
    ops::error(
        409,
        "memory_retrieval_history_budget",
        "live evaluation budget exceeded",
    )
}
fn credential(c: &Credential) -> Option<&str> {
    if let Credential::Node { digest, .. } = c {
        Some(digest)
    } else {
        None
    }
}
pub fn session(r: &Json) -> Option<&str> {
    let s = r["session_id"].as_str()?;
    (r["session_kind"] == "interactive"
        && !s.is_empty()
        && s.len() <= 128
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_.:".contains(&b)))
    .then_some(s)
}
/// Only the server calls this with its own ranked reply, never client results.
pub fn search(c: &Credential, request: &Json, reply: &Json, now: i64) -> Option<Prepared> {
    credential(c)?;
    session(request)?;
    if request["query_source"] != "explicit"
        || request["eval"] == true
        || reply["retrieval"]["stale"] == true
        || reply["retrieval"]["degraded"] == true
    {
        return None;
    }
    let query = request["query"].as_str()?;
    if query.is_empty() || query.len() > 4096 {
        return None;
    }
    let payload = json!({"session_id":request["session_id"],"session_kind":"interactive",
        "query":query,"results":reply["matches"].as_array()?.iter().map(|m| json!({"scope":m["scope"],"card_id":m["card_id"],"content_hash":m["content_hash"],"revision":m["revision"]})).collect::<Vec<_>>(),"epoch":reply["log_epoch"]});
    Some(Prepared {
        credential: c.clone(),
        action: ops::Action::RetrievalSearch(payload),
        client_event_id: ids::mint(now),
        request_digest: String::new(),
        now_ms: now,
    })
}
pub fn write(
    tx: &dyn WriteTx,
    p: &Prepared,
    request: &Json,
    search: bool,
) -> rusqlite::Result<Reply> {
    let (Some(key), Some(sid)) = (credential(&p.credential), session(request)) else {
        return Ok(refused());
    };
    let usage = tx.query("SELECT COUNT(*),COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM memory_retrieval_history", &[])?;
    if usage[0][0].as_i64().unwrap_or(i64::MAX) >= 10_000
        || usage[0][1].as_i64().unwrap_or(i64::MAX) + request.to_string().len() as i64
            > 16 * 1024 * 1024
    {
        return Ok(budget());
    }
    let payload = if search {
        if request["epoch"] != ops::epoch(tx)? {
            return Ok(refused());
        }
        request.clone()
    } else {
        let rows = tx.query("SELECT seq,created_ms,payload FROM memory_retrieval_history WHERE credential=?1 AND session=?2 AND kind='search' AND created_ms>=?3 AND created_ms<=?4 ORDER BY seq DESC LIMIT 501",
            &[ops::t(key),ops::t(sid),Value::Integer(p.now_ms-600_000),Value::Integer(p.now_ms)])?;
        if rows.len() > 500 {
            return Ok(budget());
        }
        let mut found = None;
        for row in rows {
            let r: Json = serde_json::from_str(&ops::s(&row[2])).unwrap_or(Json::Null);
            let matches: Vec<_> = r["results"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|m| {
                    m["card_id"] == request["card_id"]
                        && m["content_hash"] == request["content_hash"]
                })
                .collect();
            if matches.len() > 1 {
                return Ok(refused());
            }
            if let Some(m) = matches.first() {
                if !auth::has_right(tx, &p.credential, m["scope"].as_str(), "read")? {
                    return Ok(refused());
                }
                found = Some(json!({"search_seq":row[0].as_i64(),"match":m}));
                break;
            }
        }
        let Some(found) = found else {
            return Ok(refused());
        };
        found
    };
    tx.execute("INSERT INTO memory_retrieval_history(principal,credential,session,kind,created_ms,payload) VALUES(?1,?2,?3,?4,?5,?6)",
        &[ops::t(p.credential.principal()),ops::t(key),ops::t(sid),ops::t(if search {"search"}else{"use"}),Value::Integer(p.now_ms),ops::t(&payload.to_string())])?;
    let seq = tx.query("SELECT last_insert_rowid()", &[])?;
    Ok(Reply::new(201, json!({"search_id":seq[0][0].as_i64()})))
}

/// Derive at a frozen watermark. Current heads and negatives can only remove labels.
pub fn derive<T: ReadTx + ?Sized>(
    tx: &T,
    scope: &str,
    high: Option<i64>,
    now: i64,
) -> rusqlite::Result<Result<Json, Reply>> {
    if !ids::valid_scope(scope) {
        return Ok(Err(refused()));
    }
    let max = tx.query(
        "SELECT COALESCE(MAX(seq),0) FROM memory_retrieval_history",
        &[],
    )?[0][0]
        .as_i64()
        .unwrap_or(0);
    let high = high.unwrap_or(max);
    if high < 0 || high > max {
        return Ok(Err(refused()));
    }
    let rows=tx.query("SELECT u.seq,s.payload,u.payload,u.session,u.credential,u.created_ms,s.created_ms FROM memory_retrieval_history u JOIN memory_retrieval_history s ON s.seq=json_extract(u.payload,'$.search_seq') AND s.kind='search' AND s.session=u.session AND s.credential=u.credential WHERE u.kind='use' AND u.seq<=?1 AND u.created_ms>=?2 AND u.created_ms<=?3 ORDER BY u.seq LIMIT 501", &[Value::Integer(high),Value::Integer(now-AGE_MS),Value::Integer(now)])?;
    if rows.len() > 500 {
        return Ok(Err(budget()));
    }
    // Bound the current corpus before parsing relation metadata. A later
    // supersede on another card invalidates its predecessor's old labels too.
    let size=tx.query("SELECT COUNT(*),COALESCE(SUM(b.size),0) FROM memory_cards c JOIN memory_blobs b ON b.content_hash=c.content_hash WHERE c.scope=?1 AND c.state='live'", &[ops::t(scope)])?;
    if size[0][0].as_i64().unwrap_or(i64::MAX) > 2000
        || size[0][1].as_i64().unwrap_or(i64::MAX) > 16 * 1024 * 1024
    {
        return Ok(Err(budget()));
    }
    let corpus=tx.query("SELECT c.card_id,c.trust,b.bytes FROM memory_cards c JOIN memory_blobs b ON b.content_hash=c.content_hash WHERE c.scope=?1 AND c.state='live' LIMIT 2001", &[ops::t(scope)])?;
    if corpus.len() > 2000 {
        return Ok(Err(budget()));
    }
    let mut retired = BTreeSet::new();
    let mut bytes = 0;
    for row in corpus {
        let Value::Blob(raw) = &row[2] else {
            continue;
        };
        bytes += raw.len();
        if bytes > 16 * 1024 * 1024 {
            return Ok(Err(budget()));
        }
        let links = super::ranking::Links::parse(&String::from_utf8_lossy(raw), scope);
        if links.successor.is_some() {
            retired.insert(ops::s(&row[0]));
        }
        if super::ranking::trusted(&ops::s(&row[1])) {
            retired.extend(links.predecessors);
        }
    }
    let mut grouped: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    let mut sessions = BTreeSet::new();
    let mut days = BTreeSet::new();
    for row in rows {
        let s: Json = serde_json::from_str(&ops::s(&row[1])).unwrap_or(Json::Null);
        let u: Json = serde_json::from_str(&ops::s(&row[2])).unwrap_or(Json::Null);
        let m = &u["match"];
        if m["scope"] != scope || s["epoch"] != ops::epoch(tx)? {
            continue;
        }
        let card = string(m, "card_id");
        if retired.contains(card) {
            continue;
        }
        let live=tx.query("SELECT content_hash,revision FROM memory_cards WHERE scope=?1 AND card_id=?2 AND state='live'", &[ops::t(scope),ops::t(card)])?;
        let Some(head) = live.first() else {
            continue;
        };
        if ops::s(&head[0]) != string(m, "content_hash")
            || head[1].as_i64() != m["revision"].as_i64()
        {
            continue;
        }
        let bad=tx.query("SELECT 1 FROM memory_evidence WHERE scope=?1 AND card_id=?2 AND stance IN ('contrary','negative') AND created_ms>=?3 LIMIT 1", &[ops::t(scope),ops::t(card),row[6].clone()])?;
        if !bad.is_empty() {
            continue;
        }
        let query = string(&s, "query")
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
            .to_lowercase();
        if query.is_empty() {
            continue;
        }
        grouped.entry(query).or_default().insert(card.into());
        sessions.insert((ops::s(&row[4]).to_string(), ops::s(&row[3]).to_string()));
        days.insert(row[5].as_i64().unwrap_or(0) / 86_400_000);
    }
    let mut shares: BTreeMap<&str, usize> = BTreeMap::new();
    for gold in grouped.values() {
        for card in gold {
            *shares.entry(card).or_default() += 1;
        }
    }
    if grouped.len() < 20
        || sessions.len() < 3
        || days.len() < 3
        || shares.values().any(|n| n * 5 > grouped.len())
    {
        return Ok(Err(refused()));
    }
    let pairs: Vec<_> = grouped.iter().map(|(q, g)| json!([q, g])).collect();
    let digest = ids::sha256_hex(ids::canonical(&json!(pairs)).as_bytes());
    Ok(Ok(
        json!({"queries":pairs,"query_set_digest":digest,"history_seq":high,"query_count":grouped.len()}),
    ))
}
pub fn valid<T: ReadTx + ?Sized>(tx: &T, body: &Json, now: i64) -> rusqlite::Result<bool> {
    let Some(high) = body["live"]["history_seq"].as_i64() else {
        return Ok(false);
    };
    let judged = derive(
        tx,
        string(&body["live"]["binding"], "scope"),
        Some(high),
        now,
    )?;
    Ok(judged.is_ok_and(|j| {
        j["query_set_digest"] == body["query_set_digest"]
            && j["query_count"] == body["adapters"]["before"]["overall"]["query_count"]
    }))
}
trait Integer {
    fn as_i64(&self) -> Option<i64>;
}
impl Integer for Value {
    fn as_i64(&self) -> Option<i64> {
        if let Value::Integer(n) = self {
            Some(*n)
        } else {
            None
        }
    }
}
