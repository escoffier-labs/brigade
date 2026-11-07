//! Admin receipt refresh. No card, revision, trust or blob mutation.
use super::{Reply, ops, scan};
use crate::writer::WriteTx;
use rusqlite::types::Value;
use serde_json::json;

pub(super) fn run(
    tx: &dyn WriteTx,
    p: &ops::Prepared,
    scanner: &dyn scan::Scanner,
    limit: usize,
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
    // Repeated calls drain missing receipts, so no cursor can skip a head.
    let rows = tx.query(
        "SELECT DISTINCT c.content_hash FROM memory_cards c WHERE c.state='live' AND NOT EXISTS (SELECT 1 FROM memory_scans s WHERE s.content_hash=c.content_hash AND s.ruleset_version=?1) ORDER BY c.content_hash LIMIT ?2",
        &[ops::t(&version), Value::Integer(limit as i64 + 1)],
    )?;
    let stamp = super::ids::isoformat(p.now_ms);
    let mut counts = json!({"clean": 0, "private": 0, "flagged": 0, "blocked": 0, "failed": 0});
    let mut scanned = 0;
    let mut bytes = 0;
    for row in rows.iter().take(limit) {
        let hash = ops::s(&row[0]);
        let blobs = tx.query(
            "SELECT bytes FROM memory_blobs WHERE content_hash=?1",
            &[ops::t(&hash)],
        )?;
        let blob = blobs.first().and_then(|r| r.first()).and_then(|v| match v {
            Value::Blob(blob) => Some(blob),
            _ => None,
        });
        let size = blob.map_or(0, Vec::len);
        if scanned > 0 && bytes + size > super::ids::MAX_TEXT_BYTES {
            break;
        }
        // No normalization: the receipt names these exact immutable bytes.
        let result = blob
            .and_then(|b| std::str::from_utf8(b).ok())
            .map(|text| scanner.scan(text));
        match result {
            None | Some(scan::Scan::Failed) => {
                // A deterministic failure must not block later hashes. Pending
                // stays ineligible for push and is not retried under this ID.
                tx.execute(
                    "INSERT INTO memory_scans(content_hash,ruleset_version,state,rule_ids,scanned_at) VALUES(?1,?2,'pending','[\"scan-failed\"]',?3)",
                    &[ops::t(&hash), ops::t(&version), ops::t(&stamp)],
                )?;
                counts["failed"] = json!(counts["failed"].as_u64().unwrap() + 1);
            }
            Some(result) => {
                if matches!(result, scan::Scan::Unavailable) || result.ruleset() != version {
                    return Ok(Reply::new(
                        503,
                        json!({
                            "code":"memory_scan_failed", "error":"input cannot be scanned, retrying will not help", "retryable":false
                        }),
                    ));
                }
                ops::store_scan(tx, &hash, &result, &stamp)?;
                counts[result.state()] = json!(counts[result.state()].as_u64().unwrap() + 1);
            }
        }
        scanned += 1;
        bytes += size;
    }
    let summary = json!({
        "ruleset_version": version,
        "scanned": scanned,
        "counts": counts,
        "complete": scanned == rows.len(),
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
