//! S7 native claims. Python's `fleet_hub_claims` is the oracle: the same
//! validation order, arbitration, fencing, retirement and response bytes. The
//! Python hub keeps the schema (D6), so nothing here creates or migrates it.
mod arbiter;
use crate::wire::{controls, object, py_float, py_trim, quote, text, valid_node};
use crate::writer::{ApplyError, Command, ReadTx, WriteTx};
use rusqlite::types::Value;
use serde_json::{Value as Json, json};
use subtle::ConstantTimeEq;

pub const MAX_GENERATION: i64 = i64::MAX;
const TTL_MIN: i64 = 1;
const TTL_MAX: i64 = 86400;
const DEFAULT_TTL: i64 = 900;
const LEASE_MAX: usize = 1024;
const LABEL_MAX: usize = 128;
const LABELS: [&str; 4] = ["harness", "role", "job", "session"];

/// A finished decision: status and Python's exact body.
pub type Reply = (u16, String);
/// Python raised: the reply stands, and every write of the decision rolls back.
pub enum Stop {
    Reply(Reply),
    /// `_RetirementCapacityError`: a full retirement budget, which an
    /// unrelated prune skips instead of failing.
    Capacity(Reply),
    Sql(rusqlite::Error),
}
impl From<rusqlite::Error> for Stop {
    fn from(e: rusqlite::Error) -> Self {
        Self::Sql(e)
    }
}
pub fn error(status: u16, message: &str) -> Reply {
    (status, object(&[("error", quote(message))]))
}
fn invalid<T>(message: &str) -> Result<T, Reply> {
    Err(error(400, message))
}
/// `ClaimFenceError` as `_post_api` sends it.
pub(crate) fn fence<T>(message: &str) -> Result<T, Stop> {
    Err(Stop::Reply(fence_reply(message)))
}
pub(crate) fn fence_reply(message: &str) -> Reply {
    (
        409,
        object(&[
            ("error", quote(message)),
            ("reason", quote("claim-fence-refused")),
            ("owner", "{}".into()),
        ]),
    )
}

#[derive(Clone, Debug)]
pub struct Lease {
    pub token: String,
    pub acquired_at: Option<String>,
    pub run_dir: Option<String>,
}
#[derive(Clone, Debug)]
pub struct Request {
    pub action: &'static str,
    pub scope: &'static str,
    pub target: String,
    pub node: String,
    pub holder: Option<String>,
    pub conductor: Option<String>,
    pub labels: [Option<String>; 4],
    pub ttl: i64,
    pub acquired_at: Option<String>,
    pub lock: Option<Lease>,
    pub supersede: Option<Lease>,
    pub generation: Option<i64>,
}
impl Request {
    /// A legacy operator release binds to the fence it observed before the write lock.
    pub fn needs_snapshot(&self) -> bool {
        self.action == "release" && self.scope != "holder" && self.generation.is_none()
    }
}

/// Bodies whose target needs Python's `repr` of non-ASCII text stay on the
/// Python upstream. Every other claim refusal text is ASCII or plain `str()`.
pub fn python_only(raw: &Json) -> bool {
    raw.get("target")
        .and_then(Json::as_str)
        .is_some_and(|t| !t.is_ascii())
}
/// Python `repr()` of an ASCII string with no controls.
pub(crate) fn repr(s: &str) -> String {
    let q = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::from(q);
    for c in s.chars() {
        if c == '\\' || c == q {
            out.push('\\');
        }
        out.push(c);
    }
    out.push(q);
    out
}
fn reject_controls(value: &str, field: &str) -> Result<(), Reply> {
    if controls(value) {
        return Err(error(
            422,
            &format!("claim field '{field}' must not contain control characters"),
        ));
    }
    Ok(())
}
fn nonempty(v: Option<&Json>) -> Option<&str> {
    v.and_then(Json::as_str).filter(|s| !py_trim(s).is_empty())
}
fn lease(raw: &Json, field: &str) -> Result<Lease, Reply> {
    let Some(o) = raw.as_object() else {
        return invalid(&format!("claim field '{field}' must be a JSON object"));
    };
    let Some(token) = nonempty(o.get("token")).filter(|t| t.chars().count() <= LEASE_MAX) else {
        return invalid(&format!(
            "claim field '{field}' must carry a non-empty 'token'"
        ));
    };
    let mut parts = [None, None];
    for (slot, key) in parts.iter_mut().zip(["acquired_at", "run_dir"]) {
        match o.get(key) {
            None | Some(Json::Null) => {}
            Some(Json::String(s)) if s.chars().count() <= LEASE_MAX => {
                *slot = Some(py_trim(s).to_string()).filter(|s| !s.is_empty());
            }
            Some(_) => return invalid(&format!("claim field '{field}'.{key} must be a string")),
        }
    }
    let [acquired_at, run_dir] = parts;
    Ok(Lease {
        token: py_trim(token).into(),
        acquired_at,
        run_dir,
    })
}
/// `_validate_claim_request` plus `handle_claim`'s generation check, in order.
pub fn validate(raw: &Json) -> Result<Request, Reply> {
    let Some(o) = raw.as_object() else {
        return invalid("claim request must be a JSON object");
    };
    let action = match o.get("action").and_then(Json::as_str) {
        Some("acquire") => "acquire",
        Some("renew") => "renew",
        Some("release") => "release",
        Some("inspect") => "inspect",
        _ => {
            return invalid(
                "claim field 'action' must be one of: acquire, renew, release, inspect",
            );
        }
    };
    let allowed: &[&'static str] = match action {
        "acquire" => &["holder", "node"],
        "release" => &["force", "holder", "node"],
        _ => &["holder"],
    };
    let scope = match o.get("scope") {
        None => Some("holder"),
        Some(Json::String(s)) => allowed.iter().copied().find(|a| a == s),
        Some(_) => None,
    };
    let Some(scope) = scope else {
        return invalid(&format!(
            "claim field 'scope' for {action} must be one of: {}",
            allowed.join(", ")
        ));
    };
    let holder_optional = action == "inspect" || (action == "release" && scope != "holder");
    let mut ids: [Option<String>; 3] = [None, None, None];
    for (slot, field) in ids.iter_mut().zip(["target", "node_id", "holder"]) {
        let value = o.get(field);
        if field == "holder" && holder_optional && value.is_none_or(Json::is_null) {
            continue;
        }
        let Some(value) = nonempty(value) else {
            return invalid(&format!("claim field '{field}' must be a non-empty string"));
        };
        if field == "target" {
            reject_controls(value, field)?;
        }
        *slot = Some(py_trim(value).into());
    }
    let [target, node, holder] = ids;
    for (field, value) in [("node_id", &node), ("holder", &holder)] {
        if value.as_deref().is_some_and(|v| !valid_node(v)) {
            return invalid(&format!(
                "claim field '{field}' must be a real identity ([A-Za-z0-9._-], max 128 chars, never the literal 'unknown')"
            ));
        }
    }
    let conductor = o.get("conductor").and_then(Json::as_str);
    if let Some(c) = conductor {
        reject_controls(c, "conductor")?;
    }
    let conductor = conductor
        .map(py_trim)
        .filter(|c| !c.is_empty())
        .map(String::from);
    let mut labels: [Option<String>; 4] = Default::default();
    for (slot, field) in labels.iter_mut().zip(LABELS) {
        let value = o.get(field).filter(|v| !v.is_null());
        if value.is_some() && action != "acquire" {
            return invalid(&format!("claim field '{field}' is only valid for acquire"));
        }
        if let Some(value) = value {
            let Some(s) = nonempty(Some(value)).filter(|s| s.chars().count() <= LABEL_MAX) else {
                return invalid(&format!(
                    "claim field '{field}' must be a non-empty string at most {LABEL_MAX} characters"
                ));
            };
            reject_controls(s, field)?;
            *slot = Some(py_trim(s).into());
        }
    }
    let ttl = match o.get("ttl_seconds") {
        None => Some(DEFAULT_TTL),
        Some(v) => v.as_i64().filter(|t| (TTL_MIN..=TTL_MAX).contains(t)),
    };
    let Some(ttl) = ttl else {
        return invalid(&format!(
            "claim field 'ttl_seconds' must be an integer in [{TTL_MIN}, {TTL_MAX}]"
        ));
    };
    let acquired_at = match o.get("acquired_at").filter(|v| !v.is_null()) {
        None => None,
        Some(_) if action != "release" => {
            return invalid("claim field 'acquired_at' is only valid for release");
        }
        Some(v) => match nonempty(Some(v)).filter(|s| s.chars().count() <= LEASE_MAX) {
            Some(s) => Some(py_trim(s).to_string()),
            None => return invalid("claim field 'acquired_at' must be a non-empty string"),
        },
    };
    if action == "release" && scope == "node" && acquired_at.is_none() {
        return invalid(
            "claim field 'acquired_at' is required for a token-less node-scoped release",
        );
    }
    let lock = match o.get("lock").filter(|v| !v.is_null()) {
        None => None,
        Some(_) if action != "acquire" => {
            return invalid("claim field 'lock' is only valid for acquire");
        }
        Some(v) => Some(lease(v, "lock")?),
    };
    let supersede = o.get("supersede").filter(|v| !v.is_null());
    let supersede = if action == "acquire" && scope == "node" {
        let Some(v) = supersede else {
            return invalid(
                "claim field 'supersede' (the reconciled dead lease) is required for a node-scoped acquire",
            );
        };
        Some(lease(v, "supersede")?)
    } else if supersede.is_some() {
        return invalid("claim field 'supersede' is only valid for a node-scoped acquire");
    } else {
        None
    };
    let generation = match o.get("generation") {
        None => None,
        Some(v) => match v.as_i64().filter(|g| *g >= 0) {
            Some(g) => Some(g),
            None => {
                return invalid(
                    "claim field 'generation' must be an integer in [0, 9223372036854775807]",
                );
            }
        },
    };
    Ok(Request {
        action,
        scope,
        target: target.unwrap(),
        node: node.unwrap(),
        holder,
        conductor,
        labels,
        ttl,
        acquired_at,
        lock,
        supersede,
        generation,
    })
}

/// Python's `json.dumps` of a stored SQLite value.
pub(crate) fn json_value(v: &Value) -> String {
    match v {
        Value::Null | Value::Blob(_) => "null".into(),
        Value::Integer(i) => i.to_string(),
        Value::Real(f) => py_float(*f),
        Value::Text(s) => quote(s),
    }
}

/// `_caller`: the admin token in constant time, then the node token digest.
/// `Ok(None)` is the admin, `Ok(Some(node))` a live node. Admin writes follow
/// `admit_post`.
pub type Caller = Result<Option<String>, Reply>;
pub fn caller<T: ReadTx + ?Sized>(
    tx: &T,
    key: &str,
    admin: &str,
    write: bool,
    allow_admin_writes: bool,
) -> rusqlite::Result<Caller> {
    if bool::from(key.as_bytes().ct_eq(admin.as_bytes())) {
        if write && !allow_admin_writes {
            return Ok(Err(error(
                403,
                "the admin token may not post events or claims: enroll this node with 'brigade fleet nodes add' and configure its node token, or start the hub with --allow-admin-writes",
            )));
        }
        return Ok(Ok(None));
    }
    let rows = tx.query(
        "SELECT node_id,token_sha256,revoked_at FROM nodes WHERE token_sha256=?1",
        &[text(key)],
    )?;
    let Some(row) = rows.first().filter(
        |r| matches!(&r[1], Value::Text(s) if bool::from(s.as_bytes().ct_eq(key.as_bytes()))),
    ) else {
        return Ok(Err(error(401, "unauthorized")));
    };
    if row[2] != Value::Null {
        return Ok(Err(error(401, "unauthorized: node token revoked")));
    }
    Ok(Ok(Some(match &row[0] {
        Value::Text(s) => s.clone(),
        Value::Integer(i) => i.to_string(),
        other => json_value(other),
    })))
}
/// `handle_claim`'s check of the body's node against the caller's token.
pub fn forbidden(request: &Request, caller_node: Option<&str>) -> Option<Reply> {
    let caller = caller_node?;
    (request.node != caller).then(|| {
        error(
            403,
            &format!(
                "claim node_id {} does not match the caller's node token ({caller})",
                repr(&request.node)
            ),
        )
    })
}

/// A reader connection inside the reader pool's snapshot transaction.
pub struct ReadConnection<'a>(pub &'a rusqlite::Connection);
impl ReadTx for ReadConnection<'_> {
    fn query(&self, sql: &str, params: &[Value]) -> rusqlite::Result<Vec<Vec<Value>>> {
        let mut statement = self.0.prepare_cached(sql)?;
        let n = statement.column_count();
        statement
            .query_map(rusqlite::params_from_iter(params), |r| {
                (0..n).map(|i| r.get(i)).collect()
            })?
            .collect()
    }
}
fn finish(result: Result<Reply, Stop>) -> rusqlite::Result<Reply> {
    match result {
        Ok(reply) | Err(Stop::Reply(reply) | Stop::Capacity(reply)) => Ok(reply),
        Err(Stop::Sql(e)) => Err(e),
    }
}
/// The legacy release fence snapshot `handle_claim` reads before `BEGIN IMMEDIATE`.
pub fn observe(c: &rusqlite::Connection, request: &mut Request) -> rusqlite::Result<Option<Reply>> {
    match arbiter::observe(&ReadConnection(c), &request.target) {
        Ok(generation) => {
            request.generation = Some(generation);
            Ok(None)
        }
        Err(Stop::Reply(reply) | Stop::Capacity(reply)) => Ok(Some(reply)),
        Err(Stop::Sql(e)) => Err(e),
    }
}
/// An inspect runs in one read snapshot, like Python's deferred `BEGIN`.
pub fn inspect(
    c: &rusqlite::Connection,
    key: &str,
    admin: &str,
    allow_admin_writes: bool,
    request: &Request,
    clock: Option<&str>,
) -> rusqlite::Result<Reply> {
    let tx = ReadConnection(c);
    match caller(&tx, key, admin, true, allow_admin_writes)? {
        Err(reply) => return Ok(reply),
        Ok(node) => {
            if let Some(reply) = forbidden(request, node.as_deref()) {
                return Ok(reply);
            }
        }
    }
    finish(arbiter::inspect(&tx, request, crate::wire::epoch(clock)))
}
/// `GET /claims`, or `None` when active Grok Bot leases need Python's display
/// projection. The caller has already been authorized for this snapshot.
pub fn list(
    c: &rusqlite::Connection,
    key: &str,
    admin: &str,
    include_all: bool,
    clock: Option<&str>,
) -> rusqlite::Result<Option<Reply>> {
    let tx = ReadConnection(c);
    if let Err(reply) = caller(&tx, key, admin, false, false)? {
        return Ok(Some(reply));
    }
    let grokbot = tx.query(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'grokbot_jobs'",
        &[],
    )?;
    if !grokbot.is_empty()
        && !tx
            .query(
                "SELECT 1 FROM grokbot_jobs WHERE state IN ('claimed', 'running') LIMIT 1",
                &[],
            )?
            .is_empty()
    {
        return Ok(None);
    }
    finish(arbiter::list(&tx, include_all, crate::wire::epoch(clock))).map(Some)
}

/// One claim write. Apply re-checks the credential inside the writer
/// transaction and reads the clock only after the write lock is held.
pub struct Claim {
    pub request: Request,
    pub key: String,
    pub admin: String,
    pub allow_admin_writes: bool,
    pub clock: Option<String>,
    pub events: bool,
}
pub fn pack((status, body): Reply) -> Json {
    json!({"status": status, "body": body})
}
impl Command for Claim {
    fn apply(&self, tx: &dyn WriteTx) -> Result<Json, ApplyError> {
        let node = match caller(tx, &self.key, &self.admin, true, self.allow_admin_writes)? {
            Ok(node) => node,
            Err(reply) => return Ok(pack(reply)),
        };
        if let Some(reply) = forbidden(&self.request, node.as_deref()) {
            return Ok(pack(reply));
        }
        let clock = arbiter::Clock {
            now: crate::wire::epoch(self.clock.as_deref()),
            time: raw_time(self.clock.as_deref()),
            events: self.events,
        };
        match arbiter::decide(tx, &self.request, &clock) {
            Ok(reply) => Ok(pack(reply)),
            Err(Stop::Reply(reply) | Stop::Capacity(reply)) => {
                Err(ApplyError::Refused(pack(reply)))
            }
            Err(Stop::Sql(e)) => Err(e.into()),
        }
    }
}
/// `time.time()`: the acceptance clock's file value as written, unrounded.
fn raw_time(clock: Option<&str>) -> f64 {
    clock
        .and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|s| s.trim().parse::<f64>().ok())
        .filter(|v| v.is_finite() && *v >= 0.0 && *v < 253402300799.0)
        .unwrap_or_else(|| {
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_secs_f64()
        })
}
/// `fleet_hub_events.config_from_environment(...).enabled`.
pub fn events_enabled() -> bool {
    std::env::var("BRIGADE_FLEET_EVENTS_CALLBACK_HOSTS")
        .unwrap_or_default()
        .split(',')
        .any(|part| !py_trim(part).is_empty())
}

#[cfg(test)]
mod tests;
