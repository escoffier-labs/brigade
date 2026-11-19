//! Admin-authenticated evaluation admissions. Workspace files carry no authority.
use super::{
    Reply,
    auth::Credential,
    ids,
    ops::{self, Authority, NewEvent, Prepared, error, s, t},
    proposals::{self, string},
};
use crate::writer::{ReadTx, WriteTx};
use serde_json::{Value as Json, json};

pub const DDL: &str = "CREATE TABLE IF NOT EXISTS memory_verify_evals(
 seq INTEGER PRIMARY KEY, principal TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS memory_verify_evals_latest ON memory_verify_evals(principal,seq DESC);
CREATE TRIGGER IF NOT EXISTS memory_verify_evals_no_update BEFORE UPDATE ON memory_verify_evals
 BEGIN SELECT RAISE(ABORT,'memory_verify_evals is immutable'); END;
CREATE TRIGGER IF NOT EXISTS memory_verify_evals_no_delete BEFORE DELETE ON memory_verify_evals
 BEGIN SELECT RAISE(ABORT,'memory_verify_evals is immutable'); END;";

pub fn latest<T: ReadTx + ?Sized>(tx: &T, principal: &str) -> rusqlite::Result<Json> {
    Ok(tx
        .query(
            "SELECT body FROM memory_verify_evals WHERE principal=?1 ORDER BY seq DESC LIMIT 1",
            &[t(principal)],
        )?
        .first()
        .and_then(|r| serde_json::from_str(&s(&r[0])).ok())
        .unwrap_or(Json::Null))
}
fn hash(value: &Json) -> bool {
    value.as_str().is_some_and(|s| {
        s.len() == 64
            && s.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}
pub fn record(
    tx: &dyn WriteTx,
    p: &Prepared,
    request: &Json,
    authority: &Authority,
) -> rusqlite::Result<Reply> {
    let principal = string(request, "principal");
    let r = &request["record"];
    let policy_raw = string(request, "policy");
    let policy: Json = serde_json::from_str(policy_raw).unwrap_or(Json::Null);
    if p.credential != Credential::Admin {
        return Ok(error(
            403,
            "memory_grant_missing",
            "only the authenticated evaluator admin may publish admission",
        ));
    }
    if !ids::valid_principal(principal)
        || !super::verifier::flagged(tx, principal)?
        || request["base_epoch"] != ops::epoch(tx)?
        || authority.policy.is_none()
        || r["schema"] != "brigade.memory-verify-decision.v1"
        || !hash(&r["binding_sha256"])
        || !hash(&r["policy_sha256"])
        || r["policy_sha256"] != ids::sha256_hex(policy_raw.as_bytes())
        || policy["auto_apply"].as_bool().is_none()
        || policy["max_age_days"]
            .as_i64()
            .is_none_or(|n| !(1..=30).contains(&n))
        || r["classes"].as_object().is_none_or(|c| c.len() != 5)
        || ["confirm", "correct", "history", "supersede", "no-change"]
            .iter()
            .any(|name| {
                !matches!(
                    r["classes"][name]["decision"].as_str(),
                    Some("go" | "no-go")
                )
            })
        || request.to_string().len() > 512 * 1024
    {
        return Ok(error(
            422,
            "memory_verify_eval_invalid",
            "invalid evaluator admission or epoch",
        ));
    }
    let body = json!({"record":r,"policy":policy,"principal":principal,"evaluator":"admin",
        "created_ms":p.now_ms,"base_epoch":ops::epoch(tx)?,
        "evaluator_credential":authority.admin_credential,
        "hub_policy_digest":authority.policy.as_ref().map(|a| &a.1),
        "hub_policy_revision":authority.policy.as_ref().map(|a| a.0)});
    let seq = ops::append(
        tx,
        p,
        NewEvent {
            kind: "grant.change",
            scope: None,
            card_id: None,
            changes_head: false,
            base_revision: None,
            content_hash: None,
            trust: None,
            origin: None,
            payload: json!({"principal":principal,"binding_sha256":r["binding_sha256"]}),
            policy_digest: None,
        },
    )?;
    tx.execute(
        "INSERT INTO memory_verify_evals(seq,principal,body) VALUES(?1,?2,?3)",
        &[
            rusqlite::types::Value::Integer(seq),
            t(principal),
            t(&body.to_string()),
        ],
    )?;
    Ok(Reply::new(201, json!({"verify_evaluation":body})))
}

/// Recognize only _manifest_op's exact trailing shape and its authenticated binding.
/// Other evidence appends, including mixed ids or duplicate bindings, remain corrections.
/// `binding_state` is `pending_review` before apply and `active` once the
/// verifier proposal holding the binding has applied.
fn manifest_binding(
    tx: &dyn WriteTx,
    body: &Json,
    ops: &[Json],
    binding_state: &str,
) -> rusqlite::Result<bool> {
    let Some((binding, corrections)) = ops.split_last().filter(|(_, rest)| !rest.is_empty()) else {
        return Ok(false);
    };
    let first = &corrections[0];
    let Some(evidence) = binding["evidence_ids"]
        .as_array()
        .filter(|ids| ids.len() == 1)
    else {
        return Ok(false);
    };
    let Some(id) = evidence[0].as_str() else {
        return Ok(false);
    };
    if *binding
        != json!({"type":"append_evidence","scope":first["scope"],
        "card_id":first["card_id"],"base_revision":first["base_revision"],
        "block_id":first["block_id"],"expected_block_hash":first["expected_block_hash"],
        "evidence_ids":[id]})
    {
        return Ok(false);
    }
    let rows = tx.query(
        "SELECT b.evidence_id FROM memory_evidence_bindings b JOIN memory_evidence e USING(evidence_id)
         WHERE b.evidence_id=?1 AND b.proposal_id=?2 AND b.principal=?3
         AND b.state=?4 AND b.purged_ms IS NULL
         AND b.scope=?5 AND b.base_revision=?6 AND e.card_id=?7
         AND e.principal=b.principal AND e.scope=b.scope AND e.tombstoned=0
         AND e.artifact_kind='memory-verify-manifest'",
        &[t(id), t(string(body,"proposal_id")), t(string(body,"proposer")), t(binding_state),
          t(string(first,"scope")), rusqlite::types::Value::Integer(first["base_revision"].as_i64().unwrap_or(0)),
          t(string(first,"card_id"))],
    )?;
    Ok(!rows.is_empty())
}

fn has_manifest_binding(tx: &dyn WriteTx, body: &Json, ops: &[Json]) -> rusqlite::Result<bool> {
    manifest_binding(tx, body, ops, "pending_review")
}

/// The ops class admission owns. A verifier's trailing authenticated manifest
/// binding is an attach, not a content class, so admission exempts it.
pub(super) fn content_ops<'a>(
    tx: &dyn WriteTx,
    body: &Json,
    ops: &'a [Json],
) -> rusqlite::Result<&'a [Json]> {
    Ok(if has_manifest_binding(tx, body, ops)? {
        &ops[..ops.len() - 1]
    } else {
        ops
    })
}

/// The same exemption for an applied verifier proposal, whose binding has
/// settled to `active`. Compensation holds its content classes only.
pub(super) fn applied_content_ops<'a>(
    tx: &dyn WriteTx,
    body: &Json,
    ops: &'a [Json],
) -> rusqlite::Result<&'a [Json]> {
    Ok(
        if body["verifier"] == true && manifest_binding(tx, body, ops, "active")? {
            &ops[..ops.len() - 1]
        } else {
            ops
        },
    )
}

/// Invoked under the writer transaction immediately before any card head changes.
pub fn check(
    tx: &dyn WriteTx,
    p: &Prepared,
    body: &Json,
    authority: &Authority,
) -> rusqlite::Result<Option<Reply>> {
    let ops::Action::Proposal(cmd) = &p.action else {
        unreachable!()
    };
    let admission = latest(tx, p.credential.principal())?;
    let r = &admission["record"];
    let policy = &admission["policy"];
    let memory = &authority.policy_document["memory"];
    let killed = tx
        .query(
            "SELECT value FROM memory_meta WHERE key='auto_apply_enabled'",
            &[],
        )?
        .first()
        .is_some_and(|r| s(&r[0]) != "true");
    let ops = body["ops"].as_array().map(Vec::as_slice).unwrap_or(&[]);
    let op_types: Vec<_> = ops.iter().map(|op| op["type"].clone()).collect();
    let corrections = content_ops(tx, body, ops)?;
    let class = |op: &Json| match op.as_str() {
        Some("append_evidence") => "confirm",
        Some("replace_block") => "correct",
        Some("add_previously") => "history",
        Some("supersede_card") => "supersede",
        _ => "invalid",
    };
    let age = admission["created_ms"]
        .as_i64()
        .map(|ms| p.now_ms.saturating_sub(ms));
    let reason = if memory["curation_owner"] != "hub" {
        Some("owner_not_hub")
    } else if memory["auto_apply"] != true || killed || policy["auto_apply"] != true {
        Some("auto_apply_disabled")
    } else if body["verifier"] != true
        || !super::verifier::flagged(tx, p.credential.principal())?
        || string(body, "proposer") != p.credential.principal()
        || !authority
            .valid_credentials
            .contains(string(body, "proposer_credential"))
    {
        Some("verifier_authority_invalid")
    } else if admission["evaluator"] != "admin"
        || admission["evaluator_credential"] != authority.admin_credential
        || admission["base_epoch"] != ops::epoch(tx)?
        || admission["hub_policy_digest"] != json!(authority.policy.as_ref().map(|a| &a.1))
        || admission["hub_policy_revision"] != json!(authority.policy.as_ref().map(|a| a.0))
        || cmd.request["binding_sha256"] != r["binding_sha256"]
        || cmd.request["policy_sha256"] != r["policy_sha256"]
        || age.is_none_or(|age| {
            age < 0 || age > policy["max_age_days"].as_i64().unwrap_or(0) * 86_400_000
        })
    {
        Some("verify_admission_missing_or_stale")
    } else if corrections.is_empty()
        || cmd.request["expected_op_types"] != json!(op_types)
        || corrections
            .iter()
            .any(|op| r["classes"][class(&op["type"])]["decision"] != "go")
    {
        Some("verify_class_not_admitted")
    } else if body["policy_digest"] != json!(authority.policy.as_ref().map(|a| &a.1))
        || body["policy_revision"] != json!(authority.policy.as_ref().map(|a| a.0))
    {
        Some("policy_changed")
    } else {
        proposals::stale_reason(tx, body)?
    };
    Ok(reason.map(|reason| error(409, "memory_verify_auto_held", reason)))
}
