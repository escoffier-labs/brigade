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

/// Only hub snapshots supply the inverse. Revisions also reject edit-and-revert.
pub fn exact<T: ReadTx + ?Sized>(tx: &T, body: &Json) -> rusqlite::Result<bool> {
    let Some(id) = body["rollback_of"].as_str() else {
        return Ok(false);
    };
    let Some(original) = proposals::load(tx, id)? else {
        return Ok(false);
    };
    if original["state"] != "applied"
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

/// A class stays held across policy changes. A later explicit switch-on is the go record.
pub fn class_held<T: ReadTx + ?Sized>(tx: &T, body: &Json) -> rusqlite::Result<bool> {
    for op in body["ops"].as_array().into_iter().flatten() {
        if !tx
            .query(
                "SELECT 1 FROM memory_meta WHERE key=?1",
                &[t(&format!("compensation_hold:{}", string(op, "type")))],
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
        matches!((before[*key].as_f64(),after[*key].as_f64()),(Some(b),Some(a)) if a.is_finite() && b.is_finite() && a < b)
    })
}

fn retrieval_trip<T: ReadTx + ?Sized>(
    tx: &T,
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
    for row in tx.query("SELECT report_json,record_seq FROM memory_retrieval_evals WHERE mode=?1 AND record_seq>?2 AND evaluated_ms>?3 AND evaluated_ms<=?4 ORDER BY record_seq LIMIT 128", &[t(&s(&row[1])),Value::Integer(seq),Value::Integer(applied),Value::Integer(now)])? {
        let after: Json = serde_json::from_str(&s(&row[0])).unwrap_or(Json::Null);
        let a = &after["adapters"][adapter];
        // Compare the same workload, scope, epoch, ranking and scorer. Corpus may change.
        let compatible = before["fixture_digest"] == after["fixture_digest"] && before["k"] == after["k"]
            && ["scope","log_epoch","ranking","scorer_version","embedder_id","rrf_weights","policy_digest"].iter().all(|key| b["binding"][*key] == a["binding"][*key]);
        if compatible && a["available"] == true && (regresses(&b["overall"],&a["overall"]) || b["by_category"].as_object().is_some_and(|cats| cats.iter().any(|(key,v)| regresses(v,&a["by_category"][key])))) {
            return Ok(Some(json!({"trigger":"retrieval-regression","record_seq":if let Value::Integer(n)=row[1]{n}else{0}})))
        }
    }
    Ok(None)
}

fn trigger<T: ReadTx + ?Sized>(
    tx: &T,
    body: &Json,
    applied: i64,
    now: i64,
    negatives: &Json,
    signal_scope: &str,
) -> rusqlite::Result<Option<Json>> {
    if let Some(trip) = retrieval_trip(tx, body, applied, now)? {
        return Ok(Some(trip));
    }
    for card in proposals::cards(body) {
        for row in negatives.as_array().into_iter().flatten() {
            if card["scope"] == signal_scope
                && row["card_id"] == card["card_id"]
                && row["observed_ms"]
                    .as_i64()
                    .is_some_and(|ms| ms > applied && ms <= now)
            {
                return Ok(Some(
                    json!({"trigger":"negative-outcome","evidence_id":row["evidence_id"]}),
                ));
            }
        }
        // Pending verifier manifests count as a watch signal without becoming positive proof.
        let rows = tx.query("SELECT e.artifact_bytes,e.artifact_sha256 FROM memory_evidence e JOIN memory_evidence_bindings b USING(evidence_id) WHERE e.scope=?1 AND e.card_id=?2 AND b.base_revision=?3 AND e.observed_ms>?4 AND e.observed_ms<=?5 AND e.tombstoned=0 AND e.scan_state='clean' AND e.artifact_kind='memory-verify-manifest' ORDER BY e.created_ms DESC LIMIT 128", &[t(string(card,"scope")),t(string(card,"card_id")),Value::Integer(card["applied_revision"].as_i64().unwrap_or(0)),Value::Integer(applied),Value::Integer(now)])?;
        for row in rows {
            let Value::Blob(bytes) = &row[0] else {
                continue;
            };
            if ids::sha256_hex(bytes) != s(&row[1]) {
                continue;
            }
            let manifest: Json = serde_json::from_slice(bytes).unwrap_or(Json::Null);
            if manifest["card"]["scope"] != card["scope"]
                || manifest["card"]["card_id"] != card["card_id"]
            {
                continue;
            }
            if manifest["blocks"].as_array().is_some_and(|blocks| {
                blocks.iter().any(|b| {
                    b["verdict"] == "contradicted"
                        && body["ops"].as_array().is_some_and(|ops| {
                            ops.iter().any(|op| {
                                op["card_id"] == card["card_id"] && op["block_id"] == b["block_id"]
                            })
                        })
                })
            }) {
                return Ok(Some(
                    json!({"trigger":"contradicted","evidence_sha256":s(&row[1])}),
                ));
            }
        }
    }
    // Add a canary-miss evidence resolver here when canaries have an attested contract.
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
        let Some(trip) = trigger(
            tx,
            &body,
            applied,
            p.now_ms,
            &request["negative_outcomes"],
            scope,
        )?
        else {
            continue;
        };
        for op in body["ops"].as_array().into_iter().flatten() {
            tx.execute("INSERT INTO memory_meta(key,value) VALUES(?1,?2) ON CONFLICT(key) DO UPDATE SET value=excluded.value", &[t(&format!("compensation_hold:{}",string(op,"type"))),t(&trip.to_string())])?;
        }
        body["watch_trip"] = trip.clone();
        tx.execute(
            "INSERT INTO memory_meta(key,value) VALUES(?1,?2)",
            &[t(&format!("compensation_watch:{id}")), t(&trip.to_string())],
        )?;
        let mut result = proposals::compensate(tx, p, &body, a, scanner)?;
        result.body["rollback_of"] = json!(id);
        result.body["trigger"] = trip["trigger"].clone();
        results.push(json!({"rollback_of":id,"proposal_id":result.body["proposal_id"],"trigger":trip["trigger"],"result":if result.body["state"]=="applied" {"applied"} else {"failed"},"reason":result.body["reason"]}));
    }
    Ok(Reply::new(
        200,
        json!({"results":results,"next_after":if rows.len()>32 {rows.get(31).map(|r|s(&r[0]))} else {None}}),
    ))
}
