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
/// A guard on the query as sent, in UTF-8 bytes, before any normalization.
/// It only bounds the work normalization does; the contract's limit is
/// [`QUERY_CHARS`] after normalization.
pub const RAW_QUERY_BYTES: usize = 4096;
/// A literal query (no word characters) reads at most this many cards and
/// this much card text per request, so its cost does not grow with the
/// corpus. A scan that stops before the last visible card answers
/// `retrieval.truncated: true`.
pub const LITERAL_DOCS: usize = 1000;
pub const LITERAL_BYTES: usize = 4 << 20;
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
    pub prompt: bool,
    pub push: bool,
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
    if raw.len() > RAW_QUERY_BYTES {
        return Err(invalid(&format!(
            "query must be at most {RAW_QUERY_BYTES} bytes as sent, before normalization"
        )));
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
        prompt: matches!(
            map.get("query_source").and_then(Json::as_str),
            Some("prompt")
        ),
        push: false,
    })
}

/// Literal hits, and whether the scan covered every visible card.
pub struct Literal {
    pub hits: Vec<(u32, f64)>,
    pub complete: bool,
}
/// `memory_bm25.rank` for a query with no word characters: the literal
/// substring match on the lowered field, every match scoring 0.0. `body`
/// reads a document's card text from the store.
///
/// The scan goes in answer order (`cards/<card_id>.md`, then scope) and
/// stops at `limit` hits, after [`LITERAL_DOCS`] cards or after
/// [`LITERAL_BYTES`] of card text, whichever comes first. Every hit scores
/// 0.0, so the hits it returns are exactly the first `limit` of a full scan,
/// whatever the document numbers are. When it stops early, `complete` is
/// false and the hit count is only a lower bound on a full scan's.
pub fn literal(
    snap: &Snapshot,
    allowed: &[bool],
    query: &str,
    limit: usize,
    mut body: impl FnMut(&str) -> rusqlite::Result<Option<String>>,
) -> rusqlite::Result<Literal> {
    let lowered = text::py_lower(query);
    let needles = text::py_split(&lowered);
    let mut out = Literal {
        hits: Vec::new(),
        complete: true,
    };
    if needles.is_empty() {
        return Ok(out);
    }
    let (order, visible) = snap.in_answer_order(allowed, LITERAL_DOCS);
    // The card and byte budgets bound the cost. Stopping at `limit` hits only pays off when the
    // budget would cut the scan anyway; below it, read every visible card so `match_count` stays
    // exact (as Python's) and `truncated` means only that the budget stopped the scan.
    let early = visible > LITERAL_DOCS;
    let (mut docs, mut bytes) = (0, 0);
    for n in order {
        if (early && out.hits.len() >= limit) || bytes >= LITERAL_BYTES {
            break;
        }
        docs += 1;
        let doc = snap.doc(n);
        let Some(body) = body(&doc.content_hash)? else {
            continue;
        };
        bytes += body.len();
        let field = text::py_lower(&text::card_text(&doc.fields, &body));
        if needles.iter().any(|needle| field.contains(needle)) {
            out.hits.push((n, 0.0));
        }
    }
    out.complete = docs == visible;
    Ok(out)
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
    scanner: &dyn super::scan::Scanner,
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
    let (hits, complete) = if request.tokens.is_empty() {
        let found = literal(snap, &allowed, &request.query, request.limit, |hash| {
            blob(tx, hash)
        })?;
        (found.hits, found.complete)
    } else {
        (snap.score(&allowed, &request.tokens), true)
    };
    // Push revalidates the exact snapshot head and its current scan receipt.
    // Unavailable, pending, private, flagged, retired and stale versions never
    // leave here: only a clean receipt may reach a connector.
    let hits = if request.push {
        let mut eligible = Vec::new();
        let (available, ruleset) = scanner.describe();
        if available {
            if let Some(ruleset) = ruleset {
                for (n, score) in hits {
                    let d = snap.doc(n);
                    let rows = tx.query(
                        "SELECT 1 FROM memory_cards c JOIN memory_card_revisions r ON r.scope=c.scope AND r.card_id=c.card_id AND r.revision=c.revision JOIN memory_scans s ON s.content_hash=c.content_hash WHERE c.scope=?1 AND c.card_id=?2 AND c.revision=?3 AND c.content_hash=?4 AND c.state='live' AND c.trust IN ('imported','reviewed','policy-promoted') AND r.scan_state='clean' AND s.ruleset_version=?5 AND s.state='clean'",
                        &[Value::Text(scopes[d.scope as usize].to_string()), Value::Text(d.card_id.to_string()), Value::Integer(d.revision), Value::Text(d.content_hash.to_string()), Value::Text(ruleset.clone())],
                    )?;
                    if !rows.is_empty() {
                        eligible.push((n, score));
                    }
                }
            }
        }
        eligible
    } else {
        hits
    };
    let total = hits.len();
    let matches: Vec<Json> = snap
        .top(hits, TOP)
        .into_iter()
        .take(request.limit)
        .enumerate()
        .map(|(rank, (n, score))| {
            let d = snap.doc(n);
            let hit = json!({
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
            });
            if request.prompt {
                let Some(body) = blob(tx, &d.content_hash)? else {
                    return Ok(
                        json!({"card_id": &*d.card_id, "blocked": "memory_scan_unavailable"}),
                    );
                };
                Ok(super::scan::prompt_hit(
                    scanner,
                    &d.card_id,
                    hit,
                    &[
                        &d.fields.title,
                        &d.fields.summary,
                        &d.fields.tags.join("\n"),
                        &body,
                    ],
                    // A push hit goes to a connector's remote model. A local
                    // prompt load may carry private text.
                    request.push,
                ))
            } else {
                Ok(hit)
            }
        })
        .collect::<rusqlite::Result<_>>()?;
    let stale = snap.stale || request.min_seq.is_some_and(|m| snap.indexed_seq < m);
    let mut retrieval = json!({"mode": "lexical", "degraded": false, "stale": stale});
    if !complete {
        retrieval["truncated"] = json!(true);
    }
    Ok(Reply::new(
        200,
        json!({
            "content_trust": "untrusted",
            "query": request.query,
            "matches": matches,
            "match_count": total,
            "retrieval": retrieval,
            "indexed_seq": snap.indexed_seq,
        }),
    ))
}
