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
    "read_stats",
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
        .map(|r| (text(&r[0]).to_string(), rights_set(&r[1])))
        .collect())
}
fn rights_set(v: &Value) -> BTreeSet<String> {
    text(v)
        .split(',')
        .filter(|s| !s.is_empty())
        .map(String::from)
        .collect()
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
/// Row caps for the reserved-scope computation. A status call must not scan
/// without bound, so each input is read with `LIMIT cap + 1`. Past any cap the
/// computation reserves nothing: an empty set means an unlisted job proposes as
/// it did before reserved scopes existed, the safe direction.
#[derive(Clone, Copy, Debug)]
pub struct ReservedCaps {
    /// Reflect grant rows across all listed principals.
    pub listed_grants: usize,
    /// Reflect grant rows of the caller.
    pub caller_grants: usize,
    /// Distinct card scopes read when wildcards sit on both sides.
    pub card_scopes: usize,
}
/// A few thousand rows is far past any real fleet's grants and card scopes.
pub const RESERVED_CAPS: ReservedCaps = ReservedCaps {
    listed_grants: 2000,
    caller_grants: 2000,
    card_scopes: 5000,
};

/// A principal's reflect grant patterns, answerable by set lookup. `covers`
/// mirrors the free function: an exact pattern, `repo:*` for repository
/// scopes, `*` for repository scopes and `fleet`.
#[derive(Default)]
struct ReflectPatterns {
    exact: std::collections::HashSet<String>,
    repo_star: bool,
    star: bool,
    wide: bool,
}
impl ReflectPatterns {
    fn add(&mut self, pattern: String) {
        self.wide |= pattern.contains('*');
        self.repo_star |= pattern == "repo:*";
        self.star |= pattern == "*";
        self.exact.insert(pattern);
    }
    fn covers(&self, scope: &str) -> bool {
        self.exact.contains(scope)
            || (self.repo_star && scope.starts_with("repo:"))
            || (self.star && (scope == "fleet" || scope.starts_with("repo:")))
    }
}

/// `SELECT ... FROM memory_grants` rows holding reflect, one more than `cap`.
/// `instr` is case-sensitive, so the match is the exact comma token `has_right`
/// reads through `rights_set`; `LIKE` would also accept `REFLECT`.
/// `None` when the cap is exceeded.
fn reflect_grant_rows<T: ReadTx + ?Sized>(
    tx: &T,
    filter: &str,
    mut params: Vec<Value>,
    cap: usize,
) -> rusqlite::Result<Option<Vec<Vec<Value>>>> {
    params.push(Value::Integer(cap as i64 + 1));
    let n = params.len();
    let rows = tx.query(
        &format!(
            "SELECT principal,scope,rights FROM memory_grants WHERE {filter} \
             AND instr(','||rights||',', ',reflect,')>0 LIMIT ?{n}"
        ),
        &params,
    )?;
    Ok((rows.len() <= cap).then_some(rows))
}

/// For a credential that holds `reflect` on some scope, the scopes where it
/// holds the right and a listed principal holds it too. `None` for a
/// credential with no reflect grant. `listed` is the live, listed principals
/// (the caller adds itself when the policy lists it, and `hub:admin` is in it
/// whenever the policy lists it), already filtered by the caller against the
/// policy and the hub's credential liveness, so a revoked or credential-less
/// principal never reserves a scope. Only those principals' reflect grants are
/// read: a constant number of queries however many grants exist, each bounded
/// by `RESERVED_CAPS`; past a cap the answer is the empty set.
/// Scopes are the ones with cards plus every exact scope a reflect grant
/// names, and a wildcard grant covers the scopes that have cards. The card
/// scopes are read only when a wildcard (or the admin) sits on both sides.
/// Each answer uses the `has_right` rules, so wildcards and the private
/// `principal:` scope behave the same here. The result names no principal.
pub fn reserved_reflect_scopes<T: ReadTx + ?Sized>(
    tx: &T,
    credential: &Credential,
    listed: &[String],
) -> rusqlite::Result<Option<BTreeSet<String>>> {
    reserved_reflect_scopes_capped(tx, credential, listed, RESERVED_CAPS)
}

pub fn reserved_reflect_scopes_capped<T: ReadTx + ?Sized>(
    tx: &T,
    credential: &Credential,
    listed: &[String],
    caps: ReservedCaps,
) -> rusqlite::Result<Option<BTreeSet<String>>> {
    let caller_admin = *credential == Credential::Admin;
    let mut caller = ReflectPatterns::default();
    // Node defaults carry read and write only, so reflect needs a grant row.
    match reflect_grant_rows(
        tx,
        "principal=?1",
        vec![Value::Text(credential.principal().into())],
        caps.caller_grants,
    )? {
        Some(rows) => rows
            .iter()
            .for_each(|r| caller.add(text(&r[1]).to_string())),
        // More reflect grants than the cap: it holds the right, reserve nothing.
        None => return Ok(Some(BTreeSet::new())),
    }
    if !caller_admin && caller.exact.is_empty() {
        return Ok(None);
    }
    let admin_listed = listed.iter().any(|name| name == ADMIN);
    let names: Vec<&String> = listed.iter().filter(|name| *name != ADMIN).collect();
    let mut holder_patterns: std::collections::BTreeMap<String, ReflectPatterns> =
        std::collections::BTreeMap::new();
    if !names.is_empty() {
        let marks = vec!["?"; names.len()].join(",");
        let params: Vec<Value> = names.iter().map(|n| Value::Text((*n).clone())).collect();
        let Some(rows) = reflect_grant_rows(
            tx,
            &format!("principal IN ({marks})"),
            params,
            caps.listed_grants,
        )?
        else {
            return Ok(Some(BTreeSet::new()));
        };
        for row in &rows {
            holder_patterns
                .entry(text(&row[0]).to_string())
                .or_default()
                .add(text(&row[1]).to_string());
        }
    }
    let mut scopes: BTreeSet<String> = caller
        .exact
        .iter()
        .filter(|p| !p.contains('*'))
        .cloned()
        .collect();
    for patterns in holder_patterns.values() {
        scopes.extend(patterns.exact.iter().filter(|p| !p.contains('*')).cloned());
    }
    let holder_wide = admin_listed || holder_patterns.values().any(|p| p.wide);
    if (caller_admin || caller.wide) && holder_wide {
        let rows = tx.query(
            "SELECT DISTINCT scope FROM memory_cards LIMIT ?1",
            &[Value::Integer(caps.card_scopes as i64 + 1)],
        )?;
        if rows.len() > caps.card_scopes {
            return Ok(Some(BTreeSet::new()));
        }
        scopes.extend(rows.iter().map(|r| text(&r[0]).to_string()));
    }
    let holds = |who: &Credential, patterns: &ReflectPatterns, scope: &str| {
        *who == Credential::Admin
            || without_grants(who, scope, "reflect").unwrap_or_else(|| patterns.covers(scope))
    };
    let holders: Vec<(Credential, &ReflectPatterns)> = holder_patterns
        .iter()
        .map(|(name, patterns)| {
            (
                Credential::Principal {
                    id: name.clone(),
                    digest: String::new(),
                },
                patterns,
            )
        })
        .collect();
    // The admin credential holds every right on every scope, with no grant row.
    let no_patterns = ReflectPatterns::default();
    Ok(Some(
        scopes
            .into_iter()
            .filter(|scope| holds(credential, &caller, scope))
            .filter(|scope| {
                (admin_listed && holds(&Credential::Admin, &no_patterns, scope))
                    || holders
                        .iter()
                        .any(|(who, patterns)| holds(who, patterns, scope))
            })
            .collect(),
    ))
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

/// Rights that only one grant row naming exactly this scope can open, all of
/// them on that one row. Wildcard grants and node defaults never count, so
/// `*` with every right still does not open per-card read counts. The admin
/// token needs no grant.
pub fn has_exact_rights<T: ReadTx + ?Sized>(
    tx: &T,
    credential: &Credential,
    scope: &str,
    rights: &[&str],
) -> rusqlite::Result<bool> {
    if *credential == Credential::Admin {
        return Ok(true);
    }
    if scope
        .strip_prefix("principal:")
        .is_some_and(|owner| owner != credential.principal())
    {
        return Ok(false);
    }
    Ok(grant_rows(tx, credential.principal())?
        .iter()
        .any(|(pattern, held)| pattern == scope && rights.iter().all(|r| held.contains(*r))))
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
