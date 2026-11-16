//! Opaque workspace evidence heads, appended under the memory writer's lock.
use super::{Reply, auth, ids, invalid, ops::Prepared};
use crate::writer::{ReadTx, WriteTx};
use rusqlite::types::Value;
use serde_json::{Map, Value as Json, json};

pub(super) const DDL: &str = "
CREATE TABLE IF NOT EXISTS memory_evidence_chain(
  principal TEXT NOT NULL,
  workspace_id TEXT NOT NULL CHECK(length(workspace_id)=64 AND workspace_id NOT GLOB '*[^0-9a-f]*'),
  stream TEXT NOT NULL CHECK(stream IN ('card_reads','lesson_observations')),
  seq INTEGER NOT NULL CHECK(seq>0),
  digest TEXT NOT NULL CHECK(length(digest)=64 AND digest NOT GLOB '*[^0-9a-f]*'),
  prev_digest TEXT CHECK(prev_digest IS NULL OR (length(prev_digest)=64 AND prev_digest NOT GLOB '*[^0-9a-f]*')),
  kind TEXT NOT NULL CHECK(kind IN ('append','reset')),
  created_at TEXT NOT NULL,
  PRIMARY KEY(principal,workspace_id,stream,seq)
) WITHOUT ROWID;
CREATE TRIGGER IF NOT EXISTS memory_evidence_chain_no_update BEFORE UPDATE ON memory_evidence_chain
  BEGIN SELECT RAISE(ABORT, 'memory_evidence_chain is append-only'); END;
CREATE TRIGGER IF NOT EXISTS memory_evidence_chain_no_delete BEFORE DELETE ON memory_evidence_chain
  BEGIN SELECT RAISE(ABORT, 'memory_evidence_chain is append-only'); END;
";

#[derive(Clone, Debug)]
pub(super) struct Chain {
    pub scope: String,
    workspace: String,
    stream: String,
}
#[derive(Clone, Debug)]
pub struct Append {
    pub(super) chain: Chain,
    seq: i64,
    digest: String,
    prev: Option<String>,
    kind: String,
}
fn hex(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn string<'a>(map: &'a Map<String, Json>, key: &str) -> &'a str {
    map.get(key).and_then(Json::as_str).unwrap_or("")
}
fn parse(map: &Map<String, Json>, append: bool) -> Result<Chain, Reply> {
    let allowed = ["action", "operation", "scope", "workspace_id", "stream"];
    let extra = ["seq", "digest", "prev_digest", "kind", "client_event_id"];
    if map
        .keys()
        .any(|k| !(allowed.contains(&k.as_str()) || append && extra.contains(&k.as_str())))
    {
        return Err(invalid("unknown evidence_chain field"));
    }
    if string(map, "operation") != if append { "append" } else { "latest" } {
        return Err(invalid("operation must be append or latest"));
    }
    let scope = string(map, "scope");
    let workspace = string(map, "workspace_id");
    let stream = string(map, "stream");
    if !scope
        .strip_prefix("principal:")
        .is_some_and(ids::valid_principal)
        || !hex(workspace)
        || !matches!(stream, "card_reads" | "lesson_observations")
    {
        return Err(invalid(
            "evidence_chain requires a principal scope, a 64-character lowercase hex workspace_id and a known stream",
        ));
    }
    Ok(Chain {
        scope: scope.into(),
        workspace: workspace.into(),
        stream: stream.into(),
    })
}
pub(super) fn prepare(map: &Map<String, Json>) -> Result<Append, Reply> {
    let chain = parse(map, true)?;
    let seq = map
        .get("seq")
        .and_then(Json::as_i64)
        .filter(|n| *n > 0)
        .ok_or_else(|| invalid("seq must be a positive integer"))?;
    let digest = string(map, "digest");
    let kind = string(map, "kind");
    let prev = match map.get("prev_digest") {
        Some(Json::Null) => None,
        Some(Json::String(s)) if hex(s) => Some(s.clone()),
        _ => return Err(invalid("prev_digest must be lowercase hex or null")),
    };
    if !hex(digest) || !matches!(kind, "append" | "reset") {
        return Err(invalid(
            "digest must be lowercase hex and kind must be append or reset",
        ));
    }
    Ok(Append {
        chain,
        seq,
        digest: digest.into(),
        prev,
        kind: kind.into(),
    })
}
fn authorized<T: ReadTx + ?Sized>(
    tx: &T,
    who: &auth::Credential,
    chain: &Chain,
) -> rusqlite::Result<bool> {
    Ok(
        chain.scope.strip_prefix("principal:") == Some(who.principal())
            && auth::has_exact_rights(tx, who, &chain.scope, &["evidence_chain"])?,
    )
}
fn denied() -> Reply {
    super::error(
        403,
        "memory_grant_missing",
        "evidence_chain requires its owner's exact principal grant",
    )
}
fn head<T: ReadTx + ?Sized>(
    tx: &T,
    who: &auth::Credential,
    chain: &Chain,
) -> rusqlite::Result<Json> {
    let rows = tx.query(
        "SELECT seq,digest,prev_digest,kind,created_at FROM memory_evidence_chain
         WHERE principal=?1 AND workspace_id=?2 AND stream=?3 ORDER BY seq DESC LIMIT 1",
        &[
            Value::Text(who.principal().into()),
            Value::Text(chain.workspace.clone()),
            Value::Text(chain.stream.clone()),
        ],
    )?;
    Ok(rows.first().map_or(Json::Null, |r| json!({
        "seq": match r[0] { Value::Integer(n) => n, _ => 0 }, "digest": super::ops::s(&r[1]),
        "prev_digest": if r[2] == Value::Null { Json::Null } else { json!(super::ops::s(&r[2])) },
        "kind": super::ops::s(&r[3]), "created_at": super::ops::s(&r[4])
    })))
}
pub(super) fn latest(
    tx: &dyn ReadTx,
    who: &auth::Credential,
    map: &Map<String, Json>,
) -> rusqlite::Result<Reply> {
    if let Some(r) = super::verifier::check(tx, who)? {
        return Ok(r);
    }
    let chain = match parse(map, false) {
        Ok(c) => c,
        Err(r) => return Ok(r),
    };
    if !authorized(tx, who, &chain)? {
        return Ok(denied());
    }
    Ok(Reply::new(200, json!({"head": head(tx, who, &chain)?})))
}
pub(super) fn append(tx: &dyn WriteTx, p: &Prepared, a: &Append) -> rusqlite::Result<Reply> {
    if !authorized(tx, &p.credential, &a.chain)? {
        return Ok(denied());
    }
    let current = head(tx, &p.credential, &a.chain)?;
    let seq = current["seq"].as_i64().unwrap_or(0);
    if seq.checked_add(1) != Some(a.seq) || current["digest"].as_str() != a.prev.as_deref() {
        return Ok(super::error(
            409,
            "memory_evidence_chain_conflict",
            "evidence chain head does not match",
        ));
    }
    let created_at = ids::isoformat(p.now_ms);
    tx.execute(
        "INSERT INTO memory_evidence_chain(principal,workspace_id,stream,seq,digest,prev_digest,kind,created_at)
         VALUES(?1,?2,?3,?4,?5,?6,?7,?8)",
        &[Value::Text(p.credential.principal().into()), Value::Text(a.chain.workspace.clone()),
          Value::Text(a.chain.stream.clone()), Value::Integer(a.seq), Value::Text(a.digest.clone()),
          a.prev.clone().map_or(Value::Null, Value::Text), Value::Text(a.kind.clone()), Value::Text(created_at)],
    )?;
    Ok(Reply::new(
        201,
        json!({"head": head(tx, &p.credential, &a.chain)?}),
    ))
}
