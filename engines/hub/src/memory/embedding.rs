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
CREATE TABLE memory_lineage(scope TEXT NOT NULL,card_id TEXT NOT NULL,revision INTEGER NOT NULL,artifact TEXT NOT NULL,artifact_key TEXT NOT NULL,content_hash TEXT NOT NULL,PRIMARY KEY(scope,card_id,revision,artifact,artifact_key)) WITHOUT ROWID;
CREATE TABLE memory_embed_jobs(content_hash TEXT NOT NULL,embedder_id TEXT NOT NULL,state TEXT NOT NULL,lease_id TEXT,holder TEXT,expires_ms INTEGER,attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,finished_ms INTEGER,PRIMARY KEY(content_hash,embedder_id)) WITHOUT ROWID;
CREATE INDEX memory_cards_hash ON memory_cards(content_hash);
CREATE INDEX memory_lineage_hash ON memory_lineage(content_hash);
";
#[derive(Clone, Debug)]
pub enum Command {
    Reindex {
        id: String,
        drop: bool,
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
        reason: String,
    },
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

pub fn parse(action: &str, raw: &Json, config: &Config) -> Result<Command, Reply> {
    let invalid = || error(422, "memory_vector_invalid", "invalid embedding command");
    let id = config.id.key();
    match action {
        "reindex" => Ok(Command::Reindex {
            id: raw["drop"].as_str().unwrap_or(&id).into(),
            drop: raw["drop"].is_string(),
        }),
        "embed_lease" if raw["embedder_id"].as_str() == Some(&id) => Ok(Command::Lease {
            max: raw["max"].as_u64().unwrap_or(4).clamp(1, 4) as usize,
        }),
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
                    .filter(|h| h.len() == 64 && h.bytes().all(|c| c.is_ascii_hexdigit()))
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
pub fn apply(
    tx: &dyn WriteTx,
    p: &Prepared,
    command: &Command,
    config: Option<&Config>,
    scanner: &dyn Scanner,
) -> rusqlite::Result<Reply> {
    if let Command::Reindex { id, drop: true } = command {
        for table in ["memory_vectors", "memory_embed_jobs"] {
            tx.execute(
                &format!("DELETE FROM {table} WHERE embedder_id=?1"),
                &[t(id)],
            )?;
        }
        tx.execute(
            "DELETE FROM memory_lineage WHERE artifact='vector' AND artifact_key=?1",
            &[t(id)],
        )?;
        return Ok(Reply::new(200, json!({"dropped": id})));
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
        Command::Reindex { .. } => {
            cleanup(tx)?;
            let resumed = tx.execute("UPDATE memory_embed_jobs SET state='pending',finished_ms=NULL WHERE embedder_id=?1 AND state='done' AND NOT EXISTS(SELECT 1 FROM memory_vectors v WHERE v.content_hash=memory_embed_jobs.content_hash AND v.embedder_id=?1) AND EXISTS(SELECT 1 FROM memory_cards c WHERE c.content_hash=memory_embed_jobs.content_hash AND c.state='live')", &[t(&id)])?;
            // Queue in bounded pages. Existing jobs and vectors make repeats idempotent.
            let rows = tx.query("SELECT DISTINCT c.content_hash FROM memory_cards c JOIN memory_card_revisions r ON r.scope=c.scope AND r.card_id=c.card_id AND r.revision=c.revision WHERE c.state='live' AND r.scan_state IN ('clean','flagged') AND NOT EXISTS(SELECT 1 FROM memory_vectors v WHERE v.content_hash=c.content_hash AND v.embedder_id=?1 AND v.normalizer_version=1) AND NOT EXISTS(SELECT 1 FROM memory_embed_jobs j WHERE j.content_hash=c.content_hash AND j.embedder_id=?1) LIMIT 256", &[t(&id)])?;
            for r in &rows {
                tx.execute("INSERT INTO memory_embed_jobs(content_hash,embedder_id,state) VALUES(?1,?2,'pending')", &[r[0].clone(), t(&id)])?;
            }
            tx.execute("INSERT OR IGNORE INTO memory_lineage SELECT c.scope,c.card_id,c.revision,'vector',v.embedder_id,c.content_hash FROM memory_cards c JOIN memory_vectors v ON v.content_hash=c.content_hash WHERE c.state='live' AND v.embedder_id=?1", &[t(&id)])?;
            Ok(Reply::new(
                200,
                json!({"queued": rows.len() + resumed, "embedder_id": id}),
            ))
        }
        Command::Lease { max } => {
            if !scanner.describe().0 {
                return Ok(error(503, "memory_scan_unavailable", "scan unavailable"));
            }
            let rows = tx.query("SELECT j.content_hash,b.bytes,c.scope,r.scan_state,c.card_id FROM memory_embed_jobs j JOIN memory_cards c ON c.content_hash=j.content_hash JOIN memory_card_revisions r ON r.scope=c.scope AND r.card_id=c.card_id AND r.revision=c.revision JOIN memory_blobs b ON b.content_hash=c.content_hash WHERE j.embedder_id=?1 AND c.state='live' AND r.scan_state IN ('clean','flagged') AND (j.state='pending' OR (j.state='leased' AND j.expires_ms<=?2)) ORDER BY j.content_hash,c.scope,c.card_id LIMIT 256", &[t(&id), Value::Integer(p.now_ms)])?;
            let lease = ids::mint(p.now_ms);
            let mut jobs = Vec::new();
            let mut bytes = 0;
            let mut hashes = std::collections::HashSet::new();
            for row in rows {
                if jobs.len() == *max {
                    break;
                }
                let hash = s(&row[0]);
                if hashes.contains(&hash)
                    || !auth::has_right(tx, &p.credential, Some(&s(&row[2])), "embed")?
                {
                    continue;
                }
                let body = match &row[1] {
                    Value::Blob(b) => String::from_utf8_lossy(b).into_owned(),
                    other => s(other),
                };
                let text = document_text(&body, config.input_chars);
                if !matches!(
                    scanner.scan(&text),
                    Scan::Clean { .. } | Scan::Flagged { .. }
                ) || bytes + text.len() > 32 * 1024
                {
                    continue;
                }
                bytes += text.len();
                hashes.insert(hash.clone());
                tx.execute("UPDATE memory_embed_jobs SET state='leased',lease_id=?3,holder=?4,expires_ms=?5,attempts=attempts+1 WHERE content_hash=?1 AND embedder_id=?2", &[t(&hash),t(&id),t(&lease),t(p.credential.principal()),Value::Integer(p.now_ms+60_000)])?;
                jobs.push(json!({"content_hash": hash, "text": text, "scan_state": s(&row[3])}));
            }
            Ok(Reply::new(
                200,
                json!({"lease_id": lease, "embedder_id": id, "model_digest": config.id.model_digest, "dimensions": config.id.dimensions, "jobs": jobs, "expires_ms": p.now_ms+60_000}),
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
            cleanup(tx)?;
            Ok(Reply::new(200, json!({"stored": vectors.len()})))
        }
        Command::Failed { lease, reason } => {
            tx.execute("UPDATE memory_embed_jobs SET state='pending',last_error=?3 WHERE lease_id=?1 AND holder=?2", &[t(lease),t(p.credential.principal()),t(reason)])?;
            Ok(Reply::new(200, json!({"ok": true})))
        }
    }
}
pub fn coverage(tx: &dyn ReadTx, id: &str) -> rusqlite::Result<(usize, usize)> {
    let rows = tx.query("SELECT count(*),COALESCE(sum(EXISTS(SELECT 1 FROM memory_vectors v WHERE v.content_hash=c.content_hash AND v.embedder_id=?1 AND v.normalizer_version=1)),0) FROM memory_cards c WHERE c.state='live'", &[t(id)])?;
    Ok((n(&rows[0][1]) as usize, n(&rows[0][0]) as usize))
}
