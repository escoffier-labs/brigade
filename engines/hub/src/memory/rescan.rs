//! Admin receipt refresh. No card, revision, trust or blob mutation.
use super::{Reply, ops, scan};
use crate::writer::WriteTx;
use rusqlite::types::Value;
use serde_json::json;

// Use the hub's structured stderr convention without another logging crate.
macro_rules! warn {
    ($hash:expr, $rule:expr) => {
        eprintln!("{}", json!({"level":"warn", "event":"memory_rescan_corrupt", "content_hash":$hash, "rule_id":$rule}));
    };
}

fn pending(
    tx: &dyn WriteTx,
    hash: &str,
    version: &str,
    rule: &str,
    stamp: &str,
) -> rusqlite::Result<()> {
    tx.execute(
        "INSERT INTO memory_scans(content_hash,ruleset_version,state,rule_ids,scanned_at) VALUES(?1,?2,'pending',?3,?4) ON CONFLICT(content_hash,ruleset_version) DO UPDATE SET state=excluded.state,rule_ids=excluded.rule_ids,scanned_at=excluded.scanned_at",
        &[ops::t(hash), ops::t(version), ops::t(&json!([rule]).to_string()), ops::t(stamp)],
    )?;
    Ok(())
}

pub(super) fn run(
    tx: &dyn WriteTx,
    p: &ops::Prepared,
    scanner: &dyn scan::Scanner,
    limit: usize,
    retry_failed: bool,
    after: Option<&str>,
) -> rusqlite::Result<Reply> {
    let (available, version) = scanner.describe();
    let Some(version) = version.filter(|_| available) else {
        return Ok(ops::error(
            503,
            "memory_scan_unavailable",
            "scanner unavailable",
        ));
    };
    // Fetch only keys first. Blob reads are bounded by the batch size and a
    // 512 KiB byte budget, with at least one supported card making progress.
    // The cursor lets a retry pass attempt each failing hash once.
    let rows = tx.query(
        "SELECT DISTINCT c.content_hash FROM memory_cards c WHERE c.state='live' AND (?4 IS NULL OR c.content_hash>?4) AND (NOT EXISTS (SELECT 1 FROM memory_scans s WHERE s.content_hash=c.content_hash AND s.ruleset_version=?1) OR (?3 AND EXISTS (SELECT 1 FROM memory_scans s WHERE s.content_hash=c.content_hash AND s.ruleset_version=?1 AND s.state='pending' AND s.rule_ids='[\"scan-failed\"]'))) ORDER BY c.content_hash LIMIT ?2",
        &[ops::t(&version), Value::Integer(limit as i64 + 1), Value::Integer(i64::from(retry_failed)), after.map(ops::t).unwrap_or(Value::Null)],
    )?;
    let stamp = super::ids::isoformat(p.now_ms);
    let mut counts =
        json!({"clean": 0, "private": 0, "flagged": 0, "blocked": 0, "failed": 0, "corrupt": 0});
    let mut scanned = 0;
    let mut bytes = 0;
    let mut last = None;
    for row in rows.iter().take(limit) {
        let hash = ops::s(&row[0]);
        let blobs = tx.query(
            "SELECT bytes FROM memory_blobs WHERE content_hash=?1",
            &[ops::t(&hash)],
        )?;
        let blob = blobs.first().and_then(|r| r.first()).and_then(|v| match v {
            Value::Blob(blob) => Some(blob.as_slice()),
            Value::Text(text) => Some(text.as_bytes()),
            _ => None,
        });
        let size = blob.map_or(0, <[u8]>::len);
        if scanned > 0 && bytes + size > super::ids::MAX_TEXT_BYTES {
            break;
        }
        // Preserve exact stored bytes. Distinguish storage corruption from a
        // scanner failure and keep every non-clean receipt closed to push.
        let text = match blob {
            None => Err("blob-missing"),
            Some(bytes) => std::str::from_utf8(bytes).map_err(|_| "blob-not-utf8"),
        };
        match text {
            Err(rule) => {
                pending(tx, &hash, &version, rule, &stamp)?;
                warn!(&hash, rule);
                counts["corrupt"] = json!(counts["corrupt"].as_u64().unwrap() + 1);
            }
            Ok(text) => {
                let result = scanner.scan(text);
                if matches!(result, scan::Scan::Failed) {
                    pending(tx, &hash, &version, "scan-failed", &stamp)?;
                    counts["failed"] = json!(counts["failed"].as_u64().unwrap() + 1);
                } else {
                    if matches!(result, scan::Scan::Unavailable) || result.ruleset() != version {
                        return Ok(Reply::new(
                            503,
                            json!({"code":"memory_scan_failed", "error":"input cannot be scanned, retrying will not help", "retryable":false}),
                        ));
                    }
                    ops::store_scan(tx, &hash, &result, &stamp)?;
                    counts[result.state()] = json!(counts[result.state()].as_u64().unwrap() + 1);
                }
            }
        }
        last = Some(hash);
        scanned += 1;
        bytes += size;
    }
    let summary = json!({
        "ruleset_version": version,
        "scanned": scanned,
        "counts": counts,
        "complete": scanned == rows.len(),
        "next_after": if scanned == rows.len() { None } else { last },
        "principal": p.credential.principal(),
        "scanned_at": stamp,
    });
    if scanned > 0 {
        // One immutable audit summary per batch, committed with the receipts.
        // A replay returns the saved response. An empty rerun writes nothing.
        tx.execute(
            "INSERT INTO memory_meta(key,value) VALUES(?1,?2)",
            &[
                ops::t(&format!("scan_rescan:{}:{}", p.now_ms, p.client_event_id)),
                ops::t(&summary.to_string()),
            ],
        )?;
    }
    Ok(Reply::new(200, summary))
}
