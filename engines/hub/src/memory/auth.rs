//! Credentials and memory grants. Token digests are compared in constant time
//! and never logged or echoed.
use super::{ADMIN, Reply};
use crate::writer::ReadTx;
use rusqlite::types::Value;
use std::collections::BTreeSet;
use subtle::ConstantTimeEq;

pub const RIGHTS: &[&str] = &[
    "read",
    "write",
    "handoff",
    "embed",
    "reflect",
    "review",
    "import",
    "list_proposals",
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
    // repository scopes and their own private scope. A `collection:` scope is
    // no one's default: an imported collection opens only to a grant naming
    // it. A message principal holds only what the admin grants.
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
/// One principal's grants: (scope pattern, rights).
type Grants = Vec<(String, BTreeSet<String>)>;
fn grant_rows<T: ReadTx + ?Sized>(tx: &T, principal: &str) -> rusqlite::Result<Grants> {
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
/// Whether a grant pattern covers a scope. The wildcards cover only scopes a
/// node already reads by default: `repo:*` the repository scopes, `*` those
/// and `fleet`. Neither covers a private `principal:` scope or a `collection:`
/// scope. Each of those needs a grant on the exact scope, and a `principal:`
/// grant is only accepted on the holder's own private scope.
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
    if let Some(answer) = without_grants(credential, scope, right) {
        return Ok(answer);
    }
    Ok(granted(&grant_rows(tx, principal)?, scope, right))
}
/// The answer for a scope when the grant rows cannot change it.
fn without_grants(credential: &Credential, scope: &str, right: &str) -> Option<bool> {
    if let Some(owner) = scope.strip_prefix("principal:") {
        if owner != credential.principal() {
            return Some(false);
        }
    }
    defaults(credential, scope).contains(&right).then_some(true)
}
fn granted(grants: &Grants, scope: &str, right: &str) -> bool {
    grants
        .iter()
        .any(|(pattern, rights)| covers(pattern, scope) && rights.contains(right))
}

/// Per-request cache of readable scopes, for filtering a page of the log or
/// a search's corpus. The grant rows are read at most once per request.
pub struct Readable<'a, T: ReadTx + ?Sized> {
    tx: &'a T,
    credential: &'a Credential,
    seen: std::collections::HashMap<String, bool>,
    grants: Option<Grants>,
}
impl<'a, T: ReadTx + ?Sized> Readable<'a, T> {
    pub fn new(tx: &'a T, credential: &'a Credential) -> Self {
        Self {
            tx,
            credential,
            seen: Default::default(),
            grants: None,
        }
    }
    pub fn scope(&mut self, scope: &str) -> rusqlite::Result<bool> {
        if let Some(v) = self.seen.get(scope) {
            return Ok(*v);
        }
        let v = match (
            self.credential,
            without_grants(self.credential, scope, "read"),
        ) {
            (Credential::Admin, _) => true,
            (_, Some(v)) => v,
            (_, None) => {
                if self.grants.is_none() {
                    self.grants = Some(grant_rows(self.tx, self.credential.principal())?);
                }
                granted(self.grants.as_ref().unwrap(), scope, "read")
            }
        };
        self.seen.insert(scope.into(), v);
        Ok(v)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;

    struct Counting {
        db: rusqlite::Connection,
        grant_reads: Cell<usize>,
    }
    impl ReadTx for Counting {
        fn query(&self, sql: &str, params: &[Value]) -> rusqlite::Result<Vec<Vec<Value>>> {
            if sql.contains("memory_grants") {
                self.grant_reads.set(self.grant_reads.get() + 1);
            }
            let mut statement = self.db.prepare(sql)?;
            let width = statement.column_count();
            let rows = statement.query_map(rusqlite::params_from_iter(params), |row| {
                (0..width).map(|i| row.get::<_, Value>(i)).collect()
            })?;
            rows.collect()
        }
    }

    #[test]
    fn readable_reads_the_grant_rows_once_per_request() {
        let db = rusqlite::Connection::open_in_memory().unwrap();
        db.execute_batch(
            "CREATE TABLE memory_grants(principal TEXT, scope TEXT, rights TEXT);
             INSERT INTO memory_grants VALUES('agent:dot','repo:keep','read'),('agent:dot','repo:*','write');",
        )
        .unwrap();
        let tx = Counting {
            db,
            grant_reads: Cell::new(0),
        };
        let credential = Credential::Principal {
            id: "agent:dot".into(),
            digest: "d".into(),
        };
        let mut readable = Readable::new(&tx, &credential);
        let mut read = 0;
        for n in 0..5000 {
            read += readable.scope(&format!("repo:r{n}")).unwrap() as usize;
        }
        assert!(readable.scope("repo:keep").unwrap());
        assert!(!readable.scope("principal:other").unwrap());
        assert!(!readable.scope("fleet").unwrap());
        assert_eq!((read, tx.grant_reads.get()), (0, 1));
        // The cached answers agree with the per-call check.
        for scope in ["repo:keep", "repo:r7", "fleet", "principal:agent:dot"] {
            assert_eq!(
                Readable::new(&tx, &credential).scope(scope).unwrap(),
                has_right(&tx, &credential, Some(scope), "read").unwrap(),
                "{scope}"
            );
        }
    }

    fn grants(rows: &str) -> Counting {
        let db = rusqlite::Connection::open_in_memory().unwrap();
        db.execute_batch(&format!(
            "CREATE TABLE memory_grants(principal TEXT, scope TEXT, rights TEXT);{rows}"
        ))
        .unwrap();
        Counting {
            db,
            grant_reads: Cell::new(0),
        }
    }

    #[test]
    fn collection_scopes_open_only_to_a_grant_that_names_them() {
        let node = Credential::Node {
            id: "node-1".into(),
            digest: "n".into(),
        };
        let agent = Credential::Principal {
            id: "agent:dot".into(),
            digest: "d".into(),
        };
        let all = RIGHTS.join(",");
        let vault = "collection:vault/cerebro";
        let right = |tx: &Counting, who: &Credential, scope: &str, r: &str| {
            has_right(tx, who, Some(scope), r).unwrap()
        };
        let reads = |tx: &Counting, who: &Credential, scope: &str| {
            let cached = Readable::new(tx, who).scope(scope).unwrap();
            assert_eq!(cached, right(tx, who, scope, "read"), "{scope}");
            cached
        };

        // No grant rows: a node keeps its fleet and repository defaults and
        // gets nothing on a collection.
        let none = grants("");
        assert!(right(&none, &node, "fleet", "write") && reads(&none, &node, "repo:x"));
        for r in RIGHTS {
            assert!(!right(&none, &node, vault, r), "{r}");
            assert!(!right(&none, &agent, vault, r), "{r}");
        }
        assert!(!reads(&none, &node, vault) && !reads(&none, &agent, vault));

        // Wildcard grants carrying every right still never match a collection.
        let wild = grants(&format!(
            "INSERT INTO memory_grants VALUES
               ('node-1','*','{all}'),('node-1','repo:*','{all}'),
               ('agent:dot','*','{all}'),('agent:dot','repo:*','{all}');"
        ));
        for who in [&node, &agent] {
            assert!(right(&wild, who, "repo:x", "import") && right(&wild, who, "fleet", "embed"));
            for r in RIGHTS {
                assert!(!right(&wild, who, vault, r), "{r}");
            }
            assert!(!reads(&wild, who, vault));
        }

        // A grant naming the collection opens exactly its rights, on exactly it.
        let named = grants(&format!(
            "INSERT INTO memory_grants VALUES
               ('node-1','{vault}','import,read'),('agent:dot','{vault}','read');"
        ));
        assert!(reads(&named, &node, vault) && right(&named, &node, vault, "import"));
        assert!(!right(&named, &node, vault, "write") && !right(&named, &node, vault, "embed"));
        assert!(reads(&named, &agent, vault) && !right(&named, &agent, vault, "import"));
        assert!(!reads(&named, &node, "collection:other"));
        assert!(!reads(&named, &agent, "collection:vault"));

        // The admin token needs no grant.
        for r in RIGHTS {
            assert!(right(&none, &Credential::Admin, vault, r), "{r}");
        }
        assert!(reads(&none, &Credential::Admin, vault));
    }
}
