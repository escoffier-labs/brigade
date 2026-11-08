//! Verifier principals and the Tier 2 floor (phase-memory-verify D4).
//!
//! `memory_verifiers` holds the principal record's flag. Only the admin
//! `verifier` action writes it, and no row is ever deleted, so the flag
//! outlives revocation of every credential and the removal of the principal's
//! hub row: the row is the tombstone, and the same principal id comes back as
//! a verifier until an admin clears it.
//!
//! `memory_verifier_credentials` holds each credential's copy of the flag,
//! keyed by the credential identity that proposals already record. Native
//! registration binds the copy through the memory writer before the hub
//! issues the credential, and setting or clearing the flag rewrites every
//! copy of the principal in the same transaction. A missing principal row is
//! unflagged and so is a missing copy, so a credential issued on a path that
//! did not bind one is refused for a verifier principal until an admin sets
//! the flag again, which binds every live credential.
//!
//! Python never opens the memory file (D7), so after a committed flag change
//! the writer publishes the flagged principal ids to a sidecar file while it
//! still holds the hub lock. The Python policy writer reads that file inside
//! its own hub write transaction and refuses a `reflect_principals` entry
//! that names one of them.
use super::{
    Reply,
    attestation::credential_id,
    auth::Credential,
    ids,
    ops::{Authority, NewEvent, Prepared, append, error, s, t},
};
use crate::writer::{ReadTx, WriteTx};
use rusqlite::types::Value;
use serde_json::{Value as Json, json};
use std::path::{Path, PathBuf};

pub const DDL: &str = "CREATE TABLE IF NOT EXISTS memory_verifiers(
  principal TEXT PRIMARY KEY,
  verifier INTEGER NOT NULL CHECK(verifier IN (0,1)),
  updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS memory_verifier_credentials(
  credential TEXT PRIMARY KEY,
  principal TEXT NOT NULL,
  verifier INTEGER NOT NULL CHECK(verifier IN (0,1)),
  bound_at TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS memory_verifier_credentials_principal ON memory_verifier_credentials(principal);
";

/// The capability `status` advertises once the hub enforces the floor.
pub const CAPABILITY: &str = "verifier_floor";
/// Beside the memory file. Holds only principal ids, never a credential.
pub const SIDECAR: &str = "fleet-hub-memory-verifiers.json";
pub const SIDECAR_SCHEMA: &str = "brigade.memory-verifiers.v1";

fn one<T: ReadTx + ?Sized>(tx: &T, sql: &str, key: &str) -> rusqlite::Result<bool> {
    Ok(tx
        .query(sql, &[t(key)])?
        .first()
        .is_some_and(|r| r[0] == Value::Integer(1)))
}

/// The principal record's flag. No row means unflagged.
pub fn flagged<T: ReadTx + ?Sized>(tx: &T, principal: &str) -> rusqlite::Result<bool> {
    one(
        tx,
        "SELECT verifier FROM memory_verifiers WHERE principal=?1",
        principal,
    )
}

/// The identity a credential's copy is stored under, the same one proposals
/// record as `proposer_credential`.
pub fn credential(class: &str, principal: &str, digest: &str) -> String {
    credential_id(class, principal, digest)
}

/// Every request refuses a credential whose copy differs from the principal
/// record. The admin token is not a principal and carries no flag.
pub fn check<T: ReadTx + ?Sized>(tx: &T, who: &Credential) -> rusqlite::Result<Option<Reply>> {
    let digest = match who {
        Credential::Admin => return Ok(None),
        Credential::Node { digest, .. } | Credential::Principal { digest, .. } => digest,
    };
    let principal = who.principal();
    let copy = one(
        tx,
        "SELECT verifier FROM memory_verifier_credentials WHERE credential=?1",
        &credential(who.class(), principal, digest),
    )?;
    if copy == flagged(tx, principal)? {
        return Ok(None);
    }
    Ok(Some(error(
        403,
        "memory_verifier_flag_mismatch",
        "this credential's verifier flag differs from its principal's. An admin must set the flag again or issue a new credential",
    )))
}

/// Membership in the current policy's `reflect_principals`.
pub fn listed(policy_document: &Json, principal: &str) -> bool {
    policy_document["memory"]["reflect_principals"]
        .as_array()
        .is_some_and(|items| items.iter().any(|v| v.as_str() == Some(principal)))
}

/// The admin `verifier` action. Sets or clears the flag on the principal
/// record and on every copy the principal holds, binds a copy for each live
/// hub credential that has none, and logs one `grant.change` event, all in
/// the caller's single memory transaction under the hub lock.
pub fn set(
    tx: &dyn WriteTx,
    p: &Prepared,
    authority: &Authority,
    principal: &str,
    verifier: bool,
) -> rusqlite::Result<Reply> {
    if verifier && listed(&authority.policy_document, principal) {
        return Ok(error(
            409,
            "memory_policy_verifier_listed",
            "the current fleet policy lists this principal in memory.reflect_principals. Remove it from the policy before flagging it",
        ));
    }
    let stamp = ids::isoformat(p.now_ms);
    let flag = Value::Integer(i64::from(verifier));
    tx.execute(
        "INSERT INTO memory_verifiers(principal,verifier,updated_at,updated_by) VALUES(?1,?2,?3,?4) ON CONFLICT(principal) DO UPDATE SET verifier=excluded.verifier,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
        &[t(principal), flag.clone(), t(&stamp), t(p.credential.principal())],
    )?;
    tx.execute(
        "UPDATE memory_verifier_credentials SET verifier=?2 WHERE principal=?1",
        &[t(principal), flag.clone()],
    )?;
    let mut live = 0;
    for (class, owner, digest) in &authority.live {
        if owner == principal {
            live += 1;
            tx.execute(
                "INSERT INTO memory_verifier_credentials(credential,principal,verifier,bound_at) VALUES(?1,?2,?3,?4) ON CONFLICT(credential) DO UPDATE SET verifier=excluded.verifier",
                &[t(&credential(class, owner, digest)), t(principal), flag.clone(), t(&stamp)],
            )?;
        }
    }
    let change = json!({"principal": principal, "verifier": verifier});
    append(
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
            payload: change.clone(),
            policy_digest: None,
        },
    )?;
    Ok(Reply::new(
        200,
        json!({"verifier": change, "live_credentials": live}),
    ))
}

/// Bind the copy for a credential native registration is about to issue.
/// `requested` is the enrollment request's own `verifier` field: a request
/// for an unflagged credential of a verifier principal is refused, and so is
/// a request that would flag a credential the principal record does not.
pub fn bind(
    tx: &dyn WriteTx,
    p: &Prepared,
    principal: &str,
    class: &str,
    digest: &str,
    requested: Option<bool>,
) -> rusqlite::Result<Reply> {
    let verifier = flagged(tx, principal)?;
    match requested {
        Some(false) if verifier => {
            return Ok(error(
                409,
                "memory_verifier_credential_unflagged",
                "this principal is a verifier, so every credential it holds carries the flag. Nothing was issued",
            ));
        }
        Some(true) if !verifier => {
            return Ok(error(
                409,
                "memory_verifier_flag_mismatch",
                "this principal is not a verifier. Set the flag with the memory verifier action first. Nothing was issued",
            ));
        }
        _ => {}
    }
    tx.execute(
        "INSERT INTO memory_verifier_credentials(credential,principal,verifier,bound_at) VALUES(?1,?2,?3,?4) ON CONFLICT(credential) DO UPDATE SET principal=excluded.principal,verifier=excluded.verifier",
        &[
            t(&credential(class, principal, digest)),
            t(principal),
            Value::Integer(i64::from(verifier)),
            t(&ids::isoformat(p.now_ms)),
        ],
    )?;
    Ok(Reply::new(200, json!({"verifier": verifier})))
}

/// Flagged principal ids, sorted.
pub fn principals<T: ReadTx + ?Sized>(tx: &T) -> rusqlite::Result<Vec<String>> {
    Ok(tx
        .query(
            "SELECT principal FROM memory_verifiers WHERE verifier=1 ORDER BY principal",
            &[],
        )?
        .iter()
        .map(|r| s(&r[0]))
        .collect())
}

pub fn sidecar(memory: &Path) -> PathBuf {
    memory.with_file_name(SIDECAR)
}

/// Replace the sidecar with `principals`. A temporary file is renamed over
/// the old one, so a reader sees the old list or the new one. The writer
/// calls this after a committed flag change, before it releases the hub lock,
/// and once at startup. A stale file after a crash is replaced at the next
/// start.
pub fn publish(memory: &Path, principals: &[String]) -> std::io::Result<()> {
    use std::io::Write;
    let path = sidecar(memory);
    let temp = path.with_extension(format!("json.{}.tmp", std::process::id()));
    let body = json!({"schema": SIDECAR_SCHEMA, "verifiers": principals}).to_string();
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let written = options
        .open(&temp)
        .and_then(|mut f| f.write_all(body.as_bytes()));
    if let Err(e) = written.and_then(|()| std::fs::rename(&temp, &path)) {
        let _ = std::fs::remove_file(&temp);
        return Err(e);
    }
    Ok(())
}
