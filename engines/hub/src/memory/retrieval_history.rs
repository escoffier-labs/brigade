//! Private search/use veto labels. History never authorizes an apply.
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
 WHEN NOT EXISTS(SELECT 1 FROM memory_meta WHERE key='retrieval_history_rotation')
 BEGIN SELECT RAISE(ABORT,'retrieval history is append-only'); END;";

fn refused() -> Reply {
    ops::error(
        409,
        "memory_retrieval_history_insufficient",
        "judged history does not meet provenance requirements",
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
    let size = payload.to_string().len() as i64;
    for (condition, params, rows, bytes) in [
        ("1=1", vec![], 10_000, 16 * 1024 * 1024),
        (
            "principal=?1",
            vec![ops::t(p.credential.principal())],
            1_000,
            2 * 1024 * 1024,
        ),
        ("credential=?1", vec![ops::t(key)], 500, 1024 * 1024),
    ] {
        let usage = tx.query(&format!("SELECT COUNT(*),COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM memory_retrieval_history WHERE {condition}"), &params)?;
        if usage[0][0].as_i64().unwrap_or(i64::MAX) >= rows
            || usage[0][1]
                .as_i64()
                .unwrap_or(i64::MAX)
                .saturating_add(size)
                > bytes
        {
            return Ok(budget());
        }
    }
    for (field, value) in [("principal", p.credential.principal()), ("credential", key)] {
        let recent = tx.query(
            &format!(
                "SELECT COUNT(*) FROM memory_retrieval_history WHERE {field}=?1 AND created_ms>=?2"
            ),
            &[
                ops::t(value),
                Value::Integer(p.now_ms.saturating_sub(60_000)),
            ],
        )?;
        if recent[0][0].as_i64().unwrap_or(i64::MAX) >= 120 {
            return Ok(budget());
        }
    }
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
    let rows=tx.query("SELECT u.seq,s.payload,u.payload,u.session,u.credential,u.created_ms,s.created_ms FROM memory_retrieval_history u JOIN memory_retrieval_history s ON s.seq=json_extract(u.payload,'$.search_seq') AND s.kind='search' AND s.session=u.session AND s.credential=u.credential WHERE json_extract(u.payload,'$.match.scope')=?4 AND u.kind='use' AND u.seq<=?1 AND u.created_ms>=?2 AND u.created_ms<=?3 ORDER BY u.seq LIMIT 501", &[Value::Integer(high),Value::Integer(now-AGE_MS),Value::Integer(now),ops::t(scope)])?;
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
    // With no judged use there is nothing for the retired set to filter, so the
    // result is the empty query set whatever the corpus holds. The size bound
    // above still refuses an oversized scope. Parsing every card's relation
    // metadata is the cost this skips.
    if rows.is_empty() {
        return Ok(Ok(judged_queries(&BTreeMap::new(), high)));
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
    }
    Ok(Ok(judged_queries(&grouped, high)))
}
fn judged_queries(grouped: &BTreeMap<String, BTreeSet<String>>, high: i64) -> Json {
    let pairs: Vec<_> = grouped.iter().map(|(q, g)| json!([q, g])).collect();
    let digest = ids::sha256_hex(ids::canonical(&json!(pairs)).as_bytes());
    json!({"queries":pairs,"query_set_digest":digest,"history_seq":high,"query_count":grouped.len()})
}
/// Bounded rotation only discards history too old for a still-live report or join.
pub fn rotate(tx: &dyn WriteTx, p: &Prepared, limit: usize) -> rusqlite::Result<Reply> {
    if !matches!(p.credential, Credential::Admin) {
        return Ok(ops::error(
            403,
            "memory_admin_required",
            "history rotation requires admin authority",
        ));
    }
    if !(1..=500).contains(&limit) {
        return Ok(ops::error(
            422,
            "memory_retrieval_rotation_invalid",
            "rotation limit must be from 1 through 500",
        ));
    }
    let cutoff = p
        .now_ms
        .saturating_sub(AGE_MS + super::retrieval_eval::MAX_AGE_MS + 600_000);
    tx.execute(
        "INSERT INTO memory_meta(key,value) VALUES('retrieval_history_rotation','admin')",
        &[],
    )?;
    let deleted = tx.execute("DELETE FROM memory_retrieval_history WHERE seq IN (SELECT seq FROM memory_retrieval_history WHERE created_ms<?1 ORDER BY seq LIMIT ?2)", &[Value::Integer(cutoff),Value::Integer(limit as i64)])?;
    tx.execute(
        "DELETE FROM memory_meta WHERE key='retrieval_history_rotation'",
        &[],
    )?;
    // Audit in the existing non-head event class, containing no private queries.
    ops::append(
        tx,
        p,
        ops::NewEvent {
            kind: "recall.capture",
            scope: None,
            card_id: None,
            changes_head: false,
            base_revision: None,
            content_hash: None,
            trust: None,
            origin: None,
            policy_digest: None,
            payload: json!({"action":"retrieval_history_rotate","deleted":deleted,"before_ms":cutoff}),
        },
    )?;
    Ok(Reply::new(
        200,
        json!({"deleted":deleted,"before_ms":cutoff}),
    ))
}

/// Current history is a veto only. Replay every surviving gold independently.
pub fn veto<T: ReadTx + ?Sized>(
    tx: &T,
    proposal: &Json,
    now: i64,
) -> rusqlite::Result<Option<&'static str>> {
    let started = std::time::Instant::now();
    let cards = super::proposals::cards(proposal);
    let Some(target) = cards.first() else {
        return Ok(Some("retrieval_eval_projection_changed"));
    };
    let scope = string(target, "scope");
    let judged = match derive(tx, scope, None, now)? {
        Ok(j) => j,
        Err(_) => return Ok(Some("retrieval_eval_history_unavailable")),
    };
    // Each judged query replays against a before and an after index of the whole
    // scope. Building those two is the cost of this veto, so a scope with no
    // judged query, which cannot be vetoed, never builds them.
    if judged["queries"].as_array().is_none_or(Vec::is_empty) {
        return Ok(None);
    }
    let rows=tx.query(&format!("SELECT c.card_id,c.revision,c.content_hash,c.trust,b.bytes,{} FROM memory_cards c JOIN memory_blobs b ON b.content_hash=c.content_hash WHERE c.scope=?1 AND c.state='live' ORDER BY c.card_id",super::index::AUTOMATED_SQL), &[ops::t(scope)])?;
    let before: Vec<_> = rows
        .iter()
        .map(|r| super::index::Card {
            scope: scope.into(),
            card_id: ops::s(&r[0]),
            revision: r[1].as_i64().unwrap_or(0),
            content_hash: ops::s(&r[2]),
            trust: ops::s(&r[3]),
            text: if let Value::Blob(bytes) = &r[4] {
                String::from_utf8_lossy(bytes).into_owned()
            } else {
                String::new()
            },
            automated: r[5].as_i64().unwrap_or(0) != 0,
        })
        .collect();
    let mut after = before.clone();
    let next = before
        .iter()
        .map(|c| c.revision)
        .max()
        .unwrap_or(0)
        .saturating_add(1);
    for (offset, c) in cards.iter().enumerate() {
        if c["after_state"] == "retired" {
            after.retain(|a| a.card_id != string(c, "card_id"));
        } else if let Some(a) = after.iter_mut().find(|a| a.card_id == string(c, "card_id")) {
            if c["after_text"] != c["before_text"] || c["after_state"] != c["before_state"] {
                a.revision = next.saturating_add(offset as i64);
            }
            a.text = string(c, "after_text").into();
            a.content_hash = string(c, "after_hash").into();
        }
    }
    // These snapshots are undated on purpose. The proposal's `after` card is not
    // in the store, so its alias dates cannot be read, and an undated builder
    // forms every alias edge. The veto can therefore collapse a reused id that
    // the live index leaves visible, and judge a proposal against lineage the
    // live index no longer has. That is tracked separately (#2726).
    let b = super::index::Builder::from_cards(before, 0).snapshot();
    let a = super::index::Builder::from_cards(after, 0).snapshot();
    for q in judged["queries"].as_array().into_iter().flatten() {
        if started.elapsed().as_secs() >= 60 {
            return Ok(Some("retrieval_eval_history_unavailable"));
        }
        let tokens = super::text::tokenize(q[0].as_str().unwrap_or(""));
        // Default lexical ranking includes lineage and source demotion too.
        // Literal searches have a separate bounded blob scan. Hold until its
        // candidate replay is implemented rather than treating it as no hits.
        if tokens.is_empty() {
            return Ok(Some("retrieval_eval_history_unavailable"));
        }
        let rank = |s: &super::index::Snapshot| -> rusqlite::Result<Option<Vec<String>>> {
            let allowed = vec![true; s.scopes().len()];
            let ordered = s.top(s.score(&allowed, &tokens), super::index::TOP);
            let collapsed = super::ranking::collapse_eligible(s, &allowed, ordered, |_| Ok(true))?;
            if collapsed.overflow {
                return Ok(None);
            }
            let mut hits = collapsed.hits;
            hits.sort_by_key(|(n, _)| {
                let d = s.doc(*n);
                d.automated && &*d.trust == "agent-unreviewed"
            });
            Ok(Some(
                hits.into_iter()
                    .map(|(n, _)| s.doc(n).card_id.to_string())
                    .collect(),
            ))
        };
        let (Some(br), Some(ar)) = (rank(&b)?, rank(&a)?) else {
            return Ok(Some("retrieval_eval_history_unavailable"));
        };
        for g in q[1]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Json::as_str)
        {
            if ar.iter().position(|id| id == g).unwrap_or(usize::MAX)
                > br.iter().position(|id| id == g).unwrap_or(usize::MAX)
            {
                return Ok(Some("retrieval_eval_query_veto"));
            }
        }
    }
    Ok(None)
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
