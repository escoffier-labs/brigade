//! Exact policy-actor reversals and bounded post-apply watches.
use super::{
    Reply,
    auth::Credential,
    ids,
    ops::{self, Authority, Prepared, s, t},
    proposals::{self, string},
    scan::Scanner,
};
use crate::writer::{ReadTx, WriteTx};
use rusqlite::types::Value;
use serde_json::{Value as Json, json};

pub const CAPABILITY: &str = "automatic_compensation";
const DEFAULT_WINDOW: i64 = 86_400;
const METRIC_TOLERANCE: f64 = 0.01;

/// Only hub snapshots supply the inverse. Revisions also reject edit-and-revert.
pub fn exact<T: ReadTx + ?Sized>(tx: &T, body: &Json) -> rusqlite::Result<bool> {
    let Some(id) = body["rollback_of"].as_str() else {
        return Ok(false);
    };
    let Some(original) = proposals::load(tx, id)? else {
        return Ok(false);
    };
    if body["watch_trip"]["review_required"] == true
        || original["state"] != "applied"
        || original["decision_mode"] != "automatic"
        || !original["rollback_of"].is_null()
        || original["proposer"] != body["proposer"]
        || original["compacted"] == true
    {
        return Ok(false);
    }
    let decider = tx.query(
        "SELECT decided_by FROM memory_proposals WHERE proposal_id=?1",
        &[t(id)],
    )?;
    if decider
        .first()
        .is_none_or(|r| s(&r[0]) != string(body, "proposer"))
    {
        return Ok(false);
    }
    if body["ops"].as_array().is_none_or(|ops| !ops.is_empty()) {
        return Ok(false);
    }
    let expected = proposals::rollback(&original);
    let cards = proposals::cards(body);
    if cards.is_empty() || cards.len() != proposals::cards(&expected).len() {
        return Ok(false);
    }
    let mut seen = std::collections::BTreeSet::new();
    for card in cards {
        if !seen.insert((string(card, "scope"), string(card, "card_id"))) {
            return Ok(false);
        }
        let Some(want) = proposals::cards(&expected)
            .iter()
            .find(|c| c["scope"] == card["scope"] && c["card_id"] == card["card_id"])
        else {
            return Ok(false);
        };
        for key in [
            "base_revision",
            "before_hash",
            "before_text",
            "before_state",
            "before_trust",
            "after_hash",
            "after_text",
            "after_state",
            "restore_trust",
            "pins",
        ] {
            if card[key] != want[key] {
                return Ok(false);
            }
        }
        if ids::sha256_hex(string(card, "after_text").as_bytes()) != string(card, "after_hash") {
            return Ok(false);
        }
    }
    Ok(proposals::stale_reason(tx, body)?.is_none())
}

pub fn actor(body: &Json, who: &Credential, a: &Authority) -> bool {
    string(body, "proposer") == who.principal()
        && a.policy_document["memory"]["curation_owner"] == "hub"
        && a.valid_credentials
            .contains(string(body, "proposer_credential"))
        && a.policy_document["memory"]["reflect_principals"]
            .as_array()
            .is_some_and(|ps| ps.iter().any(|p| p.as_str() == Some(who.principal())))
}

pub fn hold_key(scope: &str, op_type: &str) -> String {
    format!("compensation_hold:{scope}/{op_type}")
}

pub fn hold(tx: &dyn WriteTx, original: &Json) -> rusqlite::Result<()> {
    for op in original["ops"].as_array().into_iter().flatten() {
        tx.execute("INSERT INTO memory_meta(key,value) VALUES(?1,?2) ON CONFLICT(key) DO UPDATE SET value=excluded.value", &[t(&hold_key(string(op,"scope"), string(op,"type"))), t(string(original,"proposal_id"))])?;
    }
    Ok(())
}

/// Automatic reversals obey the same operator stop controls as forward writes.
pub fn automatic_blocker<T: ReadTx + ?Sized>(
    tx: &T,
    a: &Authority,
) -> rusqlite::Result<Option<&'static str>> {
    if a.policy_document["memory"]["auto_apply"] != true {
        return Ok(Some("auto_apply_disabled"));
    }
    Ok(tx
        .query(
            "SELECT value FROM memory_meta WHERE key='auto_apply_enabled'",
            &[],
        )?
        .first()
        .is_some_and(|r| s(&r[0]) != "true")
        .then_some("auto_apply_killed"))
}

pub fn in_window<T: ReadTx + ?Sized>(
    tx: &T,
    original: &Json,
    a: &Authority,
    now: i64,
) -> rusqlite::Result<bool> {
    let window = a.policy_document["memory"]
        .get("rollback_watch_seconds")
        .map(|v| v.as_i64().filter(|s| (1..=604_800).contains(s)))
        .unwrap_or(Some(DEFAULT_WINDOW));
    let Some(window) = window else {
        return Ok(false);
    };
    Ok(tx.query("SELECT terminal_ms FROM memory_proposals WHERE proposal_id=?1", &[t(string(original,"proposal_id"))])?
        .first().is_some_and(|r| matches!(r[0],Value::Integer(ms) if ms <= now && ms >= now.saturating_sub(window*1000))))
}

/// A class stays held until an admin releases this exact scope and operation.
pub fn class_held<T: ReadTx + ?Sized>(tx: &T, body: &Json) -> rusqlite::Result<bool> {
    for op in body["ops"].as_array().into_iter().flatten() {
        if !tx
            .query(
                "SELECT 1 FROM memory_meta WHERE key=?1",
                &[t(&hold_key(string(op, "scope"), string(op, "type")))],
            )?
            .is_empty()
        {
            return Ok(true);
        }
    }
    Ok(false)
}

fn regresses(before: &Json, after: &Json) -> bool {
    ["hit_rate","mrr","precision_at_k","recall_at_k"].iter().any(|key| {
        matches!((before[*key].as_f64(),after[*key].as_f64()),(Some(b),Some(a)) if a.is_finite() && b.is_finite() && b - a > METRIC_TOLERANCE)
    })
}

fn retrieval_trip(
    tx: &dyn WriteTx,
    original: &Json,
    applied: i64,
    now: i64,
) -> rusqlite::Result<Option<Json>> {
    let Some(seq) = original["preflight"]["retrieval_eval_record"]["record_seq"].as_i64() else {
        return Ok(None);
    };
    let rows = tx.query(
        "SELECT report_json,mode FROM memory_retrieval_evals WHERE record_seq=?1",
        &[Value::Integer(seq)],
    )?;
    let Some(row) = rows.first() else {
        return Ok(None);
    };
    let before: Json = serde_json::from_str(&s(&row[0])).unwrap_or(Json::Null);
    let adapter = if s(&row[1]) == "lexical" {
        "hub-lexical"
    } else {
        "hub-hybrid"
    };
    let b = &before["adapters"][adapter];
    let cursor_key = format!("compensation_retrieval:{}", string(original, "proposal_id"));
    let cursor = tx
        .query(
            "SELECT value FROM memory_meta WHERE key=?1",
            &[t(&cursor_key)],
        )?
        .first()
        .and_then(|r| s(&r[0]).parse::<i64>().ok())
        .unwrap_or(seq);
    for row in tx.query("SELECT report_json,record_seq FROM memory_retrieval_evals WHERE mode=?1 AND record_seq>?2 AND evaluated_ms>?3 AND evaluated_ms<=?4 ORDER BY record_seq LIMIT 128", &[t(&s(&row[1])),Value::Integer(cursor),Value::Integer(applied),Value::Integer(now)])? {
        let Value::Integer(record_seq) = row[1] else { continue };
        tx.execute("INSERT INTO memory_meta(key,value) VALUES(?1,?2) ON CONFLICT(key) DO UPDATE SET value=excluded.value", &[t(&cursor_key), t(&record_seq.to_string())])?;
        let after: Json = serde_json::from_str(&s(&row[0])).unwrap_or(Json::Null);
        let a = &after["adapters"][adapter];
        let compatible = before["fixture_digest"] == after["fixture_digest"] && before["k"] == after["k"]
            && ["scope","log_epoch","ranking","scorer_version","embedder_id","rrf_weights","policy_digest"].iter().all(|key| b["binding"][*key] == a["binding"][*key]);
        if compatible && a["available"] == true && (regresses(&b["overall"],&a["overall"]) || b["by_category"].as_object().is_some_and(|cats| cats.iter().any(|(key,v)| regresses(v,&a["by_category"][key])))) {
            // Every corpus mutation since the evaluated baseline must be this apply.
            // Include edits followed by reverts and trust-only revisions.
            let baseline = b["binding"]["indexed_seq"].as_i64();
            let unattributed = if let Some(baseline) = baseline {
                !tx.query("SELECT 1 FROM memory_events WHERE scope=?1 AND revision IS NOT NULL AND seq>?2 AND seq<?3 AND (kind!='proposal.apply' OR COALESCE(json_extract(payload,'$.proposal_id'),'')!=?4) LIMIT 1", &[t(string(&b["binding"],"scope")),Value::Integer(baseline),Value::Integer(record_seq),t(string(original,"proposal_id"))])?.is_empty()
            } else { true };
            // Dense corpus changes also include vector-only mutations. Until a
            // counterfactual evaluator exists, these regressions require review.
            return Ok(Some(json!({"trigger":"retrieval-regression","record_seq":record_seq,"review_required":unattributed || adapter == "hub-hybrid"})));
        }
    }
    Ok(None)
}

/// Only immutable, active verifier attestations can authorize a watch signal.
fn trigger(
    tx: &dyn WriteTx,
    body: &Json,
    applied: i64,
    now: i64,
    request: &Json,
    authority: &Authority,
    scanner: &dyn Scanner,
) -> rusqlite::Result<Option<Json>> {
    if let Some(trip) = retrieval_trip(tx, body, applied, now)? {
        return Ok(Some(trip));
    }
    for card in proposals::cards(body) {
        let scope = string(card, "scope");
        let rows = tx.query("SELECT e.evidence_id,e.artifact_bytes,e.artifact_sha256,e.principal,e.credential_id,e.observed_ms,e.credential_class FROM memory_evidence e JOIN memory_evidence_bindings b USING(evidence_id) JOIN memory_verifier_credentials v ON v.credential=e.credential_id AND v.principal=e.principal AND v.verifier=1 JOIN memory_verifiers p ON p.principal=e.principal AND p.verifier=1 WHERE e.scope=?1 AND e.card_id=?2 AND b.base_revision=?3 AND b.state='active' AND e.observed_ms>?4 AND e.observed_ms<=?5 AND e.created_ms>=e.observed_ms AND e.tombstoned=0 AND e.scan_state='clean' AND e.artifact_kind='memory-verify-manifest' ORDER BY e.evidence_id", &[t(scope),t(string(card,"card_id")),Value::Integer(card["applied_revision"].as_i64().unwrap_or(0)),Value::Integer(applied),Value::Integer(now)])?;
        let mut principals = std::collections::BTreeSet::new();
        let mut evidence_ids = Vec::new();
        let mut kind = "contradicted";
        for row in rows {
            let Value::Blob(bytes) = &row[1] else {
                continue;
            };
            let principal = s(&row[3]);
            let credential = s(&row[4]);
            if !authority.valid_credentials.contains(&credential)
                || principal == string(body, "proposer")
                || ids::sha256_hex(bytes) != s(&row[2])
            {
                continue;
            }
            let who = match s(&row[6]).as_str() {
                "principal" => Credential::Principal {
                    id: principal.clone(),
                    digest: String::new(),
                },
                "node" => Credential::Node {
                    id: principal.clone(),
                    digest: String::new(),
                },
                _ => continue,
            };
            if !proposals::scoped_right(tx, &who, scope, "reflect")? {
                continue;
            }
            let Ok(text) = std::str::from_utf8(bytes) else {
                continue;
            };
            if !matches!(scanner.scan(text), super::scan::Scan::Clean{ruleset} if !ruleset.is_empty())
            {
                continue;
            }
            let manifest: Json = serde_json::from_slice(bytes).unwrap_or(Json::Null);
            if manifest["schema"] != "brigade.memory-verify-manifest.v1"
                || manifest["card"]["scope"] != card["scope"]
                || manifest["card"]["card_id"] != card["card_id"]
                || manifest["card"]["base_revision"] != card["applied_revision"]
            {
                continue;
            }
            let contradiction = manifest["blocks"].as_array().is_some_and(|blocks| {
                blocks.iter().any(|b| {
                    b["verdict"] == "contradicted"
                        && body["ops"].as_array().is_some_and(|ops| {
                            ops.iter().any(|op| {
                                op["scope"] == card["scope"]
                                    && op["card_id"] == card["card_id"]
                                    && op["block_id"] == b["block_id"]
                            })
                        })
                })
            });
            let failed = scope == string(request, "scope")
                && request["negative_outcomes"]
                    .as_array()
                    .is_some_and(|ids| ids.iter().any(|id| id.as_str() == Some(&s(&row[0]))))
                && manifest["effectiveness"]["status"] == "failed"
                && manifest["card"]["content_hash"] == card["after_hash"]
                && manifest["effectiveness"]["verifier_credential"] == credential
                && manifest["effectiveness"]["observed_ms"]
                    .as_i64()
                    .is_some_and(|ms| row[5] == Value::Integer(ms));
            if contradiction || failed {
                if failed {
                    kind = "negative-outcome";
                }
                principals.insert(principal);
                evidence_ids.push(s(&row[0]));
            }
        }
        if !principals.is_empty() {
            return Ok(Some(
                json!({"trigger":kind,"evidence_ids":evidence_ids,"review_required":principals.len()<2}),
            ));
        }
    }
    Ok(None)
}

pub fn watch(
    tx: &dyn WriteTx,
    p: &Prepared,
    request: &Json,
    a: &Authority,
    scanner: &dyn Scanner,
) -> rusqlite::Result<Reply> {
    let scope = string(request, "scope");
    if string(request, "base_epoch") != ops::epoch(tx)? {
        return Ok(ops::error(
            409,
            "memory_epoch_changed",
            "memory epoch changed",
        ));
    }
    let window = a.policy_document["memory"]
        .get("rollback_watch_seconds")
        .map(|v| v.as_i64().filter(|s| (1..=604_800).contains(s)))
        .unwrap_or(Some(DEFAULT_WINDOW));
    let Some(window) = window else {
        return Ok(ops::error(
            422,
            "memory_watch_window_invalid",
            "rollback watch window must be 1 through 604800 seconds",
        ));
    };
    let after = string(request, "after");
    let rows = tx.query("SELECT proposal_id,terminal_ms FROM memory_proposals WHERE principal=?1 AND state='applied' AND compacted=0 AND terminal_ms>=?2 AND proposal_id>?3 AND proposal_id IN (SELECT proposal_id FROM memory_proposal_scopes WHERE scope=?4) AND json_extract(body,'$.decision_mode')='automatic' AND json_extract(body,'$.rollback_of') IS NULL AND NOT EXISTS(SELECT 1 FROM memory_meta WHERE key='compensation_watch:' || memory_proposals.proposal_id) ORDER BY proposal_id LIMIT 33", &[t(p.credential.principal()),Value::Integer(p.now_ms.saturating_sub(window*1000)),t(after),t(scope)])?;
    let mut results = Vec::new();
    for row in rows.iter().take(32) {
        let id = s(&row[0]);
        let Some(mut body) = proposals::load(tx, &id)? else {
            continue;
        };
        if !actor(&body, &p.credential, a) {
            continue;
        }
        // Watch only proposals wholly visible to this caller.
        if proposals::authorize(
            tx,
            &p.credential,
            &json!({"action":"auto_apply","proposal_id":id}),
        )?
        .is_some()
        {
            continue;
        }
        let applied = if let Value::Integer(n) = row[1] {
            n
        } else {
            continue;
        };
        let Some(trip) = trigger(tx, &body, applied, p.now_ms, request, a, scanner)? else {
            continue;
        };
        body["watch_trip"] = trip.clone();
        tx.execute(
            "INSERT INTO memory_meta(key,value) VALUES(?1,?2)",
            &[t(&format!("compensation_watch:{id}")), t(&trip.to_string())],
        )?;
        let mut result = proposals::compensate(tx, p, &body, a, scanner)?;
        result.body["rollback_of"] = json!(id);
        result.body["trigger"] = trip["trigger"].clone();
        results.push(json!({"rollback_of":id,"proposal_id":result.body["proposal_id"],"trigger":trip["trigger"],"result":if result.body["state"]=="applied" {"applied"} else if trip["review_required"]==true && result.body["state"]=="pending" {"review_required"} else {"failed"},"reason":result.body["reason"]}));
    }
    Ok(Reply::new(
        200,
        json!({"results":results,"next_after":if rows.len()>32 {rows.get(31).map(|r|s(&r[0]))} else {None}}),
    ))
}
