//! Derived vectors, leased jobs and exact live-head lineage. All mutations use the memory writer.
use super::{
    Reply, auth,
    embedder::{Config, normalize_vector},
    ids,
    ops::{Prepared, error},
    scan::{Scan, Scanner},
};
use crate::writer::{ReadTx, WriteTx};
use rusqlite::types::Value;
use serde_json::{Value as Json, json};

pub const DDL: &str = "
CREATE TABLE memory_vectors(content_hash TEXT NOT NULL,embedder_id TEXT NOT NULL,normalizer_version INTEGER NOT NULL,vector BLOB NOT NULL,PRIMARY KEY(content_hash,embedder_id,normalizer_version)) WITHOUT ROWID;
CREATE INDEX memory_vectors_embedder ON memory_vectors(embedder_id,normalizer_version,content_hash);
CREATE TABLE memory_vector_log(seq INTEGER PRIMARY KEY AUTOINCREMENT,embedder_id TEXT NOT NULL,content_hash TEXT NOT NULL,present INTEGER NOT NULL);
CREATE TRIGGER memory_vectors_added AFTER INSERT ON memory_vectors WHEN new.normalizer_version=1 BEGIN INSERT INTO memory_vector_log(embedder_id,content_hash,present) VALUES(new.embedder_id,new.content_hash,1); END;
CREATE TRIGGER memory_vectors_removed AFTER DELETE ON memory_vectors WHEN old.normalizer_version=1 BEGIN INSERT INTO memory_vector_log(embedder_id,content_hash,present) VALUES(old.embedder_id,old.content_hash,0); END;
CREATE TABLE memory_lineage(scope TEXT NOT NULL,card_id TEXT NOT NULL,revision INTEGER NOT NULL,artifact TEXT NOT NULL,artifact_key TEXT NOT NULL,content_hash TEXT NOT NULL,PRIMARY KEY(scope,card_id,revision,artifact,artifact_key)) WITHOUT ROWID;
CREATE TABLE memory_embed_jobs(content_hash TEXT NOT NULL,embedder_id TEXT NOT NULL,state TEXT NOT NULL,lease_id TEXT,holder TEXT,expires_ms INTEGER,attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,finished_ms INTEGER,not_before INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(content_hash,embedder_id)) WITHOUT ROWID;
CREATE INDEX memory_embed_jobs_queue ON memory_embed_jobs(embedder_id,state,attempts,content_hash);
CREATE INDEX memory_cards_hash ON memory_cards(content_hash);
CREATE INDEX memory_lineage_hash ON memory_lineage(content_hash);
CREATE INDEX memory_lineage_artifact ON memory_lineage(artifact,artifact_key);
";
/// A document that fails this many times alone is parked as `failed`.
pub const MAX_ATTEMPTS: i64 = 5;
/// Backoff after a document fails alone: 10 s, doubled per attempt, capped at 1 h.
pub const BACKOFF_MS: i64 = 10_000;
pub const LEASE_MS: i64 = 60_000;
/// Rows each `--drop` writer turn deletes from each derived table.
pub const DROP_BATCH: i64 = 512;
// The hub's own document request must finish inside the lease it holds.
const _: () = assert!((super::embedder::DOCUMENT_DEADLINE.as_millis() as i64) < LEASE_MS - 5_000);
/// Failures of the endpoint or the hub, not of one document. They release
/// the job without counting an attempt.
const TRANSIENT: &[&str] = &[
    "embedder-busy",
    "embedder-unavailable",
    "embedder-timeout",
    "embedder-refused",
    "endpoint-refused",
    "scan-unavailable",
    "scan-pending",
    "digest-mismatch",
];
#[derive(Clone, Debug)]
pub enum Command {
    Reindex {
        id: String,
        drop: bool,
        /// The operator's `memory reindex`. It also requeues parked jobs.
        explicit: bool,
    },
    Lease {
        max: usize,
    },
    Submit {
        lease: String,
        vectors: Vec<(String, Vec<u8>)>,
        digest: String,
    },
    Failed {
        lease: String,
        /// `(content_hash, reason, alone)` for each document the holder gives back.
        /// `alone` says the holder already embedded that document by itself.
        failures: Vec<(String, String, bool)>,
    },
}
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Coverage {
    /// Live cards whose text has a vector for the active embedder.
    pub covered: usize,
    /// Live cards that can be embedded: a vector, or clean and not parked.
    pub eligible: usize,
    pub total: usize,
}
impl Coverage {
    pub fn complete(&self) -> bool {
        self.covered >= self.eligible
    }
    pub fn json(&self) -> Json {
        json!({"cards": self.covered, "eligible": self.eligible, "unembeddable": self.total - self.eligible, "total": self.total})
    }
}
fn t(s: &str) -> Value {
    Value::Text(s.into())
}
fn s(v: &Value) -> String {
    if let Value::Text(s) = v {
        s.clone()
    } else {
        String::new()
    }
}
fn n(v: &Value) -> i64 {
    if let Value::Integer(n) = v { *n } else { 0 }
}
fn hex64(h: &str) -> bool {
    h.len() == 64 && h.bytes().all(|c| matches!(c, b'0'..=b'9' | b'a'..=b'f'))
}
fn invalid() -> Reply {
    error(422, "memory_vector_invalid", "invalid embedding command")
}
/// `--drop` names exactly one embedder id: 64 lowercase hex characters.
pub fn parse_drop(raw: &Json) -> Result<Option<String>, Reply> {
    match raw.get("drop") {
        None => Ok(None),
        Some(Json::String(id)) if hex64(id) => Ok(Some(id.clone())),
        Some(_) => Err(error(
            422,
            "memory_vector_invalid",
            "drop needs a 64-character lowercase hex embedder id",
        )),
    }
}

pub fn parse(action: &str, raw: &Json, config: &Config) -> Result<Command, Reply> {
    let id = config.id.key();
    match action {
        "reindex" => Ok(Command::Reindex {
            id,
            drop: false,
            explicit: true,
        }),
        "embed_lease" if raw["embedder_id"].as_str() == Some(&id) => Ok(Command::Lease {
            max: raw["max"].as_u64().unwrap_or(4).clamp(1, 4) as usize,
        }),
        "embed_failed" => {
            let lease = raw["lease_id"].as_str().ok_or_else(invalid)?.to_string();
            let rows = raw["failures"]
                .as_array()
                .filter(|v| !v.is_empty() && v.len() <= 4)
                .ok_or_else(invalid)?;
            let mut failures = Vec::new();
            for row in rows {
                let hash = row["content_hash"]
                    .as_str()
                    .filter(|h| hex64(h))
                    .ok_or_else(invalid)?;
                let reason = row["reason"]
                    .as_str()
                    .filter(|r| {
                        !r.is_empty()
                            && r.len() <= 40
                            && r.bytes().all(|c| c.is_ascii_lowercase() || c == b'-')
                    })
                    .ok_or_else(invalid)?;
                if failures.iter().any(|(h, _, _)| h == hash) {
                    return Err(invalid());
                }
                let alone = match &row["alone"] {
                    Json::Null => false,
                    Json::Bool(b) => *b,
                    _ => return Err(invalid()),
                };
                failures.push((hash.to_string(), reason.to_string(), alone));
            }
            Ok(Command::Failed { lease, failures })
        }
        "embed_submit" => {
            use base64::Engine;
            let lease = raw["lease_id"].as_str().ok_or_else(invalid)?.to_string();
            let digest = raw["model_digest"]
                .as_str()
                .ok_or_else(invalid)?
                .to_string();
            if digest != config.id.model_digest {
                return Err(error(422, "memory_vector_invalid", "digest-mismatch"));
            }
            let rows = raw["vectors"]
                .as_array()
                .filter(|v| !v.is_empty() && v.len() <= 4)
                .ok_or_else(invalid)?;
            let mut vectors = Vec::new();
            for row in rows {
                let hash = row["content_hash"]
                    .as_str()
                    .filter(|h| hex64(h))
                    .ok_or_else(invalid)?;
                let text = row["vector"]
                    .as_str()
                    .filter(|s| s.len() <= config.id.dimensions * 8)
                    .ok_or_else(invalid)?;
                let bytes = base64::engine::general_purpose::STANDARD
                    .decode(text)
                    .map_err(|_| invalid())?;
                if bytes.len() != config.id.dimensions * 4 || vectors.iter().any(|(h, _)| h == hash)
                {
                    return Err(invalid());
                }
                let floats = bytes
                    .chunks_exact(4)
                    .map(|b| f32::from_le_bytes(b.try_into().unwrap()))
                    .collect();
                let vector =
                    normalize_vector(floats, config.id.dimensions).map_err(|_| invalid())?;
                vectors.push((
                    hash.into(),
                    vector.iter().flat_map(|f| f.to_le_bytes()).collect(),
                ));
            }
            Ok(Command::Submit {
                lease,
                vectors,
                digest,
            })
        }
        _ => Err(invalid()),
    }
}
pub fn cleanup(tx: &dyn WriteTx) -> rusqlite::Result<()> {
    tx.execute("DELETE FROM memory_lineage WHERE NOT EXISTS(SELECT 1 FROM memory_cards c WHERE c.scope=memory_lineage.scope AND c.card_id=memory_lineage.card_id AND c.revision=memory_lineage.revision AND c.content_hash=memory_lineage.content_hash AND c.state='live')", &[])?;
    // A vector's last reference is global, not scoped to the card being retired.
    tx.execute("DELETE FROM memory_vectors WHERE NOT EXISTS(SELECT 1 FROM memory_cards c WHERE c.content_hash=memory_vectors.content_hash AND c.state='live')", &[])?;
    Ok(())
}
pub fn cleanup_card(tx: &dyn WriteTx, scope: &str, card: &str) -> rusqlite::Result<()> {
    let hashes = tx.query(
        "SELECT DISTINCT content_hash FROM memory_lineage WHERE scope=?1 AND card_id=?2",
        &[t(scope), t(card)],
    )?;
    tx.execute("DELETE FROM memory_lineage WHERE scope=?1 AND card_id=?2 AND NOT EXISTS(SELECT 1 FROM memory_cards c WHERE c.scope=?1 AND c.card_id=?2 AND c.state='live' AND c.revision=memory_lineage.revision)", &[t(scope),t(card)])?;
    for row in hashes {
        tx.execute("DELETE FROM memory_vectors WHERE content_hash=?1 AND NOT EXISTS(SELECT 1 FROM memory_cards WHERE content_hash=?1 AND state='live')", &[row[0].clone()])?;
    }
    // Reusing a vector on an identical-text revision needs a new lineage row, not a new embedding.
    tx.execute("INSERT OR IGNORE INTO memory_lineage SELECT c.scope,c.card_id,c.revision,'vector',v.embedder_id,c.content_hash FROM memory_cards c JOIN memory_vectors v ON v.content_hash=c.content_hash WHERE c.scope=?1 AND c.card_id=?2 AND c.state='live'", &[t(scope),t(card)])?;
    Ok(())
}
/// Queue the card's live head for the active embedder inside the same
/// transaction as its `put` or `retire`, so the scheduler never scans the
/// corpus to find new work. Only text whose stored scan receipt is `clean`
/// is queued. Jobs left for text no live card holds any more are removed.
pub fn queue_card(tx: &dyn WriteTx, scope: &str, card: &str, id: &str) -> rusqlite::Result<()> {
    tx.execute("DELETE FROM memory_embed_jobs WHERE state IN ('pending','failed') AND content_hash IN (SELECT content_hash FROM memory_card_revisions WHERE scope=?1 AND card_id=?2) AND NOT EXISTS(SELECT 1 FROM memory_cards c WHERE c.content_hash=memory_embed_jobs.content_hash AND c.state='live')", &[t(scope), t(card)])?;
    tx.execute("INSERT INTO memory_embed_jobs(content_hash,embedder_id,state) SELECT c.content_hash,?3,'pending' FROM memory_cards c JOIN memory_card_revisions r ON r.scope=c.scope AND r.card_id=c.card_id AND r.revision=c.revision WHERE c.scope=?1 AND c.card_id=?2 AND c.state='live' AND r.scan_state='clean' AND NOT EXISTS(SELECT 1 FROM memory_vectors v WHERE v.content_hash=c.content_hash AND v.embedder_id=?3 AND v.normalizer_version=1) ON CONFLICT(content_hash,embedder_id) DO UPDATE SET state='pending',finished_ms=NULL WHERE state='done'", &[t(scope), t(card), t(id)])?;
    Ok(())
}
/// Normalizer 1 is content-addressed. Card aliases and frontmatter unrelated to
/// title/summary cannot change the payload for identical content bytes.
pub fn document_text(body: &str, input_chars: usize) -> String {
    let fields = super::text::fields(body, "");
    let without_frontmatter = if let Some(after) = body.strip_prefix("---\n") {
        after
            .find("\n---\n")
            .map(|n| &after[n + 5..])
            .unwrap_or(body)
    } else {
        body
    };
    let text = format!(
        "{}\n{}\n{}",
        fields.title, fields.summary, without_frontmatter
    );
    let mut bytes = 0;
    text.chars()
        .take(input_chars)
        .take_while(|c| {
            bytes += c.len_utf8();
            bytes <= 32 * 1024
        })
        .collect()
}
/// Delete at most [`DROP_BATCH`] rows from each table for one embedder id.
fn drop_turn(tx: &dyn WriteTx, id: &str) -> rusqlite::Result<Reply> {
    let limit = Value::Integer(DROP_BATCH);
    let vectors = tx.execute("DELETE FROM memory_vectors WHERE (content_hash,embedder_id,normalizer_version) IN (SELECT content_hash,embedder_id,normalizer_version FROM memory_vectors WHERE embedder_id=?1 LIMIT ?2)", &[t(id), limit.clone()])?;
    let jobs = tx.execute("DELETE FROM memory_embed_jobs WHERE embedder_id=?1 AND content_hash IN (SELECT content_hash FROM memory_embed_jobs WHERE embedder_id=?1 LIMIT ?2)", &[t(id), limit.clone()])?;
    let lineage = tx.execute("DELETE FROM memory_lineage WHERE (scope,card_id,revision,artifact,artifact_key) IN (SELECT scope,card_id,revision,artifact,artifact_key FROM memory_lineage WHERE artifact='vector' AND artifact_key=?1 LIMIT ?2)", &[t(id), limit])?;
    Ok(Reply::new(
        200,
        json!({"dropped": id, "vectors": vectors, "jobs": jobs, "lineage": lineage}),
    ))
}
/// Scopes on which this credential may embed, or None for every scope.
fn embed_scopes(tx: &dyn WriteTx, p: &Prepared) -> rusqlite::Result<Option<String>> {
    if p.credential == super::Credential::Admin {
        return Ok(None);
    }
    let mut allowed = Vec::new();
    for row in tx.query(
        "SELECT DISTINCT scope FROM memory_cards WHERE state='live'",
        &[],
    )? {
        let scope = s(&row[0]);
        if auth::has_right(tx, &p.credential, Some(&scope), "embed")? {
            allowed.push(scope);
        }
    }
    Ok(Some(Json::from(allowed).to_string()))
}
pub fn apply(
    tx: &dyn WriteTx,
    p: &Prepared,
    command: &Command,
    config: Option<&Config>,
    scanner: &dyn Scanner,
) -> rusqlite::Result<Reply> {
    if let Command::Reindex { id, drop: true, .. } = command {
        if config.is_some_and(|c| c.id.key() == *id) {
            return Ok(error(
                409,
                "memory_embedder_active",
                "this embedder is active. Set memory.retrieval to lexical and restart the hub before dropping it",
            ));
        }
        return drop_turn(tx, id);
    }
    let Some(config) = config else {
        return Ok(error(
            503,
            "memory_action_unavailable",
            "no embedder configured",
        ));
    };
    let id = config.id.key();
    match command {
        Command::Reindex { explicit, .. } => {
            cleanup(tx)?;
            // Only the operator's command gives parked documents another chance.
            let requeued = if *explicit {
                tx.execute("UPDATE memory_embed_jobs SET state='pending',attempts=0,not_before=0,lease_id=NULL,holder=NULL,expires_ms=NULL WHERE embedder_id=?1 AND state='failed'", &[t(&id)])?
            } else {
                0
            };
            let resumed = tx.execute("UPDATE memory_embed_jobs SET state='pending',finished_ms=NULL WHERE embedder_id=?1 AND state='done' AND NOT EXISTS(SELECT 1 FROM memory_vectors v WHERE v.content_hash=memory_embed_jobs.content_hash AND v.embedder_id=?1) AND EXISTS(SELECT 1 FROM memory_cards c WHERE c.content_hash=memory_embed_jobs.content_hash AND c.state='live')", &[t(&id)])?;
            // Queue in bounded pages. Existing jobs and vectors make repeats idempotent.
            // Flagged text never leaves the host, so only clean receipts are queued.
            let rows = tx.query("SELECT DISTINCT c.content_hash FROM memory_cards c JOIN memory_card_revisions r ON r.scope=c.scope AND r.card_id=c.card_id AND r.revision=c.revision WHERE c.state='live' AND r.scan_state='clean' AND NOT EXISTS(SELECT 1 FROM memory_vectors v WHERE v.content_hash=c.content_hash AND v.embedder_id=?1 AND v.normalizer_version=1) AND NOT EXISTS(SELECT 1 FROM memory_embed_jobs j WHERE j.content_hash=c.content_hash AND j.embedder_id=?1) LIMIT 256", &[t(&id)])?;
            for r in &rows {
                tx.execute("INSERT INTO memory_embed_jobs(content_hash,embedder_id,state) VALUES(?1,?2,'pending')", &[r[0].clone(), t(&id)])?;
            }
            tx.execute("INSERT OR IGNORE INTO memory_lineage SELECT c.scope,c.card_id,c.revision,'vector',v.embedder_id,c.content_hash FROM memory_cards c JOIN memory_vectors v ON v.content_hash=c.content_hash WHERE c.state='live' AND v.embedder_id=?1", &[t(&id)])?;
            Ok(Reply::new(
                200,
                json!({"queued": rows.len() + resumed + requeued, "embedder_id": id}),
            ))
        }
        Command::Lease { max } => {
            if !scanner.describe().0 {
                return Ok(error(503, "memory_scan_unavailable", "scan unavailable"));
            }
            // Distinct eligible hashes are chosen in SQL, after the scope's
            // embed right and the backoff, before the limit applies.
            let scopes = embed_scopes(tx, p)?;
            let scopes = scopes.map_or(Value::Null, Value::Text);
            let rows = tx.query("SELECT j.content_hash,j.attempts,(SELECT b.bytes FROM memory_blobs b WHERE b.content_hash=j.content_hash) FROM memory_embed_jobs j WHERE j.embedder_id=?1 AND (j.state='pending' OR (j.state='leased' AND j.expires_ms<=?2)) AND j.not_before<=?2 AND EXISTS(SELECT 1 FROM memory_cards c JOIN memory_card_revisions r ON r.scope=c.scope AND r.card_id=c.card_id AND r.revision=c.revision WHERE c.content_hash=j.content_hash AND c.state='live' AND r.scan_state='clean' AND (?3 IS NULL OR c.scope IN (SELECT value FROM json_each(?3)))) ORDER BY j.attempts>0,j.content_hash LIMIT ?4", &[t(&id), Value::Integer(p.now_ms), scopes, Value::Integer(4 * *max as i64)])?;
            let lease = ids::mint(p.now_ms);
            let mut jobs = Vec::new();
            let mut bytes = 0;
            let mut parked_now = 0;
            for row in rows {
                // A document that has failed before is retried alone, so it
                // cannot hold back the documents batched with it.
                let retry = n(&row[1]) > 0;
                if jobs.len() == *max || retry && !jobs.is_empty() {
                    break;
                }
                let hash = s(&row[0]);
                let body = match &row[2] {
                    Value::Blob(b) => String::from_utf8_lossy(b).into_owned(),
                    other => s(other),
                };
                let text = document_text(&body, config.input_chars);
                let parked = match scanner.scan(&text) {
                    Scan::Clean { .. } => None,
                    Scan::Flagged { .. } => Some("content-flagged"),
                    Scan::Blocked { .. } => Some("content-blocked"),
                    Scan::Unavailable | Scan::Failed => continue,
                };
                if let Some(reason) = parked {
                    // Counted as unembeddable. `memory reindex` requeues it.
                    tx.execute("UPDATE memory_embed_jobs SET state='failed',last_error=?3,finished_ms=?4 WHERE content_hash=?1 AND embedder_id=?2", &[t(&hash), t(&id), t(reason), Value::Integer(p.now_ms)])?;
                    parked_now += 1;
                    continue;
                }
                if bytes + text.len() > 32 * 1024 {
                    break;
                }
                bytes += text.len();
                tx.execute("UPDATE memory_embed_jobs SET state='leased',lease_id=?3,holder=?4,expires_ms=?5 WHERE content_hash=?1 AND embedder_id=?2", &[t(&hash),t(&id),t(&lease),t(p.credential.principal()),Value::Integer(p.now_ms+LEASE_MS)])?;
                jobs.push(json!({"content_hash": hash, "text": text, "scan_state": "clean"}));
                if retry {
                    break;
                }
            }
            Ok(Reply::new(
                200,
                json!({"lease_id": lease, "embedder_id": id, "model_digest": config.id.model_digest, "dimensions": config.id.dimensions, "jobs": jobs, "parked": parked_now, "expires_ms": p.now_ms+LEASE_MS}),
            ))
        }
        Command::Submit {
            lease,
            vectors,
            digest,
        } => {
            if digest != &config.id.model_digest {
                return Ok(error(422, "memory_vector_invalid", "digest-mismatch"));
            }
            for (hash, vector) in vectors {
                let jobs = tx.query("SELECT 1 FROM memory_embed_jobs WHERE content_hash=?1 AND embedder_id=?2 AND lease_id=?3 AND holder=?4 AND state='leased' AND expires_ms>?5", &[t(hash),t(&id),t(lease),t(p.credential.principal()),Value::Integer(p.now_ms)])?;
                if jobs.is_empty() {
                    return Ok(error(
                        409,
                        "memory_lease_expired",
                        "embedding lease expired or not owned",
                    ));
                }
                tx.execute(
                    "INSERT OR IGNORE INTO memory_vectors VALUES(?1,?2,1,?3)",
                    &[t(hash), t(&id), Value::Blob(vector.clone())],
                )?;
                tx.execute("INSERT OR IGNORE INTO memory_lineage SELECT scope,card_id,revision,'vector',?2,content_hash FROM memory_cards WHERE content_hash=?1 AND state='live'", &[t(hash),t(&id)])?;
                tx.execute("UPDATE memory_embed_jobs SET state='done',last_error=NULL,finished_ms=?3 WHERE content_hash=?1 AND embedder_id=?2", &[t(hash),t(&id),Value::Integer(p.now_ms)])?;
            }
            // A vector for text retired while it was leased has no reference.
            for (hash, _) in vectors {
                tx.execute("DELETE FROM memory_vectors WHERE content_hash=?1 AND embedder_id=?2 AND NOT EXISTS(SELECT 1 FROM memory_cards WHERE content_hash=?1 AND state='live')", &[t(hash), t(&id)])?;
            }
            Ok(Reply::new(200, json!({"stored": vectors.len()})))
        }
        Command::Failed { lease, failures } => {
            let owner = [t(lease), t(p.credential.principal()), t(&id)];
            let mut released = 0;
            let mut parked = 0;
            for (hash, reason, alone) in failures {
                let row = tx.query("SELECT attempts FROM memory_embed_jobs WHERE content_hash=?4 AND lease_id=?1 AND holder=?2 AND embedder_id=?3 AND state='leased'", &[owner[0].clone(), owner[1].clone(), owner[2].clone(), t(hash)])?;
                let Some(row) = row.first() else {
                    continue;
                };
                let key = [t(hash), t(&id), t(reason), Value::Integer(p.now_ms)];
                if TRANSIENT.contains(&reason.as_str()) {
                    tx.execute("UPDATE memory_embed_jobs SET state='pending',lease_id=NULL,holder=NULL,expires_ms=NULL,last_error=?3,not_before=?4+1000 WHERE content_hash=?1 AND embedder_id=?2", &key)?;
                    released += 1;
                    continue;
                }
                let attempts = n(&row[0]) + 1;
                if attempts >= MAX_ATTEMPTS {
                    tx.execute("UPDATE memory_embed_jobs SET state='failed',attempts=attempts+1,lease_id=NULL,holder=NULL,expires_ms=NULL,last_error=?3,finished_ms=?4 WHERE content_hash=?1 AND embedder_id=?2", &key)?;
                    parked += 1;
                    continue;
                }
                // A batch failure only proves one of its documents bad: each is
                // retried alone at once. A row the holder reports as already
                // tried alone backs off, and so does the only row of a report.
                // The decision reads the report, not how many jobs are still leased.
                let wait = if *alone || failures.len() == 1 {
                    (BACKOFF_MS << (attempts - 1)).min(3_600_000)
                } else {
                    0
                };
                tx.execute("UPDATE memory_embed_jobs SET state='pending',attempts=attempts+1,lease_id=NULL,holder=NULL,expires_ms=NULL,last_error=?3,not_before=?4+?5 WHERE content_hash=?1 AND embedder_id=?2", &[key[0].clone(), key[1].clone(), key[2].clone(), key[3].clone(), Value::Integer(wait)])?;
                released += 1;
            }
            Ok(Reply::new(
                200,
                json!({"released": released, "parked": parked}),
            ))
        }
    }
}
pub fn coverage(tx: &dyn ReadTx, id: &str) -> rusqlite::Result<Coverage> {
    let rows = tx.query("SELECT count(*),COALESCE(sum(v),0),COALESCE(sum(v OR (scan_state='clean' AND NOT parked)),0) FROM (SELECT r.scan_state,EXISTS(SELECT 1 FROM memory_vectors x WHERE x.content_hash=c.content_hash AND x.embedder_id=?1 AND x.normalizer_version=1) AS v,EXISTS(SELECT 1 FROM memory_embed_jobs j WHERE j.content_hash=c.content_hash AND j.embedder_id=?1 AND j.state='failed') AS parked FROM memory_cards c JOIN memory_card_revisions r ON r.scope=c.scope AND r.card_id=c.card_id AND r.revision=c.revision WHERE c.state='live')", &[t(id)])?;
    Ok(Coverage {
        covered: n(&rows[0][1]) as usize,
        eligible: n(&rows[0][2]) as usize,
        total: n(&rows[0][0]) as usize,
    })
}
