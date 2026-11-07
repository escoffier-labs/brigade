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
    let mut counts = json!({"clean": 0, "private": 0, "flagged": 0, "blocked": 0});
    let mut scanned = 0;
    let mut bytes = 0;
    for row in rows.iter().take(limit) {
        let hash = ops::s(&row[0]);
        let blobs = tx.query(
            "SELECT bytes FROM memory_blobs WHERE content_hash=?1",
            &[ops::t(&hash)],
        )?;
        let Some(Value::Blob(blob)) = blobs.first().and_then(|r| r.first()) else {
            return Ok(ops::error(
                503,
                "memory_scan_failed",
                "stored blob unavailable",
            ));
        };
        if scanned > 0 && bytes + blob.len() > super::ids::MAX_TEXT_BYTES {
            break;
        }
        let Ok(text) = std::str::from_utf8(blob) else {
            return Ok(ops::error(
                503,
                "memory_scan_failed",
                "stored blob is not UTF-8",
            ));
        };
        // No normalization: the receipt names these exact immutable bytes.
        let result = scanner.scan(text);
        if matches!(result, scan::Scan::Failed | scan::Scan::Unavailable)
            || result.ruleset() != version
        {
            return Ok(Reply::new(
                503,
                json!({"code":"memory_scan_failed", "error":"rescan failed", "retryable":false}),
            ));
        }
        ops::store_scan(tx, &hash, &result, &stamp)?;
        let count = counts[result.state()].as_u64().unwrap();
        counts[result.state()] = json!(count + 1);
        scanned += 1;
        bytes += blob.len();
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
                ops::t(&format!("scan_rescan:{}", p.client_event_id)),
                ops::t(&summary.to_string()),
            ],
        )?;
    }
    Ok(Reply::new(200, summary))
}
