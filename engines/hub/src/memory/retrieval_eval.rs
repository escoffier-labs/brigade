//! Admin-attested S17 reports. Raw digest bytes cross the Python/Rust boundary.
use super::{
    Reply,
    auth::Credential,
    ids,
    ops::{self, Authority, NewEvent, Prepared},
    proposals::string,
};
use crate::writer::{ReadTx, WriteTx};
use rusqlite::types::Value;
use serde_json::{Value as Json, json};

/// No head-changing writes may follow the evaluated sequence. Non-head audit
/// events do not invalidate a run. The timestamp of the harness evaluation id
/// expires it after 24 hours even when the corpus never changes.
pub const MAX_AGE_MS: i64 = 86_400_000;
/// Match event-id admission: tolerate five minutes of client clock skew.
/// A future run still cannot arm the gate until its evaluated time arrives.
pub const MAX_FUTURE_MS: i64 = ids::FUTURE_MS;
pub const MAX_REPORT_BYTES: usize = 512 * 1024;
pub const DDL: &str = "CREATE TABLE memory_retrieval_evals(
 record_seq INTEGER PRIMARY KEY, mode TEXT NOT NULL CHECK(mode IN ('lexical','hybrid')),
 report_sha256 TEXT NOT NULL, report_json TEXT NOT NULL, principal TEXT NOT NULL,
 credential_id TEXT NOT NULL, created_ms INTEGER NOT NULL);
CREATE INDEX memory_retrieval_evals_latest ON memory_retrieval_evals(mode,record_seq DESC);
CREATE TRIGGER memory_retrieval_evals_no_update BEFORE UPDATE ON memory_retrieval_evals
 BEGIN SELECT RAISE(ABORT,'memory_retrieval_evals is immutable'); END;
CREATE TRIGGER memory_retrieval_evals_no_delete BEFORE DELETE ON memory_retrieval_evals
 BEGIN SELECT RAISE(ABORT,'memory_retrieval_evals is immutable'); END;";

#[derive(Clone, Debug)]
pub struct Live {
    pub indexed_seq: i64,
    pub embedder_id: Option<String>,
    pub weights: Json,
}
#[derive(Clone, Debug)]
pub struct Report {
    pub raw: String,
    pub digest: String,
    body: Json,
    mode: &'static str,
    evaluated_ms: i64,
    passed: bool,
}
fn hex(v: &Json) -> bool {
    v.as_str().is_some_and(|s| {
        s.len() == 64
            && s.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}
fn policy_digest(v: &Json) -> bool {
    v.as_str()
        .and_then(|s| s.strip_prefix("sha256:"))
        .is_some_and(|s| hex(&json!(s)))
}
fn positive_string(v: &Json) -> bool {
    v.as_str()
        .is_some_and(|s| !s.is_empty() && s.len() <= 256 && !s.chars().any(char::is_control))
}
fn binding(r: &Report) -> &Json {
    &r.body["adapters"][if r.mode == "lexical" {
        "hub-lexical"
    } else {
        "hub-hybrid"
    }]["binding"]
}
pub fn prepare(request: &Json) -> Result<Report, Reply> {
    let invalid = || {
        ops::error(
            422,
            "memory_retrieval_eval_invalid",
            "an integrity-checked S17 R0 or R1-public report with complete bindings is required",
        )
    };
    let raw = request["report_json"].as_str().ok_or_else(invalid)?;
    if raw.len() > MAX_REPORT_BYTES {
        return Err(ops::error(
            413,
            "memory_retrieval_eval_too_large",
            "retrieval report exceeds 512 KiB",
        ));
    }
    if !hex(&request["report_sha256"]) {
        return Err(invalid());
    }
    let digest = string(request, "report_sha256");
    if ids::sha256_hex(raw.as_bytes()) != digest {
        return Err(ops::error(
            422,
            "memory_retrieval_eval_digest_mismatch",
            "retrieval report bytes do not match its stable digest",
        ));
    }
    let body: Json = serde_json::from_str(raw).map_err(|_| invalid())?;
    if !body.is_object() || body.get("digest").is_some() || !hex(&body["fixture_digest"]) {
        return Err(invalid());
    }
    let mode = match body["gate"]["name"].as_str() {
        Some("R0") => "lexical",
        Some("R1-public") => "hybrid",
        _ => return Err(invalid()),
    };
    let (_, evaluated_ms) =
        ids::parse_event_id(string(&body, "evaluation_id")).ok_or_else(invalid)?;
    let passed = body["gate"]["passed"] == true && super::retrieval_eval_gates::passed(&body, mode);
    let report = Report {
        raw: raw.into(),
        digest: digest.into(),
        body,
        mode,
        evaluated_ms,
        passed,
    };
    let b = binding(&report);
    let available = report.body["adapters"][if mode == "lexical" {
        "hub-lexical"
    } else {
        "hub-hybrid"
    }]["available"]
        .as_bool()
        == Some(true)
        && report.body["adapters"]["bm25"]["available"].as_bool() == Some(true);
    let passed = report.body["gate"]["passed"]
        .as_bool()
        .ok_or_else(invalid)?;
    let failures = report.body["gate"]["failures"]
        .as_array()
        .ok_or_else(invalid)?;
    if (passed && (!available || !failures.is_empty()))
        || failures.iter().any(|f| !f.is_string())
        || b["mode"] != mode
        || b["indexed_seq"].as_i64().is_none_or(|n| n < 0)
        || !positive_string(&b["index_format"])
        || !positive_string(&b["scorer_version"])
        || b["policy_revision"].as_i64().is_none_or(|n| n < 1)
        || !policy_digest(&b["policy_digest"])
        || ids::parse_event_id(string(b, "log_epoch")).is_none()
        || !ids::valid_scope(string(b, "scope"))
        || !hex(&b["corpus_identity"]["digest"])
        || b["corpus_identity"]["live_count"]
            .as_i64()
            .is_none_or(|n| n < 1)
        || !matches!(b["ranking"].as_str(), Some("raw" | "default"))
        || (passed && b["ranking"] != if mode == "lexical" { "raw" } else { "default" })
        || !b["rrf_weights"].get("lexical").is_some_and(Json::is_number)
        || !b["rrf_weights"].get("dense").is_some_and(Json::is_number)
        || super::ranking::Weights::parse(Some(&b["rrf_weights"])).is_err()
        || (mode == "hybrid" && !hex(&b["embedder_id"]))
        || (mode == "lexical" && !b["embedder_id"].is_null())
    {
        return Err(invalid());
    }
    Ok(report)
}
pub fn record(
    tx: &dyn WriteTx,
    p: &Prepared,
    r: &Report,
    a: &Authority,
) -> rusqlite::Result<Reply> {
    if !matches!(p.credential, Credential::Admin) {
        return Ok(ops::error(
            403,
            "memory_admin_required",
            "retrieval evaluation records require admin authority",
        ));
    }
    // Reverify the raw report at the write boundary, not a prepared pass bit.
    let checked = match prepare(&json!({"report_json":r.raw,"report_sha256":r.digest})) {
        Ok(r) => r,
        Err(r) => return Ok(r),
    };
    let r = &checked;
    let latest=tx.query("SELECT report_json,report_sha256 FROM memory_retrieval_evals WHERE mode=?1 ORDER BY record_seq DESC LIMIT 1",&[ops::t(r.mode)])?;
    if let Some(row) = latest.first() {
        let previous = match prepare(
            &json!({"report_json":ops::s(&row[0]),"report_sha256":ops::s(&row[1])}),
        ) {
            Ok(previous) => previous,
            Err(_) => {
                return Ok(ops::error(
                    409,
                    "memory_retrieval_eval_record_invalid",
                    "the latest retrieval record is invalid, no record was appended",
                ));
            }
        };
        if r.evaluated_ms <= previous.evaluated_ms {
            return Ok(ops::error(
                409,
                "memory_retrieval_eval_superseded",
                "evaluation time must be strictly newer than the latest record for this mode",
            ));
        }
    }
    if r.evaluated_ms > p.now_ms.saturating_add(MAX_FUTURE_MS) {
        return Ok(ops::error(
            422,
            "memory_retrieval_eval_future",
            "evaluation time exceeds the five-minute clock skew allowance",
        ));
    }
    if p.now_ms.saturating_sub(r.evaluated_ms) > MAX_AGE_MS {
        return Ok(ops::error(
            422,
            "memory_retrieval_eval_expired",
            "evaluation time is more than 24 hours old",
        ));
    }
    let payload = json!({"report_sha256":r.digest,"mode":r.mode,"gate":r.body["gate"]["name"],"passed":r.passed});
    let seq = ops::append(
        tx,
        p,
        NewEvent {
            kind: "retrieval_eval.record",
            scope: None,
            card_id: None,
            changes_head: false,
            base_revision: None,
            content_hash: None,
            trust: None,
            origin: None,
            payload: payload.clone(),
            policy_digest: None,
        },
    )?;
    let credential = &a.admin_credential;
    tx.execute("INSERT INTO memory_retrieval_evals(record_seq,mode,report_sha256,report_json,principal,credential_id,created_ms) VALUES(?1,?2,?3,?4,?5,?6,?7)", &[
        Value::Integer(seq),ops::t(r.mode),ops::t(&r.digest),ops::t(&r.raw),ops::t(p.credential.principal()),ops::t(credential),Value::Integer(p.now_ms)])?;
    Ok(Reply::new(
        201,
        json!({"record_seq":seq,"report_sha256":r.digest,"mode":r.mode,"passed":r.passed}),
    ))
}
/// Called from preflight and again under the hub lock in the apply transaction.
/// The newest record for the configured mode wins, including a failed record.
pub fn hold<T: ReadTx + ?Sized>(
    tx: &T,
    proposal: &Json,
    a: &Authority,
    now: i64,
) -> rusqlite::Result<Option<&'static str>> {
    let Some(live) = &a.retrieval else {
        return Ok(Some("retrieval_eval_runtime_unavailable"));
    };
    let mode = if live.embedder_id.is_some() {
        "hybrid"
    } else {
        "lexical"
    };
    let rows=tx.query("SELECT report_json,report_sha256 FROM memory_retrieval_evals WHERE mode=?1 ORDER BY record_seq DESC LIMIT 1",&[ops::t(mode)])?;
    let Some(row) = rows.first() else {
        return Ok(Some("retrieval_eval_unbound"));
    };
    let r = match prepare(&json!({"report_json":ops::s(&row[0]),"report_sha256":ops::s(&row[1])})) {
        Ok(r) => r,
        Err(_) => return Ok(Some("retrieval_eval_record_invalid")),
    };
    let b = binding(&r);
    if let Some(policy) = a.policy_document["memory"].get("retrieval_eval") {
        let Some(policy) = policy.as_object() else {
            return Ok(Some("retrieval_eval_policy_invalid"));
        };
        if policy.keys().any(|k| k != "fixture_digest") {
            return Ok(Some("retrieval_eval_policy_invalid"));
        }
        if let Some(pin) = policy.get("fixture_digest") {
            if !hex(pin) {
                return Ok(Some("retrieval_eval_policy_invalid"));
            }
            if *pin != r.body["fixture_digest"] {
                return Ok(Some("retrieval_eval_fixture_mismatch"));
            }
        }
    }
    let reason = if r.mode != mode {
        Some("retrieval_eval_mode_mismatch")
    } else if !r.passed {
        Some("retrieval_eval_failed")
    } else if r.evaluated_ms > now {
        Some("retrieval_eval_future")
    } else if now.saturating_sub(r.evaluated_ms) > MAX_AGE_MS {
        Some("retrieval_eval_expired")
    } else if b["log_epoch"] != ops::epoch(tx)? {
        Some("retrieval_eval_epoch_mismatch")
    } else if b["index_format"] != super::index::FORMAT_VERSION {
        Some("retrieval_eval_index_format_mismatch")
    } else if b["scorer_version"] != super::ranking::SCORER_VERSION {
        Some("retrieval_eval_scorer_mismatch")
    } else if b["policy_revision"] != json!(a.policy.as_ref().map(|p| p.0)) {
        Some("retrieval_eval_policy_revision_mismatch")
    } else if b["policy_digest"] != json!(a.policy.as_ref().map(|p| &p.1)) {
        Some("retrieval_eval_policy_digest_mismatch")
    } else if b["embedder_id"] != json!(live.embedder_id) {
        Some("retrieval_eval_embedder_mismatch")
    } else if b["rrf_weights"] != live.weights {
        Some("retrieval_eval_weights_mismatch")
    } else if super::proposals::cards(proposal)
        .iter()
        .any(|c| c["scope"] != b["scope"])
    {
        Some("retrieval_eval_scope_mismatch")
    } else {
        None
    };
    if reason.is_some() {
        return Ok(reason);
    }
    let seq = b["indexed_seq"].as_i64().unwrap();
    let last_head = tx.query(
        "SELECT COALESCE(MAX(seq),0) FROM memory_events WHERE revision IS NOT NULL",
        &[],
    )?;
    let high = tx.query("SELECT COALESCE(MAX(seq),0) FROM memory_events", &[])?;
    let integer = |v: &Value| if let Value::Integer(n) = v { *n } else { 0 };
    if seq > integer(&high[0][0]) || seq > live.indexed_seq {
        return Ok(Some("retrieval_eval_seq_future"));
    }
    if seq < integer(&last_head[0][0]) {
        return Ok(Some("retrieval_eval_seq_stale"));
    }
    // Validate exact scoped corpus from raw content hashes. The timestamp and
    // sequence checks alone cannot detect a report from another hub at seq N.
    let members=tx.query("SELECT card_id,content_hash FROM memory_cards WHERE scope=?1 AND state='live' ORDER BY card_id",&[ops::t(string(b,"scope"))])?;
    let members: Vec<_> = members
        .iter()
        .map(|r| json!([ops::s(&r[0]), ops::s(&r[1])]))
        .collect();
    let identity = json!({"live_count":members.len(),"digest":ids::sha256_hex(ids::canonical(&json!(members)).as_bytes())});
    Ok((b["corpus_identity"] != identity).then_some("retrieval_eval_corpus_mismatch"))
}

/// Only review-capable diagnostics expose the selected trust-bearing record.
pub fn summary<T: ReadTx + ?Sized>(tx: &T, a: &Authority) -> rusqlite::Result<Json> {
    let Some(live) = &a.retrieval else {
        return Ok(Json::Null);
    };
    let mode = if live.embedder_id.is_some() {
        "hybrid"
    } else {
        "lexical"
    };
    let rows=tx.query("SELECT record_seq,report_sha256 FROM memory_retrieval_evals WHERE mode=?1 ORDER BY record_seq DESC LIMIT 1",&[ops::t(mode)])?;
    Ok(rows.first().map(|r|json!({"record_seq":if let Value::Integer(n)=r[0]{n}else{0},"report_sha256":ops::s(&r[1])})).unwrap_or(Json::Null))
}
