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
use std::collections::{BTreeMap, BTreeSet};

/// Reserved synthetic scope. Fixture reports carry no authority elsewhere.
pub const FIXTURE_SCOPE: &str = "repo:brigade/retrieval-eval-fixture";

/// The harness evaluation id expires after 24 hours even with no corpus drift.
pub const MAX_AGE_MS: i64 = 86_400_000;
/// Match event-id admission: tolerate five minutes of client clock skew.
/// A future run still cannot arm the gate until its evaluated time arrives.
pub const MAX_FUTURE_MS: i64 = ids::FUTURE_MS;
pub const MAX_REPORT_BYTES: usize = 512 * 1024;
pub const DDL: &str = "CREATE TABLE memory_retrieval_evals(
 record_seq INTEGER PRIMARY KEY, mode TEXT NOT NULL CHECK(mode IN ('lexical','hybrid')),
 report_sha256 TEXT NOT NULL, report_json TEXT NOT NULL, principal TEXT NOT NULL,
 credential_id TEXT NOT NULL, created_ms INTEGER NOT NULL,
 evaluated_ms INTEGER NOT NULL, corpus_members TEXT);
CREATE INDEX memory_retrieval_evals_latest ON memory_retrieval_evals(mode,record_seq DESC);
CREATE TRIGGER memory_retrieval_evals_no_update BEFORE UPDATE ON memory_retrieval_evals
 BEGIN SELECT RAISE(ABORT,'memory_retrieval_evals is immutable'); END;
CREATE TRIGGER memory_retrieval_evals_no_delete BEFORE DELETE ON memory_retrieval_evals
 BEGIN SELECT RAISE(ABORT,'memory_retrieval_evals is immutable'); END;";

/// Version 7 did not retain the evaluated member set or evaluation timestamp.
pub(super) fn legacy_ddl() -> String {
    DDL.replace(",\n evaluated_ms INTEGER NOT NULL, corpus_members TEXT", "")
}

/// Called inside the schema migration transaction. Never reconstruct an old
/// evaluated snapshot from today's heads. Legacy records hold until replaced.
pub(super) fn migrate(c: &rusqlite::Connection) -> rusqlite::Result<()> {
    c.execute_batch(
        "ALTER TABLE memory_retrieval_evals RENAME TO memory_retrieval_evals_v7;
        DROP INDEX memory_retrieval_evals_latest;
        DROP TRIGGER memory_retrieval_evals_no_update;
        DROP TRIGGER memory_retrieval_evals_no_delete;",
    )?;
    c.execute_batch(DDL)?;
    let mut rows = c.prepare("SELECT record_seq,mode,report_sha256,report_json,principal,credential_id,created_ms FROM memory_retrieval_evals_v7")?;
    let mut rows = rows.query([])?;
    while let Some(row) = rows.next()? {
        let raw: String = row.get(3)?;
        let created_ms: i64 = row.get(6)?;
        // Decode only the stable timestamp, independent of prepare's evolving
        // report requirements. Corrupt legacy bytes get a conservative floor
        // equal to the latest timestamp record admission could have allowed.
        let evaluated_ms = serde_json::from_str::<Json>(&raw)
            .ok()
            .and_then(|body| ids::parse_event_id(string(&body, "evaluation_id")))
            .map(|(_, ms)| ms)
            .unwrap_or_else(|| created_ms.saturating_add(MAX_FUTURE_MS));
        c.execute("INSERT INTO memory_retrieval_evals(record_seq,mode,report_sha256,report_json,principal,credential_id,created_ms,evaluated_ms) VALUES(?1,?2,?3,?4,?5,?6,?7,?8)", rusqlite::params![row.get::<_, i64>(0)?, row.get::<_, String>(1)?, row.get::<_, String>(2)?, raw, row.get::<_, String>(4)?, row.get::<_, String>(5)?, created_ms, evaluated_ms])?;
    }
    c.execute_batch("DROP TABLE memory_retrieval_evals_v7")
}

fn members<T: ReadTx + ?Sized>(tx: &T, scope: &str) -> rusqlite::Result<BTreeMap<String, String>> {
    Ok(tx.query("SELECT card_id,content_hash FROM memory_cards WHERE scope=?1 AND state='live' ORDER BY card_id", &[ops::t(scope)])?
        .iter().map(|row| (ops::s(&row[0]).to_string(), ops::s(&row[1]).to_string())).collect())
}
/// Includes missing vectors and their exact bytes, so coverage and replacements bind.
pub(super) fn dense_identity<T: ReadTx + ?Sized>(
    tx: &T,
    scope: &str,
    embedder: &str,
) -> rusqlite::Result<Json> {
    let rows=tx.query("SELECT c.content_hash,v.embedder_id,v.normalizer_version,v.vector FROM memory_cards c JOIN memory_vectors v ON v.content_hash=c.content_hash AND v.embedder_id=?2 AND v.normalizer_version=1 WHERE c.scope=?1 AND c.state='live' ORDER BY c.card_id",&[ops::t(scope),ops::t(embedder)])?;
    let mut covered = 0;
    let pairs: Vec<_> = rows
        .iter()
        .map(|r| {
            let vector = if let Value::Blob(bytes) = &r[3] {
                covered += 1;
                json!(ids::sha256_hex(bytes))
            } else {
                Json::Null
            };
            json!([ops::s(&r[0]), r.get(1).map(ops::s), vector])
        })
        .collect();
    let seq = tx.query(
        "SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name='memory_vector_log'),0)",
        &[],
    )?;
    Ok(
        json!({"embedder_id":embedder,"vector_log_seq":if let Value::Integer(n)=seq[0][0]{n}else{0},"coverage_count":covered,"digest":ids::sha256_hex(ids::canonical(&json!(pairs)).as_bytes())}),
    )
}

fn member_pairs(members: &BTreeMap<String, String>) -> Vec<(&str, &str)> {
    members
        .iter()
        .map(|(id, hash)| (id.as_str(), hash.as_str()))
        .collect()
}
fn identity(members: &BTreeMap<String, String>) -> Json {
    json!({"live_count":members.len(),"digest":ids::sha256_hex(ids::canonical(&json!(member_pairs(members))).as_bytes())})
}
fn drift_budget(member_count: usize) -> usize {
    // Use the smaller snapshot/live count. Integer division floors 2%.
    (member_count / 50).min(5)
}

#[derive(Clone, Debug)]
pub struct Live {
    /// Credential class that will apply the proposal, which sets writer class.
    pub applier: &'static str,
    /// Credential principal that will apply it, which sets the head's writer.
    pub principal: String,
    /// The published index the gate is judged against. Search answers from it,
    /// so its trusted lineage graph is read here beside the live store.
    pub snapshot: Option<std::sync::Arc<super::index::Snapshot>>,
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
    if r.body["gate"]["name"] == "Live-R0" {
        return &r.body["live"]["binding"];
    }
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
    if !body.is_object()
        || body.get("digest").is_some()
        || !(if body["gate"]["name"] == "Live-R0" {
            hex(&body["query_set_digest"])
                && ids::parse_event_id(string(&body["live"], "proposal_id")).is_some()
                && body["live"]["targets"]
                    .as_array()
                    .is_some_and(|t| !t.is_empty())
        } else {
            hex(&body["fixture_digest"])
        })
    {
        return Err(invalid());
    }
    let mode = match body["gate"]["name"].as_str() {
        Some("R0" | "Live-R0") => "lexical",
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
    let available = if report.body["gate"]["name"] == "Live-R0" {
        report.body["adapters"]["before"]["available"] == true
            && report.body["adapters"]["candidate"]["available"] == true
    } else {
        report.body["adapters"][if mode == "lexical" {
            "hub-lexical"
        } else {
            "hub-hybrid"
        }]["available"]
            .as_bool()
            == Some(true)
            && report.body["adapters"]["bm25"]["available"].as_bool() == Some(true)
    };
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
    let latest = tx.query(
        "SELECT MAX(evaluated_ms) FROM memory_retrieval_evals WHERE mode=?1",
        &[ops::t(r.mode)],
    )?;
    if matches!(latest[0][0], Value::Integer(ms) if r.evaluated_ms <= ms) {
        return Ok(ops::error(
            409,
            "memory_retrieval_eval_superseded",
            "evaluation time must be strictly newer than every record for this mode",
        ));
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
    if r.body["gate"]["name"] == "Live-R0"
        && !super::retrieval_history::valid(tx, &r.body, p.now_ms)?
    {
        return Ok(ops::error(
            409,
            "memory_retrieval_history_insufficient",
            "judged history does not match the report",
        ));
    }
    let snapshot = members(tx, string(binding(r), "scope"))?;
    if binding(r)["corpus_identity"] != identity(&snapshot) {
        return Ok(ops::error(
            409,
            "memory_retrieval_eval_corpus_mismatch",
            "live scoped members do not match the evaluated corpus identity",
        ));
    }
    let dense = if r.mode == "hybrid" {
        Some(dense_identity(
            tx,
            string(binding(r), "scope"),
            a.retrieval
                .as_ref()
                .and_then(|r| r.embedder_id.as_deref())
                .unwrap_or(""),
        )?)
    } else {
        None
    };
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
    tx.execute("INSERT INTO memory_retrieval_evals(record_seq,mode,report_sha256,report_json,principal,credential_id,created_ms,evaluated_ms,corpus_members,dense_identity) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)", &[
        Value::Integer(seq),ops::t(r.mode),ops::t(&r.digest),ops::t(&r.raw),ops::t(p.credential.principal()),ops::t(credential),Value::Integer(p.now_ms),Value::Integer(r.evaluated_ms),ops::t(&ids::canonical(&json!(member_pairs(&snapshot)))),dense.map(|d|ops::t(&d.to_string())).unwrap_or(Value::Null)])?;
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
    // Fixture reports have authority only in this reserved test/CI scope.
    // On real scopes history can veto, but can never supply permission.
    if super::proposals::cards(proposal)
        .iter()
        .any(|c| c["scope"] != FIXTURE_SCOPE)
    {
        return real_scope_hold(tx, proposal, mode, live, now);
    }
    let rows=tx.query("SELECT report_json,report_sha256,corpus_members,dense_identity FROM memory_retrieval_evals WHERE mode=?1 ORDER BY record_seq DESC LIMIT 1",&[ops::t(mode)])?;
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
    if r.body["gate"]["name"] == "Live-R0"
        && (r.body["live"]["proposal_id"] != proposal["proposal_id"]
            || r.body["live"]["targets"] != live_targets(proposal, false))
    {
        return Ok(Some("retrieval_eval_candidate_mismatch"));
    }
    if r.body["gate"]["name"] == "Live-R0" && !super::retrieval_history::valid(tx, &r.body, now)? {
        return Ok(Some("retrieval_eval_judgments_changed"));
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
    if mode == "hybrid" {
        let current = dense_identity(
            tx,
            string(b, "scope"),
            live.embedder_id.as_deref().unwrap_or(""),
        )?;
        let stored = serde_json::from_str::<Json>(&ops::s(&row[3])).unwrap_or(Json::Null);
        if current != stored {
            return Ok(Some("retrieval_eval_vector_changed"));
        }
    }
    let seq = b["indexed_seq"].as_i64().unwrap();
    let high = tx.query("SELECT COALESCE(MAX(seq),0) FROM memory_events", &[])?;
    let integer = |v: &Value| if let Value::Integer(n) = v { *n } else { 0 };
    if seq > integer(&high[0][0]) || seq > live.indexed_seq {
        return Ok(Some("retrieval_eval_seq_future"));
    }
    let Value::Text(raw_members) = &row[2] else {
        return Ok(Some("retrieval_eval_snapshot_missing"));
    };
    let snapshot = match serde_json::from_str::<Vec<(String, String)>>(raw_members) {
        Ok(pairs) => {
            let len = pairs.len();
            let snapshot: BTreeMap<_, _> = pairs.into_iter().collect();
            if snapshot.len() != len || identity(&snapshot) != b["corpus_identity"] {
                return Ok(Some("retrieval_eval_corpus_mismatch"));
            }
            snapshot
        }
        Err(_) => return Ok(Some("retrieval_eval_record_invalid")),
    };
    let live_members = members(tx, string(b, "scope"))?;
    let mut changed: BTreeSet<_> = snapshot
        .keys()
        .chain(live_members.keys())
        .filter(|id| snapshot.get(*id) != live_members.get(*id))
        .cloned()
        .collect();
    // Default ranking also depends on trust-gated lineage, head recency and
    // source demotion. Hash equality cannot account for those changes. Raw
    // ranking uses only content and membership, so unrelated trust is free.
    if b["ranking"] == "default" {
        let head_changes = tx.query("SELECT DISTINCT card_id FROM memory_events WHERE scope=?1 AND revision IS NOT NULL AND seq>?2",
            &[ops::t(string(b, "scope")), Value::Integer(seq)])?;
        changed.extend(head_changes.iter().map(|row| ops::s(&row[0])));
    }
    for card in super::proposals::cards(proposal) {
        let id = string(card, "card_id");
        // Any target head change since indexing holds, even a content revert or
        // a trust-only edit, regardless of ranking mode.
        let revisions = tx.query("SELECT 1 FROM memory_events WHERE scope=?1 AND card_id=?2 AND revision IS NOT NULL AND seq>?3 LIMIT 1",
            &[ops::t(string(card, "scope")), ops::t(id), Value::Integer(seq)])?;
        if changed.contains(id) || !revisions.is_empty() {
            return Ok(Some("retrieval_eval_target_drifted"));
        }
    }
    // Snapshot::score filters IDF and length statistics to allowed scopes.
    // The bound single-scope corpus is unaffected by writes outside it.
    Ok(
        (changed.len() > drift_budget(snapshot.len().min(live_members.len())))
            .then_some("retrieval_eval_drift_exceeded"),
    )
}

/// Recomputed at preflight and apply from current hub heads, never report metrics.
/// `append_evidence` and `anchor_blocks` change bytes search never reads, so each
/// target's ranking projection must come out identical. An exact-duplicate merge
/// cannot, so `merge_gate` admits it under bounded drift instead.
fn real_scope_hold<T: ReadTx + ?Sized>(
    tx: &T,
    proposal: &Json,
    mode: &str,
    live: &Live,
    now: i64,
) -> rusqlite::Result<Option<&'static str>> {
    let applier = live.applier;
    if mode != "lexical" {
        return Ok(Some("retrieval_eval_projection_changed"));
    }
    if let Some(reason) = super::retrieval_history::veto(tx, proposal, now)? {
        return Ok(Some(reason));
    }
    if proposal["tier"] != 0 {
        return Ok(Some("retrieval_eval_projection_changed"));
    }
    let ops = proposal["ops"].as_array().map(Vec::as_slice).unwrap_or(&[]);
    let merging = ops.iter().any(super::merge_gate::is_merge);
    if merging && !ops.iter().all(super::merge_gate::is_merge) {
        return Ok(Some(super::merge_gate::MIXED_OPS));
    }
    if ops.is_empty()
        || (!merging
            && ops
                .iter()
                .any(|op| !matches!(string(op, "type"), "append_evidence" | "anchor_blocks")))
    {
        return Ok(Some("retrieval_eval_projection_changed"));
    }
    let cards = super::proposals::cards(proposal);
    if cards.is_empty() {
        return Ok(Some("retrieval_eval_projection_changed"));
    }
    // Backstop for a card that gained a lineage link after tiering.
    if super::proposal_tiers::lineage_review(tx, proposal)? {
        return Ok(Some("retrieval_eval_lineage_exposed"));
    }
    // Search answers from the published graph. The scan above reads the live
    // store, which no longer shows an edge its owner has since dropped.
    let published_lineage = |target: &Json| {
        live.snapshot
            .as_ref()
            .is_some_and(|s| s.in_lineage(string(target, "scope"), string(target, "card_id")))
    };
    if merging {
        if cards.iter().any(|target| published_lineage(target)) {
            return Ok(Some("retrieval_eval_lineage_exposed"));
        }
        return super::merge_gate::hold(tx, proposal, live);
    }
    for target in cards {
        let scope = string(target, "scope");
        let id = string(target, "card_id");
        if published_lineage(target) {
            return Ok(Some("retrieval_eval_lineage_exposed"));
        }
        let rows = tx.query(&format!("SELECT c.revision,c.content_hash,c.trust,b.bytes,c.writer_class,{},c.state,c.writer FROM memory_cards c JOIN memory_blobs b ON b.content_hash=c.content_hash WHERE c.scope=?1 AND c.card_id=?2", super::index::ORIGIN_AUTOMATED_SQL), &[ops::t(scope),ops::t(id)])?;
        let Some(row) = rows.first() else {
            return Ok(Some("retrieval_eval_target_drifted"));
        };
        let (Value::Integer(revision), Value::Blob(bytes)) = (&row[0], &row[3]) else {
            return Ok(Some("retrieval_eval_projection_changed"));
        };
        if target["base_revision"] != *revision || target["before_hash"] != ops::s(&row[1]) {
            return Ok(Some("retrieval_eval_target_drifted"));
        }
        // An apply also rewrites the head's writer and writer class to the
        // applier's. Push and connector search admit an agent-unreviewed head
        // only for the credential that wrote it, so a different applier would
        // move the card in or out of a connector's results. The digest cannot
        // see that, so it is held here.
        if ops::s(&row[2]) == "agent-unreviewed"
            && (ops::s(&row[7]) != live.principal || ops::s(&row[4]) != applier)
        {
            return Ok(Some("retrieval_eval_writer_changed"));
        }
        // An apply rewrites the card's writer class to the applier's. Source
        // demotion reads the result, so project it as it will be afterwards.
        let origin = matches!(row[5], Value::Integer(n) if n != 0);
        let before = super::index::Card {
            scope: scope.into(),
            card_id: id.into(),
            revision: *revision,
            content_hash: ops::s(&row[1]),
            trust: ops::s(&row[2]),
            text: String::from_utf8_lossy(bytes).into_owned(),
            automated: ops::s(&row[4]) == "principal" || origin,
        };
        let after = super::index::Card {
            content_hash: string(target, "after_hash").into(),
            text: string(target, "after_text").into(),
            automated: applier == "principal" || origin,
            ..before.clone()
        };
        if super::index::ranking_digest(&before, ops::s(&row[6]) == "live")
            != super::index::ranking_digest(&after, target["after_state"] == "live")
        {
            return Ok(Some("retrieval_eval_projection_changed"));
        }
    }
    Ok(None)
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

pub(super) fn expires<T: ReadTx + ?Sized>(tx: &T, a: &Authority) -> rusqlite::Result<Option<i64>> {
    let mode = if a
        .retrieval
        .as_ref()
        .is_some_and(|r| r.embedder_id.is_some())
    {
        "hybrid"
    } else {
        "lexical"
    };
    let rows=tx.query("SELECT evaluated_ms FROM memory_retrieval_evals WHERE mode=?1 ORDER BY record_seq DESC LIMIT 1",&[ops::t(mode)])?;
    Ok(rows.first().and_then(|r| {
        if let Value::Integer(ms) = r[0] {
            Some(ms.saturating_add(MAX_AGE_MS))
        } else {
            None
        }
    }))
}

/// Exact candidate binding shared by review-readable inputs and apply checks.
pub(super) fn live_targets(proposal: &Json, include_text: bool) -> Json {
    json!(super::proposals::cards(proposal).iter().map(|c| {
        let mut target = json!({"scope":c["scope"],"card_id":c["card_id"],
            "before_hash":c["before_hash"],"after_hash":c["after_hash"],"after_state":c["after_state"]});
        if include_text { target["after_text"] = c["after_text"].clone(); }
        target
    }).collect::<Vec<_>>())
}
