//! Immutable evidence records. Full namespace, bytes and credential are bound.
use super::{
    Reply,
    auth::Credential,
    ids,
    ops::{self, Authority, NewEvent, Prepared},
    proposals::{self, string},
    scan::{Scan, Scanner},
};
use crate::writer::{ReadTx, WriteTx};
use base64::{Engine, engine::general_purpose::STANDARD};
use rusqlite::types::Value;
use serde_json::{Value as Json, json};
use std::collections::{BTreeMap, BTreeSet};

pub const DDL: &str = "CREATE TABLE memory_evidence(
 evidence_id TEXT PRIMARY KEY, scope TEXT NOT NULL, card_id TEXT NOT NULL,
 workspace_id TEXT NOT NULL, principal TEXT NOT NULL, credential_id TEXT NOT NULL,
 credential_class TEXT NOT NULL, artifact_kind TEXT NOT NULL, artifact_sha256 TEXT NOT NULL,
 artifact_bytes BLOB NOT NULL, observed_ms INTEGER NOT NULL, stance TEXT NOT NULL,
 scan_state TEXT NOT NULL, created_ms INTEGER NOT NULL
) WITHOUT ROWID;
CREATE INDEX memory_evidence_target ON memory_evidence(scope,card_id,workspace_id);
CREATE TRIGGER memory_evidence_no_update BEFORE UPDATE ON memory_evidence
 BEGIN SELECT RAISE(ABORT,'memory_evidence is immutable'); END;
CREATE TRIGGER memory_evidence_no_delete BEFORE DELETE ON memory_evidence
 BEGIN SELECT RAISE(ABORT,'memory_evidence is immutable'); END;";
pub fn component(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-:".contains(&b))
}
/// Domain-separated, non-secret audit identity. Never expose the token digest.
pub fn credential_id(class: &str, principal: &str, digest: &str) -> String {
    ids::sha256_hex(
        format!("brigade.evidence.credential.v1/{class}/{principal}/{digest}").as_bytes(),
    )
}
pub fn identity(who: &Credential, authority: &Authority) -> String {
    match who {
        Credential::Admin => authority.admin_credential.clone(),
        Credential::Node { digest, .. } | Credential::Principal { digest, .. } => {
            credential_id(who.class(), who.principal(), digest)
        }
    }
}
#[derive(Clone, Debug)]
pub struct Attachment {
    pub request: Json,
    pub bytes: Vec<u8>,
}
pub fn prepare(request: &Json, who: &Credential, now: i64) -> Result<Attachment, Reply> {
    let bad = || {
        ops::error(
            422,
            "memory_evidence_invalid",
            "invalid evidence attachment",
        )
    };
    let workspace = string(request, "workspace_id");
    let id = string(request, "evidence_id");
    let parts: Vec<_> = id.split('/').collect();
    if parts.len() != 3
        || parts[0] != who.principal()
        || parts[1] != workspace
        || !component(workspace)
        || !component(parts[2])
        || !ids::valid_scope(string(request, "scope"))
        || !ids::valid_card_id(string(request, "card_id"))
        || !component(string(request, "artifact_kind"))
        || request
            .get("stance")
            .is_some_and(|v| !matches!(v.as_str(), Some("support" | "contrary" | "negative")))
    {
        return Err(bad());
    }
    if request["observed_at"]
        .as_i64()
        .is_none_or(|t| t < 0 || t > now)
    {
        return Err(bad());
    }
    let encoded = request["artifact_base64"]
        .as_str()
        .filter(|s| s.len() <= 87384)
        .ok_or_else(bad)?;
    let bytes = STANDARD.decode(encoded).map_err(|_| bad())?;
    if bytes.len() > 65536 || ids::sha256_hex(&bytes) != string(request, "artifact_sha256") {
        return Err(bad());
    }
    Ok(Attachment {
        request: request.clone(),
        bytes,
    })
}
pub fn attach(
    tx: &dyn WriteTx,
    p: &Prepared,
    a: &Attachment,
    authority: &Authority,
    scanner: &dyn Scanner,
) -> rusqlite::Result<Reply> {
    let r = &a.request;
    let scope = string(r, "scope");
    if !proposals::scoped_right(tx, &p.credential, scope, "reflect")? {
        return Ok(ops::error(
            403,
            "memory_grant_missing",
            "evidence requires read and reflect on the target scope",
        ));
    }
    // Revalidate even direct callers at the transaction boundary.
    let checked = match prepare(r, &p.credential, p.now_ms) {
        Ok(a) => a,
        Err(reply) => return Ok(reply),
    };
    if ops::head(tx, scope, string(r, "card_id"))?.is_none() {
        return Ok(ops::error(404, "memory_card_not_found", "card not found"));
    }
    if !tx
        .query(
            "SELECT 1 FROM memory_evidence WHERE evidence_id=?1",
            &[ops::t(string(r, "evidence_id"))],
        )?
        .is_empty()
    {
        return Ok(ops::error(
            409,
            "memory_evidence_conflict",
            "evidence id already exists",
        ));
    }
    let scan = std::str::from_utf8(&checked.bytes)
        .ok()
        .map(|s| scanner.scan(s))
        .unwrap_or(Scan::Unavailable);
    if matches!(scan, Scan::Blocked { .. } | Scan::Failed) {
        return Ok(ops::error(
            422,
            "memory_evidence_scan_refused",
            "evidence scan refused",
        ));
    }
    let key = identity(&p.credential, authority);
    let stance = r["stance"].as_str().unwrap_or("support");
    tx.execute(
        "INSERT INTO memory_evidence VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14)",
        &[
            ops::t(string(r, "evidence_id")),
            ops::t(scope),
            ops::t(string(r, "card_id")),
            ops::t(string(r, "workspace_id")),
            ops::t(p.credential.principal()),
            ops::t(&key),
            ops::t(p.credential.class()),
            ops::t(string(r, "artifact_kind")),
            ops::t(string(r, "artifact_sha256")),
            Value::Blob(checked.bytes.clone()),
            Value::Integer(r["observed_at"].as_i64().unwrap()),
            ops::t(stance),
            ops::t(scan.state()),
            Value::Integer(p.now_ms),
        ],
    )?;
    let payload = json!({"evidence_id":r["evidence_id"],"workspace_id":r["workspace_id"],"artifact_kind":r["artifact_kind"],"artifact_sha256":r["artifact_sha256"],"observed_at":r["observed_at"],"credential_id":key,"stance":stance,"scan_state":scan.state()});
    ops::append(
        tx,
        p,
        NewEvent {
            kind: "evidence.attach",
            scope: Some(scope),
            card_id: Some(string(r, "card_id")),
            changes_head: false,
            base_revision: None,
            content_hash: None,
            trust: None,
            origin: None,
            payload: payload.clone(),
            policy_digest: authority.policy.as_ref().map(|a| a.1.as_str()),
        },
    )?;
    Ok(Reply::new(201, payload))
}
#[derive(Default)]
pub struct Proof {
    pub hashes: BTreeMap<String, String>,
    pub credentials: BTreeSet<String>,
    pub contrary: bool,
    pub complete: bool,
}
pub fn resolve<T: ReadTx + ?Sized>(
    tx: &T,
    body: &Json,
    authority: &Authority,
    scanner: &dyn Scanner,
    now: i64,
) -> rusqlite::Result<Proof> {
    let mut proof = Proof::default();
    let workspace = string(body, "workspace_id");
    let Some(window) = authority.policy_document["memory"]["evidence_freshness_seconds"]
        .as_i64()
        .filter(|w| (1..=31536000).contains(w))
    else {
        return Ok(proof);
    };
    if !component(workspace) {
        return Ok(proof);
    }
    proof.complete = true;
    for card in proposals::cards(body) {
        let refs = match super::proposal_metadata::list(string(card, "after_text"), "evidence") {
            Ok(refs) if refs.len() <= 64 => refs,
            _ => {
                proof.complete = false;
                continue;
            }
        };
        let rows=tx.query("SELECT evidence_id,principal,credential_id,artifact_sha256,artifact_bytes,observed_ms,stance,scan_state,credential_class FROM memory_evidence WHERE scope=?1 AND card_id=?2 AND workspace_id=?3 LIMIT 65",&[ops::t(string(card,"scope")),ops::t(string(card,"card_id")),ops::t(workspace)])?;
        if rows.len() > 64 {
            proof.complete = false;
            continue;
        }
        for row in rows {
            let id = ops::s(&row[0]);
            let principal = ops::s(&row[1]);
            let key = ops::s(&row[2]);
            let observed = if let Value::Integer(n) = row[5] {
                n
            } else {
                continue;
            };
            if observed > now
                || observed < now.saturating_sub(window * 1000)
                || !authority.valid_credentials.contains(&key)
            {
                continue;
            }
            let who = match ops::s(&row[8]).as_str() {
                "admin" => Credential::Admin,
                "node" => Credential::Node {
                    id: principal.clone(),
                    digest: String::new(),
                },
                "principal" => Credential::Principal {
                    id: principal.clone(),
                    digest: String::new(),
                },
                _ => continue,
            };
            if !proposals::scoped_right(tx, &who, string(card, "scope"), "reflect")? {
                continue;
            }
            // Contrary evidence holds even when not named in the proposal.
            if matches!(ops::s(&row[6]).as_str(), "contrary" | "negative") {
                proof.contrary = true;
                continue;
            }
            let allowed = authority.policy_document["memory"]["reflect_principals"]
                .as_array()
                .is_some_and(|a| a.iter().any(|v| v.as_str() == Some(&principal)));
            let Value::Blob(bytes) = &row[4] else {
                continue;
            };
            if !refs.contains(&id)
                || !allowed
                || ops::s(&row[7]) != "clean"
                || ids::sha256_hex(bytes) != ops::s(&row[3])
            {
                continue;
            }
            if !std::str::from_utf8(bytes).ok().is_some_and(
                |s| matches!(scanner.scan(s),Scan::Clean{ruleset} if !ruleset.is_empty()),
            ) {
                continue;
            }
            // Never normalize or alias a namespace to another workspace.
            let prefix = format!("{principal}/{workspace}/");
            if !id.strip_prefix(&prefix).is_some_and(component) {
                continue;
            }
            proof.hashes.insert(id, ops::s(&row[3]));
            proof.credentials.insert(key);
        }
    }
    Ok(proof)
}
