//! Hub memory v2 store (S13a): the event log, card revisions, blobs,
//! compare-and-set on revision with `log_epoch`, idempotency, grants and
//! trust rules, behind the authority protocol in [`writer`].
//!
//! `search` answers from the lexical index in [`index`] (S13b), with the
//! scorer and tokenizer copied from Python in [`text`].
//!
//! Writes and prompt loads use the versioned guard scanner (S18).
//! Embedding and dense retrieval use the S18 scan gate (S15).
//! Reviewed proposals, atomic decisions and rollback use [`proposals`] (S19a).
pub mod auth;
pub mod dense;
pub mod embedder;
pub mod embedding;
#[cfg(test)]
mod embedding_tests;
pub mod ids;
pub mod index;
pub mod ops;
mod preflight;
mod proposal_blocks;
mod proposal_metadata;
mod proposal_tiers;
pub mod proposals;
mod pytables;
mod ranking;
#[cfg(test)]
mod ranking_tests;
mod rescan;
#[cfg(test)]
mod rescan_tests;
pub mod scan;
pub mod schema;
pub mod search;
pub mod text;
pub mod writer;

#[cfg(test)]
mod collection_tests;
#[cfg(test)]
mod embedder_tests;
#[cfg(test)]
mod lexical_tests;
#[cfg(test)]
mod powercut;
#[cfg(test)]
mod preflight_tests;
#[cfg(test)]
mod private_tests;
#[cfg(test)]
mod proposal_review_tests;
#[cfg(test)]
mod proposal_tests;
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
/// The embedding scheduler's wait after an empty lease, doubled up to the max.
/// Status and search reason when every embeddable card parked and none has a vector.
/// A wrong row count is parked per document (`vector-invalid`), so an endpoint that
/// always miscounts parks the whole corpus. Treating a run of them across distinct
/// documents as an endpoint fault needs a shared window in the lease path. Add it
/// when a second cause of an endpoint-wide refusal shows up. Until then `memory
/// reindex` requeues the parked documents once the endpoint is fixed.
const ALL_FAILED: &str = "all-embeddings-failed";
/// The first dense load failed, so retrieval stays lexical until a retry works.
const DENSE_LOAD_FAILED: &str = "dense-load-failed";
/// No dense matrix has been installed yet for the active embedder.
const DENSE_NOT_LOADED: &str = "dense-not-loaded";
const IDLE_MIN: Duration = Duration::from_secs(1);
const IDLE_MAX: Duration = Duration::from_secs(30);

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
    retrieval: Option<Arc<embedder::Runtime>>,
    rrf: ranking::Weights,
    dense: Arc<dense::Index>,
    coverage: std::sync::RwLock<embedding::Coverage>,
    /// Woken by a committed `put`, so an idle scheduler starts at once.
    embed_wake: Arc<tokio::sync::Notify>,
    embedding_task: std::sync::Mutex<Option<tokio::task::JoinHandle<()>>>,
    pull_embeddings: bool,
    /// The memory file, for the startup load's own connection.
    memory_path: PathBuf,
    /// Set while the last unbounded dense load failed and no matrix exists.
    dense_fault: std::sync::atomic::AtomicBool,
}

impl Drop for Store {
    /// Stops and joins the index rebuild thread first, so its read-only
    /// connection is closed before anything swaps the memory file. The
    /// writer then answers what is queued and closes its connections.
    fn drop(&mut self) {
        if let Some(task) = self.embedding_task.lock().unwrap().take() {
            task.abort();
        }
        self.dense.stop();
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
        let dense = Arc::new(dense::Index::default());
        let mut config = writer::Config::new(
            options.memory.clone(),
            options.hub,
            options.admin_digest.clone(),
        );
        config.hook = options.hook;
        config.scanner = options.scanner.clone();
        config.index = Some(index.clone());
        config.dense = Some(dense.clone());
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
            retrieval: None,
            rrf: ranking::Weights::default(),
            dense,
            coverage: std::sync::RwLock::new(embedding::Coverage::default()),
            embed_wake: Arc::new(tokio::sync::Notify::new()),
            embedding_task: std::sync::Mutex::new(None),
            pull_embeddings: false,
            memory_path: options.memory,
            dense_fault: std::sync::atomic::AtomicBool::new(false),
        })
    }
    /// Lexical mode ignores an unused embedder table, including credentials.
    pub fn configure_retrieval(&mut self, config: &Json) -> Result<(), String> {
        self.rrf = ranking::Weights::parse(config.get("rrf"))?;
        self.pull_embeddings = config["embedder"]["pull"].as_bool().unwrap_or(false);
        // Absent means lexical. Present but not a string is a typo, never lexical.
        let retrieval = match config.get("retrieval") {
            None => "lexical",
            Some(Json::String(mode)) => mode.as_str(),
            Some(_) => return Err("memory.retrieval must be lexical or hybrid".into()),
        };
        match retrieval {
            "lexical" => {
                if let Some(task) = self.embedding_task.lock().unwrap().take() {
                    task.abort();
                }
                self.retrieval = None;
                self.dense.configure(None);
                self.dense.set_private_ok(false);
                if config.get("embedder").is_some() {
                    eprintln!("INFO memory.embedder unused in lexical mode");
                }
            }
            "hybrid" => {
                self.retrieval = Some(embedder::Runtime::new(
                    embedder::Config::parse(&config["embedder"])?,
                    self.scanner.clone(),
                )?)
            }
            _ => return Err("memory.retrieval must be lexical or hybrid".into()),
        }
        if let Some(runtime) = &self.retrieval {
            self.dense.configure(Some((
                runtime.config.id.key(),
                runtime.config.id.dimensions,
            )));
            self.dense
                .set_private_ok(embedding::private_ok(&runtime.config));
        }
        let live = self
            .index
            .current()
            .live(&vec![true; self.index.current().scopes().len()])
            .len();
        *self.coverage.write().unwrap() = embedding::Coverage {
            covered: 0,
            eligible: live,
            total: live,
            failed: 0,
        };
        Ok(())
    }
    async fn embedding_request(&self, credential: &Credential, action: &str, raw: &Json) -> Reply {
        let allowed: &[&str] = match action {
            "reindex" => &["action", "client_event_id", "drop"],
            "embed_lease" => &["action", "client_event_id", "embedder_id", "max"],
            "embed_failed" => &["action", "client_event_id", "lease_id", "failures"],
            _ => &[
                "action",
                "client_event_id",
                "lease_id",
                "model_digest",
                "vectors",
            ],
        };
        if raw
            .as_object()
            .unwrap()
            .keys()
            .any(|k| !allowed.contains(&k.as_str()))
        {
            return invalid("unknown embedding field");
        }
        let drop_id = if action == "reindex" {
            match embedding::parse_drop(raw) {
                Ok(id) => id,
                Err(reply) => return reply,
            }
        } else {
            None
        };
        let command = if let Some(id) = &drop_id {
            embedding::Command::Reindex {
                id: id.clone(),
                drop: true,
                explicit: true,
            }
        } else {
            let Some(runtime) = &self.retrieval else {
                return error(503, "memory_action_unavailable", "no embedder configured");
            };
            match embedding::parse(action, raw, &runtime.config) {
                Ok(command) => command,
                Err(reply) => return reply,
            }
        };
        let Some((event, at)) = raw["client_event_id"]
            .as_str()
            .and_then(ids::parse_event_id)
        else {
            return error(
                400,
                "memory_client_event_invalid",
                "client_event_id required",
            );
        };
        let now = self.clock.now_ms();
        if at < now - ids::WINDOW_MS || at > now + ids::FUTURE_MS {
            return error(
                409,
                "memory_client_event_expired",
                "event outside acceptance window",
            );
        }
        let mut reply = self
            .write(Prepared {
                credential: credential.clone(),
                action: Action::Embedding {
                    command: command.clone(),
                    config: self.retrieval.as_ref().map(|r| r.config.clone()),
                },
                client_event_id: event,
                request_digest: ids::sha256_hex(raw.to_string().as_bytes()),
                now_ms: now,
            })
            .await;
        if reply.status != 200 {
            return reply;
        }
        match (&command, &drop_id) {
            // Bounded writer turns until the named embedder has no rows left.
            (_, Some(id)) => {
                let mut turn = reply.clone();
                while ["vectors", "jobs", "lineage"]
                    .iter()
                    .any(|k| turn.body[k].as_u64().unwrap_or(0) > 0)
                {
                    turn = self.derived(command.clone()).await;
                    if turn.status != 200 {
                        return turn;
                    }
                    for k in ["vectors", "jobs", "lineage"] {
                        reply.body[k] = json!(
                            reply.body[k].as_u64().unwrap_or(0)
                                + turn.body[k].as_u64().unwrap_or(0)
                        );
                    }
                }
                self.dense.drop_id(id);
            }
            (embedding::Command::Reindex { .. }, None) => {
                // Complete the queue in bounded writer turns. No transaction holds all missing rows.
                self.drain_reindex().await;
            }
            _ => {}
        }
        if matches!(command, embedding::Command::Reindex { .. }) {
            let _ = self.refresh_dense().await;
        }
        reply
    }
    /// Queue every missing vector in pages, then stop. Used at start-up and
    /// by the operator's `memory reindex`. Later cards are queued by `put`.
    async fn drain_reindex(&self) {
        let Some(runtime) = &self.retrieval else {
            return;
        };
        let id = runtime.config.id.key();
        loop {
            let turn = self
                .derived(embedding::Command::Reindex {
                    id: id.clone(),
                    drop: false,
                    explicit: false,
                })
                .await;
            if turn.status != 200 || turn.body["queued"].as_u64().unwrap_or(0) == 0 {
                return;
            }
        }
    }
    /// Scheduler and drain turns. Embedding commands write no idempotency row.
    async fn derived(&self, command: embedding::Command) -> Reply {
        let now = self.clock.now_ms();
        self.write(Prepared {
            credential: Credential::Admin,
            action: Action::Embedding {
                command,
                config: self.retrieval.as_ref().map(|r| r.config.clone()),
            },
            client_event_id: ids::mint(now),
            request_digest: String::new(),
            now_ms: now,
        })
        .await
    }
    async fn refresh_coverage(&self) {
        if let Some(runtime) = &self.retrieval {
            let id = runtime.config.id.key();
            let private = embedding::private_ok(&runtime.config);
            if let Ok(pair) = self
                .readers
                .submit(move |c| embedding::coverage(&Conn(c), &id, private))
                .await
            {
                *self.coverage.write().unwrap() = pair;
            }
        }
    }
    pub async fn refresh_dense(&self) -> Result<(), String> {
        let Some(runtime) = &self.retrieval else {
            return Ok(());
        };
        let id = runtime.config.id.key();
        let dimensions = runtime.config.id.dimensions;
        let dense = self.dense.clone();
        self.readers
            .submit(move |c| dense.refresh(c, &id, dimensions).map(|_| ()))
            .await
            .map_err(|_| "dense refresh failed")?;
        self.refresh_coverage().await;
        Ok(())
    }
    /// The first matrix load, with no reader deadline. It walks every vector
    /// row, so a large corpus can run past `READ_LIMIT`, and the pool would
    /// reject that as a timeout. It runs on its own read-only connection in a
    /// blocking task. A failure never stops the hub: retrieval reports
    /// `degraded` and stays lexical, and the scheduler retries. Returns
    /// whether a matrix is installed.
    pub async fn load_dense(&self) -> bool {
        let Some(runtime) = &self.retrieval else {
            return true;
        };
        let id = runtime.config.id.key();
        let dimensions = runtime.config.id.dimensions;
        let dense = self.dense.clone();
        let path = self.memory_path.clone();
        let loaded = tokio::task::spawn_blocking(move || -> Result<(), String> {
            let mut db = crate::schema::audit::open(
                &path,
                rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY
                    | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
            )?;
            db.execute_batch("PRAGMA query_only=ON; PRAGMA wal_autocheckpoint=0;")
                .map_err(|e| e.to_string())?;
            db.busy_timeout(Duration::from_secs(5))
                .map_err(|e| e.to_string())?;
            // One snapshot, so the heads and the rows agree.
            let tx = db
                .transaction_with_behavior(rusqlite::TransactionBehavior::Deferred)
                .map_err(|e| e.to_string())?;
            dense
                .refresh(&tx, &id, dimensions)
                .map(|_| ())
                .map_err(|e| e.to_string())
        })
        .await
        .unwrap_or_else(|e| Err(e.to_string()));
        let ok = loaded.is_ok();
        if let Err(e) = loaded {
            eprintln!("memory dense load failed: {e}");
        }
        self.dense_fault
            .store(!ok, std::sync::atomic::Ordering::SeqCst);
        if ok {
            // The load read one snapshot, so a vector committed during the
            // scan skipped `after_commit` (no matrix yet) and is not in it.
            // Replay that tail on a fresh reader now. A failure here keeps
            // the installed matrix, and the next commit replays the same tail.
            if self.refresh_dense().await.is_err() {
                self.refresh_coverage().await;
            }
        } else {
            self.refresh_coverage().await;
        }
        ok
    }
    /// Startup for hybrid mode: load the matrix, then start the scheduler.
    /// Never fails. See [`Store::load_dense`].
    pub async fn start_retrieval(self: &Arc<Self>) {
        self.load_dense().await;
        self.start_embeddings();
    }
    /// A failed first load, retried by the scheduler until a matrix exists.
    /// Another commit's refresh may install one first, which ends the retry.
    async fn retry_dense(&self) {
        if !self.dense_fault.load(std::sync::atomic::Ordering::SeqCst) {
            return;
        }
        let Some(runtime) = &self.retrieval else {
            return;
        };
        if self.dense.current(&runtime.config.id.key()).is_some() {
            self.dense_fault
                .store(false, std::sync::atomic::Ordering::SeqCst);
            return;
        }
        self.load_dense().await;
    }
    /// The active embedder has no installed matrix. `Some` carries the reason
    /// status and search report: a failed load is a fault, a load that has not
    /// run yet is only `switching`.
    fn dense_missing(&self) -> Option<&'static str> {
        let runtime = self.retrieval.as_ref()?;
        if self.dense.current(&runtime.config.id.key()).is_some() {
            return None;
        }
        Some(
            if self.dense_fault.load(std::sync::atomic::Ordering::SeqCst) {
                DENSE_LOAD_FAILED
            } else {
                DENSE_NOT_LOADED
            },
        )
    }
    /// One cancellable scheduler per store. Lexical mode creates no task.
    /// It queues missing vectors once, then leases only while work is
    /// queued. An empty lease doubles the idle wait up to [`IDLE_MAX`], and
    /// a committed `put` wakes it at once.
    pub fn start_embeddings(self: &Arc<Self>) {
        if self.retrieval.is_none() {
            return;
        }
        let mut task = self.embedding_task.lock().unwrap();
        if task.is_some() {
            return;
        }
        let weak = Arc::downgrade(self);
        let wake = self.embed_wake.clone();
        *task = Some(tokio::spawn(async move {
            match weak.upgrade() {
                Some(store) => store.drain_reindex().await,
                None => return,
            }
            let mut idle = IDLE_MIN;
            loop {
                let Some(store) = weak.upgrade() else {
                    return;
                };
                store.retry_dense().await;
                let faulted = store.dense_fault.load(std::sync::atomic::Ordering::SeqCst);
                if store.pull_embeddings && !faulted {
                    return;
                }
                let worked = !store.pull_embeddings && store.embedding_tick().await;
                drop(store);
                idle = if worked {
                    IDLE_MIN
                } else {
                    (idle * 2).min(IDLE_MAX)
                };
                if worked {
                    tokio::task::yield_now().await;
                    continue;
                }
                tokio::select! {
                    _ = tokio::time::sleep(idle) => {}
                    _ = wake.notified() => idle = IDLE_MIN,
                }
            }
        }));
    }
    /// One in-process lease and embedding. Returns whether it leased work.
    pub async fn embedding_tick(&self) -> bool {
        let Some(runtime) = self.retrieval.clone() else {
            return false;
        };
        // Never take a lease the endpoint cannot start on. A lease taken
        // during a Retry-After backoff would expire before the request ran.
        if self.pull_embeddings || !runtime.documents_ready() {
            return false;
        }
        let lease = self.derived(embedding::Command::Lease { max: 4 }).await;
        if lease.status != 200 {
            return false;
        }
        let jobs = lease.body["jobs"].as_array().cloned().unwrap_or_default();
        if jobs.is_empty() {
            return false;
        }
        let lease_id: String = lease.body["lease_id"].as_str().unwrap_or("").into();
        let hashes: Vec<String> = jobs
            .iter()
            .map(|j| j["content_hash"].as_str().unwrap_or("").into())
            .collect();
        let texts = jobs
            .iter()
            .map(|j| {
                (
                    j["text"].as_str().unwrap_or("").into(),
                    j["scan_state"].as_str().unwrap_or("pending").into(),
                )
            })
            .collect();
        let command = match runtime.documents(texts).await {
            Ok(rows) => embedding::Command::Submit {
                lease: lease_id,
                digest: runtime.config.id.model_digest.clone(),
                vectors: hashes
                    .into_iter()
                    .zip(rows)
                    .map(|(h, v)| (h, v.iter().flat_map(|f| f.to_le_bytes()).collect()))
                    .collect(),
            },
            // A batch failure names every document, and the next lease retries each alone.
            Err(e) => embedding::Command::Failed {
                lease: lease_id,
                failures: hashes
                    .into_iter()
                    .map(|h| (h, e.reason.to_string(), false))
                    .collect(),
            },
        };
        self.derived(command).await;
        true
    }
    pub fn retrieval_status(&self) -> Json {
        let mut status = match &self.retrieval {
            None => json!({"mode": "lexical", "effective": "lexical", "state": "ok"}),
            Some(runtime) => match runtime.reason() {
                Some(reason) => {
                    json!({"mode": "hybrid", "effective": "lexical-fallback", "state": "degraded", "reason": reason, "endpoint_host": url::Url::parse(&runtime.config.endpoint).ok().and_then(|u| u.host_str().map(String::from)), "breaker_open": runtime.breaker_open()})
                }
                None => {
                    let coverage = *self.coverage.read().unwrap();
                    if let Some(reason) = self.dense_missing() {
                        // No matrix to rank with, so search answers lexically.
                        if reason == DENSE_LOAD_FAILED {
                            json!({"mode": "hybrid", "effective": "lexical-fallback", "state": "degraded", "reason": reason})
                        } else {
                            json!({"mode": "hybrid", "effective": "lexical", "state": "switching", "reason": reason})
                        }
                    } else if coverage.all_failed() {
                        // Every embeddable card parked and none has a vector.
                        json!({"mode": "hybrid", "effective": "lexical-fallback", "state": "degraded", "reason": ALL_FAILED})
                    } else {
                        let complete = coverage.complete();
                        // Search answers lexically while no matrix row can
                        // score, so status says so. Covered cards with an
                        // empty matrix mean it lags the table (`switching`).
                        // With no covered card there is nothing to rank (`ok`).
                        let ranks = self
                            .dense
                            .current(&runtime.config.id.key())
                            .is_some_and(|m| m.has_live_rows());
                        let hybrid = complete && ranks;
                        let idle = complete && !ranks && coverage.covered == 0;
                        json!({"mode": "hybrid", "effective": if hybrid { "hybrid" } else { "lexical" }, "state": if hybrid || idle { "ok" } else { "switching" }})
                    }
                }
            },
        };
        if let Some(runtime) = &self.retrieval {
            status["endpoint_host"] = json!(
                url::Url::parse(&runtime.config.endpoint)
                    .ok()
                    .and_then(|u| u.host_str().map(String::from))
            );
            status["breaker_open"] = json!(runtime.breaker_open());
            status["pull"] = json!(runtime.config.pull);
            if runtime.config.pull {
                // Whether the hub leases private cards to its pull workers.
                status["pull_private"] = json!(runtime.config.pull_private);
            }
            status["coverage"] = self.coverage.read().unwrap().json();
            status["embedder_id"] = json!(runtime.config.id.key());
        }
        status
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
            "preflight" => self.preflight(credential, map).await,
            "reindex" | "embed_lease" | "embed_submit" | "embed_failed" => {
                self.embedding_request(credential, action, &raw).await
            }
            "propose" | "decide" | "rollback" => {
                self.proposal_request(credential, action, map, &raw).await
            }
            "put" | "import" | "retire" | "handoff" | "capture" | "grant" | "rescan" => {
                match self.prepare(credential, action, map, &raw) {
                    Ok(prepared) => self.write(prepared).await,
                    Err(reply) => reply,
                }
            }
            _ => invalid("unknown memory action"),
        }
    }
    async fn preflight(&self, credential: &Credential, map: &Map<String, Json>) -> Reply {
        if map
            .keys()
            .any(|k| !["action", "proposal_id"].contains(&k.as_str()))
            || map
                .get("proposal_id")
                .and_then(Json::as_str)
                .is_none_or(|id| ids::parse_event_id(id).is_none())
        {
            return invalid("preflight requires only a proposal_id");
        }
        let authority = match self.hub.submit(|db| ops::Authority::read(&Conn(db))).await {
            Ok(a) => a,
            Err(_) => return error(503, "memory_action_unavailable", "policy read unavailable"),
        };
        let who = credential.clone();
        let request = Json::Object(map.clone());
        let scanner = self.scanner.clone();
        let now = self.clock.now_ms();
        match self
            .readers
            .submit(move |db| {
                // ReaderPool already pins one deferred, query-only snapshot.
                // No writer, event, idempotency entry or index refresh is involved.
                preflight::read(&Conn(db), &who, &request, &authority, scanner.as_ref(), now)
            })
            .await
        {
            Ok(r) => r,
            Err(_) => error(
                503,
                "memory_action_unavailable",
                "preflight read unavailable",
            ),
        }
    }
    async fn proposal_request(
        &self,
        credential: &Credential,
        action: &str,
        map: &Map<String, Json>,
        raw: &Json,
    ) -> Reply {
        let mut prepared = match self.prepare(credential, action, map, raw) {
            Ok(p) => p,
            Err(r) => return r,
        };
        let scanner = self.scanner.clone();
        let who = credential.clone();
        let request = raw.clone();
        let command = self
            .readers
            .submit(move |db| proposals::prepare(&Conn(db), &who, request, scanner.as_ref()))
            .await;
        match command {
            Ok(command) => prepared.action = Action::Proposal(command),
            Err(_) => {
                return error(
                    503,
                    "memory_action_unavailable",
                    "proposal preparation unavailable",
                );
            }
        }
        self.write(prepared).await
    }
    async fn write(&self, prepared: Prepared) -> Reply {
        // A lease changes no coverage unless it parks text (the reply says so),
        // and the scheduler sends one per tick.
        let lease = matches!(
            prepared.action,
            Action::Embedding {
                command: embedding::Command::Lease { .. },
                ..
            }
        );
        let queued = matches!(prepared.action, Action::Put { .. } | Action::Proposal(_));
        match self
            .writer
            .submit(prepared, Instant::now() + COMMAND_DEADLINE)
            .await
        {
            Ok(reply) => {
                if reply.status < 300 {
                    if !lease || reply.body["parked"].as_u64().unwrap_or(0) > 0 {
                        self.refresh_coverage().await;
                    }
                    if queued {
                        self.embed_wake.notify_one();
                    }
                }
                reply
            }
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
            "propose" => &["action", "base_epoch", "ops", "client_event_id", "tier"],
            "decide" => &[
                "action",
                "proposal_id",
                "base_epoch",
                "decision",
                "client_event_id",
                "expected_policy_digest",
            ],
            "rollback" => &["action", "proposal_id", "base_epoch", "client_event_id"],
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
            "rescan" => &[
                "action",
                "limit",
                "retry_failed",
                "after",
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
        if matches!(action, "grant" | "rescan") && *credential != Credential::Admin {
            return Err(error(
                403,
                "memory_grant_missing",
                "the admin token is required for this memory action",
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
                    "scope must be fleet, repo:<identity>, collection:<name> or principal:<id>",
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
            "rescan" => {
                let limit = match map.get("limit") {
                    None => 4,
                    Some(value) => value
                        .as_u64()
                        .filter(|n| (1..=16).contains(n))
                        .ok_or_else(|| invalid("limit must be an integer from 1 to 16"))?,
                };
                let retry_failed = match map.get("retry_failed") {
                    None => false,
                    Some(value) => value
                        .as_bool()
                        .ok_or_else(|| invalid("retry_failed must be a boolean"))?,
                };
                let after = match map.get("after") {
                    None => None,
                    Some(Json::String(value))
                        if value.len() == 64
                            && value
                                .bytes()
                                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) =>
                    {
                        Some(value.clone())
                    }
                    _ => return Err(invalid("after must be a lowercase SHA-256 hash")),
                };
                Action::Rescan {
                    limit: limit as usize,
                    retry_failed,
                    after,
                }
            }
            "propose" | "decide" | "rollback" => {
                if action != "propose"
                    && str_field("proposal_id")
                        .and_then(ids::parse_event_id)
                        .is_none()
                {
                    return Err(invalid("proposal_id must be a time-ordered id"));
                }
                if action == "decide" && !matches!(str_field("decision"), Some("accept" | "reject"))
                {
                    return Err(error(
                        422,
                        "memory_auto_apply_disabled",
                        "a reviewer must explicitly accept or reject the proposal",
                    ));
                }
                Action::Proposal(proposals::Command {
                    request: raw.clone(),
                    draft: Err(invalid("proposal not prepared")),
                })
            }
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
        let mut reply = self.search_ranked(credential, map, push).await;
        if reply.status == 200 && map.get("eval").and_then(Json::as_bool) == Some(true) {
            if let Ok(authority) = self.hub.submit(|db| ops::Authority::read(&Conn(db))).await {
                reply.body["policy_revision"] = json!(authority.policy.as_ref().map(|p| p.0));
                reply.body["policy_digest"] = json!(authority.policy.as_ref().map(|p| &p.1));
            }
        }
        reply
    }

    async fn search_ranked(
        &self,
        credential: &Credential,
        map: &Map<String, Json>,
        push: bool,
    ) -> Reply {
        let mut request = match search::parse(map) {
            Ok(r) => r,
            Err(reply) => return reply,
        };
        request.weights = self.rrf;
        request.push = push;
        request.prompt |= push;
        let snapshot = match request.min_seq {
            Some(min) => self.index.wait(min, MIN_SEQ_WAIT).await,
            None => self.index.current(),
        };
        let query = request.query.clone();
        let lexical = map.get("mode").and_then(Json::as_str) == Some("lexical");
        let c = credential.clone();
        let r = request.clone();
        let snap = snapshot.clone();
        let scanner = self.scanner.clone();
        let first = self
            .readers
            .submit(move |db| search::run(&Conn(db), &c, &r, &snap, scanner.as_ref()))
            .await;
        let mut reply = match first {
            Ok(reply) => reply,
            Err(_) => {
                return Reply::new(
                    503,
                    json!({"error": "hub busy", "reason": "memory-reader-timeout"}),
                );
            }
        };
        if reply.status != 200 || lexical {
            return reply;
        }
        if let Some(runtime) = &self.retrieval {
            let all_failed = self.coverage.read().unwrap().all_failed();
            let load_failed = self.dense_missing().filter(|r| *r == DENSE_LOAD_FAILED);
            if let Some(reason) = runtime
                .reason()
                .or(load_failed)
                .or(all_failed.then_some(ALL_FAILED))
            {
                reply.body["retrieval"]["mode"] = json!("lexical-fallback");
                reply.body["retrieval"]["effective"] = json!("lexical-fallback");
                reply.body["retrieval"]["degraded"] = json!(true);
                reply.body["retrieval"]["reason"] = json!(reason);
            }
            let Some(matrix) = self.dense.current(&runtime.config.id.key()) else {
                return reply;
            };
            // Coverage over embeddable cards, kept current by each write. An
            // unembeddable card (pending scan, flagged, parked) is answered by
            // BM25 alone and does not hold hybrid back.
            if !self.coverage.read().unwrap().complete() {
                return reply;
            }
            // A matrix with no live row has nothing to rank (an empty corpus,
            // all cards unembeddable, or every vector collected). Sending the
            // query text to the endpoint would cost a call, and a down
            // endpoint would flip such a hub to degraded.
            if !matrix.has_live_rows() {
                return reply;
            }
            match runtime.query(&query).await {
                Err(error) => {
                    reply.body["retrieval"]["mode"] = json!("lexical-fallback");
                    reply.body["retrieval"]["effective"] = json!("lexical-fallback");
                    reply.body["retrieval"]["degraded"] = json!(true);
                    reply.body["retrieval"]["reason"] = json!(error.reason);
                }
                Ok(vector) => {
                    request.dense = Some((matrix, vector));
                    let credential = credential.clone();
                    let scanner = self.scanner.clone();
                    if let Ok(mut hybrid) = self
                        .readers
                        .submit(move |db| {
                            search::run(
                                &Conn(db),
                                &credential,
                                &request,
                                &snapshot,
                                scanner.as_ref(),
                            )
                        })
                        .await
                    {
                        hybrid.body["embedder_id"] = json!(runtime.config.id.key());
                        return hybrid;
                    }
                    reply.body["retrieval"]["mode"] = json!("lexical-fallback");
                    reply.body["retrieval"]["effective"] = json!("lexical-fallback");
                    reply.body["retrieval"]["degraded"] = json!(true);
                    reply.body["retrieval"]["reason"] = json!("memory-reader-timeout");
                }
            }
        }
        reply
    }

    async fn read(&self, credential: &Credential, action: &str, map: &Map<String, Json>) -> Reply {
        let credential = credential.clone();
        let map = map.clone();
        let action = action.to_string();
        let now_ms = self.clock.now_ms();
        let scan = self.scanner.describe();
        let indexed_seq = self.index.current().indexed_seq;
        let retrieval = self.retrieval_status();
        let is_status = action == "status";
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
            Ok(mut reply) => {
                if is_status && reply.status == 200 {
                    reply.body["default_mode"] = retrieval["mode"].clone();
                    if let Some(id) = retrieval.get("embedder_id") {
                        reply.body["embedders"] =
                            json!([{"id": id, "coverage": retrieval["coverage"]}]);
                    }
                    reply.body["retrieval"] = retrieval;
                }
                reply
            }
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
                                "scopes must name fleet, repo:<identity>, collection:<name> or principal:<id>",
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
                            "scope must be fleet, repo:<identity>, collection:<name> or principal:<id>",
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
