//! `search` (S13b): BM25 over the caller's visible corpus, answered from one
//! published index snapshot. Steps 1 to 3 and 8 of the retrieval pipeline:
//! normalize the query, take live cards in the readable scopes, score them as
//! `memory_bm25.py` does and return the page. Dense retrieval, fusion,
//! lineage collapsing and automation demotion belong to later slices.
use super::{
    Credential, Reply, auth,
    index::{Snapshot, TOP},
    ops::error,
    text,
};
use crate::writer::ReadTx;
use rusqlite::types::Value;
use serde_json::{Map, Value as Json, json};

/// Query length after normalization, in characters.
pub const QUERY_CHARS: usize = 256;
/// Raw query text read before normalization, in characters.
const RAW_QUERY_CHARS: usize = 4096;
pub const LIMIT_MAX: i64 = 20;
const LIMIT_DEFAULT: i64 = 10;
const SCOPES_MAX: usize = 64;

#[derive(Clone, Debug)]
pub struct Request {
    pub query: String,
    pub tokens: Vec<String>,
    pub limit: usize,
    pub scopes: Option<Vec<String>>,
    pub min_seq: Option<i64>,
}

fn invalid(message: &str) -> Reply {
    error(400, "memory_query_invalid", message)
}

/// Validates a `search` body. Field errors answer `memory_query_invalid`.
pub fn parse(map: &Map<String, Json>) -> Result<Request, Reply> {
    const ALLOWED: &[&str] = &[
        "action",
        "query",
        "limit",
        "mode",
        "query_source",
        "scopes",
        "sources",
        "min_seq",
    ];
    if let Some(k) = map.keys().find(|k| !ALLOWED.contains(&k.as_str())) {
        return Err(invalid(&format!("unknown field {k:?} for search")));
    }
    let raw = map
        .get("query")
        .and_then(Json::as_str)
        .ok_or_else(|| invalid("query must be a string"))?;
    if raw.chars().count() > RAW_QUERY_CHARS {
        return Err(invalid("query is too long"));
    }
    // One line, as the recall hooks normalize it.
    let query = text::single_line(raw);
    if query.is_empty() {
        return Err(invalid("query must not be empty"));
    }
    if query.chars().count() > QUERY_CHARS {
        return Err(invalid(&format!(
            "query must be at most {QUERY_CHARS} characters after normalization"
        )));
    }
    let limit = match map.get("limit") {
        None | Some(Json::Null) => LIMIT_DEFAULT,
        Some(v) => v
            .as_i64()
            .filter(|n| (1..=LIMIT_MAX).contains(n))
            .ok_or_else(|| invalid(&format!("limit must be an integer from 1 to {LIMIT_MAX}")))?,
    } as usize;
    let one_of = |key: &str, values: &[&str]| -> Result<(), Reply> {
        match map.get(key) {
            None | Some(Json::Null) => Ok(()),
            Some(Json::String(s)) if values.contains(&s.as_str()) => Ok(()),
            _ => Err(invalid(&format!(
                "{key} must be one of {}",
                values.join(", ")
            ))),
        }
    };
    // With no embedder configured every mode is answered by BM25 (D30).
    one_of("mode", &["auto", "lexical", "hybrid"])?;
    one_of("query_source", &["prompt", "cwd", "explicit"])?;
    match map.get("sources") {
        None | Some(Json::Null) => {}
        Some(Json::Array(items)) if items.iter().all(|i| i.as_str() == Some("automation")) => {}
        _ => return Err(invalid("sources must be a list naming only automation")),
    }
    let scopes = match map.get("scopes") {
        None | Some(Json::Null) => None,
        Some(Json::Array(items)) if !items.is_empty() && items.len() <= SCOPES_MAX => {
            let mut scopes = Vec::new();
            for item in items {
                match item.as_str().filter(|s| super::ids::valid_scope(s)) {
                    Some(s) => scopes.push(s.to_string()),
                    None => {
                        return Err(invalid(
                            "scopes must name fleet, repo:<identity> or principal:<id>",
                        ));
                    }
                }
            }
            scopes.sort();
            scopes.dedup();
            Some(scopes)
        }
        _ => {
            return Err(invalid(&format!(
                "scopes must be a list of 1 to {SCOPES_MAX} scopes"
            )));
        }
    };
    let min_seq = match map.get("min_seq") {
        None | Some(Json::Null) => None,
        Some(v) => Some(
            v.as_i64()
                .filter(|n| *n >= 0)
                .ok_or_else(|| invalid("min_seq must be a non-negative integer"))?,
        ),
    };
    let tokens = text::tokenize(&query);
    Ok(Request {
        query,
        tokens,
        limit,
        scopes,
        min_seq,
    })
}

/// `memory_bm25.rank` for a query with no word characters: the literal
/// substring match on the lowered field, every match scoring 0.0. `body`
/// reads a document's card text from the store.
pub fn literal(
    snap: &Snapshot,
    allowed: &[bool],
    query: &str,
    mut body: impl FnMut(&str) -> rusqlite::Result<Option<String>>,
) -> rusqlite::Result<Vec<(u32, f64)>> {
    let lowered = text::py_lower(query);
    let needles = text::py_split(&lowered);
    let mut hits = Vec::new();
    if needles.is_empty() {
        return Ok(hits);
    }
    for n in snap.live(allowed) {
        let doc = snap.doc(n);
        let Some(body) = body(&doc.content_hash)? else {
            continue;
        };
        let field = text::py_lower(&text::card_text(&doc.fields, &body));
        if needles.iter().any(|needle| field.contains(needle)) {
            hits.push((n, 0.0));
        }
    }
    Ok(hits)
}
fn blob<T: ReadTx + ?Sized>(tx: &T, hash: &str) -> rusqlite::Result<Option<String>> {
    let rows = tx.query(
        "SELECT bytes FROM memory_blobs WHERE content_hash=?1",
        &[Value::Text(hash.into())],
    )?;
    Ok(match rows.first().map(|r| &r[0]) {
        Some(Value::Blob(b)) => Some(String::from_utf8_lossy(b).into_owned()),
        Some(Value::Text(t)) => Some(t.clone()),
        _ => None,
    })
}

pub fn run<T: ReadTx + ?Sized>(
    tx: &T,
    credential: &Credential,
    request: &Request,
    snap: &Snapshot,
) -> rusqlite::Result<Reply> {
    if !auth::has_right(tx, credential, None, "read")? {
        return Ok(error(
            403,
            "memory_grant_missing",
            "this credential has no memory read grant",
        ));
    }
    let mut readable = auth::Readable::new(tx, credential);
    let scopes = snap.scopes();
    let mut allowed = vec![false; scopes.len()];
    match &request.scopes {
        Some(requested) => {
            for s in requested {
                if !readable.scope(s)? {
                    return Ok(error(
                        403,
                        "memory_grant_missing",
                        "this credential has no read grant on a requested scope",
                    ));
                }
            }
            for (i, name) in scopes.iter().enumerate() {
                allowed[i] = requested.binary_search(name).is_ok();
            }
        }
        None => {
            for (i, name) in scopes.iter().enumerate() {
                allowed[i] = readable.scope(name)?;
            }
        }
    }
    let hits = if request.tokens.is_empty() {
        literal(snap, &allowed, &request.query, |hash| blob(tx, hash))?
    } else {
        snap.score(&allowed, &request.tokens)
    };
    let total = hits.len();
    let matches: Vec<Json> = snap
        .top(hits, TOP)
        .into_iter()
        .take(request.limit)
        .enumerate()
        .map(|(rank, (n, score))| {
            let d = snap.doc(n);
            json!({
                "path": format!("cards/{}.md", d.card_id),
                "scope": scopes[d.scope as usize],
                "card_id": &*d.card_id,
                "card_aliases": [],
                "revision": d.revision,
                "title": d.fields.title,
                "summary": d.fields.summary,
                "tags": d.fields.tags,
                "score": score,
                "content_hash": &*d.content_hash,
                "trust": &*d.trust,
                "ranks": {"lexical": rank + 1},
            })
        })
        .collect();
    let stale = snap.stale || request.min_seq.is_some_and(|m| snap.indexed_seq < m);
    Ok(Reply::new(
        200,
        json!({
            "query": request.query,
            "matches": matches,
            "match_count": total,
            "retrieval": {"mode": "lexical", "degraded": false, "stale": stale},
            "indexed_seq": snap.indexed_seq,
        }),
    ))
}
