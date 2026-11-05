//! Hub memory v2 store (S13a): the event log, card revisions, blobs,
//! compare-and-set on revision with `log_epoch`, idempotency, grants and
//! trust rules, behind the authority protocol in [`writer`].
//!
//! `search` answers from the lexical index in [`index`] (S13b), with the
//! scorer and tokenizer copied from Python in [`text`].
//!
//! Writes and prompt loads use the versioned guard scanner (S18).
//! Proposals and decisions (S19a) and embedding (S15) follow in later slices.
pub mod auth;
pub mod ids;
pub mod index;
pub mod ops;
mod pytables;
pub mod scan;
pub mod schema;
pub mod search;
pub mod text;
pub mod writer;

#[cfg(test)]
mod lexical_tests;
#[cfg(test)]
mod powercut;
#[cfg(test)]
mod tests;

use crate::{
    read::{MEMORY_QUEUE, MEMORY_THREADS, READ_LIMIT, ReaderPool},
    writer::{Arbiter, ReadTx, worker},
};
pub use auth::Credential;
use ops::{Action, Base, Prepared, error};
use rusqlite::types::Value;
use scan::{Scan, Scanner};
use serde_json::{Map, Value as Json, json};
use std::{
    path::PathBuf,
    sync::Arc,
    time::{Duration, Instant},
};

/// The admin token's principal id. Never a valid node id or message principal.
pub const ADMIN: &str = "hub:admin";
/// Card text returned per `card` call, in characters.
pub const CHUNK_CHARS: usize = 30_000;
const COMMAND_DEADLINE: Duration = Duration::from_secs(10);
/// The most scopes one `status` request may ask about.
const STATUS_SCOPES_MAX: usize = 64;
/// How long `search` with `min_seq` waits for the index to reach it.
pub const MIN_SEQ_WAIT: Duration = Duration::from_millis(500);
/// Actions in the route table that later slices build.
const LATER: &[&str] = &[
    "propose",
    "decide",
    "rollback",
    "preflight",
    "embed_lease",
    "embed_submit",
];

#[derive(Clone, Debug, PartialEq)]
pub struct Reply {
    pub status: u16,
    pub body: Json,
}
impl Reply {
    pub fn new(status: u16, body: Json) -> Self {
        Self { status, body }
    }
}
fn invalid(message: &str) -> Reply {
    error(400, "memory_request_invalid", message)
}

pub struct Options {
    pub memory: PathBuf,
    pub hub: PathBuf,
    /// SHA-256 hex of the admin token.
    pub admin_digest: String,
    pub clock: ids::Clock,
    pub scanner: Arc<dyn Scanner>,
    /// The arbiter shared with every hub writer of the same hub file.
    pub arbiter: Arc<Arbiter>,
    pub hook: Option<writer::Hook>,
}

pub struct Store {
    writer: writer::Writer,
    arbiter: Arc<Arbiter>,
    readers: ReaderPool,
    hub: Arc<ReaderPool>,
    admin: String,
    clock: ids::Clock,
    scanner: Arc<dyn Scanner>,
    last_sweep: std::sync::atomic::AtomicI64,
    index: Arc<index::Index>,
}

impl Drop for Store {
    /// Stops and joins the index rebuild thread first, so its read-only
    /// connection is closed before anything swaps the memory file. The
    /// writer then answers what is queued and closes its connections.
    fn drop(&mut self) {
        self.index.stop();
    }
}

struct Conn<'a>(&'a rusqlite::Connection);
impl ReadTx for Conn<'_> {
    fn query(&self, sql: &str, params: &[Value]) -> rusqlite::Result<Vec<Vec<Value>>> {
        worker::Tx(self.0).query(sql, params)
    }
}

/// True when the body names an action `Store::handle` answers from the
/// query-only readers. Disk-pressure admission lets these through (S4b:
/// reads continue). A body that does not parse is not a read.
pub fn read_only(body: &[u8]) -> bool {
    serde_json::from_slice::<Json>(body).is_ok_and(|v| {
        matches!(
            v.get("action").and_then(Json::as_str),
            Some("card" | "log" | "search" | "status")
        )
    })
}

/// The disk-pressure refusal for one memory request, if any. Writes are
/// non-durable admission (D21 and S4b); reads go on.
pub fn pressure_refusal(
    disk: &crate::maintenance::DiskPressure,
    body: &[u8],
) -> Option<&'static str> {
    disk.admit(false).err().filter(|_| !read_only(body))
}

impl Store {
    /// Creates the memory file when it is absent, then migrates and checks it.
    pub fn open(options: Options, hub: Arc<ReaderPool>) -> Result<Self, String> {
        let created = !options.memory.exists();
        let db = worker::open_file(&options.memory, true).map_err(|e| e.to_string())?;
        schema::migrate(&db, options.clock.now_ms())?;
        let ruleset = options
            .scanner
            .describe()
            .1
            .unwrap_or_else(|| "unavailable".into());
        db.execute("INSERT INTO memory_meta(key,value) VALUES('scan_ruleset_version',?1) ON CONFLICT(key) DO UPDATE SET value=excluded.value", [&ruleset]).map_err(|e| e.to_string())?;
        // A restart rebuilds the index from the store before the hub serves.
        let index = index::Index::open(&db, options.memory.clone()).map_err(|e| e.to_string())?;
        if created {
            // A new file's directory entry must be durable before any write is acknowledged.
            worker::sync_directory(&db).map_err(|e| e.to_string())?;
        }
        let readers =
            ReaderPool::open_unchecked(&options.memory, MEMORY_THREADS, MEMORY_QUEUE, READ_LIMIT)?;
        let mut config = writer::Config::new(
            options.memory.clone(),
            options.hub,
            options.admin_digest.clone(),
        );
        config.hook = options.hook;
        config.scanner = options.scanner.clone();
        config.index = Some(index.clone());
        let writer = writer::Writer::start(config, db, options.arbiter.clone())
            .map_err(|e| e.to_string())?;
        Ok(Self {
            writer,
            arbiter: options.arbiter,
            readers,
            hub,
            admin: options.admin_digest,
            clock: options.clock,
            scanner: options.scanner,
            last_sweep: std::sync::atomic::AtomicI64::new(i64::MIN),
            index,
        })
    }
    /// The lexical index, for `/metrics` and `status`.
    pub fn index(&self) -> &Arc<index::Index> {
        &self.index
    }
    pub fn metrics(&self) -> writer::Metrics {
        self.writer.metrics.lock().unwrap().clone()
    }
    /// Queues one bounded idempotency retention sweep on the memory writer,
    /// at most once a minute. Maintenance asks every second.
    pub fn sweep(&self) -> bool {
        use std::sync::atomic::Ordering;
        let now = self.clock.now_ms();
        let last = self.last_sweep.load(Ordering::Relaxed);
        if last != i64::MIN && now.saturating_sub(last) < 60_000 {
            return true;
        }
        self.last_sweep.store(now, Ordering::Relaxed);
        self.writer.sweep(now)
    }
    /// The memory reader queue, for `/metrics`.
    pub fn reader_probe(&self) -> crate::telemetry::QueueProbe {
        self.readers.queue_probe()
    }
    /// The arbiter's residence bucket, for `/metrics`.
    pub fn bucket(&self) -> f64 {
        self.arbiter.balance()
    }
    /// Admission: the bearer token's digest against the admin token and the
    /// hub's credential rows. The token itself is hashed here and dropped.
    pub async fn authenticate(&self, token: &str) -> Result<Credential, Reply> {
        let digest = ids::sha256_hex(token.as_bytes());
        let admin = self.admin.clone();
        match self
            .hub
            .submit(move |c| auth::resolve(&Conn(c), &digest, &admin))
            .await
        {
            Ok(result) => result,
            Err(_) => Err(Reply::new(
                503,
                json!({"error": "hub busy", "reason": "credential-lookup"}),
            )),
        }
    }
    pub async fn handle(&self, credential: &Credential, body: &[u8]) -> Reply {
        let raw: Json = match serde_json::from_slice(body) {
            Ok(v) => v,
            Err(_) => return invalid("body must be a JSON object"),
        };
        let Some(map) = raw.as_object() else {
            return invalid("body must be a JSON object");
        };
        let action = map.get("action").and_then(Json::as_str).unwrap_or("");
        match action {
            "card" | "log" | "status" => self.read(credential, action, map).await,
            "search" => self.search(credential, map).await,
            "put" | "import" | "retire" | "handoff" | "capture" | "grant" => {
                match self.prepare(credential, action, map, &raw) {
                    Ok(prepared) => self.write(prepared).await,
                    Err(reply) => reply,
                }
            }
            a if LATER.contains(&a) => error(
                501,
                "memory_action_unavailable",
                "this memory action is not available on this hub yet",
            ),
            _ => invalid("unknown memory action"),
        }
    }
    async fn write(&self, prepared: Prepared) -> Reply {
        match self
            .writer
            .submit(prepared, Instant::now() + COMMAND_DEADLINE)
            .await
        {
            Ok(reply) => reply,
            Err(failure) => Reply::new(
                failure.status,
                json!({"error": "hub busy", "reason": failure.reason}),
            ),
        }
    }

    /// Validation, normalization and hashing outside locks. Scanning is authorized in apply.
    pub fn prepare(
        &self,
        credential: &Credential,
        action: &str,
        map: &Map<String, Json>,
        raw: &Json,
    ) -> Result<Prepared, Reply> {
        let allowed: &[&str] = match action {
            "put" | "import" => &[
                "action",
                "scope",
                "card_id",
                "base_revision",
                "base_epoch",
                "text",
                "origin",
                "client_event_id",
            ],
            "retire" => &[
                "action",
                "scope",
                "card_id",
                "base_revision",
                "base_epoch",
                "reason",
                "client_event_id",
            ],
            "handoff" => &["action", "text", "kind", "client_event_id"],
            "capture" => &[
                "action",
                "session_ref",
                "pushed",
                "opened",
                "outcome",
                "client_event_id",
            ],
            _ => &["action", "principal", "scope", "rights", "client_event_id"],
        };
        if let Some(k) = map.keys().find(|k| !allowed.contains(&k.as_str())) {
            return Err(invalid(&format!("unknown field {k:?} for {action}")));
        }
        if action == "grant" && *credential != Credential::Admin {
            return Err(error(
                403,
                "memory_grant_missing",
                "the admin token is required to change memory grants",
            ));
        }
        let now_ms = self.clock.now_ms();
        let Some((client_event_id, at)) = map
            .get("client_event_id")
            .and_then(Json::as_str)
            .and_then(ids::parse_event_id)
        else {
            return Err(error(
                400,
                "memory_client_event_invalid",
                "client_event_id must be a 26-character time-ordered Crockford base32 id",
            ));
        };
        if at < now_ms - ids::WINDOW_MS || at > now_ms + ids::FUTURE_MS {
            return Err(error(
                409,
                "memory_client_event_expired",
                "client_event_id is outside the hub's acceptance window",
            ));
        }
        let str_field = |key: &str| map.get(key).and_then(Json::as_str);
        let card = || -> Result<(String, String), Reply> {
            let scope = str_field("scope").filter(|s| ids::valid_scope(s));
            let card_id = str_field("card_id").filter(|s| ids::valid_card_id(s));
            match (scope, card_id) {
                (Some(s), Some(c)) => Ok((s.into(), c.into())),
                (None, _) => Err(invalid(
                    "scope must be fleet, repo:<identity> or principal:<id>",
                )),
                _ => Err(invalid("card_id must match [a-z0-9][a-z0-9._-]{0,127}")),
            }
        };
        let base = || -> Result<Option<Base>, Reply> {
            match map.get("base_revision") {
                None | Some(Json::Null) => Ok(None),
                Some(v) => {
                    let revision = v.as_i64().filter(|r| *r > 0).ok_or_else(|| {
                        invalid("base_revision must be a positive integer or null")
                    })?;
                    let epoch = str_field("base_epoch")
                        .and_then(ids::parse_event_id)
                        .map(|(e, _)| e)
                        .ok_or_else(|| {
                            invalid("base_epoch is required with base_revision: the log_epoch the revision was read under")
                        })?;
                    Ok(Some(Base { revision, epoch }))
                }
            }
        };
        let text = || -> Result<(String, String, Scan), Reply> {
            let text = str_field("text").ok_or_else(|| invalid("text must be a string"))?;
            if text.trim().is_empty() {
                return Err(invalid("text must not be empty"));
            }
            if text.len() > ids::MAX_TEXT_BYTES + 1024 {
                return Err(error(413, "memory_card_too_large", "text is too large"));
            }
            let normalized = ids::normalize(text);
            if normalized.len() > ids::MAX_TEXT_BYTES {
                return Err(error(413, "memory_card_too_large", "text is too large"));
            }
            let hash = ids::sha256_hex(normalized.as_bytes());
            let scan = Scan::Unavailable;
            Ok((normalized, hash, scan))
        };
        let short = |key: &str, limit: usize, required: bool| -> Result<Option<String>, Reply> {
            match map.get(key) {
                None | Some(Json::Null) if !required => Ok(None),
                Some(Json::String(s)) if !s.is_empty() && s.chars().count() <= limit => {
                    Ok(Some(s.clone()))
                }
                _ => Err(invalid(&format!(
                    "{key} must be a non-empty string of at most {limit} characters"
                ))),
            }
        };
        let action = match action {
            "put" | "import" => {
                let (scope, card_id) = card()?;
                let base = base()?;
                let origin = match map.get("origin") {
                    None | Some(Json::Null) => None,
                    Some(Json::String(o))
                        if matches!(o.as_str(), "interactive" | "scheduled" | "worker") =>
                    {
                        Some(o.clone())
                    }
                    _ => return Err(invalid("origin must be interactive, scheduled or worker")),
                };
                let (text, hash, scan) = text()?;
                Action::Put {
                    scope,
                    card_id,
                    base,
                    text,
                    hash,
                    origin,
                    scan,
                    import: action == "import",
                }
            }
            "retire" => {
                let (scope, card_id) = card()?;
                let base =
                    base()?.ok_or_else(|| invalid("retire needs base_revision and base_epoch"))?;
                Action::Retire {
                    scope,
                    card_id,
                    base,
                    reason: short("reason", 500, false)?,
                }
            }
            "handoff" => {
                let kind = short("kind", 64, true)?.unwrap();
                if !kind
                    .bytes()
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || b"._-".contains(&c))
                {
                    return Err(invalid("kind must match [a-z0-9._-]{1,64}"));
                }
                let (text, hash, scan) = text()?;
                Action::Handoff {
                    kind,
                    text,
                    hash,
                    scan,
                }
            }
            "capture" => {
                let session = str_field("session_ref").filter(|s| {
                    (16..=128).contains(&s.len())
                        && s.bytes()
                            .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c))
                });
                let Some(session) = session else {
                    return Err(invalid(
                        "session_ref must be 16 to 128 characters of [A-Za-z0-9_-]",
                    ));
                };
                let list = |key: &str| -> Result<Json, Reply> {
                    match map.get(key) {
                        Some(Json::Array(items))
                            if items.len() <= 50
                                && items.iter().all(|i| {
                                    i.as_str().is_some_and(|s| !s.is_empty() && s.len() <= 300)
                                }) =>
                        {
                            Ok(Json::Array(items.clone()))
                        }
                        _ => Err(invalid(&format!(
                            "{key} must be a list of at most 50 card keys"
                        ))),
                    }
                };
                Action::Capture {
                    payload: json!({
                        "session_ref": session,
                        "pushed": list("pushed")?,
                        "opened": list("opened")?,
                        "outcome": short("outcome", 64, false)?,
                    }),
                }
            }
            _ => {
                let principal = str_field("principal")
                    .filter(|p| ids::valid_principal(p) && *p != ADMIN)
                    .ok_or_else(|| invalid("principal must be a node id or agent:<name>"))?;
                let scope = str_field("scope")
                    .filter(|s| ids::valid_grant_scope(s))
                    .ok_or_else(|| invalid("scope must be a memory scope, repo:* or *"))?;
                if let Some(owner) = scope.strip_prefix("principal:") {
                    if owner != principal {
                        return Err(invalid(
                            "a principal: scope is private to its principal and the admin",
                        ));
                    }
                }
                let Some(items) = map.get("rights").and_then(Json::as_array) else {
                    return Err(invalid("rights must be a list"));
                };
                let mut rights = Vec::new();
                for item in items {
                    match item.as_str() {
                        Some(r) if auth::RIGHTS.contains(&r) => rights.push(r.to_string()),
                        _ => {
                            return Err(invalid(&format!(
                                "rights must be drawn from {}",
                                auth::RIGHTS.join(", ")
                            )));
                        }
                    }
                }
                rights.sort();
                rights.dedup();
                Action::Grant {
                    principal: principal.into(),
                    scope: scope.into(),
                    rights,
                }
            }
        };
        // The digest names the id in its canonical spelling, so a retry that
        // spells it in lower case is the same request.
        let mut canonical = raw.clone();
        canonical["client_event_id"] = json!(client_event_id);
        Ok(Prepared {
            credential: credential.clone(),
            action,
            client_event_id,
            request_digest: ids::sha256_hex(ids::canonical(&canonical).as_bytes()),
            now_ms,
        })
    }

    /// S23's internal push boundary. The public search contract stays unchanged.
    pub async fn push(&self, credential: &Credential, map: &Map<String, Json>) -> Reply {
        self.search_for(credential, map, true).await
    }

    async fn search(&self, credential: &Credential, map: &Map<String, Json>) -> Reply {
        self.search_for(credential, map, false).await
    }

    async fn search_for(
        &self,
        credential: &Credential,
        map: &Map<String, Json>,
        push: bool,
    ) -> Reply {
        let mut request = match search::parse(map) {
            Ok(r) => r,
            Err(reply) => return reply,
        };
        request.push = push;
        request.prompt |= push;
        let snapshot = match request.min_seq {
            Some(min) => self.index.wait(min, MIN_SEQ_WAIT).await,
            None => self.index.current(),
        };
        let credential = credential.clone();
        let scanner = self.scanner.clone();
        match self
            .readers
            .submit(move |c| {
                search::run(&Conn(c), &credential, &request, &snapshot, scanner.as_ref())
            })
            .await
        {
            Ok(reply) => reply,
            Err(_) => Reply::new(
                503,
                json!({"error": "hub busy", "reason": "memory-reader-timeout"}),
            ),
        }
    }

    async fn read(&self, credential: &Credential, action: &str, map: &Map<String, Json>) -> Reply {
        let credential = credential.clone();
        let map = map.clone();
        let action = action.to_string();
        let now_ms = self.clock.now_ms();
        let scan = self.scanner.describe();
        let indexed_seq = self.index.current().indexed_seq;
        match self
            .readers
            .submit(move |c| {
                reads(
                    &Conn(c),
                    &credential,
                    &action,
                    &map,
                    now_ms,
                    scan,
                    indexed_seq,
                )
            })
            .await
        {
            Ok(reply) => reply,
            Err(_) => Reply::new(
                503,
                json!({"error": "hub busy", "reason": "memory-reader-timeout"}),
            ),
        }
    }
}

fn text(v: &Value) -> String {
    match v {
        Value::Text(s) => s.clone(),
        Value::Blob(b) => String::from_utf8_lossy(b).into_owned(),
        _ => String::new(),
    }
}
fn num(v: &Value) -> Option<i64> {
    if let Value::Integer(i) = v {
        Some(*i)
    } else {
        None
    }
}
fn nullable(v: &Value) -> Json {
    match v {
        Value::Null => Json::Null,
        Value::Integer(i) => json!(i),
        other => json!(text(other)),
    }
}

fn reads<T: ReadTx + ?Sized>(
    tx: &T,
    credential: &Credential,
    action: &str,
    map: &Map<String, Json>,
    now_ms: i64,
    scan: (bool, Option<String>),
    indexed_seq: i64,
) -> rusqlite::Result<Reply> {
    let allowed: &[&str] = match action {
        "card" => &["action", "card_id", "scope", "revision", "offset"],
        "log" => &["action", "after_seq", "limit"],
        "status" => &["action", "scopes"],
        _ => &["action"],
    };
    if let Some(k) = map.keys().find(|k| !allowed.contains(&k.as_str())) {
        return Ok(invalid(&format!("unknown field {k:?} for {action}")));
    }
    if !auth::has_right(tx, credential, None, "read")? {
        return Ok(error(
            403,
            "memory_grant_missing",
            "this credential has no memory read grant",
        ));
    }
    let epoch = tx
        .query("SELECT value FROM memory_meta WHERE key='log_epoch'", &[])?
        .first()
        .map(|r| text(&r[0]))
        .unwrap_or_default();
    let int_field = |key: &str, min: i64, max: i64, default: i64| -> Result<i64, Reply> {
        match map.get(key) {
            None | Some(Json::Null) => Ok(default),
            Some(v) => v
                .as_i64()
                .filter(|n| (min..=max).contains(n))
                .ok_or_else(|| invalid(&format!("{key} must be an integer from {min} to {max}"))),
        }
    };
    match action {
        "status" => {
            // `scopes` asks which of the named scopes this credential can read
            // now, so a client can drop what it replicated under a revoked
            // grant. The answer names the principal, never the credential.
            let readable_scopes = match map.get("scopes") {
                None | Some(Json::Null) => None,
                Some(Json::Array(items))
                    if !items.is_empty() && items.len() <= STATUS_SCOPES_MAX =>
                {
                    let mut readable = auth::Readable::new(tx, credential);
                    let mut names = Vec::new();
                    for item in items {
                        let Some(scope) = item.as_str().filter(|s| ids::valid_scope(s)) else {
                            return Ok(invalid(
                                "scopes must name fleet, repo:<identity> or principal:<id>",
                            ));
                        };
                        if readable.scope(scope)? {
                            names.push(scope.to_string());
                        }
                    }
                    names.sort();
                    names.dedup();
                    Some(names)
                }
                _ => {
                    return Ok(invalid(&format!(
                        "scopes must be a list of 1 to {STATUS_SCOPES_MAX} scopes"
                    )));
                }
            };
            let count = |sql: &str| -> rusqlite::Result<i64> {
                Ok(tx
                    .query(sql, &[])?
                    .first()
                    .and_then(|r| num(&r[0]))
                    .unwrap_or(0))
            };
            let mut reply = Reply::new(
                200,
                json!({
                    "cards": count("SELECT count(*) FROM memory_cards WHERE state='live'")?,
                    "revisions": count("SELECT count(*) FROM memory_card_revisions")?,
                    "seq": count("SELECT COALESCE(max(seq),0) FROM memory_events")?,
                    "indexed_seq": indexed_seq,
                    "log_epoch": epoch,
                    "hub_time": ids::isoformat(now_ms),
                    "scan": {"available": scan.0, "ruleset_version": scan.1},
                    "embedders": [],
                    "default_mode": "lexical",
                    "principal": credential.principal(),
                    "credential": credential.class(),
                }),
            );
            if let Some(names) = readable_scopes {
                reply.body["readable_scopes"] = json!(names);
            }
            Ok(reply)
        }
        "log" => {
            let after = match int_field("after_seq", 0, i64::MAX, 0) {
                Ok(v) => v,
                Err(r) => return Ok(r),
            };
            let limit = match int_field("limit", 1, 500, 100) {
                Ok(v) => v as usize,
                Err(r) => return Ok(r),
            };
            let rows = tx.query(
                "SELECT seq,event_id,client_event_id,kind,principal,scope,card_id,revision,base_revision,content_hash,trust,origin,payload,policy_digest,created_at FROM memory_events WHERE seq>?1 ORDER BY seq LIMIT 5000",
                &[Value::Integer(after)],
            )?;
            let mut readable = auth::Readable::new(tx, credential);
            let mut events = Vec::new();
            let mut next = after;
            for r in &rows {
                let seq = num(&r[0]).unwrap_or(0);
                let visible = match &r[5] {
                    Value::Text(scope) => readable.scope(scope)?,
                    // Unscoped events (grants, handoffs, captures) are the
                    // writer's own business and the admin's.
                    _ => *credential == Credential::Admin || text(&r[4]) == credential.principal(),
                };
                next = seq;
                if visible {
                    events.push(json!({
                        "seq": seq,
                        "event_id": text(&r[1]),
                        "client_event_id": text(&r[2]),
                        "kind": text(&r[3]),
                        "principal": text(&r[4]),
                        "scope": nullable(&r[5]),
                        "card_id": nullable(&r[6]),
                        "revision": nullable(&r[7]),
                        "base_revision": nullable(&r[8]),
                        "content_hash": nullable(&r[9]),
                        "trust": nullable(&r[10]),
                        "origin": nullable(&r[11]),
                        "payload": serde_json::from_str::<Json>(&text(&r[12])).unwrap_or(Json::Null),
                        "policy_digest": nullable(&r[13]),
                        "created_at": text(&r[14]),
                    }));
                    if events.len() == limit {
                        break;
                    }
                }
            }
            Ok(Reply::new(
                200,
                json!({"events": events, "next_seq": next, "log_epoch": epoch}),
            ))
        }
        _ => {
            let Some(card_id) = map
                .get("card_id")
                .and_then(Json::as_str)
                .filter(|c| ids::valid_card_id(c))
            else {
                return Ok(invalid("card_id must match [a-z0-9][a-z0-9._-]{0,127}"));
            };
            let offset = match int_field("offset", 0, i64::MAX, 0) {
                Ok(v) => v as usize,
                Err(r) => return Ok(r),
            };
            let revision = match map.get("revision") {
                None | Some(Json::Null) => None,
                Some(v) => match v.as_i64().filter(|r| *r > 0) {
                    Some(r) => Some(r),
                    None => return Ok(invalid("revision must be a positive integer")),
                },
            };
            let scope = match map.get("scope") {
                None | Some(Json::Null) => None,
                Some(v) => match v.as_str().filter(|s| ids::valid_scope(s)) {
                    Some(s) => Some(s.to_string()),
                    None => {
                        return Ok(invalid(
                            "scope must be fleet, repo:<identity> or principal:<id>",
                        ));
                    }
                },
            };
            let mut readable = auth::Readable::new(tx, credential);
            let scope = match scope {
                Some(s) => {
                    if !readable.scope(&s)? {
                        return Ok(error(
                            403,
                            "memory_grant_missing",
                            "this credential has no read grant on that scope",
                        ));
                    }
                    s
                }
                None => {
                    let mut scopes = Vec::new();
                    for r in tx.query(
                        "SELECT scope FROM memory_cards WHERE card_id=?1 ORDER BY scope",
                        &[Value::Text(card_id.into())],
                    )? {
                        let s = text(&r[0]);
                        if readable.scope(&s)? {
                            scopes.push(s);
                        }
                    }
                    match scopes.len() {
                        0 => return Ok(error(404, "memory_card_not_found", "card not found")),
                        1 => scopes.pop().unwrap(),
                        _ => {
                            let mut reply = error(
                                409,
                                "memory_card_ambiguous",
                                "this card id exists in several readable scopes; name one",
                            );
                            reply.body["scopes"] = json!(scopes);
                            return Ok(reply);
                        }
                    }
                }
            };
            let params = [Value::Text(scope.clone()), Value::Text(card_id.into())];
            let row = match revision {
                Some(rev) => tx.query(
                    "SELECT r.revision,r.content_hash,r.trust,r.state,b.bytes,r.scan_state,r.writer,r.created_at FROM memory_card_revisions r JOIN memory_blobs b ON b.content_hash=r.content_hash WHERE r.scope=?1 AND r.card_id=?2 AND r.revision=?3",
                    &[params[0].clone(), params[1].clone(), Value::Integer(rev)],
                )?,
                None => tx.query(
                    "SELECT c.revision,c.content_hash,c.trust,c.state,b.bytes,r.scan_state,r.writer,r.created_at FROM memory_cards c JOIN memory_card_revisions r ON r.scope=c.scope AND r.card_id=c.card_id AND r.revision=c.revision JOIN memory_blobs b ON b.content_hash=c.content_hash WHERE c.scope=?1 AND c.card_id=?2",
                    &params,
                )?,
            };
            let Some(row) = row.first() else {
                return Ok(error(404, "memory_card_not_found", "card not found"));
            };
            let body = text(&row[4]);
            let total = body.chars().count();
            let chunk: String = body.chars().skip(offset).take(CHUNK_CHARS).collect();
            let next_offset = (offset + CHUNK_CHARS < total).then_some(offset + CHUNK_CHARS);
            let revision = num(&row[0]).unwrap_or(0);
            Ok(Reply::new(
                200,
                json!({"card": {
                    "scope": scope,
                    "card_id": card_id,
                    "revision": revision,
                    "content_hash": text(&row[1]),
                    "text": chunk,
                    "next_offset": next_offset,
                    "trust": text(&row[2]),
                    "state": text(&row[3]),
                    "scan_state": text(&row[5]),
                    "writer": text(&row[6]),
                    "updated_at": text(&row[7]),
                    "seq": revision,
                }, "log_epoch": epoch}),
            ))
        }
    }
}
