//! Immutable evidence records. Full namespace, bytes and credential are bound.
use super::{
    Reply,
    auth::Credential,
    ids, manifest_binding as binding,
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
 scan_state TEXT NOT NULL, created_ms INTEGER NOT NULL,
 tombstoned INTEGER NOT NULL DEFAULT 0 CHECK(tombstoned IN (0,1))
) WITHOUT ROWID;
CREATE INDEX memory_evidence_target ON memory_evidence(scope,card_id,workspace_id);
CREATE TRIGGER memory_evidence_no_update BEFORE UPDATE ON memory_evidence
 WHEN OLD.tombstoned!=0 OR NEW.tombstoned!=1 OR NEW.artifact_bytes!=x''
 OR OLD.evidence_id IS NOT NEW.evidence_id OR OLD.scope IS NOT NEW.scope
 OR OLD.card_id IS NOT NEW.card_id OR OLD.workspace_id IS NOT NEW.workspace_id
 OR OLD.principal IS NOT NEW.principal OR OLD.credential_id IS NOT NEW.credential_id
 OR OLD.credential_class IS NOT NEW.credential_class OR OLD.artifact_kind IS NOT NEW.artifact_kind
 OR OLD.artifact_sha256 IS NOT NEW.artifact_sha256 OR OLD.observed_ms IS NOT NEW.observed_ms
 OR OLD.stance IS NOT NEW.stance OR OLD.scan_state IS NOT NEW.scan_state
 OR OLD.created_ms IS NOT NEW.created_ms
 BEGIN SELECT RAISE(ABORT,'memory_evidence is immutable'); END;
CREATE TRIGGER memory_evidence_no_delete BEFORE DELETE ON memory_evidence
 BEGIN SELECT RAISE(ABORT,'memory_evidence is immutable'); END;";
/// Version 5 is retained exactly for upgrades from older memory files.
pub fn legacy_ddl() -> String {
    let start = DDL
        .find("CREATE TRIGGER memory_evidence_no_update")
        .unwrap();
    let end = DDL
        .find("CREATE TRIGGER memory_evidence_no_delete")
        .unwrap();
    DDL.replace(&DDL[start..end],"CREATE TRIGGER memory_evidence_no_update BEFORE UPDATE ON memory_evidence BEGIN SELECT RAISE(ABORT,'memory_evidence is immutable'); END;\n")
        .replace(",\n tombstoned INTEGER NOT NULL DEFAULT 0 CHECK(tombstoned IN (0,1))", "")
}
pub fn tombstone_migration() -> String {
    let start = DDL
        .find("CREATE TRIGGER memory_evidence_no_update")
        .unwrap();
    let end = DDL
        .find("CREATE TRIGGER memory_evidence_no_delete")
        .unwrap();
    format!(
        "ALTER TABLE memory_evidence ADD COLUMN tombstoned INTEGER NOT NULL DEFAULT 0 CHECK(tombstoned IN (0,1)); DROP TRIGGER memory_evidence_no_update; {}",
        &DDL[start..end]
    )
}
const WORKSPACE_CAP: i64 = 64;
// Eight active records per principal and card across all workspaces reserves
// seven eighths of a full workspace bucket for other attesters.
const PRINCIPAL_CAP: i64 = 8;
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
    if bytes.len() > 65536
        || std::str::from_utf8(&bytes).is_err()
        || ids::sha256_hex(&bytes) != string(request, "artifact_sha256")
    {
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
    // D5: a verifier's record is a manifest for exactly this card.
    let manifest = if super::verifier::flagged(tx, p.credential.principal())? {
        match binding::manifest_card(r, &checked.bytes) {
            Ok(revision) => Some(revision),
            Err(reply) => return Ok(reply),
        }
    } else {
        None
    };
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
    let count = |sql: &str, params: &[Value]| -> rusqlite::Result<i64> {
        Ok(tx
            .query(sql, params)?
            .first()
            .and_then(|r| {
                if let Value::Integer(n) = r[0] {
                    Some(n)
                } else {
                    None
                }
            })
            .unwrap_or(i64::MAX))
    };
    let target = [
        ops::t(scope),
        ops::t(string(r, "card_id")),
        ops::t(string(r, "workspace_id")),
    ];
    if count(
        &format!(
            "SELECT count(*) FROM memory_evidence WHERE scope=?1 AND card_id=?2 AND workspace_id=?3 AND tombstoned=0{}",
            binding::OCCUPIES
        ),
        &target,
    )? >= WORKSPACE_CAP
    {
        return Ok(ops::error(
            409,
            "memory_evidence_workspace_cap",
            "workspace evidence capacity reached",
        ));
    }
    let principal = [
        ops::t(scope),
        ops::t(string(r, "card_id")),
        ops::t(p.credential.principal()),
    ];
    if count(
        &format!(
            "SELECT count(*) FROM memory_evidence WHERE scope=?1 AND card_id=?2 AND principal=?3 AND tombstoned=0{}",
            binding::OCCUPIES
        ),
        &principal,
    )? >= PRINCIPAL_CAP
    {
        return Ok(ops::error(
            409,
            "memory_evidence_principal_cap",
            "principal evidence capacity reached",
        ));
    }
    if manifest.is_some() {
        let size = checked.bytes.len() as i64;
        let principal = p.credential.principal();
        if let Some(r) = binding::retained_refusal(tx, principal, scope, size, &binding::BUDGET)? {
            return Ok(r);
        }
    }
    let text = std::str::from_utf8(&checked.bytes).unwrap();
    let scan = scanner.scan(text);
    if !matches!(&scan,Scan::Clean{ruleset} if !ruleset.is_empty()) {
        return Ok(ops::error(
            if matches!(scan, Scan::Unavailable) {
                503
            } else {
                422
            },
            "memory_evidence_scan_refused",
            "clean evidence scan required",
        ));
    }
    let key = identity(&p.credential, authority);
    let stance = r["stance"].as_str().unwrap_or("support");
    tx.execute(
        "INSERT INTO memory_evidence(evidence_id,scope,card_id,workspace_id,principal,credential_id,credential_class,artifact_kind,artifact_sha256,artifact_bytes,observed_ms,stance,scan_state,created_ms) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14)",
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
    if let Some(revision) = manifest {
        let size = checked.bytes.len() as i64;
        let id = string(r, "evidence_id");
        let principal = p.credential.principal();
        binding::record(tx, id, principal, scope, revision, size, p.now_ms)?;
    }
    let mut payload = json!({"evidence_id":r["evidence_id"],"workspace_id":r["workspace_id"],"artifact_kind":r["artifact_kind"],"artifact_sha256":r["artifact_sha256"],"observed_at":r["observed_at"],"credential_id":key,"stance":stance,"scan_state":scan.state()});
    if manifest.is_some() {
        payload["state"] = json!("pending_review");
    }
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
/// Purge keeps identity and audit metadata but clears the stored artifact.
pub fn purge(tx: &dyn WriteTx, p: &Prepared, id: &str) -> rusqlite::Result<Reply> {
    if p.credential != Credential::Admin {
        return Ok(ops::error(403, "memory_grant_missing", "admin required"));
    }
    let rows = tx.query(
        "SELECT scope,card_id,tombstoned FROM memory_evidence WHERE evidence_id=?1",
        &[ops::t(id)],
    )?;
    let Some(row) = rows.first() else {
        return Ok(ops::error(
            404,
            "memory_evidence_not_found",
            "evidence not found",
        ));
    };
    let payload = json!({"evidence_id":id,"tombstoned":true});
    if row[2] == Value::Integer(0) {
        tx.execute("UPDATE memory_evidence SET tombstoned=1,artifact_bytes=x'' WHERE evidence_id=?1 AND tombstoned=0",&[ops::t(id)])?;
        ops::append(
            tx,
            p,
            NewEvent {
                kind: "evidence.purge",
                scope: Some(&ops::s(&row[0])),
                card_id: Some(&ops::s(&row[1])),
                changes_head: false,
                base_revision: None,
                content_hash: None,
                trust: None,
                origin: None,
                payload: payload.clone(),
                policy_digest: None,
            },
        )?;
    }
    Ok(Reply::new(200, payload))
}
#[derive(Default)]
pub struct Proof {
    pub hashes: BTreeMap<String, String>,
    pub credentials: BTreeSet<String>,
    pub principals: BTreeSet<String>,
    pub contrary: bool,
    pub complete: bool,
    pub expires_ms: Option<i64>,
}
fn live<T: ReadTx + ?Sized>(
    tx: &T,
    row: &[Value],
    scope: &str,
    authority: &Authority,
    now: i64,
    window: i64,
) -> rusqlite::Result<bool> {
    // Shared cheap filters for contrary and supporting metadata rows.
    let observed = if let Value::Integer(n) = row[4] {
        n
    } else {
        return Ok(false);
    };
    if observed > now
        || observed < now.saturating_sub(window * 1000)
        || !authority.valid_credentials.contains(&ops::s(&row[2]))
    {
        return Ok(false);
    }
    let principal = ops::s(&row[1]);
    let who = match ops::s(&row[7]).as_str() {
        "admin" => Credential::Admin,
        "node" => Credential::Node {
            id: principal,
            digest: String::new(),
        },
        "principal" => Credential::Principal {
            id: principal,
            digest: String::new(),
        },
        _ => return Ok(false),
    };
    proposals::scoped_right(tx, &who, scope, "reflect")
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
        let scope = string(card, "scope");
        let card_id = string(card, "card_id");
        // Read metadata only. Contrary facts are card-wide across workspaces.
        let contrary=tx.query(&format!("SELECT evidence_id,principal,credential_id,artifact_sha256,observed_ms,stance,scan_state,credential_class FROM memory_evidence WHERE scope=?1 AND card_id=?2 AND tombstoned=0 AND stance IN ('contrary','negative'){} LIMIT 65", binding::COUNTS),&[ops::t(scope),ops::t(card_id)])?;
        if contrary.len() > 64 {
            proof.complete = false;
        }
        for row in contrary {
            if live(tx, &row, scope, authority, now, window)? {
                proof.contrary = true;
            }
        }
        if card["before_text"] == card["after_text"] && card["before_state"] == card["after_state"]
        {
            continue;
        }
        let refs = match (
            super::proposal_metadata::list(string(card, "after_text"), "evidence"),
            super::proposal_metadata::list(string(card, "before_text"), "evidence"),
        ) {
            (Ok(after), Ok(before)) if after.len() <= 64 => {
                after.difference(&before).cloned().collect::<BTreeSet<_>>()
            }
            _ => {
                proof.complete = false;
                continue;
            }
        };
        if refs.is_empty() {
            continue;
        }
        let rows=tx.query(&format!("SELECT evidence_id,principal,credential_id,artifact_sha256,observed_ms,stance,scan_state,credential_class FROM memory_evidence WHERE scope=?1 AND card_id=?2 AND workspace_id=?3 AND tombstoned=0 AND stance='support'{} LIMIT 65", binding::COUNTS),&[ops::t(scope),ops::t(card_id),ops::t(workspace)])?;
        if rows.len() > 64 {
            proof.complete = false;
            continue;
        }
        for row in rows {
            let id = ops::s(&row[0]);
            let principal = ops::s(&row[1]);
            let allowed = authority.policy_document["memory"]["reflect_principals"]
                .as_array()
                .is_some_and(|a| a.iter().any(|v| v.as_str() == Some(&principal)));
            let prefix = format!("{principal}/{workspace}/");
            if !refs.contains(&id)
                || !allowed
                || ops::s(&row[6]) != "clean"
                || !id.strip_prefix(&prefix).is_some_and(component)
                || !live(tx, &row, scope, authority, now, window)?
            {
                continue;
            }
            let bytes = tx.query(
                "SELECT artifact_bytes FROM memory_evidence WHERE evidence_id=?1 AND tombstoned=0",
                &[ops::t(&id)],
            )?;
            let Some(Value::Blob(bytes)) = bytes.first().and_then(|r| r.first()) else {
                continue;
            };
            if ids::sha256_hex(bytes) != ops::s(&row[3])
                || !std::str::from_utf8(bytes).ok().is_some_and(
                    |s| matches!(scanner.scan(s),Scan::Clean{ruleset} if !ruleset.is_empty()),
                )
            {
                continue;
            }
            let observed = if let Value::Integer(ms) = row[4] {
                ms
            } else {
                0
            };
            let expiry = observed.saturating_add(window * 1000);
            proof.expires_ms = Some(proof.expires_ms.map_or(expiry, |old| old.min(expiry)));
            proof.hashes.insert(id, ops::s(&row[3]));
            proof.credentials.insert(ops::s(&row[2]));
            if principal != string(body, "proposer") {
                proof.principals.insert(principal);
            }
        }
    }
    Ok(proof)
}
