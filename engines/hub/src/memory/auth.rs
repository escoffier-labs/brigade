//! Credentials and memory grants. Token digests are compared in constant time
//! and never logged or echoed.
use super::{ADMIN, Reply};
use crate::writer::ReadTx;
use rusqlite::types::Value;
use std::collections::BTreeSet;
use subtle::ConstantTimeEq;

pub const RIGHTS: &[&str] = &[
    "read", "write", "handoff", "embed", "reflect", "review", "import",
];

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Credential {
    Admin,
    Node { id: String, digest: String },
    Principal { id: String, digest: String },
}
impl Credential {
    pub fn principal(&self) -> &str {
        match self {
            Credential::Admin => ADMIN,
            Credential::Node { id, .. } | Credential::Principal { id, .. } => id,
        }
    }
    pub fn class(&self) -> &'static str {
        match self {
            Credential::Admin => "admin",
            Credential::Node { .. } => "node",
            Credential::Principal { .. } => "principal",
        }
    }
}

fn text(v: &Value) -> &str {
    if let Value::Text(s) = v { s } else { "" }
}
fn unauthorized(message: &str) -> Reply {
    Reply::new(401, serde_json::json!({"error": message}))
}

/// Resolve a bearer token's SHA-256 hex digest against the hub file. Used for
/// admission (on a hub reader) and again under the hub write lock (D23).
pub fn resolve<T: ReadTx + ?Sized>(
    hub: &T,
    digest: &str,
    admin: &str,
) -> rusqlite::Result<Result<Credential, Reply>> {
    if bool::from(digest.as_bytes().ct_eq(admin.as_bytes())) {
        return Ok(Ok(Credential::Admin));
    }
    let key = Value::Text(digest.into());
    let rows = hub.query(
        "SELECT node_id,token_sha256,revoked_at FROM nodes WHERE token_sha256=?1",
        std::slice::from_ref(&key),
    )?;
    if let Some(row) = rows
        .first()
        .filter(|r| bool::from(text(&r[1]).as_bytes().ct_eq(digest.as_bytes())))
    {
        return Ok(if row[2] == Value::Null {
            Ok(Credential::Node {
                id: text(&row[0]).into(),
                digest: digest.into(),
            })
        } else {
            Err(unauthorized("unauthorized: node token revoked"))
        });
    }
    let rows = hub.query(
        "SELECT principal_id,token_hash,revoked_at FROM fleet_message_principals WHERE token_hash=?1",
        &[key],
    )?;
    if let Some(row) = rows
        .first()
        .filter(|r| bool::from(text(&r[1]).as_bytes().ct_eq(digest.as_bytes())))
    {
        if row[2] == Value::Null {
            return Ok(Ok(Credential::Principal {
                id: text(&row[0]).into(),
                digest: digest.into(),
            }));
        }
    }
    Ok(Err(unauthorized("unauthorized")))
}

/// True when the credential that was valid at admission is still the same,
/// unrevoked credential. Runs under the hub write lock for every memory write.
pub fn still_valid<T: ReadTx + ?Sized>(
    hub: &T,
    credential: &Credential,
    admin: &str,
) -> rusqlite::Result<Result<(), Reply>> {
    let digest = match credential {
        Credential::Admin => return Ok(Ok(())),
        Credential::Node { digest, .. } | Credential::Principal { digest, .. } => digest,
    };
    Ok(match resolve(hub, digest, admin)? {
        Ok(current) if &current == credential => Ok(()),
        Ok(_) => Err(unauthorized("unauthorized")),
        Err(refusal) => Err(refusal),
    })
}

fn defaults(credential: &Credential, scope: &str) -> &'static [&'static str] {
    // Enrolled nodes are operator machines: they read and write the fleet and
    // repository scopes and their own private scope. A message principal holds
    // only what the admin grants.
    match credential {
        Credential::Node { id, .. }
            if scope == "fleet"
                || scope.starts_with("repo:")
                || scope.strip_prefix("principal:") == Some(id) =>
        {
            &["read", "write"]
        }
        _ => &[],
    }
}
fn grant_rows<T: ReadTx + ?Sized>(
    tx: &T,
    principal: &str,
) -> rusqlite::Result<Vec<(String, BTreeSet<String>)>> {
    Ok(tx
        .query(
            "SELECT scope,rights FROM memory_grants WHERE principal=?1",
            &[Value::Text(principal.into())],
        )?
        .iter()
        .map(|r| {
            (
                text(&r[0]).to_string(),
                text(&r[1])
                    .split(',')
                    .filter(|s| !s.is_empty())
                    .map(String::from)
                    .collect(),
            )
        })
        .collect())
}
/// Whether a grant pattern covers a scope. `*` and `repo:*` never cover a
/// private `principal:` scope; that needs a grant on the exact scope, and
/// grants are only accepted on the holder's own private scope.
fn covers(pattern: &str, scope: &str) -> bool {
    pattern == scope
        || (pattern == "repo:*" && scope.starts_with("repo:"))
        || (pattern == "*" && (scope == "fleet" || scope.starts_with("repo:")))
}

/// Read this inside the transaction that acts on it, so a grant change that
/// commits first is always seen.
pub fn has_right<T: ReadTx + ?Sized>(
    tx: &T,
    credential: &Credential,
    scope: Option<&str>,
    right: &str,
) -> rusqlite::Result<bool> {
    if *credential == Credential::Admin {
        return Ok(true);
    }
    if right == "admin" {
        return Ok(false);
    }
    let principal = credential.principal();
    let Some(scope) = scope else {
        // Unscoped actions need the right on some scope.
        let any_default =
            matches!(credential, Credential::Node { .. }) && matches!(right, "read" | "write");
        return Ok(any_default
            || grant_rows(tx, principal)?
                .iter()
                .any(|(_, rights)| rights.contains(right)));
    };
    if let Some(owner) = scope.strip_prefix("principal:") {
        if owner != principal {
            return Ok(false);
        }
    }
    if defaults(credential, scope).contains(&right) {
        return Ok(true);
    }
    Ok(grant_rows(tx, principal)?
        .iter()
        .any(|(pattern, rights)| covers(pattern, scope) && rights.contains(right)))
}

/// Per-request cache of readable scopes, for filtering a page of the log.
pub struct Readable<'a, T: ReadTx + ?Sized> {
    tx: &'a T,
    credential: &'a Credential,
    seen: std::collections::HashMap<String, bool>,
}
impl<'a, T: ReadTx + ?Sized> Readable<'a, T> {
    pub fn new(tx: &'a T, credential: &'a Credential) -> Self {
        Self {
            tx,
            credential,
            seen: Default::default(),
        }
    }
    pub fn scope(&mut self, scope: &str) -> rusqlite::Result<bool> {
        if let Some(v) = self.seen.get(scope) {
            return Ok(*v);
        }
        let v = has_right(self.tx, self.credential, Some(scope), "read")?;
        self.seen.insert(scope.into(), v);
        Ok(v)
    }
}
