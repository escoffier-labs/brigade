//! S19c diagnostic gates. Evidence strings never supply promotion authority.
use super::{
    Reply,
    auth::Credential,
    ids,
    ops::{self, Authority},
    proposals,
    scan::{Scan, Scanner},
};
use crate::writer::ReadTx;
use proposals::string;
use serde_json::{Value as Json, json};

fn check(checks: &mut Vec<Json>, gate: &str, reason: Option<&str>, state: &str) {
    let code = reason
        .map(String::from)
        .unwrap_or_else(|| format!("{gate}_satisfied"));
    let explanation = match code.as_str() {
        "evidence_authority_unbound" => {
            "The hub cannot attest evidence or contrary outcomes until S19e.".to_string()
        }
        "owner_authority_unbound" => {
            "The current policy has no curation-owner contract that authorizes promotion."
                .to_string()
        }
        "retrieval_eval_unbound" => {
            "No retrieval evaluation record is bound to the active index and policy revision."
                .to_string()
        }
        _ => format!("{}.", code.replace('_', " ")),
    };
    checks.push(json!({"gate":gate,"code":code,"state":if reason.is_none(){"satisfied"}else{state},"explanation":explanation}));
}
fn listed(memory: &Json, key: &str, value: &str) -> bool {
    memory[key]
        .as_array()
        .is_some_and(|items| items.iter().any(|v| v.as_str() == Some(value)))
}
/// Live trust and scan changes hold even an explicit reviewed apply. The
/// reviewer can repair an already flagged input if the resulting version scans.
pub fn input_blocker<T: ReadTx + ?Sized>(
    tx: &T,
    body: &Json,
) -> rusqlite::Result<Option<&'static str>> {
    for card in proposals::cards(body) {
        let Some(before_scan) = card["before_scan"].as_str() else {
            return Ok(Some("memory_input_scan_unbound"));
        };
        let Some(head) = ops::head(tx, string(card, "scope"), string(card, "card_id"))? else {
            return Ok(Some("card_missing"));
        };
        if head.trust != string(card, "before_trust") || head.state != string(card, "before_state")
        {
            return Ok(Some("memory_trust_changed"));
        }
        let rows = tx.query("SELECT scan_state FROM memory_card_revisions WHERE scope=?1 AND card_id=?2 AND revision=?3", &[
            ops::t(string(card, "scope")), ops::t(string(card, "card_id")), rusqlite::types::Value::Integer(head.revision)
        ])?;
        if rows.first().is_none_or(|r| ops::s(&r[0]) != before_scan) {
            return Ok(Some("memory_input_scan_held"));
        }
    }
    Ok(None)
}
pub fn evaluate<T: ReadTx + ?Sized>(
    tx: &T,
    body: &Json,
    authority: &Authority,
    scanner: &dyn Scanner,
    now: i64,
) -> rusqlite::Result<Json> {
    let cards = proposals::cards(body);
    let ops = body["ops"].as_array().map(Vec::as_slice).unwrap_or(&[]);
    let memory = &authority.policy_document["memory"];
    let mut checks = Vec::new();
    let policy_reason = if authority.policy.is_none() {
        Some("policy_missing")
    } else if memory.get("auto_apply").is_some_and(|v| !v.is_boolean()) {
        Some("policy_invalid")
    } else if ops.is_empty()
        || ops
            .iter()
            .any(|op| !listed(memory, "tier0_op_classes", string(op, "type")))
    {
        Some("class_disabled")
    } else if !listed(memory, "reflect_principals", string(body, "proposer")) {
        Some("reflect_principal_disabled")
    } else {
        None
    };
    check(&mut checks, "policy", policy_reason, "held");
    check(
        &mut checks,
        "auto_apply_enabled",
        (memory["auto_apply"] != true).then_some("auto_apply_disabled"),
        "held",
    );
    let switched_off = tx
        .query(
            "SELECT value FROM memory_meta WHERE key='auto_apply_enabled'",
            &[],
        )?
        .first()
        .is_some_and(|r| ops::s(&r[0]) != "true");
    check(
        &mut checks,
        "kill_switch",
        switched_off.then_some("auto_apply_killed"),
        "held",
    );
    check(
        &mut checks,
        "proposer_credential",
        (!authority
            .valid_credentials
            .contains(string(body, "proposer_credential")))
        .then_some("proposer_credential_invalid"),
        "held",
    );
    let tier = super::proposal_tiers::classify(tx, body, scanner)?;
    check(
        &mut checks,
        "candidate",
        (tier != 0).then_some("tier_requires_review"),
        "held",
    );
    let stale = proposals::stale_reason(tx, body)?;
    let binding = body["policy_revision"] != json!(authority.policy.as_ref().map(|a| a.0))
        || body["policy_digest"] != json!(authority.policy.as_ref().map(|a| &a.1));
    check(
        &mut checks,
        "snapshot",
        stale.or(binding.then_some("policy_changed")),
        "held",
    );
    let changed: Vec<_> = cards
        .iter()
        .filter(|c| c["before_text"] != c["after_text"] || c["before_state"] != c["after_state"])
        .collect();
    check(
        &mut checks,
        "delta",
        if tier != 0 || changed.is_empty() {
            Some("delta_requires_review")
        } else if changed.len() > 1 {
            Some("blast_cap_exceeded")
        } else {
            None
        },
        "held",
    );
    let proof = super::attestation::resolve(tx, body, authority, scanner, now)?;
    check(
        &mut checks,
        "evidence_floor",
        if !proof.complete {
            Some("proof_binding_incomplete")
        } else {
            (proof.hashes.len() < 3).then_some("evidence_floor_unmet")
        },
        "held",
    );
    check(
        &mut checks,
        "source_group_floor",
        if !proof.complete {
            Some("proof_binding_incomplete")
        } else {
            (proof.principals.len() < 2).then_some("source_group_floor_unmet")
        },
        "held",
    );
    let valid_targets = body["ops"]
        .as_array()
        .is_some_and(|ops| ops.iter().all(|op| proposals::op_schema(op).is_ok()));
    check(
        &mut checks,
        "host",
        (!valid_targets).then_some("card_target_invalid"),
        "held",
    );
    let trust = input_blocker(tx, body)?;
    let mut known_trust = true;
    let mut clean = true;
    for card in cards {
        known_trust &= matches!(
            string(card, "before_trust"),
            "reviewed" | "imported" | "policy-promoted"
        );
        let rows = tx.query("SELECT scan_state FROM memory_card_revisions WHERE scope=?1 AND card_id=?2 AND revision=?3", &[
            ops::t(string(card,"scope")), ops::t(string(card,"card_id")), rusqlite::types::Value::Integer(card["base_revision"].as_i64().unwrap_or(0))
        ])?;
        clean &= rows.first().is_some_and(|r| ops::s(&r[0]) == "clean");
        for text in [string(card, "before_text"), string(card, "after_text")] {
            clean &= matches!(scanner.scan(text), Scan::Clean {ruleset} if !ruleset.is_empty());
        }
    }
    check(
        &mut checks,
        "scan",
        (!clean).then_some("scan_not_clean"),
        "held",
    );
    check(
        &mut checks,
        "card_trust",
        trust.or((!known_trust).then_some("card_trust_denied")),
        "held",
    );
    check(
        &mut checks,
        "evidence_authority",
        if !proof.complete {
            Some("evidence_authority_unbound")
        } else {
            proof.contrary.then_some("contrary_evidence")
        },
        "held",
    );
    check(
        &mut checks,
        "owner_authority",
        if memory["curation_owner"] == "hub" {
            None
        } else if memory.get("curation_owner").is_none() {
            Some("owner_authority_unbound")
        } else {
            Some("owner_not_hub")
        },
        "held",
    );
    check(
        &mut checks,
        "retrieval_eval",
        super::retrieval_eval::hold(tx, body, authority, now)?,
        "unverifiable",
    );
    // Match #2187's diagnostic defaults. History is read in the same snapshot
    // (and the apply transaction), never accepted from the candidate.
    let mut recent = false;
    for card in &changed {
        let rows = tx.query("SELECT 1 FROM memory_events WHERE scope=?1 AND card_id=?2 AND kind IN ('proposal.apply','proposal.rollback') AND revision IS NOT NULL AND created_at>?3 LIMIT 1", &[
            ops::t(string(card,"scope")),ops::t(string(card,"card_id")),ops::t(&ids::isoformat(now.saturating_sub(86_400_000)))
        ])?;
        recent |= !rows.is_empty();
    }
    check(
        &mut checks,
        "cooldown_history",
        recent.then_some("cooldown_active"),
        "held",
    );
    let sources: serde_json::Map<String, Json> = cards
        .iter()
        .map(|c| {
            (
                format!("{}/{}", string(c, "scope"), string(c, "card_id")),
                c["before_hash"].clone(),
            )
        })
        .collect();
    let delta = ids::sha256_hex(string(body, "diff").as_bytes());
    let candidate = ids::sha256_hex(
        ids::canonical(
            &json!({"ops":body["ops"],"cards":body["cards"],"proposer":body["proposer"]}),
        )
        .as_bytes(),
    );
    let mut report = json!({
        "schema":"brigade.memory-tier0-preflight.v1","status":"held","eligible":false,"diagnostic_only":true,
        "candidate_id":body["proposal_id"],"run_id":null,"policy_sha256":authority.policy.as_ref().map(|a| &a.1),
        "policy_limits":{"enabled_classes":memory["tier0_op_classes"].as_array().cloned().unwrap_or_default(),"reflect_principals":memory["reflect_principals"].as_array().cloned().unwrap_or_default(),"minimum_evidence_ids":3,"minimum_source_groups":2,"max_changed_paths":1,"cooldown_seconds":86400},
        "candidate_sha256":candidate,"source_sha256":sources,"config_digests":{},
        "workspace_fingerprint":null,"evidence_log_sha256":"incomplete","delta_sha256":delta,"changed_path_count":changed.len(),
        "proof_evidence_count":if proof.complete{json!(proof.hashes.len())}else{Json::Null},"proof_source_group_count":if proof.complete{json!(proof.principals.len())}else{Json::Null},"attesting_credentials":proof.credentials,"evidence_sha256":proof.hashes,"evaluation_sha256":null,"checks":checks
    });
    report["retrieval_eval_record"] = super::retrieval_eval::summary(tx, authority)?;
    let eval_expiry = super::retrieval_eval::expires(tx, authority)?;
    report["valid_until_ms"] = json!(
        proof
            .expires_ms
            .unwrap_or(now)
            .min(eval_expiry.unwrap_or(now))
    );
    // Bind the diagnostic facts, not an unverified proof or promotion permit.
    bind_evaluation(&mut report);
    Ok(report)
}
/// The route cannot authorize using a partial report or a caller report.
pub fn all_satisfied(report: &Json) -> bool {
    const GATES: &[&str] = &[
        "policy",
        "auto_apply_enabled",
        "kill_switch",
        "proposer_credential",
        "candidate",
        "snapshot",
        "delta",
        "evidence_floor",
        "source_group_floor",
        "host",
        "scan",
        "card_trust",
        "evidence_authority",
        "owner_authority",
        "retrieval_eval",
        "cooldown_history",
    ];
    report["checks"].as_array().is_some_and(|checks| {
        checks.len() == GATES.len()
            && GATES.iter().all(|gate| {
                checks.iter().any(|c| {
                    c["gate"] == *gate
                        && c["state"] == "satisfied"
                        && c["code"] == format!("{gate}_satisfied")
                })
            })
    })
}
fn bind_evaluation(report: &mut Json) {
    report["evaluation_sha256"] = Json::Null;
    report["evaluation_sha256"] = json!(ids::sha256_hex(ids::canonical(report).as_bytes()));
}
pub fn read<T: ReadTx + ?Sized>(
    tx: &T,
    who: &Credential,
    request: &Json,
    authority: &Authority,
    scanner: &dyn Scanner,
    now: i64,
) -> rusqlite::Result<Reply> {
    if let Some(r) = proposals::authorize(tx, who, request)? {
        return Ok(r);
    }
    let body = proposals::load(tx, string(request, "proposal_id"))?.unwrap();
    if body["compacted"] == true {
        return Ok(ops::error(
            409,
            "memory_proposal_compacted",
            "proposal body retention expired, immutable audit retained",
        ));
    }
    let mut report = evaluate(tx, &body, authority, scanner, now)?;
    redact(tx, who, &body, &mut report)?;
    Ok(Reply::new(200, report))
}

/// Bind only visible diagnostics on both fresh replies and cached replays.
pub(super) fn redact<T: ReadTx + ?Sized>(
    tx: &T,
    who: &Credential,
    body: &Json,
    report: &mut Json,
) -> rusqlite::Result<bool> {
    for card in proposals::cards(body) {
        if !proposals::scoped_right(tx, who, string(card, "scope"), "review")? {
            report["retrieval_eval_record"] = Json::Null;
            report["evidence_sha256"] = Json::Null;
            report["attesting_credentials"] = Json::Null;
            report["candidate_sha256"] = Json::Null;
            report["delta_sha256"] = Json::Null;
            report["policy_limits"]["reflect_principals"] = Json::Null;
            // The evaluation fingerprint must bind only the visible report.
            bind_evaluation(report);
            return Ok(true);
        }
    }
    Ok(false)
}
